"""
extract_commit_history.py

Etapa 2 do pipeline de mineracao de violacoes de linters (dataset "Babel").

Le a lista de repositorios selecionados (saida da etapa 1) e, para cada
um, faz um clone parcial (partial clone) local e extrai o historico de
commits da branch principal, dentro de uma JANELA DE DATA (--since/--until),
que tocaram arquivos da linguagem alvo.

Nao faz checkout de working tree aqui -- isso fica pra etapa 3, quando
o linter de fato precisa da arvore de arquivos/config no disco pra
rodar. Aqui so enumeramos QUAIS commits sao candidatos a mineracao.

IMPORTANTE sobre --max-commits-per-repo: e OPCIONAL, default 0 = SEM
LIMITE (pega todos os commits relevantes da janela de data, nao importa
quantos). Se quiser um teto de seguranca (evita um unico repositorio
gigante travar a mineracao por horas), passe um numero positivo -- mas
na pratica, pra repositorio popular/ativo (o perfil tipico deste
dataset), e comum passar de 500 tranquilamente dentro de uma janela de
10 anos, entao um teto baixo demais so gera dado incompleto pra quase
todo mundo.

Uso:
    python extract_commit_history.py \
        --input selected_repos.jsonl \
        --repos-dir ./repos_cache \
        --since 2016-01-01 \
        --until 2026-12-31 \
        --output commits.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import stat
import subprocess
from pathlib import Path

LANGUAGE_EXTENSIONS = {
    "Python": ["*.py"],
    "Java": ["*.java"],
    "JavaScript": ["*.js", "*.jsx"],
    "C": ["*.c", "*.h"],
    "C++": ["*.cpp", "*.hpp", "*.cc", "*.cxx"],
}


def run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} falhou em {cwd}:\n{result.stderr}")
    return result.stdout


def _force_remove_readonly(func, path, exc_info):
    """Callback para shutil.rmtree: no Windows, arquivos dentro de .git
    costumam ficar marcados como somente-leitura e o rmtree falha sem isto."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def is_valid_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def clone_repo(url: str, dest: Path) -> None:
    if dest.exists():
        if is_valid_git_repo(dest):
            logging.info("Ja clonado, atualizando: %s", dest)
            run_git(["fetch", "origin"], cwd=dest)
            return
        # Pasta existe mas nao e um clone git valido -- sobra de uma
        # tentativa anterior interrompida (Ctrl+C, erro de rede, etc).
        # Remove e clona do zero em vez de falhar.
        logging.warning("Pasta incompleta/invalida encontrada, removendo antes de reclonar: %s", dest)
        shutil.rmtree(dest, onerror=_force_remove_readonly)

    dest.parent.mkdir(parents=True, exist_ok=True)
    logging.info("Clonando (partial clone) %s -> %s", url, dest)
    # --filter=blob:none: baixa toda a arvore de commits/metadados do
    # historico completo, mas adia o download do CONTEUDO dos arquivos
    # (blobs) ate serem de fato necessarios -- crucial pra repo grande.
    # --no-checkout: nao materializa a working tree agora; isso
    # acontece sob demanda na etapa 3, so para os commits selecionados.
    run_git(
        ["clone", "--filter=blob:none", "--no-checkout", url, str(dest)],
        cwd=dest.parent,
    )


def relevant_commits(
    repo_dir: Path, default_branch: str, extensions: list[str],
    since: str, until: str, max_commits: int,
) -> list[dict]:
    args = [
        "log",
        f"origin/{default_branch}",
        f"--since={since}",
        f"--until={until}",
        "--pretty=format:%H|%cI",
    ]
    if max_commits > 0:
        args.append(f"--max-count={max_commits}")
    args += ["--", *extensions]

    output = run_git(args, cwd=repo_dir)
    commits = []
    for line in output.splitlines():
        if not line.strip():
            continue
        commit_hash, commit_date = line.split("|", 1)
        commits.append({"commit_hash": commit_hash, "commit_date": commit_date})
    return commits


