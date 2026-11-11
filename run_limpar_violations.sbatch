#!/bin/bash -l
#SBATCH --job-name=babel-limpa
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
python3 limpar_violations_pylint.py --violations violations.jsonl --backup-dir backup_20261102 \
  > limpar_violations.log 2>&1
