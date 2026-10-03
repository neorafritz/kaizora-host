#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
HOSTING_ROOT="${KZ_HOSTING_ROOT:-/srv/kaizora-hosting}"
BIN_DIR="${KZ_INSTALL_BIN:-/usr/local/bin}"
PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python 3 is required. Install Python 3.10 or newer and try again." >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer is required." >&2
  exit 1
fi
HOSTING_ROOT="$("$PYTHON" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$HOSTING_ROOT")"
BIN_DIR="$("$PYTHON" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$BIN_DIR")"
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI was not found. Install Docker Engine and the Compose plugin before installing the node CLI." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 plugin was not found. Install it before installing the node CLI." >&2
  exit 1
fi

if ! mkdir -p \
  "$HOSTING_ROOT/engine/src/kaizora_hosting" \
  "$HOSTING_ROOT/engine/bin" \
  "$HOSTING_ROOT/projects" \
  "$HOSTING_ROOT/configs" \
  "$HOSTING_ROOT/infrastructure" \
  "$HOSTING_ROOT/backups" \
  "$HOSTING_ROOT/databases" \
  "$HOSTING_ROOT/logs"; then
  echo "Could not create $HOSTING_ROOT. Re-run with appropriate directory permissions (for example, sudo)." >&2
  exit 1
fi

cp "$REPO_DIR"/src/kaizora_hosting/*.py "$HOSTING_ROOT/engine/src/kaizora_hosting/"
cp "$REPO_DIR/bin/kz" "$HOSTING_ROOT/engine/bin/kz"
chmod 0755 "$HOSTING_ROOT/engine/bin/kz"

if [[ "$REPO_DIR" != "$HOSTING_ROOT" ]]; then
  mkdir -p "$HOSTING_ROOT/infrastructure/cloudflare"
  for file in docker-compose.yml .env.example README.md; do
    if [[ -f "$REPO_DIR/infrastructure/cloudflare/$file" && ! -e "$HOSTING_ROOT/infrastructure/cloudflare/$file" ]]; then
      cp "$REPO_DIR/infrastructure/cloudflare/$file" "$HOSTING_ROOT/infrastructure/cloudflare/$file"
    fi
  done
fi

mkdir -p "$BIN_DIR" 2>/dev/null || {
  echo "Could not create $BIN_DIR. Run this installer with appropriate permissions (for example, sudo)." >&2
  exit 1
}
LAUNCHER="$HOSTING_ROOT/engine/bin/kz"
TARGET="$BIN_DIR/kz"
if [[ -L "$TARGET" ]]; then
  CURRENT="$(readlink -f -- "$TARGET" || true)"
  if [[ "$CURRENT" != "$LAUNCHER" ]]; then
    echo "$TARGET already points to another command; leaving it unchanged." >&2
    exit 1
  fi
elif [[ -e "$TARGET" ]]; then
  echo "$TARGET already exists and is not this installer's symlink; leaving it unchanged." >&2
  exit 1
else
  ln -s "$LAUNCHER" "$TARGET" 2>/dev/null || {
    echo "Could not install $TARGET. Run this installer with appropriate permissions (for example, sudo)." >&2
    exit 1
  }
fi

echo "Installed kz at $TARGET. Hosting data remains under $HOSTING_ROOT."
