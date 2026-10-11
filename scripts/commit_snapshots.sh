#!/usr/bin/env bash
set -euo pipefail
git config user.name "alert-collector[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A snapshots
if git diff --cached --quiet; then echo "no snapshot changes"; exit 0; fi
git commit -m "snapshots $(date -u +%Y-%m-%dT%H%MZ) [skip ci]"
for i in 1 2 3 4 5; do
  if git push; then exit 0; fi
  git pull --rebase -X theirs --autostash || { git rebase --abort 2>/dev/null; git pull --rebase -X theirs || true; }
done
exit 1
