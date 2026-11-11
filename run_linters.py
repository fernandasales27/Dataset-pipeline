"""
run_linters.py

Etapa 3 do pipeline de mineracao de violacoes de linters (dataset "Babel")
-- versao unificada. Um so script pra todos os linters: cada um e um
"adapter" registrado em linter_adapters.py, e o loop principal (git
diff-tree, precheck do adapter, checkout do commit, arquivamento em
Babel/, estatisticas) roda uma unica vez aqui, independente de qual
linter foi escolhido. O checkout e o ULTIMO passo caro: so roda pro
commit que passou nas checagens baratas.

Uso:
    python run_linters.py pylint \
        --commits commits.jsonl --repos-dir C:\\dev\\babel\\repos_cache \
        --dataset-dir .\\Babel --output violations.jsonl

    python run_linters.py checkstyle \
        --commits commits.jsonl --repos-dir C:\\dev\\babel\\repos_cache \
        --dataset-dir .\\Babel --checkstyle-jar C:\\dev\\babel\\checkstyle-13.8.0-all.jar \
        --output violations.jsonl

Pra adicionar um linter novo (pylint, flake8, eslint, biome, pmd,
spotbugs...): implemente um adapter em linter_adapters.py e registre em
ADAPTERS -- nao precisa tocar neste arquivo.

Sobre --progress-log: violations.jsonl so registra violacao ENCONTRADA --
um commit checado e "limpo" (0 violacoes) nao deixa rastro nele. Sem um
registro separado de "isso ja foi processado", crescer commits.jsonl aos
poucos (mais repositorios) e rodar esta etapa de novo reprocessaria (nao
duplicaria dado, mas gastaria tempo/clone de novo) tudo que ja foi feito
antes, nao so o que e novo. O progress-log tambem e o primeiro passo pra
uma futura analise de "o warning sumiu entre um commit e outro" -- ele ja
grava se cada commit teve violacao ou nao, nao so os que tiveram.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

from common import LANGUAGE_EXTENSIONS, archive_file, checkout_commit, get_changed_files
from linter_adapters import ADAPTERS, CommitDiscarded, RepoDiscarded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Roda um linter nos commits minerados e grava as violacoes")
    subparsers = parser.add_subparsers(dest="linter", required=True, help="Qual linter rodar")

    for name, adapter_cls in ADAPTERS.items():
        sub = subparsers.add_parser(name, help=f"Roda o {name}")
        sub.add_argument("--commits", default="commits.jsonl")
        sub.add_argument("--repos-dir", default="repos_cache")
        sub.add_argument("--dataset-dir", default="Babel")
        sub.add_argument("--output", default="violations.jsonl")
        sub.add_argument("--progress-log", default="processed_commits.jsonl", help="Registro de commits ja processados por este linter (pra nao reprocessar ao crescer commits.jsonl aos poucos)")
        sub.add_argument(
            "--status-log", default="commit_status.jsonl",
            help="UMA linha por commit avaliado nesta execucao, com o desfecho (linted / excluded / "
                 "skipped / error) e o motivo (codigo estavel, ex: nao_maven, config_variavel). "
                 "E o que garante que nenhum commit some sem explicacao. Se o mesmo commit "
                 "aparecer mais de uma vez (reexecucao), vale a ultima linha.",
        )
        sub.add_argument("--force-reprocess", action="store_true", help="Reprocessa mesmo commit ja presente em --progress-log")
        sub.add_argument(
            "--selected-repos", default="selected_repos.jsonl",
            help="Saida da etapa 1 -- usado pra pular de cara commit de repositorio que nao foi "
                 "selecionado pra ESTE linter (commits.jsonl mistura todas as linguagens/linters "
                 "juntos). Sem isso, cada linter tentaria checkout em repositorio de outra "
                 "linguagem a toa, e pior: se dois linters rodarem em paralelo, os dois podem "
                 "tentar checkout na MESMA pasta ao mesmo tempo e colidir (.git/index.lock).",
        )
        sub.add_argument(
            "--only-repo", action="append", default=None, metavar="OWNER/NAME",
            help="Processa SO este repositorio (pode repetir a flag). Usado pra rodar um job por "
                 "repositorio em paralelo (ex: array do Slurm). Cada job deve ter seu proprio "
                 "--output e --progress-log.",
        )
        adapter_cls.add_cli_args(sub)

    return parser


def repos_for_linter(selected_repos_path: Path, linter_name: str) -> Optional[set[str]]:
    """Le selected_repos.jsonl e devolve o conjunto de repo_full_name
    registrados pra ESTE linter especificamente. None (nao um conjunto
    vazio) se o arquivo nao existir -- nesse caso nao ha como filtrar,
    entao processa tudo (comportamento antigo, sem quebrar quem nao
    tiver esse arquivo por algum motivo)."""
    if not selected_repos_path.exists():
        return None
    repos = set()
    with selected_repos_path.open(encoding="utf-8") as f:
        for line in f:
            try:
                repo = json.loads(line)
            except json.JSONDecodeError:
                continue
            if repo.get("linter") == linter_name:
                repos.add(repo["full_name"])
    return repos


def already_processed_commits(progress_log: Path, linter_name: str) -> set[tuple[str, str]]:
    if not progress_log.exists():
        return set()
    seen = set()
    with progress_log.open(encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("linter") == linter_name:
                seen.add((record["repo_full_name"], record["commit_hash"]))
    return seen


def run_pipeline(adapter, args: argparse.Namespace) -> None:
    repos_dir = Path(args.repos_dir)
    dataset_dir = Path(args.dataset_dir)
    output_path = Path(args.output)
    progress_log_path = Path(args.progress_log)
    status_log_path = Path(args.status_log)

    if not repos_dir.exists():
        logging.error(
            "Pasta de clones nao encontrada: %s -- confere se --repos-dir aponta pro MESMO lugar "
            "usado na etapa 2 (extract_commit_history.py).",
            repos_dir.resolve(),
        )
        sys.exit(1)

    version = adapter.get_version()
    logging.info("Usando %s versao: %s", adapter.name, version)

    already_done = set() if args.force_reprocess else already_processed_commits(progress_log_path, adapter.name)
    if already_done:
        logging.info("%d commit(s) ja processado(s) por %s -- serao pulados", len(already_done), adapter.name)

    relevant_repos = repos_for_linter(Path(args.selected_repos), adapter.name)
    if relevant_repos is None:
        logging.warning(
            "%s nao encontrado -- nao da pra filtrar por repositorio relevante a este linter. "
            "Todo commit em --commits sera tentado, inclusive de outra linguagem (desperdicio de "
            "tempo, e risco de colisao de checkout se outro linter estiver rodando ao mesmo tempo).",
            args.selected_repos,
        )
    else:
        logging.info("%d repositorio(s) registrado(s) pra %s -- demais serao ignorados de cara", len(relevant_repos), adapter.name)

    if args.only_repo:
        only = set(args.only_repo)
        relevant_repos = only if relevant_repos is None else (relevant_repos & only)
        logging.info("--only-repo: restringindo a %d repositorio(s): %s", len(relevant_repos), ", ".join(sorted(relevant_repos)))

    extensions = LANGUAGE_EXTENSIONS[adapter.language]
    stats = {
        "commits_lidos": 0,
        "pulados_outro_linter": 0,
        "pulados_ja_processado": 0,
        "pulados_sem_clone": 0,
        "pulados_sem_arquivo_mudado": 0,
        "pulados_projeto_descartado": 0,
        "commits_descartados": 0,
        "commits_com_violacao": 0,
        "erros": 0,
    }
    # Contagem das exclusoes por codigo de motivo (um commit pode ter mais
    # de um motivo; conta todos, entao a soma pode passar de commits_descartados).
    exclusion_reasons: Counter = Counter()
    total_violations = 0
    # Repositorios que o adapter descartou por inteiro (RepoDiscarded):
    # todo commit restante deles e pulado aqui, sem nenhuma chamada ao git.
    discarded_repos: set[str] = set()

    with open(args.commits, encoding="utf-8") as f, \
         output_path.open("a", encoding="utf-8") as out, \
         progress_log_path.open("a", encoding="utf-8") as progress_out, \
         status_log_path.open("a", encoding="utf-8") as status_out:
        for line in f:
            commit = json.loads(line)
            stats["commits_lidos"] += 1

            if relevant_repos is not None and commit["repo_full_name"] not in relevant_repos:
                stats["pulados_outro_linter"] += 1
                continue

            if (commit["repo_full_name"], commit["commit_hash"]) in already_done:
                stats["pulados_ja_processado"] += 1
                continue

            if commit["repo_full_name"] in discarded_repos:
                stats["pulados_projeto_descartado"] += 1
                continue

            repo_dir = repos_dir / f'{commit["owner"]}__{commit["name"]}'
            if not repo_dir.exists():
                logging.warning("Clone nao encontrado, pulando: %s", repo_dir)
                stats["pulados_sem_clone"] += 1
                continue

            def mark_processed(had_violation: bool) -> None:
                progress_out.write(json.dumps({
                    "repo_full_name": commit["repo_full_name"],
                    "commit_hash": commit["commit_hash"],
                    "linter": adapter.name,
                    "had_violation": had_violation,
                }, ensure_ascii=False) + "\n")

            def write_status(status: str, reason: Optional[str] = None, **info) -> None:
                status_out.write(json.dumps({
                    "repo_full_name": commit["repo_full_name"],
                    "commit_hash": commit["commit_hash"],
                    "commit_date": commit.get("commit_date"),
                    "linter": adapter.name,
                    "status": status,
                    "reason": reason,
                    **info,
                }, ensure_ascii=False) + "\n")

            plan: Optional[dict] = None

            # Ordem das checagens: do mais barato pro mais caro, e o
            # checkout (que num clone parcial baixa blobs da rede) so
            # roda pro commit que sobreviveu a todas as outras.
            #   1. arquivos mudados   -- git diff-tree, so le arvores
            #   2. precheck do adapter -- git show, no maximo 1 blob pequeno
            #   3. checkout + linter
            try:
                changed_files = get_changed_files(
                    repo_dir, commit["commit_hash"], extensions,
                    exclude_dirs=getattr(adapter, "EXCLUDE_DIRS", None),
                    exclude_suffixes=getattr(adapter, "EXCLUDE_SUFFIXES", None),
                )
                if not changed_files:
                    stats["pulados_sem_arquivo_mudado"] += 1
                    mark_processed(had_violation=False)
                    write_status("skipped", "sem_arquivo_mudado")
                    continue

                precheck = getattr(adapter, "precheck", None)
                if precheck is not None:
                    plan = precheck(repo_dir, commit["commit_hash"], commit["branch"])

                checkout_commit(repo_dir, commit["commit_hash"])
                own_config = adapter.has_own_config(repo_dir)
                raw_violations = adapter.run(repo_dir, changed_files)
            except RepoDiscarded as exc:
                # Decisao esperada (regra do adapter), nao falha: registra
                # UMA vez e pula o resto do repositorio sem tocar no git.
                # Nao marca nada como processado.
                discarded_repos.add(commit["repo_full_name"])
                stats["commits_descartados"] += 1
                exclusion_reasons["projeto_descartado"] += 1
                write_status("excluded", "projeto_descartado", reasons=["projeto_descartado"], detail=str(exc)[:300])
                logging.warning(
                    "%s: PROJETO descartado -- %s -- os commits restantes dele serao pulados sem checkout.",
                    commit["repo_full_name"], exc,
                )
                continue
            except CommitDiscarded as exc:
                # Idem, so que para este commit. Tambem NAO marca como
                # processado (mesma regra de antes: vale reavaliar se a
                # regra mudar), mas nao e erro -- conta a parte.
                stats["commits_descartados"] += 1
                exclusion_reasons.update(exc.reasons)
                write_status("excluded", exc.reason, reasons=exc.reasons, detail=exc.detail[:300])
                logging.info(
                    "%s@%s: commit excluido [%s] -- %s",
                    commit["repo_full_name"], commit["commit_hash"][:8], ",".join(exc.reasons), exc.detail,
                )
                continue
            except Exception as exc:
                # Um commit/config problematico (ex: pyproject.toml usando
                # regra que essa versao do linter nao reconhece) nao pode
                # derrubar a execucao inteira -- registra e segue pro
                # proximo commit. Isso fica mais comum agora que so
                # minera repos com config propria de verdade.
                #
                # NAO marca como processado de proposito -- um erro pode
                # ser causado por algo temporario/corrigivel (versao do
                # linter, por exemplo), e vale tentar de novo numa proxima
                # rodada em vez de pular pra sempre.
                logging.error(
                    "%s@%s: erro ao processar, pulando -- %s",
                    commit["repo_full_name"], commit["commit_hash"][:8], exc,
                )
                stats["erros"] += 1
                write_status("error", "erro_execucao", detail=str(exc)[:300])
                continue

            # last_run_info: o que o adapter quer gravar sobre ESTE commit.
            # "record_extra" vai pra cada violacao; o resto, pro status-log.
            run_info = dict(getattr(adapter, "last_run_info", None) or {})
            record_extra = run_info.pop("record_extra", None) or {}
            plan_info = {k: val for k, val in (plan or {}).items() if k != "extra"}

            archived_files: dict[str, str] = {}
            for v in raw_violations:
                if v["file_path"] is None:
                    continue

                if v["file_path"] not in archived_files:
                    archived_files[v["file_path"]] = archive_file(
                        repo_dir, dataset_dir, adapter.language, adapter.name,
                        commit["repo_full_name"], commit["branch"], commit["commit_hash"],
                        v["file_path"],
                    )

                record = {
                    "repo_full_name": commit["repo_full_name"],
                    "owner": commit["owner"],
                    "branch": commit["branch"],
                    "commit_hash": commit["commit_hash"],
                    "repo_url": f'https://github.com/{commit["repo_full_name"]}',
                    "linter": adapter.name,
                    # v["linter_version"] permite o adapter reportar a
                    # versao REAL usada NAQUELA violacao especifica (caso
                    # do checkstyle com multiplos jars -- ver
                    # CheckstyleAdapter.run) -- sem isso, todo mundo usa
                    # a versao "principal" fixada no inicio da execucao.
                    "linter_version": v.get("linter_version", version),
                    # v["has_own_config"] permite o adapter corrigir esse
                    # valor POR VIOLACAO -- caso do checkstyle quando a
                    # config propria existe mas falha ao carregar e ele
                    # cai pro default (ver CheckstyleAdapter.run): nesse
                    # caso especifico, own_config (pre-calculado, so
                    # confirma que o ARQUIVO existe) ficaria enganoso.
                    "has_own_config": v.get("has_own_config", own_config),
                    "own_config_load_failed": v.get("own_config_load_failed", False),
                    "file_path": v["file_path"],
                    "archived_file_path": archived_files[v["file_path"]],
                    "line": v["line"],
                    "column": v["column"],
                    "rule_id": v["rule_id"],
                    "message": v["message"],
                    "severity": v["severity"],
                }
                if plan and plan.get("extra"):
                    record.update(plan["extra"])
                if record_extra:
                    record.update(record_extra)
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                total_violations += 1

            mark_processed(had_violation=bool(raw_violations))
            write_status(
                "linted", None,
                n_files=len(changed_files), n_violations=len(raw_violations),
                **plan_info, **run_info,
            )

            if raw_violations:
                stats["commits_com_violacao"] += 1

            logging.info(
                "%s@%s: %d arquivo(s) mudado(s), %d violacao(oes)",
                commit["repo_full_name"], commit["commit_hash"][:8],
                len(changed_files), len(raw_violations),
            )

    logging.info(
        "RESUMO -- linter: %s | commits lidos: %d | outro linter: %d | ja processado(s): %d | sem clone: %d | "
        "sem arquivo mudado: %d | projeto descartado (pulados): %d | commits descartados: %d | erros: %d | "
        "com violacao: %d | total de violacoes: %d | motivos de exclusao: %s | saida: %s",
        adapter.name, stats["commits_lidos"], stats["pulados_outro_linter"], stats["pulados_ja_processado"],
        stats["pulados_sem_clone"], stats["pulados_sem_arquivo_mudado"], stats["pulados_projeto_descartado"],
        stats["commits_descartados"], stats["erros"],
        stats["commits_com_violacao"], total_violations,
        dict(exclusion_reasons) or "-", output_path.resolve(),
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    adapter_cls = ADAPTERS[args.linter]
    try:
        adapter = adapter_cls(args)
    except FileNotFoundError as e:
        logging.error(str(e))
        sys.exit(1)

    run_pipeline(adapter, args)


if __name__ == "__main__":
    main()
