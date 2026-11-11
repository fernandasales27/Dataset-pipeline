#!/usr/bin/env python3
"""
escolher_repo_calibracao.py -- ranqueia repositorios JavaScript para a
calibracao do ESLint (calibrar_eslint.py). So le arvores git (ls-tree /
diff-tree), nao instala nada e nao faz checkout.

Para cada repositorio, olha ate --amostra commits espalhados do mais novo ao
mais antigo e conta quantos: (a) tocam arquivo de codigo (apos filtros de
teste), (b) tem package.json na raiz, (c) tem config PROPRIA do ESLint.
Bom candidato = muitos commits em (c), com historico longo (datas
espalhadas) -- e, de preferencia, config com plugins.

Uso:
    python3 escolher_repo_calibracao.py --commits commits_sem_nifi.jsonl \
        --repos-dir repos_cache [--amostra 60] [--top 15]
"""
import argparse, json, sys
from argparse import Namespace
from collections import defaultdict
from pathlib import Path

from common import LANGUAGE_EXTENSIONS, get_changed_files
from linter_adapters import CommitDiscarded, ESLintAdapter


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commits", default="commits_sem_nifi.jsonl")
    ap.add_argument("--repos-dir", default="repos_cache")
    ap.add_argument("--amostra", type=int, default=60, help="commits avaliados por repositorio")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    by_repo = defaultdict(list)
    for line in open(args.commits, encoding="utf-8"):
        c = json.loads(line)
        by_repo[c["repo_full_name"]].append(c)

    a = ESLintAdapter(Namespace(eslint_bin="eslint", eslint_version=None, skip_npm_install=True,
                                install_timeout=60, repos_dir=args.repos_dir, deps_dir="/tmp/diag_deps_unused"))
    rows = []
    for k, (repo_name, cs) in enumerate(sorted(by_repo.items()), 1):
        c0 = cs[0]
        repo = Path(args.repos_dir) / f'{c0["owner"]}__{c0["name"]}'
        if not repo.exists():
            continue
        n = min(args.amostra, len(cs))
        idx = sorted({round(i * (len(cs) - 1) / max(n - 1, 1)) for i in range(n)})
        tot = cod = node = own = 0
        datas = []
        for i in idx:
            c = cs[i]
            tot += 1
            try:
                files = get_changed_files(repo, c["commit_hash"], LANGUAGE_EXTENSIONS["JavaScript"],
                                          ESLintAdapter.EXCLUDE_DIRS, ESLintAdapter.EXCLUDE_SUFFIXES)
            except Exception:
                continue
            if not files:
                continue
            cod += 1
            try:
                plan = a.precheck(repo, c["commit_hash"], c["branch"])
            except CommitDiscarded:
                continue
            except Exception:
                continue
            node += 1
            if plan["ruleset"] == "own":
                own += 1
                datas.append(c["commit_date"][:10])
        rows.append((own, repo_name, len(cs), tot, cod, node, own, min(datas) if datas else "-", max(datas) if datas else "-", plan["config_kind"] if own else "-"))
        print(f"[{k}/{len(by_repo)}] {repo_name}: own={own}/{tot}", file=sys.stderr)

    rows.sort(reverse=True)
    print(f"\n{'repositorio':42s} {'commits':>7s} {'amostra':>7s} {'c/codigo':>8s} {'c/pkg':>6s} {'c/cfg':>6s}  {'cfg mais antiga':>15s}  {'mais nova':>10s}  tipo")
    for own, name, n_all, tot, cod, node, _, d0, d1, kind in rows[:args.top]:
        print(f"{name:42s} {n_all:7d} {tot:7d} {cod:8d} {node:6d} {own:6d}  {d0:>15s}  {d1:>10s}  {kind}")
    sem = sum(1 for r in rows if r[0] == 0)
    print(f"\nrepositorios avaliados: {len(rows)} | sem nenhum commit com config propria na amostra: {sem}")
    print("c/codigo = toca arquivo JS (sem teste) | c/pkg = alem disso tem package.json na raiz | c/cfg = alem disso tem config propria do ESLint")


if __name__ == "__main__":
    main()
