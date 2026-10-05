#!/usr/bin/env bash
# Runs the AI SRE on the host network so it can reach the local Ollama
# (127.0.0.1:11434) and the kind NodePorts. The API binds to 127.0.0.1:8090 only.
set -euo pipefail
cd "$(dirname "$0")"
docker build -q -t battlefield/agent:dev . >/dev/null
docker rm -f bf-agent >/dev/null 2>&1 || true
exec docker run --name bf-agent --rm --network host \
  -e AGENT_MODEL="${AGENT_MODEL:-qwen2.5:3b}" -e AUTO_WATCH="${AUTO_WATCH:-1}" \
  -e VERIFY_WAIT_S="${VERIFY_WAIT_S:-45}" \
  -v /usr/local/bin/kubectl:/usr/local/bin/kubectl:ro \
  -v "$HOME/.kube/config:/root/.kube/config:ro" \
  battlefield/agent:dev "$@"
