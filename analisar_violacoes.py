#!/usr/bin/env python3
"""
analisar_violacoes.py

Perfil exploratorio de um (ou mais) arquivo(s) de violacoes do pipeline Babel
(pylint, checkstyle ou eslint). Le em STREAMING (arquivos de varios GB), so
leitura. Gera um resumo em texto + CSVs em --out-dir.

Uso:
    python3 analisar_violacoes.py --violations violations_checkstyle_v3.jsonl \
        --label checkstyle_v3 --commits commits.jsonl --out-dir analise_viol_checkstyle
    python3 analisar_violacoes.py --violations violations.jsonl \
        --label pylint --commits commits.jsonl --out-dir analise_viol_pylint

  --commits    (opcional) commits.jsonl: da a data de cada commit (o registro
               de violacao nao tem data) -> habilita a tabela por ano.
  --status-log (opcional) status-log do run_linters: da o denominador "commits
               lintados" (violacoes por commit contando os que deram zero).

Observacao: so aparecem commits COM violacao; commits limpos nao estao no
arquivo de violacoes (por isso o --status-log, quando existir).
"""
import argparse, csv, re, sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import orjson
    def loads(b): return orjson.loads(b)
except ImportError:
    import json
    def loads(b): return json.loads(b)

PYLINT_ID = re.compile(r"^[CRWEFI]\d{4}$")
PYLINT_CAT = {"C": "convention", "R": "refactor", "W": "warning", "E": "error", "F": "fatal", "I": "informational"}


def categoria(rule):
    """Categoria grosseira da regra, so para agrupar: pylint = letra do id;
    checkstyle = pacote depois de 'checks.'; eslint/outros = 'regra de nucleo' ou 'plugin'."""
    if not rule:
        return "(sem regra)"
    if PYLINT_ID.match(rule):
        return "pylint:" + PYLINT_CAT[rule[0]]
    if rule.startswith("com.puppycrawl") or ".checks." in rule:
        part = rule.split(".checks.", 1)[-1].split(".")
        return "checkstyle:" + (part[0] if len(part) > 1 else "raiz")
    if rule.startswith("("):
        return rule
    if "/" in rule:
        return "eslint:plugin"
    return "eslint:nucleo"


def linter_de(v):
    """Linter do registro: campo 'linter'; se faltar (registro antigo), infere
    pelo formato da regra (id do pylint, classe do checkstyle, resto = eslint)."""
    l = v.get("linter")
    if l:
        return l
    r = v.get("rule_id") or ""
    if PYLINT_ID.match(r):
        return "pylint"
    if r.startswith("com.puppycrawl") or ".checks." in r:
        return "checkstyle"
    return "eslint"


