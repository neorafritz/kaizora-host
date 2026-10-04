"""Node configuration loading and validation."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import KaizoraError


NODE_CONFIG_ENV = "KAIZORA_HOST_CONFIG"
DEFAULT_NODE_ID = "kz-home-01"
DEFAULT_NODE_NAME = "KZ-HOME-01"
NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CONFIG_FIELDS = {"node_id", "name", "type", "environment", "projects_path", "backups_path", "databases_path", "infrastructure_path"}


@dataclass(frozen=True)
class NodeConfig:
    node_id: str
    name: str
    type: str
    environment: str
    projects_path: Path
    backups_path: Path
    databases_path: Path
    infrastructure_path: Path
    config_path: Path


def default_node_config(root: Path) -> NodeConfig:
    root = root.expanduser().resolve()
    return NodeConfig(
        node_id=DEFAULT_NODE_ID,
        name=DEFAULT_NODE_NAME,
        type="home",
        environment="production",
        projects_path=(root / "projects").resolve(),
        backups_path=(root / "backups").resolve(),
        databases_path=(root / "databases").resolve(),
        infrastructure_path=(root / "infrastructure").resolve(),
        config_path=(root / "configs" / "node.json").resolve(),
    )


def _required_text(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip() or any(ord(ch) < 32 for ch in value):
        raise KaizoraError(f"Node config field {key!r} must be a non-empty string.")
    return value.strip()


def _configured_path(data: dict[str, Any], key: str, default: Path, config_dir: Path) -> Path:
    value = data.get(key, str(default))
    if not isinstance(value, str) or not value.strip() or any(ord(ch) < 32 for ch in value):
        raise KaizoraError(f"Node config field {key!r} must be a path string.")
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = config_dir / path
        return path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise KaizoraError(f"Node config field {key!r} is not a valid path.") from exc


def load_node_config(root: Path) -> NodeConfig:
    """Load the configured node or return defaults rooted at the development root."""
    root = root.expanduser().resolve()
    override = os.environ.get(NODE_CONFIG_ENV)
    config_path = Path(override).expanduser() if override else root / "configs" / "node.json"
    if not config_path.is_absolute():
        config_path = (root / config_path).resolve()
    else:
        config_path = config_path.resolve()

    if not config_path.exists():
        if override:
            raise KaizoraError(f"Node config was not found: {config_path}")
        defaults = default_node_config(root)
        return NodeConfig(**{**defaults.__dict__, "config_path": config_path})
    if not config_path.is_file():
        raise KaizoraError(f"Node config path is not a file: {config_path}")

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KaizoraError(f"Could not read node config {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise KaizoraError(f"Node config {config_path} must contain a JSON object.")
    unknown = sorted(set(data) - CONFIG_FIELDS)
    if unknown:
        raise KaizoraError(f"Node config has unsupported field(s): {', '.join(unknown)}.")

    defaults = default_node_config(root)
    node_id = _required_text(data, "node_id", defaults.node_id)
    if not NODE_ID_RE.fullmatch(node_id):
        raise KaizoraError("Node config field 'node_id' must use lowercase letters, digits, and hyphens.")
    return NodeConfig(
        node_id=node_id,
        name=_required_text(data, "name", defaults.name),
        type=_required_text(data, "type", defaults.type),
        environment=_required_text(data, "environment", defaults.environment),
        projects_path=_configured_path(data, "projects_path", defaults.projects_path, config_path.parent),
        backups_path=_configured_path(data, "backups_path", defaults.backups_path, config_path.parent),
        databases_path=_configured_path(data, "databases_path", defaults.databases_path, config_path.parent),
        infrastructure_path=_configured_path(data, "infrastructure_path", defaults.infrastructure_path, config_path.parent),
        config_path=config_path,
    )
