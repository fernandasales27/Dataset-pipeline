import json, shutil, sys
from pathlib import Path

linter = sys.argv[1]
src_root = Path.home() / "dev" / "babel" / "repos_cache"
dst_root = Path.home() / "babel-1.0" / "repos_cache"
repos = sorted({json.loads(l)["full_name"] for l in open("selected_repos.jsonl")
                if json.loads(l).get("linter") == linter})
for r in repos:
    nome = r.replace("/", "__")
    src, dst = src_root / nome, dst_root / nome
    if dst.exists():
        print(("DUPLICADO (mantida a de babel-1.0): " if src.exists() else "ja unificado: ") + r)
    elif src.exists():
        shutil.move(str(src), str(dst))
        print("movido: " + r)
    else:
        print("SEM CLONE EM NENHUMA PASTA: " + r)