def jl(path):
    with open(path, "rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield loads(line)
            except Exception:
                continue


def quantis(vals, qs=(0.5, 0.9, 0.99)):
    if not vals:
        return {}
    v = sorted(vals)
    return {q: v[min(int(q * len(v)), len(v) - 1)] for q in qs} | {"max": v[-1]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--violations", required=True, action="append")
    ap.add_argument("--label", default="violacoes")
    ap.add_argument("--commits")
    ap.add_argument("--status-log")
    ap.add_argument("--out-dir", default="analise_violacoes")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--extensoes", default="",
                    help="lista separada por virgula (ex.: .py). So conta violacoes cujo file_path termina nelas; "
                         "descarta, por ex., avisos de config do pylint (R0022/W0012), que vem com file_path='pylintrc'. "
                         "O resumo diz quanto foi descartado e o que era.")
    ap.add_argument("--linter", action="append", default=[],
                    help="so conta registros deste linter (pylint|checkstyle|eslint); repetivel. "
                         "Necessario quando o arquivo mistura linters (ex.: violations.jsonl antigo)")
    args = ap.parse_args()
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)

    data = {}
    if args.commits:
        for c in jl(args.commits):
            data[c["commit_hash"]] = (c.get("commit_date") or "")[:4]

    n = 0
    sev = Counter(); cfg = Counter(); versoes = Counter(); cat = Counter(); deps = Counter()
    rule = defaultdict(lambda: [0, 0, set()])         # n, n_own, repos
    repo = defaultdict(lambda: [0, 0, set(), set(), Counter()])  # n, n_own, commits, arquivos(hash), regras
    commit_n = Counter()
    ano = defaultdict(lambda: [0, 0, set()])          # n, n_own, commits
    sem_data = 0
    por_linter = Counter(); lidas = 0
    exts = tuple(e.strip() for e in args.extensoes.split(",") if e.strip())
    fora_ext = Counter()   # (regra, nome do arquivo) das violacoes descartadas por extensao
    for path in args.violations:
        for v in jl(path):
            lidas += 1
            if lidas % 2_000_000 == 0:
                print(f"  ... {lidas:,} registros lidos ({n:,} usados)", file=sys.stderr)
            lk = linter_de(v)
            por_linter[lk] += 1
            if args.linter and lk not in args.linter:
                continue
            if exts and not (v.get("file_path") or "").endswith(exts):
                fora_ext[(v.get("rule_id"), (v.get("file_path") or "?").rsplit("/", 1)[-1])] += 1
                continue
            n += 1
            rid = v.get("rule_id"); r = v.get("repo_full_name"); h = v.get("commit_hash")
            own = bool(v.get("has_own_config"))
            sev[v.get("severity")] += 1
            cfg["own" if own else ("default (config propria nao carregou)" if v.get("own_config_load_failed") else "default")] += 1
            if v.get("linter_version"): versoes[v["linter_version"]] += 1
            if v.get("deps_source"): deps[v["deps_source"]] += 1
            cat[categoria(rid)] += 1
            a = rule[rid]; a[0] += 1; a[1] += own; a[2].add(r)
            b = repo[r]; b[0] += 1; b[1] += own; b[2].add(h); b[3].add(hash(v.get("file_path"))); b[4][rid] += 1
            commit_n[h] += 1
            if data:
                y = data.get(h)
                if y is None: sem_data += 1; y = "????"
                c = ano[y]; c[0] += 1; c[1] += own; c[2].add(h)

    if n == 0:
        sys.exit(f"nenhuma violacao lida (linters no arquivo: {dict(por_linter)}; filtro: {args.linter})")

    lines = []
    def P(s=""): lines.append(s); print(s)

    P(f"== {args.label} ==")
    P("registros no arquivo por linter: " + ", ".join(f"{k}={c:,}" for k, c in por_linter.most_common())
      + (f"  -> usando so: {', '.join(args.linter)}" if args.linter else "  (SEM filtro --linter)"))
    if len(por_linter) > 1 and not args.linter:
        P("  ATENCAO: o arquivo mistura linters; use --linter para analisar um por vez.")
    P(f"violacoes: {n:,} | repositorios: {len(repo):,} | commits com violacao: {len(commit_n):,} | regras distintas: {len(rule):,}")
    P(f"arquivos distintos (aprox.): {sum(len(b[3]) for b in repo.values()):,}")

    if exts:
        tot_fora = sum(fora_ext.values())
        P(f"filtro de extensao {', '.join(exts)}: {tot_fora:,} registros descartados ({100*tot_fora/max(tot_fora+n,1):.1f}% do que passou pelo filtro de linter)")
        for (rid, nome), c in fora_ext.most_common(8):
            P(f"    descartado: regra {rid} em '{nome}': {c:,}")
    lintados = None
    if args.status_log:
        last = {}
        for s in jl(args.status_log):
            last[(s["repo_full_name"], s["commit_hash"])] = s
        lintados = sum(1 for s in last.values() if s["status"] == "linted")
        P(f"commits lintados (status-log): {lintados:,} -> {100*len(commit_n)/max(lintados,1):.1f}% tiveram ao menos 1 violacao; "
          f"media {n/max(lintados,1):.2f} violacoes por commit lintado")

    P("\n-- Severidade --")
    for k, c in sev.most_common(): P(f"  {str(k):12s} {c:12,d}  {100*c/n:5.1f}%")
    P("\n-- Config usada --")
    for k, c in cfg.most_common(): P(f"  {k:42s} {c:12,d}  {100*c/n:5.1f}%")
    if deps:
        P("  origem das dependencias (eslint): " + ", ".join(f"{k}={c:,}" for k, c in deps.most_common()))
    if versoes:
        P("  versoes do linter: " + ", ".join(f"{k}={c:,}" for k, c in versoes.most_common(10)))

    P("\n-- Categorias --")
    for k, c in cat.most_common(15): P(f"  {k:32s} {c:12,d}  {100*c/n:5.1f}%")

    P(f"\n-- Top {args.top} regras --")
    P(f"  {'regra':64s} {'n':>11s} {'%':>6s} {'repos':>6s}")
    top_rules = sorted(rule.items(), key=lambda kv: -kv[1][0])
    acc = 0
    for rid, (c, _, rs) in top_rules[:args.top]:
        acc += c
        P(f"  {str(rid)[:64]:64s} {c:11,d} {100*c/n:5.1f}% {len(rs):6d}")
    P(f"  as {args.top} regras mais frequentes somam {100*acc/n:.1f}% das violacoes; "
      f"{sum(1 for _, (c, _, _) in top_rules if c == 1)} regras aparecem 1 so vez")

    P("\n-- Concentracao por repositorio --")
    rs = sorted(repo.items(), key=lambda kv: -kv[1][0])
    tot_r = len(rs)
    for k in (1, 5, 10):
        P(f"  top {k:2d} repos: {100*sum(b[0] for _, b in rs[:k])/n:5.1f}% das violacoes")
    k10 = max(tot_r // 10, 1)
    P(f"  top 10% dos repos ({k10}): {100*sum(b[0] for _, b in rs[:k10])/n:5.1f}% das violacoes")
    P("  maiores: " + "; ".join(f"{name} {b[0]:,}" for name, b in rs[:5]))

    q = quantis(list(commit_n.values()))
    P("\n-- Violacoes por commit (so commits com violacao) --")
    P("  mediana {} | p90 {} | p99 {} | max {}".format(q[0.5], q[0.9], q[0.99], q["max"]))

    if data:
        P("\n-- Por ano do commit --")
        P(f"  {'ano':6s} {'violacoes':>12s} {'commits':>9s} {'viol/commit':>11s} {'%config propria':>15s}")
        for y in sorted(ano):
            c = ano[y]
            P(f"  {y:6s} {c[0]:12,d} {len(c[2]):9,d} {c[0]/len(c[2]):11.2f} {100*c[1]/c[0]:14.1f}%")
        if sem_data:
            P(f"  ({sem_data:,} violacoes de commits que nao estao no --commits)")

    with open(out / "por_regra.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["regra", "categoria", "n", "pct", "n_repos", "n_config_propria"])
        for rid, (c, o, rp) in top_rules:
            w.writerow([rid, categoria(rid), c, round(100*c/n, 4), len(rp), o])
    with open(out / "por_repo.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["repo", "n", "commits_com_violacao", "arquivos", "viol_por_commit", "pct_config_propria", "regra_mais_frequente"])
        for name, b in rs:
            top = b[4].most_common(1)[0][0] if b[4] else ""
            w.writerow([name, b[0], len(b[2]), len(b[3]), round(b[0]/len(b[2]), 2), round(100*b[1]/b[0], 1), top])
    if data:
        with open(out / "por_ano.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(["ano", "n", "commits", "viol_por_commit", "pct_config_propria"])
            for y in sorted(ano):
                c = ano[y]; w.writerow([y, c[0], len(c[2]), round(c[0]/len(c[2]), 2), round(100*c[1]/c[0], 1)])
    (out / "resumo.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    P(f"\nsalvo em {out}/ (resumo.txt, por_regra.csv, por_repo.csv" + (", por_ano.csv)" if data else ")"))


if __name__ == "__main__":
    main()
