#!/bin/bash -l
#SBATCH --job-name=babel-status
#SBATCH --partition=long-simple
#SBATCH --qos=simple
#SBATCH -c 1
#SBATCH --mem=8G
#SBATCH --time=04:00:00
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=eifernandasales@gmail.com
#SBATCH --output=%x_%j.out
#SBATCH --error=%x_%j.err

cd "$HOME/babel-1.0" || exit 1
python3 analisar_status.py \
    --status-log commit_status_checkstyle_v3.jsonl \
    --violations violations_checkstyle_v3.jsonl \
    --out-dir analise_status_v3 > analise_status_v3.log 2>&1
