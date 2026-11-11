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

# Rodada "v3": regras novas (so Maven, veredito por commit, pom S+).
# Arquivos de saida NOVOS -- os da rodada antiga ficam intactos como backup.
cd "$HOME/babel-1.0" || exit 1
source "$HOME/venv/bin/activate"
export JAVA_HOME="$HOME/dev/java/jdk-21.0.12.1+1"
export PATH="$JAVA_HOME/bin:$PATH"

JARS=()
for j in "$HOME"/dev/babel/checkstyle-*-all.jar; do JARS+=(--checkstyle-jar "$j"); done

python3 run_linters.py checkstyle \
    --commits commits_sem_nifi.jsonl \
    --repos-dir "$HOME/babel-1.0/repos_cache" \
    --dataset-dir "$HOME/babel-1.0/Babel" \
    --output violations_checkstyle_v3.jsonl \
    --progress-log processed_checkstyle_v3.jsonl \
    --status-log commit_status_checkstyle_v3.jsonl \
    "${JARS[@]}" > checkstyle_v3_run.log 2>&1
