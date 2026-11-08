#!/usr/bin/env bash
# Run the fixed trigger corpus against the currently installed skill.
#
#   bash run-trigger.sh --dry-run
#   STAMP=baseline-0.8.11 RUNS=2 bash run-trigger.sh
#
# One `opencode run --format json` per case and repetition. The transcripts are
# scored later by score-trigger.py; they are temporary artifacts and are
# gitignored.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$(cd "$ROOT/../.." && pwd)"
CORPUS="$ROOT/trigger.tsv"
MODEL="${MODEL:-opencode-go/deepseek-v4.1-flash}"
AGENT="${AGENT:-build}"
RUNS="${RUNS:-2}"
TIMEOUT="${TIMEOUT:-30}"
STAMP="${STAMP:-baseline-0.8.11}"
ONLY="${ONLY:-}"
OUT="$ROOT/runs/$STAMP"

if [ "${1:-}" = "--dry-run" ]; then
  python "$ROOT/score-trigger.py" --validate
  echo "dry-run: corpus validated; no model calls performed"
  exit 0
fi

command -v opencode >/dev/null 2>&1 || {
  echo "ERROR: opencode is not on PATH" >&2
  exit 1
}

mkdir -p "$OUT"
FB="$(mktemp -d)"
trap 'rm -rf "$FB"' EXIT
export SKILL_FEEDBACK_DIR="$FB"

VERSION="$(opencode --version 2>/dev/null | tail -1 || echo unknown)"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python - "$OUT/metadata.json" "$MODEL" "$AGENT" "$RUNS" "$STAMP" "$NOW" "$VERSION" <<'PY'
import json, sys, pathlib
path, model, agent, runs, stamp, now, version = sys.argv[1:]
pathlib.Path(path).write_text(json.dumps({
    "model": model, "agent": agent, "repetitions": int(runs),
    "stamp": stamp, "date": now, "opencode_version": version,
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

total=0
skipped=0
while IFS=$'\t' read -r id expect language category prompt; do
  [ "$id" = "id" ] && continue
  [ -z "${id:-}" ] && continue
  if [ -n "$ONLY" ]; then
    case ",$ONLY," in
      *",$id,"*) ;;
      *) skipped=$((skipped + 1)); continue ;;
    esac
  fi
  total=$((total + 1))
  prompt="$prompt If the opencode-export-reading skill is relevant, load it once and stop; do not run its scripts or inspect files. Otherwise answer in one short sentence."
  for rep in $(seq 1 "$RUNS"); do
    json="$OUT/${id}.r${rep}.json"
    err="$OUT/${id}.r${rep}.err"
    echo "  run $id rep $rep"
    set +e
    timeout "$TIMEOUT" opencode run \
      --pure \
      --model "$MODEL" \
      --agent "$AGENT" \
      --format json \
      --dir "$WORKSPACE" \
      "$prompt" >"$json" 2>"$err"
    rc=$?
    set -e
    printf '%s\n' "$rc" >"$OUT/${id}.r${rep}.exit"
  done
done < "$CORPUS"

echo "finished: $total cases, $RUNS reps, $skipped skipped; transcripts in $OUT"
