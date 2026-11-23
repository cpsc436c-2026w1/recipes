#!/usr/bin/env bash
# The rides matrix through the course LLM gateway, then the run's files copied to .local/ (or $DEST; output/ holds
# the recorded run). Run from
# prompt-optimization/ on your laptop, with COURSE_GATEWAY_URL and KB_MCP_TOKEN set (README, Run it live).
# Costs about 0.014 CAD with the defaults (measured 2026-11-23; README, Cost and clean-up); --max-cad stops it at
# 0.03 CAD whatever happens. A key under 60 requests a minute needs MATRIX_PACE_S=2.1 (README, Run it live).
#   bash ext-eval-matrix/run_live.sh [PROMPT_FILE]   # a prompt for the rides SQL step; default data/after-example.txt
set -euo pipefail
: "${COURSE_GATEWAY_URL:?set COURSE_GATEWAY_URL (the course gateway base URL, ending in /v1)}"
: "${KB_MCP_TOKEN:?set KB_MCP_TOKEN (your course token): read -s KB_MCP_TOKEN && export KB_MCP_TOKEN}"
after="${1:-ext-eval-matrix/data/after-example.txt}"
out="runs/matrix-rides-gateway-$(date +%Y%m%d-%H%M%S)"
trips=()
[ -f ext-eval-matrix/data/trips.parquet ] && trips=(--trips ext-eval-matrix/data/trips.parquet)
status=0
uv run python ext-eval-matrix/matrix.py --case rides --target gateway --max-cad 0.03 ${trips[@]+"${trips[@]}"} \
  --prompt "after=$after" \
  --compare qwen3.6-35b-a3b/seed,qwen3.6-35b-a3b/after \
  --compare qwen3.6-35b-a3b/after,qwen3.8-27b/after \
  --out "$out" || status=$?
dest=${DEST:-.local/ext-eval-matrix-rides-gateway}
mkdir -p "$dest"
cp "$out"/report.md "$out"/cases.jsonl "$out"/spend.json "$dest"/
echo "copied to $dest (exit $status); then: uv run python clean_up.py --yes"
exit "$status"
