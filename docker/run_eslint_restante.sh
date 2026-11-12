#!/bin/bash
# Equivalente ao run_eslint_restante.sbatch, em container Docker.
# Uso: ./docker/run_eslint_restante.sh   (os dados ficam no volume mflos_babel_data)
set -euo pipefail

IMAGE="${BABEL_IMAGE:-mflos_babel:latest}"
NAME="mflos_babel_eslint_restante"

docker run -d --name "$NAME" \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  --cpus 2 --memory 8g \
  -v mflos_babel_data:/data \
  "$IMAGE" bash -c '
    python3 /app/run_linters.py eslint \
      --commits commits_sem_nifi.jsonl \
      --repos-dir /data/repos_cache \
      --dataset-dir /data/Babel \
      --output violations_eslint.jsonl \
      --progress-log processed_eslint.jsonl \
      --status-log commit_status_eslint.jsonl \
      --only-repo akiran/react-slick --only-repo trekhleb/javascript-algorithms --only-repo Netflix/falcor \
      --only-repo bower/bower --only-repo http-party/http-server --only-repo GoogleChromeLabs/quicklink \
      --only-repo graphql/dataloader --only-repo fantasyland/fantasy-land \
      >> eslint_run.log 2>&1
  '

echo "Container $NAME iniciado. Acompanhe com: docker exec $NAME tail -n 20 /data/eslint_run.log"
