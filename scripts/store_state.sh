#!/usr/bin/env bash
# Store the state, snapshot and report of a sync run in the state repository.
# Usage: store_state.sh <checkout of the state repository> <run id>
# The files of this run replace the stored ones whole; other files stay.
set -u
dir=$1
run=$2
# The checkout is absent, or not a repository, when an earlier step failed.
if ! cd "$dir" 2>/dev/null || [ ! -d .git ]; then
  echo "no state checkout, nothing stored"
  exit 0
fi
keep=$(mktemp -d)
trap 'rm -rf "$keep"' EXIT
for f in state.json snapshot.json report.txt; do
  if [ -f "$f" ]; then cp "$f" "$keep/"; fi
done
for attempt in 1 2 3; do
  if git fetch -q origin main && git reset -q --hard FETCH_HEAD; then
    cp "$keep"/* . 2>/dev/null || true
    for f in state.json snapshot.json report.txt; do
      if [ -f "$f" ] && ! git add "$f"; then
        echo "state store failed: cannot stage $f"
        exit 1
      fi
    done
    if git diff --cached --quiet; then
      echo "state unchanged"
      exit 0
    fi
    if ! git commit -q -m "Sync run $run"; then
      echo "state store failed: cannot commit"
      exit 1
    fi
    if git push -q origin HEAD:main; then
      exit 0
    fi
  fi
  sleep "${STORE_RETRY_WAIT:-10}"
done
echo "state push failed after 3 attempts"
exit 1
