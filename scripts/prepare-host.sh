#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
echo "prepare-host.sh is retained as a compatibility entry point; running bootstrap-node.sh."
exec bash "$SCRIPT_DIR/bootstrap-node.sh" "$@"
