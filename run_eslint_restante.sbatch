#!/bin/bash -l
#SBATCH --job-name=babel-cs-v3
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
export PATH="$HOME/dev/node-v22.22.3-linux-x64/bin:$HOME/dev/npm-global/bin:$PATH"

python3 run_linters.py eslint \
  --commits commits_sem_nifi.jsonl \
  --repos-dir $HOME/babel-1.0/repos_cache \
  --dataset-dir $HOME/babel-1.0/Babel \
  --output violations_eslint.jsonl \
  --progress-log processed_eslint.jsonl \
  --status-log commit_status_eslint.jsonl \
  --only-repo akiran/react-slick --only-repo trekhleb/javascript-algorithms --only-repo Netflix/falcor \
  --only-repo bower/bower --only-repo http-party/http-server --only-repo GoogleChromeLabs/quicklink \
  --only-repo graphql/dataloader --only-repo fantasyland/fantasy-land \
  >> eslint_run.log 2>&1
