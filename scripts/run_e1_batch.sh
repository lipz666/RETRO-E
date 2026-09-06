#!/bin/zsh
# Sequential E1 batch with conservative concurrency.
#
# The first attempt hit a gateway-wide 429 model_cooldown on both Gemini models lasting 153
# minutes. That was a five-hour quota window being exhausted, NOT a concurrency problem:
# lowering workers does not avoid it, it only spends the same quota more slowly. Concurrency
# is therefore left high so the available window is used efficiently. What did need fixing
# was this script: it truncated logs (losing the diagnosis of a short screen tier) and kept
# going after a failure, burning the window on runs that could not succeed.
#
# Scope: seed 11 only. Three methods on one seed answers the direction question first;
# seeds 29 and 47 follow once the trend is known.
set -u
cd /Users/lpz/Desktop/AtomFlow/RETRO-E
set -a; source .env; set +a

HASH=c7a4001867a5a92b6385c3460de4e1fc3a0a3ea3ecac3fd71825eb24da3b8b7e
WORKERS=${WORKERS:-40}
GAP=${GAP:-30}
LOGDIR=results/v2/optimization/_logs
mkdir -p $LOGDIR

# Resume the partially complete run first, then the rest.
RUNS=(
  "instruction_opt 11"
  "reflective 11"
)

for spec in $RUNS; do
  m=${spec% *}; s=${spec#* }
  log=$LOGDIR/${m}_${s}.log
  echo "=== START $m seed $s $(date +%H:%M:%S) workers=$WORKERS ==="
  .venv/bin/python scripts/run_e1_matrix.py --method $m --seed $s \
      --protocol-hash $HASH --workers $WORKERS >$log 2>&1
  rc=$?
  grep -vE "INFO HTTP Request" $log | tail -6
  if [ $rc -ne 0 ]; then
    echo "=== ABORT: $m seed $s exited $rc (full log: $log) ==="
    grep -E "429|cooldown|Error|Traceback" $log | tail -5
    exit 1
  fi
  # A run that stops short of the full schedule must halt the batch, not be papered over.
  filled=$(python3 -c "import json;print(json.load(open('contexts/v2/$m/$s/promotion.json'))['screen_candidates_evaluated'])" 2>/dev/null || echo 0)
  if [ "$filled" != "16" ]; then
    echo "=== ABORT: $m seed $s filled $filled of 16 screen slots ==="
    exit 1
  fi
  echo "=== END $m seed $s $(date +%H:%M:%S) screen=$filled ==="
  sleep $GAP
done
echo "=== E1 BATCH COMPLETE ==="
