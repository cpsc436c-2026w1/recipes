#!/usr/bin/env bash
# The live red-team run of the rider-comment cases: redteam.py on the course LLM gateway, the shop cases once and the
# 16 comment cases 3 times (70 questions, 0.0098 CAD of your team's gateway pool, measured 2026-11-23; README, "Cost and clean-up").
# It creates nothing in AWS. Run it from the recipe folder (dashboard-chat/):
#   export COURSE_GATEWAY_URL=... KB_MCP_TOKEN=...
#   bash ext-guardrails-redteam/live_run.sh
set -uo pipefail
cd "$(dirname "$0")/.."
if [ -z "${COURSE_GATEWAY_URL:-${GATEWAY_URL:-}}" ] || [ -z "${KB_MCP_TOKEN:-${COURSE_MCP_TOKEN:-}}" ]; then
  echo "set COURSE_GATEWAY_URL and KB_MCP_TOKEN first (ext-guardrails-redteam/README.md, 'Run it live')" >&2
  exit 1
fi
out=${OUT:-.local/redteam-comments-gateway.txt}   # output/ holds the recorded run: a new run goes to .local/ unless OUT says so
mkdir -p "$(dirname "$out")"
log="$(dirname "$out")/redteam-log-comments-gateway.jsonl"
{
  echo "\$ uv run python ext-guardrails-redteam/redteam.py --model gateway --cases all --runs 3"
  echo "# run $(date -u +%Y-%m-%dT%H:%MZ), model ${CHAT_MODEL:-qwen3.6-35b-a3b}"
} > "$out"
uv run python ext-guardrails-redteam/redteam.py --model gateway --cases all --runs 3 2>&1 | tee -a "$out"
code=${PIPESTATUS[0]}
echo "[exit $code]" >> "$out"
cp output/redteam-log-all-gateway.jsonl "$log"
# The token never reaches the capture: redteam.py prints none (R44). Check before you commit:
if grep -q -- "${KB_MCP_TOKEN:-${COURSE_MCP_TOKEN:-}}" "$out" "$log"; then
  echo "the token is in the capture: do not commit it" >&2
  exit 1
fi
# The capture is for reading; no test reads it. The eval's scenario runs its own 48 questions: uv run behave --tags=live
echo "captured in $out (exit $code)"
exit "$code"
