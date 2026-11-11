#!/bin/bash -l
#SBATCH --job-name=babel-checkstyle
#SBATCH --partition=long-simple
#SBATCH --qos=simple
#SBATCH -c 2
#SBATCH --mem=8G
#SBATCH --time=7-00:00:00
#SBATCH --mail-type=END,FAIL,TIME_LIMIT_90
#SBATCH --mail-user=eifernandasales@gmail.com
#SBATCH --output=%x_%j.out
#SBATCH --error=%x_%j.err

cd "$HOME/babel-1.0" || exit 1
source "$HOME/venv/bin/activate"
export JAVA_HOME="$HOME/dev/java/jdk-21.0.12.1+1"
export PATH="$JAVA_HOME/bin:$PATH"
echo "[$(date)] inicio no $(hostname)" >> checkstyle_run.log
java -version >> checkstyle_run.log 2>&1

JARS=()
for v in 13.10.0 10.18.1 10.12.7 9.3 8.43 8.29 8.18 7.1; do
  JARS+=(--checkstyle-jar "$HOME/dev/babel/checkstyle-$v-all.jar")
done

python3 run_linters.py checkstyle \
    --commits commits_sem_nifi.jsonl \
    --repos-dir "$HOME/babel-1.0/repos_cache" \
    --dataset-dir ./Babel \
    "${JARS[@]}" \
    --output violations_checkstyle.jsonl \
    --progress-log processed_checkstyle.jsonl >> checkstyle_run.log 2>&1
