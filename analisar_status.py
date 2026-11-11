"""
analisar_status.py

Resume o --status-log do run_linters.py (uma linha por commit avaliado):
funil por desfecho/motivo, por repositorio, por ano, ruleset usado
(config do time x sun_checks) e opcoes do pom registradas. So leitura.

Uso:
    python3 analisar_status.py --status-log commit_status_checkstyle.jsonl \
        [--violations violations_checkstyle_v3.jsonl] [--out-dir analise_status]

Se o mesmo commit aparece mais de uma vez (reexecucao), vale a ULTIMA linha.
"""
import argparse, csv, json
from collections import Counter, defaultdict
from pathlib import Path


def jl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status-log", required=True, action="append")
    ap.add_argument("--violations", action="append", default=[])
    ap.add_argument("--out-dir", default="analise_status")
    args = ap.parse_args()

    last = {}
    for p in args.status_log:
        for r in jl(p):
            last[(r["repo_full_name"], r["commit_hash"])] = r
    rows = list(last.values())
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    lines = []
    def P(s=""): lines.append(s); print(s)

    P(f"commits avaliados (unicos): {len(rows)}")
    by_status = Counter(r["status"] for r in rows)
    P("\n== Desfecho ==")
    for k, v in by_status.most_common():
        P(f"  {k:10s} {v:8d}  {100*v/len(rows):5.1f}%")

    P("\n== Motivos (excluded/skipped/error; um commit pode ter mais de um motivo de exclusao) ==")
    reasons = Counter()
    for r in rows:
        if r["status"] == "excluded":
            for x in (r.get("reasons") or [r["reason"]]):
                reasons[("excluded", x)] += 1
        elif r["status"] in ("skipped", "error"):
            reasons[(r["status"], r["reason"])] += 1
    for (st, rs), v in reasons.most_common():
        P(f"  {st:9s} {rs:28s} {v:8d}")

    linted = [r for r in rows if r["status"] == "linted"]
    P(f"\n== Lintados: {len(linted)} ==")
    rs = Counter(r.get("ruleset_efetivo") or r.get("ruleset") for r in linted)
    for k, v in rs.most_common():
        P(f"  ruleset efetivo {k!s:8s} {v:8d}  {100*v/max(len(linted),1):5.1f}%")
    P(f"  config propria que nao carregou (caiu no default): {sum(1 for r in linted if r.get('own_config_load_failed'))}")
    P(f"  commits com >1 config candidata: {sum(1 for r in linted if (r.get('n_configs') or 0) > 1)}")
    P(f"  pom ilegivel (tratado como sem opcoes): {sum(1 for r in linted if r.get('pom_parse_error'))}")
    flags = Counter(f for r in linted for f in (r.get("pom_flags") or []))
    P("  opcoes do maven-checkstyle-plugin registradas (nao bloqueantes):")
    for k, v in flags.most_common():
        P(f"    {k:28s} {v:8d}")
    if not flags:
        P("    (nenhuma)")
    ds = Counter(r.get("deps_source") for r in linted if r.get("deps_source"))
    if ds:
        P("  origem das dependencias (eslint): " + ", ".join(f"{k}={v}" for k, v in ds.most_common()))
    P(f"  violacoes (soma n_violations): {sum(r.get('n_violations') or 0 for r in linted)}")

    P("\n== Por ano (data do commit) ==")
    by_year = defaultdict(Counter)
    for r in rows:
        y = (r.get("commit_date") or "????")[:4]
        by_year[y][r["status"]] += 1
        if r["status"] == "linted":
            by_year[y]["own" if (r.get("ruleset_efetivo") or r.get("ruleset")) == "own" else "default"] += 1
            if r.get("own_config_load_failed"):
                by_year[y]["falhou"] += 1
    P(f"  {'ano':6s} {'linted':>8s} {'excluded':>9s} {'skipped':>8s} {'error':>6s} {'own':>7s} {'default':>8s} {'cfg_falhou':>10s}")
    for y in sorted(by_year):
        c = by_year[y]
        P(f"  {y:6s} {c['linted']:8d} {c['excluded']:9d} {c['skipped']:8d} {c['error']:6d} {c['own']:7d} {c['default']:8d} {c['falhou']:10d}")

    per_repo = defaultdict(Counter)
    for r in rows:
        c = per_repo[r["repo_full_name"]]
        c["total"] += 1
        c[r["status"]] += 1
        if r["status"] == "excluded":
            for x in (r.get("reasons") or [r["reason"]]):
                c["ex_" + x] += 1
        if r["status"] == "linted":
            c["own" if (r.get("ruleset_efetivo") or r.get("ruleset")) == "own" else "default"] += 1
    cols = sorted({k for c in per_repo.values() for k in c if k.startswith("ex_")})
    with open(out / "por_repo.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["repo", "total", "linted", "excluded", "skipped", "error", "own", "default", *cols])
        for repo, c in sorted(per_repo.items(), key=lambda kv: -kv[1]["total"]):
            w.writerow([repo, c["total"], c["linted"], c["excluded"], c["skipped"], c["error"], c["own"], c["default"], *[c[k] for k in cols]])
    P(f"\nrepositorios: {len(per_repo)} (detalhe em {out/'por_repo.csv'})")
    sem_lint = [r for r, c in per_repo.items() if c["linted"] == 0]
    P(f"repositorios SEM nenhum commit lintado: {len(sem_lint)}")
    for r in sorted(sem_lint, key=lambda x: -per_repo[x]["total"])[:15]:
        c = per_repo[r]
        top = ", ".join(f"{k[3:]}={c[k]}" for k in cols if c[k])
        P(f"  {r:40s} {c['total']:6d} commits  [{top or 'so skipped/error'}]")

    if args.violations:
        n = Counter()
        for p in args.violations:
            for v in jl(p):
                n["total"] += 1
                n["own" if v.get("has_own_config") else "default"] += 1
        P(f"\n== Violacoes gravadas: {n['total']} (config do time: {n['own']}, sun_checks: {n['default']}) ==")

    (out / "resumo_status.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    P(f"\nsalvo em {out/'resumo_status.txt'}")


if __name__ == "__main__":
    main()
