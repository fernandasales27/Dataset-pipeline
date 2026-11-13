#!/bin/bash
# Roda as tres etapas do PMD em container Docker.
#
# Uso (na raiz do repositorio, no servidor):
#   ./docker/pmd.sh teste        # teste de fumaca do adaptador (rapido, nao usa os dados)
#   ./docker/pmd.sh selecionar   # etapa 1: escolhe repositorios Java que usam o PMD (precisa de GITHUB_TOKEN)
#   ./docker/pmd.sh extrair      # etapa 2: clona e lista os commits
#   ./docker/pmd.sh rodar        # etapa 3: roda o PMD commit a commit
#   ./docker/pmd.sh log <select|extract|run>   # ultimas linhas do log da etapa
#
# Aviso ao terminar (sucesso OU erro), opcional -- defina antes de iniciar o job:
#   export NOTIFY_URL=https://ntfy.sh/UM-NOME-DIFICIL-DE-ADIVINHAR
#   export NOTIFY_EMAIL=seu@email      # (opcional) o ntfy.sh tambem reenvia por e-mail
# Qualquer pessoa que souber o nome do topico le as mensagens; elas levam so o nome do
# job, o codigo de saida e as 3 ultimas linhas do log.
#
# Os dados ficam no volume mflos_babel_data (/data no container). Arquivos do PMD
# ficam separados dos outros linters: selected_repos_pmd.jsonl, commits_pmd.jsonl,
# violations_pmd.jsonl, processed_pmd.jsonl, commit_status_pmd.jsonl.
set -euo pipefail

LOGIN=mflos
IMAGE="${BABEL_IMAGE:-${LOGIN}_babel:latest}"
VOLUME="${BABEL_VOLUME:-${LOGIN}_babel_data}"
SAMPLE="${SAMPLE:-1000}"   # total de repositorios pedidos na etapa 1 (todos os que passarem nos filtros, ate este numero)

COMMON=(--user "$(id -u):$(id -g)" -e HOME=/tmp -v "$VOLUME":/data)
PMD_ARGS='args=(); for h in /opt/pmd/pmd-bin-*; do args+=(--pmd-home "$h"); done'

# Codigo Python (uma linha) que o container roda ao fim do job. Sem aspas simples dentro.
NOTIFY_PY='import os,urllib.request as u; h={"Title": os.environ["TITLE"]}; e=os.environ.get("NOTIFY_EMAIL"); h.update({"Email": e} if e else {}); u.urlopen(u.Request(os.environ["NOTIFY_URL"], data=os.environ["MSG"].encode("utf-8","replace"), headers=h), timeout=20)'

wrap_notify() {   # wrap_notify <nome> <arquivo-de-log> <comando>  -> comando + aviso no fim
  local name="$1" logf="$2" cmd="$3"
  printf '%s\n' "$cmd" 'code=$?' \
    'if [ -n "${NOTIFY_URL:-}" ]; then' \
    '  if [ "$code" -eq 0 ]; then T="terminou"; else T="FALHOU (codigo $code)"; fi' \
    "  export TITLE=\"babel: $name -- \$T\"" \
    "  export MSG=\"\$(tail -n 3 /data/$logf 2>/dev/null | cut -c1-300)\"" \
    "  python3 -c '$NOTIFY_PY' || true" \
    'fi' \
    'exit $code'
}

start_job() {   # start_job <nome> <cpus> <mem> <arquivo-de-log> <comando bash> [opcoes extras do docker run...]
  local name="$1" cpus="$2" mem="$3" logf="$4" cmd="$5"; shift 5
  local notify_env=()
  [ -n "${NOTIFY_URL:-}" ] && notify_env+=(-e NOTIFY_URL)
  [ -n "${NOTIFY_EMAIL:-}" ] && notify_env+=(-e NOTIFY_EMAIL)
  if [ -n "$(docker ps -q -f "name=^${name}$")" ]; then
    echo "O container $name ja esta rodando. Acompanhe com: $0 log ..."; exit 1
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" --cpus "$cpus" --memory "$mem" "${COMMON[@]}" "${notify_env[@]}" "$@" "$IMAGE" bash -c "$(wrap_notify "$name" "$logf" "$cmd")"
  echo "Container $name iniciado."
}

case "${1:-}" in
  teste)
    docker run --rm "${COMMON[@]}" -e PYTHONPATH=/app "$IMAGE" bash -c \
      "$PMD_ARGS"'; python3 /app/testar_pmd.py "${args[@]}"'
    ;;
  selecionar)
    : "${GITHUB_TOKEN:?Defina antes: export GITHUB_TOKEN=... (token do GitHub, so leitura de repositorios publicos)}"
    start_job "${LOGIN}_babel_pmd_select" 1 2g select_pmd.log \
      "python3 /app/select_repositories.py --language Java --linter pmd --sample-size $SAMPLE --output /data/selected_repos_pmd.jsonl >> /data/select_pmd.log 2>&1" \
      -e GITHUB_TOKEN
    echo "Acompanhe com: $0 log select"
    ;;
  extrair)
    start_job "${LOGIN}_babel_pmd_extract" 2 4g extract_pmd.log \
      "python3 /app/extract_commit_history.py --input /data/selected_repos_pmd.jsonl --repos-dir /data/repos_cache --output /data/commits_pmd.jsonl >> /data/extract_pmd.log 2>&1"
    echo "Acompanhe com: $0 log extract"
    ;;
  rodar)
    start_job "${LOGIN}_babel_pmd" 2 8g pmd_run.log \
      "$PMD_ARGS"'; python3 /app/run_linters.py pmd --commits commits_pmd.jsonl --selected-repos selected_repos_pmd.jsonl --repos-dir /data/repos_cache --dataset-dir /data/Babel --output violations_pmd.jsonl --progress-log processed_pmd.jsonl --status-log commit_status_pmd.jsonl "${args[@]}" >> pmd_run.log 2>&1'
    echo "Acompanhe com: $0 log run"
    ;;
  log)
    case "${2:-}" in select) f=select_pmd.log;; extract) f=extract_pmd.log;; run) f=pmd_run.log;;
      *) echo "Uso: $0 log <select|extract|run>"; exit 1;; esac
    docker run --rm -v "$VOLUME":/data alpine tail -n "${LINES_TO_SHOW:-25}" "/data/$f"
    ;;
  *)
    sed -n '2,20p' "$0"; exit 1
    ;;
esac
