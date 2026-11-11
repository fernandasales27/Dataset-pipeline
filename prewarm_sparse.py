import collections, json, subprocess, sys
from pathlib import Path

linter = sys.argv[1]
PATTERNS = {
    "pylint": ["*.py", "/pylintrc", "/.pylintrc", "/pylintrc.toml", "/.pylintrc.toml",
               "/pyproject.toml", "/setup.cfg", "/tox.ini"],
    "checkstyle": ["*.java", "*.xml", "*.properties", "/config/", "/checkstyle/"],
}[linter]
REPOS_DIR = Path.home() / "babel-1.0" / "repos_cache"

repos = {json.loads(l)["full_name"] for l in open("selected_repos.jsonl")
         if json.loads(l).get("linter") == linter}
fila, first = collections.Counter(), {}
for l in open("commits.jsonl"):
    c = json.loads(l)
    if c["repo_full_name"] in repos:
        fila[c["repo_full_name"]] += 1
        first.setdefault(c["repo_full_name"], c["commit_hash"])

feitos = set()
for fn in ("processed_commits.jsonl", "processed_checkstyle.jsonl"):
    if Path(fn).exists():
        for l in open(fn):
            try:
                r = json.loads(l)
            except json.JSONDecodeError:
                continue
            if r.get("linter") == linter and r["repo_full_name"] in repos:
                feitos.add((r["repo_full_name"], r["commit_hash"]))
feitos_por_repo = collections.Counter(r for r, _ in feitos)

pendentes = sorted((r for r in repos if fila[r] > feitos_por_repo[r]), key=lambda r: -fila[r])
print(f"{linter}: {len(pendentes)} repositorio(s) pendente(s)", flush=True)
for full_name in pendentes:
    d = REPOS_DIR / full_name.replace("/", "__")
    if not d.exists():
        print("SEM CLONE:", full_name, flush=True)
        continue
    (d / ".git" / "index.lock").unlink(missing_ok=True)
    subprocess.run(["git", "sparse-checkout", "init", "--no-cone"], cwd=d, check=True)
    subprocess.run(["git", "sparse-checkout", "set", *PATTERNS], cwd=d, check=True)
    print(f"checkout inicial: {full_name} ({fila[full_name] - feitos_por_repo[full_name]} commits pendentes)", flush=True)
    r = subprocess.run(["git", "checkout", "--force", "--detach", first[full_name]], cwd=d)
    print("  ->", "ok" if r.returncode == 0 else f"FALHOU ({r.returncode})", flush=True)
print("FIM", flush=True)
