#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM="${OPENBOT_DIR:-$ROOT/openbot-upstream}"
TENANT="$ROOT/openbot/tenant"

if [[ ! -d "$TENANT" ]]; then
  echo "Missing tenant package at $TENANT" >&2
  exit 1
fi

if [[ ! -d "$UPSTREAM/.git" ]]; then
  echo "Cloning OpenBot into $UPSTREAM ..."
  git clone --depth 1 https://github.com/CopilotKit/OpenBot.git "$UPSTREAM"
else
  echo "OpenBot already cloned at $UPSTREAM"
fi

if [[ ! -f "$UPSTREAM/.env" ]]; then
  cp "$ROOT/openbot/.env.example" "$UPSTREAM/.env"
  echo "Created $UPSTREAM/.env — fill OPENAI_API_KEY and CopilotKit Intelligence keys."
fi

echo
echo "Next steps:"
echo "  1. Enable AG-UI on agent-webapp backend (.env next to docker-compose.yml):"
echo "       OPENBOT_AGUI_ENABLED=true"
echo "       OPENBOT_AGUI_TOKEN=change-me-openbot-token"
echo "     docker compose up -d --build backend"
echo
echo "  2. Install OpenBot deps and start infrastructure:"
echo "       cd $UPSTREAM"
echo "       cp ../agent-webapp-langgraph/openbot/.env.example .env   # if needed"
echo "       # set TENANT_PACKAGE_DIR=$TENANT"
echo "       ./scripts/start.sh"
echo
echo "  3. Open UI: http://localhost:3010"
echo "     Register/test coworker endpoint: http://localhost:8001/ag-ui"
echo "     Health: http://localhost:8001/health"
echo
echo "See docs/openbot-integration.md for what this POC does and does not cover."
