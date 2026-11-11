#!/bin/bash -l
#SBATCH --job-name=babel-pylint
#SBATCH --partition=long-simple
#SBATCH --qos=simple
#SBATCH -c 2
#SBATCH --mem=8G
#SBATCH --time=7-00:00:00
#SBATCH --mail-type=END,FAIL,TIME_LIMIT_90
#SBATCH --mail-user=<seu-email>
#SBATCH --output=%x_%j.out
#SBATCH --error=%x_%j.err

cd "$HOME/babel-1.0" || exit 1
source "$HOME/venv/bin/activate"
echo "[$(date)] inicio no $(hostname), $(pylint --version | head -n 1)" >> pylint_run.log
python3 run_linters.py pylint \
    --commits commits.jsonl \
    --repos-dir "$HOME/babel-1.0/repos_cache" \
    --dataset-dir ./Babel \
    --output violations.jsonl \
    >> pylint_run.log 2>&1
