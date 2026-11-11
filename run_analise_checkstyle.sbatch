#!/bin/bash -l
#SBATCH --job-name=babel-analise
#SBATCH --partition=long-simple
#SBATCH --qos=simple
#SBATCH -c 1
#SBATCH --mem=16G
#SBATCH --time=04:00:00
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=eifernandasales@gmail.com
#SBATCH --output=%x_%j.out
#SBATCH --error=%x_%j.err

cd "$HOME/babel-1.0" || exit 1
python3 analisar_coleta.py --linter checkstyle \
    --commits commits_sem_nifi.jsonl \
    --progress-log processed_checkstyle.jsonl \
    --violations violations_checkstyle.jsonl --violations violations.jsonl \
    --log checkstyle_run.log --desde "2026-10-16 14:00" \
    --repos-dir "$HOME/babel-1.0/repos_cache" \
    --out-dir analise_checkstyle > analise_checkstyle.log 2>&1
