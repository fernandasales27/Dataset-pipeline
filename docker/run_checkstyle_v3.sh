#!/bin/bash
# Equivalente ao run_checkstyle_v3.sbatch, mas em container Docker.
# Uso (na raiz do repositorio):
#   BABEL_DATA=/caminho/dos/dados ./docker/run_checkstyle_v3.sh
set -euo pipefail

LOGIN=mflos
DATA="${BABEL_DATA:?defina BABEL_DATA com a pasta que contem commits_sem_nifi.jsonl, Babel/ e repos_cache/}"
TAG=$(git rev-parse --short HEAD 2>/dev/null || echo latest)
IMAGE="${LOGIN}_babel:${TAG}"
NAME="${LOGIN}_babel_cs_v3"

docker run -d --name "$NAME" \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  --cpus 2 --memory 8g \
  -v "$DATA":/data \
  "$IMAGE" bash -c '
    JARS=()
    for j in /opt/checkstyle/checkstyle-*-all.jar; do JARS+=(--checkstyle-jar "$j"); done
    python3 /app/run_linters.py checkstyle \
      --commits commits_sem_nifi.jsonl \
      --repos-dir /data/repos_cache \
      --dataset-dir /data/Babel \
      --output violations_checkstyle_v3.jsonl \
      --progress-log processed_checkstyle_v3.jsonl \
      --status-log commit_status_checkstyle_v3.jsonl \
      "${JARS[@]}" >> checkstyle_v3_run.log 2>&1
  '

echo "Container $NAME iniciado. Acompanhe com: docker logs -f $NAME  ou  tail -f \$BABEL_DATA/checkstyle_v3_run.log"
