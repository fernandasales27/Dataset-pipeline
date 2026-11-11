"""
select_repositories.py

Etapa 1 do pipeline de mineracao de violacoes de linters (dataset "Babel").

Busca repositorios candidatos no GitHub via API de busca, aplica os
criterios de selecao definidos (sem fork, licenca permissiva, nome sem
padroes de "toy repo", ADOTA O LINTER escolhido, contribuidores minimos,
atividade recente) e faz uma amostragem estratificada por tamanho para
gerar a lista de repositorios a serem minerados.

O filtro de adocao (--linter) e essencial pro objetivo do dataset: ele so
faz sentido pra estudar como devs lidam com warnings de um linter que eles
DE FATO usam -- rodar em repos que nunca configuraram aquela ferramenta
mediria outra coisa (violacoes de um ruleset que ninguem pediu).

Uso:
    export GITHUB_TOKEN="ghp_xxx"
    python select_repositories.py --language Python --linter pylint --sample-size 20
    python select_repositories.py --language Java --linter checkstyle --sample-size 20

Saida:
    selected_repos.jsonl  (um objeto JSON por repositorio selecionado,
    ja no formato da tabela `repositories` do schema do dataset).
    Rodar para linguagens/linters diferentes acumula no mesmo arquivo
    (modo append).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import requests

from linter_adapters import ADAPTERS, _request_with_retry

GITHUB_API = "https://api.github.com"

# ---------------------------------------------------------------------------
# Criterios de selecao (ver plano de mineracao do dataset "Babel")
# ---------------------------------------------------------------------------

MIN_STARS = 100
MIN_CONTRIBUTORS = 3
PUSHED_WITHIN_DAYS = 730  # ~2 anos: garante manutencao ativa

# Licencas permissivas aceitas (campo license.spdx_id retornado pela API)
PERMISSIVE_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause"}

# Padroes de nome que indicam repositorio "de brinquedo" / didatico,
# nao software real em producao
EXCLUDE_NAME_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [r"awesome-", r"tutorial", r"example", r"learning-", r"-course", r"cookbook"]
]

RESULTS_PER_PAGE = 100
MAX_PAGES = 5  # limite de seguranca (busca do GitHub so cobre ate 1000 resultados)


@dataclass
class CandidateRepo:
    name: str
    owner: str
    full_name: str
    url: str
    language_primary: str
    default_branch: str
    stars: int
    subscribers_count: int
    size_kb: int
    license_spdx: str
    pushed_at: str
    linter: str
    contributors_count: Optional[int] = None


def build_session(token: Optional[str]) -> requests.Session:
    session = requests.Session()
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    session.headers.update(headers)
    return session


def search_candidates(session: requests.Session, language: str) -> list[dict]:
    """Busca repositorios candidatos via GitHub Search API."""
    pushed_after = (datetime.now(timezone.utc) - timedelta(days=PUSHED_WITHIN_DAYS)).strftime("%Y-%m-%d")
    query = f"language:{language} stars:>={MIN_STARS} fork:false pushed:>{pushed_after}"

    items: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        response = _request_with_retry(
            session,
            f"{GITHUB_API}/search/repositories",
            params={
                "q": query,
                "sort": "stars",
                "order": "desc",
                "per_page": RESULTS_PER_PAGE,
                "page": page,
            },
        )
        payload = response.json()
        batch = payload.get("items", [])
        items.extend(batch)
        logging.info("language=%s page=%d -> %d repositorios", language, page, len(batch))
        if len(batch) < RESULTS_PER_PAGE:
            break
    return items


def passes_name_filter(name: str) -> bool:
    return not any(pattern.search(name) for pattern in EXCLUDE_NAME_PATTERNS)


def passes_license_filter(repo_item: dict) -> bool:
    license_info = repo_item.get("license") or {}
    return license_info.get("spdx_id") in PERMISSIVE_LICENSES


def get_contributors_count(session: requests.Session, full_name: str) -> int:
    """
    Estima o numero de contribuidores usando o header `Link` da paginacao
    (per_page=1 + numero da ultima pagina == contagem total), evitando
    baixar a lista inteira de contribuidores.
    """
    response = _request_with_retry(
        session,
        f"{GITHUB_API}/repos/{full_name}/contributors",
        params={"per_page": 1, "anon": "false"},
    )
    link_header = response.headers.get("Link", "")
    match = re.search(r'page=(\d+)>; rel="last"', link_header)
    if match:
        return int(match.group(1))
    # Sem header Link: so existe 1 pagina (0 ou 1 contribuidor)
    return len(response.json())


def get_subscribers_count(session: requests.Session, full_name: str) -> int:
    """
    subscribers_count = quem clicou "Watch" de verdade (recebe notificacao).
    NAO vem na resposta da API de busca (so em list/search payloads mais
    enxutos) -- precisa dessa chamada extra ao endpoint completo do repo,
    o mesmo padrao de custo que get_contributors_count ja tem.
    """
    response = _request_with_retry(session, f"{GITHUB_API}/repos/{full_name}")
    return response.json().get("subscribers_count", 0)


def build_candidate(
    session: requests.Session, repo_item: dict, language: str, linter_name: str, fetch_contributors: bool
) -> CandidateRepo:
    contributors_count = None
    subscribers_count = 0
    if fetch_contributors:
        contributors_count = get_contributors_count(session, repo_item["full_name"])
        subscribers_count = get_subscribers_count(session, repo_item["full_name"])

    return CandidateRepo(
        name=repo_item["name"],
        owner=repo_item["owner"]["login"],
        full_name=repo_item["full_name"],
        url=repo_item["html_url"],
        language_primary=language,
        default_branch=repo_item["default_branch"],
        stars=repo_item["stargazers_count"],
        subscribers_count=subscribers_count,
        size_kb=repo_item["size"],
        license_spdx=(repo_item.get("license") or {}).get("spdx_id", ""),
        pushed_at=repo_item["pushed_at"],
        linter=linter_name,
        contributors_count=contributors_count,
    )


def filter_and_enrich(
    session: requests.Session, raw_items: Iterable[dict], language: str, linter_name: str, check_contributors: bool
) -> list[CandidateRepo]:
    adapter_cls = ADAPTERS[linter_name]
    filtered: list[CandidateRepo] = []
    stats = {"nome": 0, "licenca": 0, "nao_adota_linter": 0, "build_nao_suportado": 0, "contribuidores": 0}
    # Gancho opcional do adapter (ex: checkstyle exige Maven -- pom.xml na
    # raiz): se existir, o repo fora do criterio aqui nem chega a ser
    # clonado. Obs: "config com ${var}" NAO e mais criterio de projeto --
    # virou exclusao por commit no run_linters (a config muda ao longo da
    # historia, e o mesmo repo pode ter commits validos).
    build_supported = getattr(adapter_cls, "is_build_supported_remote", None)

    for item in raw_items:
        if not passes_name_filter(item["name"]):
            stats["nome"] += 1
            continue
        if not passes_license_filter(item):
            stats["licenca"] += 1
            continue

        # Filtro de adocao: so aceita repos que de fato tem config propria
        # do linter no HEAD atual -- checado via API, sem clonar. E o que
        # garante que o dataset mede "devs que usam o linter", nao
        # "qualquer repo dessa linguagem".
        if not adapter_cls.is_adopted_remote(session, item["owner"]["login"], item["name"], item["default_branch"]):
            stats["nao_adota_linter"] += 1
            continue

        if build_supported is not None and not build_supported(
            session, item["owner"]["login"], item["name"], item["default_branch"]
        ):
            stats["build_nao_suportado"] += 1
            continue

        candidate = build_candidate(session, item, language, linter_name, fetch_contributors=check_contributors)

        if check_contributors and (candidate.contributors_count or 0) < MIN_CONTRIBUTORS:
            stats["contribuidores"] += 1
            continue

        filtered.append(candidate)

    logging.info(
        "Descartados -- nome: %d | licenca: %d | nao adota %s: %d | build nao suportado (nao-Maven): %d | contribuidores: %d",
        stats["nome"], stats["licenca"], linter_name, stats["nao_adota_linter"],
        stats["build_nao_suportado"], stats["contribuidores"],
    )
    return filtered


def random_sample(candidates: list[CandidateRepo], sample_size: int, seed: int = 42) -> list[CandidateRepo]:
    """Sorteio aleatorio simples do total de candidatos, sem estratificar
    por faixa de tamanho. size_kb continua gravado (numero real, direto
    da API) pra quem quiser descrever a amostra depois -- mas sem
    categoria pre-definida em cima, ja que os limiares antigos de
    small/medium/large eram arbitrarios, sem base metodologica nenhuma."""
    rng = random.Random(seed)
    shuffled = candidates.copy()
    rng.shuffle(shuffled)
    selected = shuffled[:sample_size]
    if len(selected) < sample_size:
        logging.warning(
            "So %d candidato(s) disponivel(is) de %d pedido(s)",
            len(selected), sample_size,
        )
    return selected


def already_selected_full_names(output_path: Path, language: str, linter_name: str) -> set[str]:
    """Le o arquivo de saida (se ja existir) e devolve os full_name ja
    selecionados PRA ESSA combinacao especifica de linguagem+linter.
    Usado pra tornar --sample-size incremental e seguro: aumentar o
    numero depois nunca troca os que ja foram minerados, so completa
    com repositorios novos, mesmo que o pool de candidatos do GitHub
    tenha mudado entre uma execucao e outra (e ele muda)."""
    if not output_path.exists():
        return set()
    existing = set()
    with output_path.open(encoding="utf-8") as f:
        for line in f:
            try:
                repo = json.loads(line)
            except json.JSONDecodeError:
                continue
            if repo.get("language_primary") == language and repo.get("linter") == linter_name:
                existing.add(repo["full_name"])
    return existing


def main() -> None:
    parser = argparse.ArgumentParser(description="Seleciona repositorios candidatos para o dataset Babel")
    parser.add_argument("--language", required=True, help="Linguagem do GitHub (ex: Python, Java)")
    parser.add_argument(
        "--linter", required=True, choices=sorted(ADAPTERS.keys()),
        help="So aceita repos que ja adotam este linter (checado via API antes de clonar)",
    )
    parser.add_argument(
        "--sample-size", type=int, default=3,
        help="Quantos repos no TOTAL essa combinacao linguagem+linter deve ter ao final "
             "(nao 'quantos sortear agora' -- se voce ja tem N e pede o mesmo N, nada de novo e adicionado)",
    )
    parser.add_argument(
        "--skip-contributors-check",
        action="store_true",
        help="Pula a checagem de contribuidores minimos E a busca de subscribers_count "
             "(duas chamadas extras por candidato, uma pra cada -- ambas controladas por esta flag)",
    )
    parser.add_argument("--output", default="selected_repos.jsonl", help="Arquivo de saida (JSON Lines, modo append)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    output_path = Path(args.output)
    already_have = already_selected_full_names(output_path, args.language, args.linter)
    if already_have:
        logging.info(
            "Ja existem %d repositorio(s) selecionado(s) pra %s+%s -- preservados, nao serao re-sorteados",
            len(already_have), args.language, args.linter,
        )

    needed = args.sample_size - len(already_have)
    if needed <= 0:
        logging.info("Ja tem %d/%d pedido -- nada a fazer.", len(already_have), args.sample_size)
        return

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        logging.warning("GITHUB_TOKEN nao definido: limite de requisicoes muito mais baixo (60/h).")

    session = build_session(token)

    raw_items = search_candidates(session, args.language)
    logging.info("Total de candidatos brutos: %d", len(raw_items))

    candidates = filter_and_enrich(
        session, raw_items, args.language, args.linter, check_contributors=not args.skip_contributors_check
    )
    # Exclui quem ja foi selecionado antes -- e o que garante que crescer
    # o sample_size so ADICIONA, nunca troca o que ja existe.
    candidates = [c for c in candidates if c.full_name not in already_have]
    logging.info("Candidatos novos apos todos os filtros (excluindo ja selecionados): %d", len(candidates))

    selected = random_sample(candidates, needed)
    logging.info("Repositorios novos selecionados: %d (total apos esta rodada: %d)", len(selected), len(already_have) + len(selected))

    with output_path.open("a", encoding="utf-8") as f:
        for repo in selected:
            f.write(json.dumps(asdict(repo), ensure_ascii=False) + "\n")

    logging.info("Gravado em %s", output_path.resolve())
    for repo in selected:
        print(f"  {repo.full_name}  (stars={repo.stars}, size_kb={repo.size_kb}, license={repo.license_spdx})")


if __name__ == "__main__":
    main()