def already_extracted_repos(output_path: Path) -> set[str]:
    """Le commits.jsonl (se ja existir) e devolve os repo_full_name que
    ja tiveram commit extraido. Usado pra pular repositorio ja feito
    quando voce cresce selected_repos.jsonl aos poucos -- sem isso, cada
    rodada nova reprocessaria (e duplicaria) tudo que ja foi extraido
    antes, nao so os repositorios novos."""
    if not output_path.exists():
        return set()
    seen = set()
    with output_path.open(encoding="utf-8") as f:
        for line in f:
            try:
                commit = json.loads(line)
            except json.JSONDecodeError:
                continue
            seen.add(commit["repo_full_name"])
    return seen


def main() -> None:
    parser = argparse.ArgumentParser(description="Extrai historico de commits relevantes por repositorio")
    parser.add_argument("--input", default="selected_repos.jsonl", help="Saida da etapa 1 (selecao de repos)")
    parser.add_argument("--repos-dir", default="repos_cache", help="Onde ficam os clones locais")
    parser.add_argument("--since", default="2016-01-01", help="Data inicial da janela de mineracao (YYYY-MM-DD)")
    parser.add_argument("--until", default="2026-12-31", help="Data final da janela de mineracao (YYYY-MM-DD)")
    parser.add_argument(
        "--max-commits-per-repo",
        type=int,
        default=0,
        help="TETO DE SEGURANCA de commits relevantes por repo dentro da janela de data. "
             "0 (padrao) = SEM LIMITE -- pega todos os commits relevantes da janela, por "
             "maior que seja o repositorio.",
    )
    parser.add_argument("--output", default="commits.jsonl", help="Arquivo de saida (JSON Lines, modo append)")
    parser.add_argument(
        "--force-reprocess", action="store_true",
        help="Reprocessa mesmo repositorio ja presente em --output (por padrao, sao pulados)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    repos_dir = Path(args.repos_dir)
    output_path = Path(args.output)

    already_done = set() if args.force_reprocess else already_extracted_repos(output_path)
    if already_done:
        logging.info("%d repositorio(s) ja tem commit extraido em %s -- serao pulados", len(already_done), output_path)

    with open(args.input, encoding="utf-8") as f, output_path.open("a", encoding="utf-8") as out:
        for line in f:
            repo = json.loads(line)

            if repo["full_name"] in already_done:
                continue

            extensions = LANGUAGE_EXTENSIONS.get(repo["language_primary"])
            if not extensions:
                logging.warning(
                    "Linguagem sem extensoes mapeadas: %s (repo %s)",
                    repo["language_primary"], repo["full_name"],
                )
                continue

            repo_dir = repos_dir / repo["full_name"].replace("/", "__")
            clone_repo(repo["url"], repo_dir)

            commits = relevant_commits(
                repo_dir, repo["default_branch"], extensions, args.since, args.until, args.max_commits_per_repo,
            )
            if args.max_commits_per_repo > 0 and len(commits) >= args.max_commits_per_repo:
                logging.warning(
                    "%s: atingiu o TETO DE SEGURANCA (%d) -- provavelmente tem mais commits relevantes "
                    "na janela %s..%s do que os capturados aqui",
                    repo["full_name"], args.max_commits_per_repo, args.since, args.until,
                )
            logging.info("%s: %d commits relevantes encontrados", repo["full_name"], len(commits))

            for commit in commits:
                out.write(json.dumps({
                    "repo_full_name": repo["full_name"],
                    "owner": repo["owner"],
                    "name": repo["name"],
                    "branch": repo["default_branch"],
                    "commit_hash": commit["commit_hash"],
                    "commit_date": commit["commit_date"],
                }, ensure_ascii=False) + "\n")

    logging.info("Gravado em %s", output_path.resolve())


if __name__ == "__main__":
    main()
