#!/usr/bin/env python3
"""
analisar_coleta.py

Relatorio de sanidade da coleta de UM linter do pipeline Babel: quanto do
que foi planejado virou dado, por que o resto ficou de fora, e se o que
foi gravado tem problema (duplicata, viés por repositorio, arquivo
arquivado sumido...). So LE -- nao altera nenhum arquivo do dataset.

Rode da pasta do projeto (os caminhos de archived_file_path sao relativos
a ela). Exemplo (checkstyle, uniao do arquivo novo e do antigo):

    python3 analisar_coleta.py --linter checkstyle \
        --commits commits_sem_nifi.jsonl \
        --progress-log processed_checkstyle.jsonl \
        --violations violations_checkstyle.jsonl --violations violations.jsonl \
        --log checkstyle_run.log --desde "2026-10-16 14:00" \
        --repos-dir repos_cache --out-dir analise_checkstyle

Saida em --out-dir: resumo.txt (o que ler primeiro) e CSVs por repositorio,
regra, ano e descarte.

Limites desta analise (por construcao do pipeline, nao do script):
  - commit "sem violacao" mistura dois casos que o progress-log nao separa:
    commit lintado com 0 violacoes e commit sem arquivo relevante.
  - descartes (projeto ou commit) NAO sao gravados em arquivo nenhum; so o
    log os registra. Por isso o log e uma entrada deste script.
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

CONFIG_CANDIDATES = [
    "checkstyle.xml", ".checkstyle.xml", "config/checkstyle/checkstyle.xml",
    "config/checkstyle/checkstyle_checks.xml", "checkstyle/checkstyle.xml",
]
VAR_RE = re.compile(r"\$\{[\w.\-]+\}")
RE_PROJ = re.compile(r"WARNING (\S+): PROJETO descartado")
RE_COMMIT_DISC = re.compile(r"INFO (\S+?)@\w+: commit descartado")
RE_ERRO = re.compile(r"ERROR (\S+?)@\w+: erro ao processar, pulando -- (.*)")


def jl(path):
    """Linhas JSON de um arquivo, ignorando linha cortada/invalida (arquivo
    que outro job ainda esta escrevendo pode terminar no meio de uma linha)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))]


