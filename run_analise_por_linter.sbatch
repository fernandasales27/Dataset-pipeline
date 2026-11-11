#!/bin/bash -l
#SBATCH --job-name=babel-viol
#SBATCH --partition=long-simple
#SBATCH --qos=simple
#SBATCH -c 2
#SBATCH --mem=8G
#SBATCH --time=7-00:00:00
#SBATCH --mail-type=END,FAIL,TIME_LIMIT_90
#SBATCH --mail-user=eifernandasales@gmail.com
#SBATCH --output=%x_%j.out
#SBATCH --error=%x_%j.err

cd $HOME/babel-1.0
source $HOME/venv/bin/activate
python3 analisar_violacoes.py --violations violations.jsonl --linter pylint --extensoes .py \
  --label pylint --commits commits.jsonl --out-dir analise_viol_antigo_pylint > analise_viol_antigo_pylint.log 2>&1 &
python3 analisar_violacoes.py --violations violations.jsonl --linter checkstyle --extensoes .java \
  --label checkstyle_antigo --commits commits.jsonl --out-dir analise_viol_antigo_checkstyle > analise_viol_antigo_checkstyle.log 2>&1 &
python3 analisar_violacoes.py --violations violations.jsonl --linter eslint --extensoes .js,.jsx,.mjs,.cjs \
  --label eslint_antigo --commits commits.jsonl --out-dir analise_viol_antigo_eslint > analise_viol_antigo_eslint.log 2>&1 &
wait
