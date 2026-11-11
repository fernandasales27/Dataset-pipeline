#!/usr/bin/env python3
"""
calibrar_eslint.py

Mede o efeito da aproximacao "dependencias da PONTA do branch aplicadas a
commits antigos" (ESLintAdapter). Para uma amostra de commits de UM
repositorio, roda o ESLint duas vezes:

  tip     -- como o pipeline faz: dependencias instaladas UMA vez, nas
             versoes da ponta do branch.
  commit  -- referencia: dependencias instaladas a partir do package.json
             (e lockfile) DAQUELE commit.

e compara as violacoes (por regra e por ocorrencia). Se as duas quase
coincidem, a aproximacao e segura; se divergem nos commits mais antigos, a
janela de tempo do ESLint deve ser menor. So le o repositorio (faz
checkout nos commits da amostra) e grava resultados em --out-dir.

Uso (no cluster, ~minutos por commit por causa do npm install):
    python3 calibrar_eslint.py --repo adam-p/markdown-here \
        --commits commits_sem_nifi.jsonl --repos-dir repos_cache \
        --n 30 --out-dir calibracao_eslint

Escolhe commits que tem config propria do ESLint e arquivo de codigo
mudado, espalhados do mais novo ao mais antigo.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import shutil
import statistics
import sys
from argparse import Namespace
from collections import Counter
from pathlib import Path

from common import LANGUAGE_EXTENSIONS, checkout_commit, get_changed_files
from linter_adapters import CommitDiscarded, ESLintAdapter, _git_show_text


class CommitDepsAdapter(ESLintAdapter):
    """Referencia: instala as dependencias DO COMMIT atual (nao da ponta)."""
    current_sha: str = ""
    keep_deps = False

    def _ensure_tip_deps(self, repo_dir: Path) -> dict:
        deps_root = self.deps_dir / f"commit_{self.current_sha[:10]}"
        target = deps_root / "node_modules"
        pkg_text = _git_show_text(repo_dir, self.current_sha, "package.json")
        if pkg_text is None:
            return {"status": "none", "target": target, "detail": "sem package.json no commit"}
        lock_text = _git_show_text(repo_dir, self.current_sha, "package-lock.json")
        ok, detail = self._install(deps_root, pkg_text, lock_text)
        return {"status": "ok" if ok else "failed", "target": target, "detail": detail}

    engine = "global"

    def _invoke(self, repo_dir, files, config_name):
        """Gabarito: usa o ESLint INSTALADO COM AS DEPENDENCIAS DO COMMIT
        (a versao que o time usava na epoca), nao o v9 global -- plugins de
        anos atras nao funcionam no motor novo, e isso nao e o que a
        referencia quer medir. Sem eslint local no commit, cai no global."""
        local = self.deps_dir / f"commit_{self.current_sha[:10]}" / "node_modules" / ".bin" / "eslint"
        self.engine = "local" if local.exists() else "global"
        saved = self.eslint_bin
        if local.exists():
            self.eslint_bin = str(local)
        try:
            return super()._invoke(repo_dir, files, config_name)
        finally:
            self.eslint_bin = saved

    def cleanup(self) -> None:
        shutil.rmtree(self.deps_dir / f"commit_{self.current_sha[:10]}", ignore_errors=True)


def lint(adapter: ESLintAdapter, repo_dir: Path, sha: str, files: list[str], branch: str):
    """precheck + run no commit ja checado-out; devolve (violacoes, info)."""
    adapter.precheck(repo_dir, sha, branch)
    violations = adapter.run(repo_dir, files)
    info = dict(adapter.last_run_info)
    info.pop("record_extra", None)
    return violations, info


def rule_counts(violations: list[dict]) -> Counter:
    return Counter(v["rule_id"] for v in violations if v["rule_id"] != ESLintAdapter.IGNORED_RULE_ID)


def occurrences(violations: list[dict]) -> set:
    return {(v["file_path"], v["line"], v["column"], v["rule_id"]) for v in violations}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="OWNER/NAME como em commits.jsonl")
    ap.add_argument("--commits", default="commits.jsonl")
    ap.add_argument("--repos-dir", default="repos_cache")
    ap.add_argument("--n", type=int, default=30, help="tamanho da amostra")
    ap.add_argument("--out-dir", default="calibracao_eslint")
    ap.add_argument("--eslint-bin", default="eslint")
    ap.add_argument("--install-timeout", type=int, default=900)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    repos_dir = Path(args.repos_dir)
    commits = [c for c in map(json.loads, open(args.commits, encoding="utf-8")) if c["repo_full_name"] == args.repo]
    if not commits:
        sys.exit(f"nenhum commit de {args.repo} em {args.commits}")
    repo_dir = repos_dir / f'{commits[0]["owner"]}__{commits[0]["name"]}'
    branch = commits[0]["branch"]

    base = dict(eslint_bin=args.eslint_bin, eslint_version=None, skip_npm_install=False,
                install_timeout=args.install_timeout, repos_dir=str(repos_dir))
    tip = ESLintAdapter(Namespace(**base, deps_dir=str(out_dir / "deps_tip")))
    ref = CommitDepsAdapter(Namespace(**base, deps_dir=str(out_dir / "deps_commit")))

    # 1) candidatos: commit com config propria e arquivo de codigo mudado (so le arvores)
    candidates = []
    for c in commits:
        files = get_changed_files(repo_dir, c["commit_hash"], LANGUAGE_EXTENSIONS["JavaScript"],
                                  ESLintAdapter.EXCLUDE_DIRS, ESLintAdapter.EXCLUDE_SUFFIXES)
        if not files:
            continue
        try:
            plan = tip.precheck(repo_dir, c["commit_hash"], branch)
        except CommitDiscarded:
            continue
        if plan["ruleset"] == "own":
            candidates.append((c, files))
    logging.info("%s: %d commits candidatos (config propria + codigo mudado) de %d", args.repo, len(candidates), len(commits))
    if not candidates:
        sys.exit("nenhum candidato")
    # espalha do mais novo ao mais antigo (commits.jsonl vem do mais novo pro mais antigo)
    n = min(args.n, len(candidates))
    idx = sorted({round(i * (len(candidates) - 1) / max(n - 1, 1)) for i in range(n)})
    sample = [candidates[i] for i in idx]

    rows = []
    for k, (c, files) in enumerate(sample, 1):
        sha = c["commit_hash"]
        logging.info("[%d/%d] %s %s (%d arquivo(s))", k, len(sample), sha[:10], c["commit_date"][:10], len(files))
        checkout_commit(repo_dir, sha)
        try:
            v_tip, i_tip = lint(tip, repo_dir, sha, files, branch)
            ref.current_sha = sha
            v_ref, i_ref = lint(ref, repo_dir, sha, files, branch)
        except Exception as exc:  # um commit ruim nao derruba a calibracao
            logging.error("%s: %s", sha[:10], exc)
            continue
        finally:
            ref.cleanup()
        rc_t, rc_r = rule_counts(v_tip), rule_counts(v_ref)
        total = max(sum(rc_t.values()), sum(rc_r.values()), 1)
        l1 = sum(abs(rc_t[r] - rc_r[r]) for r in set(rc_t) | set(rc_r))
        occ_t, occ_r = occurrences(v_tip), occurrences(v_ref)
        union = len(occ_t | occ_r)
        rows.append({
            "commit": sha, "data": c["commit_date"][:10], "n_arquivos": len(files),
            "tip_violacoes": sum(rc_t.values()), "commit_violacoes": sum(rc_r.values()),
            "tip_cfg_falhou": i_tip["own_config_load_failed"], "commit_cfg_falhou": i_ref["own_config_load_failed"],
            "tip_deps": i_tip["deps_source"], "commit_deps": i_ref["deps_source"], "commit_motor": ref.engine,
            "dif_relativa_por_regra": round(l1 / total, 4),
            "jaccard_ocorrencias": round(len(occ_t & occ_r) / union, 4) if union else 1.0,
            "regras_so_tip": len(set(rc_t) - set(rc_r)), "regras_so_commit": len(set(rc_r) - set(rc_t)),
        })

    if not rows:
        sys.exit("nenhum commit calibrado (veja os erros acima)")
    with open(out_dir / "calibracao.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

    both_ok = [r for r in rows if not r["tip_cfg_falhou"] and not r["commit_cfg_falhou"]]
    lines = [
        f"repositorio: {args.repo} | commits calibrados: {len(rows)} (de {len(candidates)} candidatos)",
        f"config da propria ponta carregou: {sum(not r['tip_cfg_falhou'] for r in rows)}/{len(rows)}",
        f"config carregou com deps DO COMMIT:   {sum(not r['commit_cfg_falhou'] for r in rows)}/{len(rows)}",
        f"os dois carregaram (comparaveis):     {len(both_ok)}/{len(rows)}",
        f"gabarito rodou com ESLint local do commit: {sum(r['commit_motor'] == 'local' for r in rows)}/{len(rows)} (resto: ESLint global)",
    ]
    if both_ok:
        d = [r["dif_relativa_por_regra"] for r in both_ok]
        j = [r["jaccard_ocorrencias"] for r in both_ok]
        ident = sum(1 for r in both_ok if r["dif_relativa_por_regra"] == 0)
        lines += [
            f"identicos por regra: {ident}/{len(both_ok)}",
            f"diferenca relativa por regra: mediana {statistics.median(d):.3f} | maxima {max(d):.3f}",
            f"Jaccard das ocorrencias:      mediana {statistics.median(j):.3f} | minimo {min(j):.3f}",
            "",
            "por idade (tercos do mais novo ao mais antigo; diferenca relativa mediana / Jaccard mediano):",
        ]
        third = max(len(both_ok) // 3, 1)
        for name, part in (("mais novos", both_ok[:third]), ("meio", both_ok[third:2 * third]), ("mais antigos", both_ok[2 * third:])):
            if part:
                lines.append(f"  {name:13s} n={len(part):3d}  dif={statistics.median(r['dif_relativa_por_regra'] for r in part):.3f}  "
                             f"jaccard={statistics.median(r['jaccard_ocorrencias'] for r in part):.3f}")
    text = "\n".join(lines) + "\n"
    (out_dir / "resumo_calibracao.txt").write_text(text, encoding="utf-8")
    print(text)
    shutil.rmtree(out_dir / "deps_commit", ignore_errors=True)


if __name__ == "__main__":
    main()
