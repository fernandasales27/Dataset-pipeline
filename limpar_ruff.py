import json, os, shutil, sys
from pathlib import Path

APPLY = "--apply" in sys.argv
roots = [Path.home() / "babel-1.0" / "repos_cache", Path.home() / "dev" / "babel" / "repos_cache"]

sel = [json.loads(l) for l in open("selected_repos.jsonl") if l.strip()]
ruff = {r["full_name"] for r in sel if r["linter"] == "ruff"}
outros = {r["full_name"] for r in sel if r["linter"] != "ruff"}
so_ruff = ruff - outros
print(f"ruff: {len(ruff)} repos | so ruff (clone sera apagado): {len(so_ruff)} | "
      f"tambem em outro linter (clone MANTIDO): {len(ruff & outros)}")
for r in sorted(ruff & outros):
    print("  mantido:", r)

regras = [
    ("selected_repos.jsonl", lambda d: d.get("linter") != "ruff"),
    ("commits.jsonl", lambda d: d["repo_full_name"] not in so_ruff),
    ("processed_commits.jsonl", lambda d: d.get("linter") != "ruff"),
]
for nome, manter in regras:
    p = Path(nome)
    total = removidas = 0
    tmp = p.with_suffix(".tmp_ruff")
    out = tmp.open("w", encoding="utf-8") if APPLY else None
    for l in p.open(encoding="utf-8"):
        total += 1
        try:
            ok = manter(json.loads(l))
        except json.JSONDecodeError:
            ok = True
        if ok:
            if out: out.write(l)
        else:
            removidas += 1
    if out:
        out.close()
        shutil.copy2(p, p.with_suffix(p.suffix + ".bak_ruff"))
        os.replace(tmp, p)
    print(f"{nome}: {total} linhas, {removidas} {'removidas' if APPLY else 'seriam removidas'}")

for r in sorted(so_ruff):
    for root in roots:
        c = root / r.replace("/", "__")
        if c.is_symlink() or c.exists():
            assert c.parent == root
            print(("apagando clone: " if APPLY else "apagaria clone: ") + str(c))
            if APPLY:
                c.unlink() if c.is_symlink() else shutil.rmtree(c)
print("FIM" if APPLY else "FIM (simulacao: nada foi alterado; use --apply para aplicar)")
