#!/usr/bin/env bash
set -euo pipefail

ROOT="${KZ_HOSTING_ROOT:-/srv/kaizora-hosting}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI is required. Install Docker Engine and the Docker Compose plugin first." >&2
  exit 1
fi

docker info >/dev/null
mkdir -p \
  "$ROOT/infrastructure/cloudflare" \
  "$ROOT/infrastructure/monitoring" \
  "$ROOT/infrastructure/scripts" \
  "$ROOT/projects" \
  "$ROOT/databases" \
  "$ROOT/backups/databases" \
  "$ROOT/backups/project-configs" \
  "$ROOT/backups/volumes" \
  "$ROOT/logs" \
  "$ROOT/configs"

if docker network inspect kaizora-network >/dev/null 2>&1; then
  echo "Docker network kaizora-network already exists."
else
  docker network create kaizora-network >/dev/null
  echo "Created Docker network kaizora-network."
fi

echo "Prepared hosting directories under $ROOT."