def short_reason(txt):
    txt = re.sub(r"/\S+", "<caminho>", txt)
    txt = re.sub(r"\b[0-9a-f]{40}\b", "<sha>", txt)
    if "excedeu o timeout" in txt:
        return "timeout"
    return txt[:90]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--linter", required=True)
    ap.add_argument("--commits", default="commits.jsonl")
    ap.add_argument("--selected-repos", default="selected_repos.jsonl")
    ap.add_argument("--progress-log", action="append", required=True)
    ap.add_argument("--violations", action="append", required=True)
    ap.add_argument("--log", default=None, help="log de execucao (descartes e erros por repositorio)")
    ap.add_argument("--desde", default="", help='so linhas do log a partir de "AAAA-MM-DD HH:MM"')
    ap.add_argument("--repos-dir", default=None, help="clones, pra ver QUAIS variaveis causaram descarte de projeto")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--amostra-arquivos", type=int, default=300)
    args = ap.parse_args()

    out_dir = Path(args.out_dir or f"analise_{args.linter}")
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = []

    def say(s=""):
        print(s, flush=True)
        lines.append(s)

    # ---------------------------------------------------------------- repos
    meta = {}
    registered = set()
    for r in jl(args.selected_repos):
        if r.get("linter") == args.linter:
            registered.add(r["full_name"])
            meta[r["full_name"]] = r
    say(f"# Analise da coleta -- {args.linter}")
    say(f"repositorios registrados para {args.linter} em {args.selected_repos}: {len(registered)}")

    # ------------------------------------------------------------- commits
    total_by_repo = collections.Counter()
    year_of = {}
    branch_of = {}
    for c in jl(args.commits):
        repo = c["repo_full_name"]
        if repo not in registered:
            continue
        total_by_repo[repo] += 1
        year_of[(repo, c["commit_hash"])] = (c.get("commit_date") or "")[:4] or "?"
        branch_of.setdefault(repo, (c.get("owner"), c.get("name"), c.get("branch")))
    n_commits = sum(total_by_repo.values())
    say(f"commits planejados ({args.commits}), so dos repos registrados: {n_commits} em {len(total_by_repo)} repos")

    # ------------------------------------------------------------ progress
    prog = {}
    for path in args.progress_log:
        if not Path(path).exists():
            say(f"AVISO: {path} nao existe")
            continue
        for r in jl(path):
            if r.get("linter") != args.linter:
                continue
            k = (r["repo_full_name"], r["commit_hash"])
            prog[k] = prog.get(k, False) or bool(r.get("had_violation"))
    prog_in_scope = {k: v for k, v in prog.items() if k in year_of}
    out_of_scope = len(prog) - len(prog_in_scope)
    say(f"commits no progress-log: {len(prog)} (fora da lista de commits analisada: {out_of_scope})")

    done_by_repo = collections.Counter(k[0] for k in prog_in_scope)
    done_v_by_repo = collections.Counter(k[0] for k, v in prog_in_scope.items() if v)

    # ------------------------------------------------- log (descartes/erros)
    proj_discarded = set()
    commit_disc = collections.Counter()
    erros = collections.Counter()
    if args.log and Path(args.log).exists():
        with open(args.log, encoding="utf-8", errors="replace") as f:
            for line in f:
                if args.desde and line[:16] < args.desde:
                    continue
                m = RE_PROJ.search(line)
                if m:
                    proj_discarded.add(m.group(1))
                    continue
                m = RE_COMMIT_DISC.search(line)
                if m:
                    commit_disc[m.group(1)] += 1
                    continue
                m = RE_ERRO.search(line)
                if m:
                    erros[(m.group(1), short_reason(m.group(2)))] += 1
    else:
        say("AVISO: sem --log -- nao da pra explicar o motivo dos commits nao processados")

    # ------------------------------------------------------------ funil
    say("\n## 1. Funil de commits (so repos registrados)")
    tot = n_commits
    done = len(prog_in_scope)
    done_v = sum(1 for v in prog_in_scope.values() if v)
    nao = tot - done
    nao_proj = sum(total_by_repo[r] - done_by_repo[r] for r in proj_discarded if r in total_by_repo)
    nao_outros = nao - nao_proj
    say(f"planejados:                         {tot:9d}")
    say(f"  processados:                      {done:9d}  ({100*done/max(tot,1):5.1f}%)")
    say(f"    com violacao:                   {done_v:9d}")
    say(f"    sem violacao (limpo OU sem arquivo relevante): {done - done_v}")
    say(f"  nao processados:                  {nao:9d}  ({100*nao/max(tot,1):5.1f}%)")
    say(f"    em projeto descartado:          {nao_proj:9d}  ({len(proj_discarded & set(total_by_repo))} repos)")
    say(f"    demais (pom.xml, erro, pendente): {nao_outros:7d}")

    # ----------------------------------------------------- violacoes (1 passada)
    say("\n## 2. Violacoes gravadas")
    key_marker = f'"linter": "{args.linter}"'
    seen = set()
    n_lines = n_dup = 0
    rows_by_repo = collections.Counter()
    commits_v = collections.defaultdict(set)
    files_by_repo = collections.defaultdict(set)
    rules = collections.Counter()
    rules_by_repo = collections.defaultdict(collections.Counter)
    sev = collections.Counter(); ver = collections.Counter()
    own_cfg = collections.Counter(); load_failed = collections.Counter()
    per_commit = collections.Counter()
    by_year = collections.Counter()
    src_unique = collections.Counter()
    null_file = null_line = 0
    reservoir = []
    n_res = 0
    rng = random.Random(7)

    for path in args.violations:
        if not Path(path).exists():
            say(f"AVISO: {path} nao existe")
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if key_marker not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                n_lines += 1
                fp = (r["repo_full_name"], r["commit_hash"], r.get("file_path"), r.get("line"), r.get("column"), r.get("rule_id"))
                h = int.from_bytes(hashlib.blake2b(repr(fp).encode(), digest_size=8).digest(), "big")
                if h in seen:
                    n_dup += 1
                    continue
                seen.add(h)
                src_unique[os.path.basename(path)] += 1
                repo, sha = r["repo_full_name"], r["commit_hash"]
                rows_by_repo[repo] += 1
                commits_v[repo].add(sha)
                if r.get("file_path") is None:
                    null_file += 1
                else:
                    files_by_repo[repo].add(r["file_path"])
                if r.get("line") is None:
                    null_line += 1
                rules[r.get("rule_id")] += 1
                rules_by_repo[repo][r.get("rule_id")] += 1
                sev[r.get("severity")] += 1
                ver[r.get("linter_version")] += 1
                own_cfg[r.get("has_own_config")] += 1
                load_failed[r.get("own_config_load_failed")] += 1
                per_commit[(repo, sha)] += 1
                by_year[year_of.get((repo, sha), "?")] += 1
                n_res += 1
                ap_ = r.get("archived_file_path")
                if ap_:
                    if len(reservoir) < args.amostra_arquivos:
                        reservoir.append(ap_)
                    else:
                        j = rng.randrange(n_res)
                        if j < args.amostra_arquivos:
                            reservoir[j] = ap_

    uniq = len(seen)
    say(f"linhas de {args.linter} lidas: {n_lines}  |  unicas: {uniq}  |  duplicadas: {n_dup} ({100*n_dup/max(n_lines,1):.2f}%)")
    for k, v in src_unique.items():
        say(f"  unicas vindas de {k}: {v}")
    say(f"repos com alguma violacao: {len(rows_by_repo)}  |  commits com violacao: {len(per_commit)}")
    say(f"file_path nulo: {null_file}  |  line nulo: {null_line}")

    vals = sorted(per_commit.values())
    say(f"violacoes por commit (so os que tem): mediana {pct(vals,.5)}  p90 {pct(vals,.9)}  p99 {pct(vals,.99)}  max {vals[-1] if vals else 0}")

    say("\nconcentracao por repositorio (viés da amostra):")
    top = rows_by_repo.most_common(8)
    for repo, n in top:
        say(f"  {repo:42s} {n:9d}  {100*n/max(uniq,1):5.1f}%  ({len(commits_v[repo])} commits)")
    say(f"  top 1 = {100*top[0][1]/max(uniq,1):.1f}% | top 3 = {100*sum(n for _, n in top[:3])/max(uniq,1):.1f}%" if top else "  (sem dados)")

    say("\nregras mais frequentes:")
    for rule, n in rules.most_common(12):
        say(f"  {str(rule):70s} {n:9d}  {100*n/max(uniq,1):5.1f}%")
    say(f"  regras distintas: {len(rules)}")

    say("\nseveridade: " + ", ".join(f"{k}={v}" for k, v in sev.most_common()))
    say("linter_version: " + ", ".join(f"{k}={v}" for k, v in ver.most_common()))
    say("has_own_config: " + ", ".join(f"{k}={v}" for k, v in own_cfg.most_common()))
    say("own_config_load_failed: " + ", ".join(f"{k}={v}" for k, v in load_failed.most_common()))

    # ---- arquivos arquivados
    ok = sum(1 for p in reservoir if os.path.isfile(p))
    say(f"\narquivo arquivado existe (amostra de {len(reservoir)}): {ok} ({100*ok/max(len(reservoir),1):.1f}%)"
        + ("" if os.path.isdir("Babel") else "  <-- pasta Babel/ nao encontrada aqui; rode da pasta do projeto"))

    # ---- consistencia
    say("\n## 3. Consistencia entre progress-log e violacoes")
    viol_commits = set(per_commit)
    prog_true = {k for k, v in prog_in_scope.items() if v}
    sem_linhas = prog_true - viol_commits
    sem_progress = {k for k in viol_commits if k in year_of} - set(prog_in_scope)
    say(f"progress diz 'com violacao' mas nao ha linha de violacao: {len(sem_linhas)}")
    say(f"ha violacao gravada mas o commit nao esta no progress:   {len(sem_progress)}  (esperado >0 se um job foi interrompido no meio de um commit)")
    falso_limpo = {k for k in viol_commits if k in prog_in_scope and not prog_in_scope[k]}
    say(f"commit marcado SEM violacao no progress mas com linhas de violacao: {len(falso_limpo)}  (deve ser 0)")

    # ---- anos
    say("\n## 4. Por ano do commit")
    tot_y = collections.Counter(v for v in year_of.values())
    done_y = collections.Counter(year_of[k] for k in prog_in_scope)
    donev_y = collections.Counter(year_of[k] for k in prog_true)
    say(f"{'ano':6s} {'planejados':>11s} {'processados':>12s} {'com viol.':>10s} {'violacoes':>10s}")
    for y in sorted(tot_y):
        say(f"{y:6s} {tot_y[y]:11d} {done_y[y]:12d} {donev_y[y]:10d} {by_year[y]:10d}")

    # ---- por repo
    say("\n## 5. Por repositorio (todos em por_repo.csv)")
    with open(out_dir / "por_repo.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["repo", "stars", "size_kb", "planejados", "processados", "com_violacao", "nao_processados",
                    "motivo_nao_proc", "commits_descartados_pom", "violacoes", "arquivos_distintos", "top3_regras"])
        for repo in sorted(total_by_repo, key=lambda r: total_by_repo[r], reverse=True):
            npr = total_by_repo[repo] - done_by_repo[repo]
            motivo = "projeto_descartado" if repo in proj_discarded else ("" if npr == 0 else "pom/erro/pendente")
            m = meta.get(repo, {})
            top3 = "; ".join(f"{k}={v}" for k, v in rules_by_repo[repo].most_common(3))
            w.writerow([repo, m.get("stars"), m.get("size_kb"), total_by_repo[repo], done_by_repo[repo],
                        len(commits_v[repo]), npr, motivo, commit_disc[repo], rows_by_repo[repo],
                        len(files_by_repo[repo]), top3])
    sem_nada = [r for r in total_by_repo if done_by_repo[r] == 0 and r not in proj_discarded]
    say(f"repos com 0 commits processados e NAO descartados como projeto: {len(sem_nada)}")
    for r in sem_nada[:15]:
        say(f"  {r:42s} planejados={total_by_repo[r]}  pom_descartados={commit_disc[r]}  erros={sum(v for (rr, _), v in erros.items() if rr == r)}")
    say("repos com violacao, ordenados por commits processados (top 15):")
    for r, n in done_by_repo.most_common(15):
        say(f"  {r:42s} processados={n:6d}  com_violacao={len(commits_v[r]):6d}  violacoes={rows_by_repo[r]:8d}")

    # ---- descartes
    say("\n## 6. Descartes")
    say(f"projetos descartados (log desde '{args.desde or 'o inicio'}'): {len(proj_discarded)}")
    say(f"commits descartados por pom.xml (log): {sum(commit_disc.values())}")
    for r, n in commit_disc.most_common(8):
        say(f"  pom.xml: {r:40s} {n:7d}")
    say(f"erros por repo e tipo (log): {sum(erros.values())}")
    for (r, why), n in erros.most_common(12):
        say(f"  {r:40s} {n:6d}  {why}")

    rows_disc = []
    for repo in sorted(proj_discarded, key=lambda r: total_by_repo.get(r, 0), reverse=True):
        row = {"repo": repo, "planejados": total_by_repo.get(repo, 0), "stars": meta.get(repo, {}).get("stars"),
               "config": "", "variaveis": "", "so_em_comentario": ""}
        if args.repos_dir and repo in branch_of:
            o, n, b = branch_of[repo]
            for c in CONFIG_CANDIDATES:
                r = subprocess.run(["git", "-C", str(Path(args.repos_dir) / f"{o}__{n}"), "show", f"origin/{b}:{c}"],
                                   capture_output=True, text=True)
                if r.returncode == 0:
                    txt = r.stdout
                    todas = sorted(set(VAR_RE.findall(txt)))
                    reais = sorted(set(VAR_RE.findall(re.sub(r"<!--.*?-->", "", txt, flags=re.S))))
                    row.update(config=c, variaveis=" ".join(todas), so_em_comentario=("sim" if todas and not reais else "nao"))
                    break
        rows_disc.append(row)
    with open(out_dir / "descartes_projeto.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["repo", "planejados", "stars", "config", "variaveis", "so_em_comentario"])
        w.writeheader(); w.writerows(rows_disc)
    if rows_disc:
        say("\nprojetos descartados (maiores primeiro; lista completa em descartes_projeto.csv):")
        for r in rows_disc[:15]:
            say(f"  {r['repo']:40s} commits={r['planejados']:6d}  {r['config']:36s} {r['variaveis']}"
                + ("   <-- SO EM COMENTARIO" if r["so_em_comentario"] == "sim" else ""))
        nv = collections.Counter(v for r in rows_disc for v in r["variaveis"].split() if v)
        if nv:
            say("variaveis que mais causam descarte: " + ", ".join(f"{k}({n})" for k, n in nv.most_common(8)))
        n_com = sum(1 for r in rows_disc if r["so_em_comentario"] == "sim")
        com_c = sum(r["planejados"] for r in rows_disc if r["so_em_comentario"] == "sim")
        say(f"descartados SO por variavel dentro de comentario XML: {n_com} projetos / {com_c} commits (descarte indevido)")

    with open(out_dir / "regras.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["rule_id", "violacoes"]); w.writerows(rules.most_common())
    with open(out_dir / "anos.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["ano", "planejados", "processados", "com_violacao", "violacoes"])
        for y in sorted(tot_y):
            w.writerow([y, tot_y[y], done_y[y], donev_y[y], by_year[y]])

    (out_dir / "resumo.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nRelatorio gravado em {out_dir.resolve()}/ (resumo.txt, por_repo.csv, regras.csv, anos.csv, descartes_projeto.csv)")


if __name__ == "__main__":
    sys.exit(main())
