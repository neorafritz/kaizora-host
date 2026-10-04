#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
HOSTING_ROOT="${KZ_HOSTING_ROOT:-/srv/kaizora-hosting}"
NODE_CONFIG="${KAIZORA_HOST_CONFIG:-$HOSTING_ROOT/configs/node.json}"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This bootstrap script supports Ubuntu or Kali Linux on WSL2. It does not configure Windows." >&2
  exit 1
fi
if [[ -r /etc/os-release ]] && ! grep -Eq '^ID=(ubuntu|kali)$|^ID_LIKE=.*ubuntu' /etc/os-release; then
  echo "This bootstrap supports Ubuntu or Kali Linux. Review the script before using another distribution." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required; install it using your distribution's normal administration process." >&2
  exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer is required." >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker Engine and the Compose v2 plugin for this distribution, then rerun this script." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 plugin is missing. Install it for this distribution, then rerun this script." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running or this user cannot access it. Start/check Docker, then rerun; no sudo password was requested." >&2
  exit 1
fi

mkdir -p "$HOSTING_ROOT" || {
  echo "Could not create $HOSTING_ROOT. Re-run with appropriate permissions (for example, sudo bash scripts/bootstrap-node.sh)." >&2
  exit 1
}

CONFIG_CREATED=false
for directory in engine projects configs infrastructure backups databases logs; do
  path="$HOSTING_ROOT/$directory"
  if [[ ! -d "$path" ]]; then
    mkdir "$path" || {
      echo "Could not create $path. Re-run with appropriate permissions (for example, sudo bash scripts/bootstrap-node.sh)." >&2
      exit 1
    }
    if [[ "$EUID" -eq 0 && -n "${SUDO_USER:-}" && "$directory" != "engine" ]]; then
      chown "$SUDO_USER:" "$path"
    fi
  fi
done

if [[ ! -e "$NODE_CONFIG" ]]; then
  CONFIG_CREATED=true
  NODE_CONFIG="$NODE_CONFIG" HOSTING_ROOT="$HOSTING_ROOT" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["NODE_CONFIG"]).expanduser()
root = Path(os.environ["HOSTING_ROOT"]).expanduser().resolve()
path.parent.mkdir(parents=True, exist_ok=True)
value = {
    "node_id": "kz-home-01",
    "name": "KZ-HOME-01",
    "type": "home",
    "environment": "production",
    "projects_path": str(root / "projects"),
    "backups_path": str(root / "backups"),
    "infrastructure_path": str(root / "infrastructure"),
}
try:
    with path.open("x", encoding="utf-8") as config_file:
        json.dump(value, config_file, indent=2)
        config_file.write("\n")
except FileExistsError:
    pass
PY
  echo "Created node config: $NODE_CONFIG"
  if [[ "$EUID" -eq 0 && -n "${SUDO_USER:-}" && "$CONFIG_CREATED" == true ]]; then
    chown "$SUDO_USER:" "$NODE_CONFIG"
  fi
else
  echo "Keeping existing node config: $NODE_CONFIG"
fi

if docker network inspect kaizora-network >/dev/null 2>&1; then
  echo "Docker network kaizora-network already exists."
else
  docker network create kaizora-network >/dev/null || {
    if ! docker network inspect kaizora-network >/dev/null 2>&1; then
      echo "Could not create Docker network kaizora-network." >&2
      exit 1
    fi
  }
  echo "Created Docker network kaizora-network."
fi

bash "$REPO_DIR/scripts/install.sh"

if command -v systemctl >/dev/null 2>&1 && [[ "$(ps -p 1 -o comm= | tr -d '[:space:]')" == "systemd" ]]; then
  PANEL_UNIT="/etc/systemd/system/kaizora-panel.service"
  if [[ ! -e "$PANEL_UNIT" ]]; then
    install -m 0644 "$REPO_DIR/scripts/kaizora-panel.service" "$PANEL_UNIT"
    echo "Installed local Kaizora panel service."
  elif cmp -s "$REPO_DIR/scripts/kaizora-panel.service" "$PANEL_UNIT"; then
    echo "Keeping existing Kaizora panel service."
  else
    echo "Keeping custom $PANEL_UNIT unchanged; not enabling the panel service." >&2
    PANEL_UNIT=""
  fi
  if [[ -n "$PANEL_UNIT" ]]; then
    systemctl daemon-reload
    systemctl enable --now kaizora-panel.service
    echo "Kaizora panel is available at http://localhost:8787"
  fi
else
  echo "systemd is not active; start the browser panel manually with: kz panel"
fi

if grep -qi microsoft /proc/version 2>/dev/null; then
  echo "WSL detected. This script does not configure Windows startup, WSL systemd, firewall, or port forwarding."
else
  echo "Linux node prepared. Windows/WSL startup behavior has not been configured."
fi
echo "KZ-HOME-01 bootstrap completed. Run: kz node"
