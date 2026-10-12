#!/usr/bin/env bash
# Runs every experiment arm ONE AFTER ANOTHER (heavy jobs never overlap). Each arm writes evals/results/*-<label>.json.
# Spend (Haiku): about $0.23 per full 22x3 arm; E1 (Sonnet for the agent step) is a single 22x1 arm, hard-stopped at $2.
# Usage: bash scripts/run_experiments.sh [arm ...]     (default: all arms)
set -u
cd "$(dirname "$0")/.."
arms=("$@")
[ ${#arms[@]} -eq 0 ] && arms=(baseline E3-lean E2-caching E5-supervisor E4-compaction-on E4-compaction-off E1-sonnet-agent)
run() {  # run <label> <env assignments...> -- <evals.run args...>
  label=$1; shift
  envs=(); while [ "$1" != "--" ]; do envs+=("$1"); shift; done; shift
  echo "=== $label  ($(date -u +%T) UTC) ==="; free -h | sed -n 2p
  env "${envs[@]}" uv run python -m evals.run --yes --label "$label" "$@" 2>&1 | grep -v '^{' | grep -v "output guard"
}
for a in "${arms[@]}"; do
  case $a in
    baseline)           run baseline-4        X=1 -- --runs 3 --limit 2.0 ;;
    E3-lean)            run E3-lean           AGENT_TOOL_MODE=lean -- --runs 3 --limit 2.0 ;;
    E2-caching)         run E2-caching        LLM_PROMPT_CACHING=true -- --runs 3 --limit 2.0 ;;
    E5-supervisor)      run E5-supervisor     AGENT_MODE=supervisor -- --runs 3 --limit 2.0 ;;
    E4-compaction-on)   run E4-compaction-on  X=1 -- --runs 3 --only long_thread_compaction --limit 1.0 ;;
    E4-compaction-off)  run E4-compaction-off AGENT_COMPACTION_ENABLED=false -- --runs 3 --only long_thread_compaction --limit 1.0 ;;
    E1-sonnet-agent)    run E1-sonnet-agent   LLM_AGENT_MODEL=claude-sonnet-5-5 -- --runs 1 --limit 2.0 ;;
    *) echo "unknown arm $a"; exit 2 ;;
  esac
done
