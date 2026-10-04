"""Project manifest parsing with compatibility for older Compose-only projects."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import KaizoraError


MANIFEST_NAME = "kaizora.json"
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MEMORY_RE = re.compile(r"^[1-9][0-9]*(?:b|k|kb|kib|m|mb|mib|g|gb|gib|t|tb|tib)?$", re.I)
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\.?$")
SUPPORTED_FIELDS = {
    "name", "display_name", "runtime", "branch", "domain", "resources", "health", "backup",
    "repository", "compose_file", "slug",
}


@dataclass(frozen=True)
class Project:
    path: Path
    manifest: dict[str, Any]
    compose_file: Path

    @property
    def slug(self) -> str:
        return str(self.manifest["slug"])

    @property
    def title(self) -> str:
        return str(self.manifest["display_name"])

    @property
    def domain(self) -> str:
        return str(self.manifest.get("domain") or "-")

    @property
    def compose_name(self) -> str:
        return f"kz-{self.slug}"


def ensure_within(path: Path, parent: Path, description: str) -> Path:
    try:
        resolved = path.resolve()
        resolved.relative_to(parent.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise KaizoraError(f"{description} must stay inside {parent}.") from exc
    return resolved


def _positive_number(value: Any, field: str, *, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KaizoraError(f"Manifest field {field!r} must be a number.")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise KaizoraError(f"Manifest field {field!r} must be a finite number.") from exc
    if not math.isfinite(number) or number <= 0 or number > maximum:
        raise KaizoraError(f"Manifest field {field!r} must be greater than 0 and at most {maximum:g}.")
    return number


def _validate_resources(value: Any, manifest_path: Path) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise KaizoraError(f"{manifest_path} field 'resources' must be an object.")
    unknown = sorted(set(value) - {"cpu", "memory", "memory_mb"})
    if unknown:
        raise KaizoraError(f"{manifest_path} has unsupported resource field(s): {', '.join(unknown)}.")
    if "memory" in value and "memory_mb" in value:
        raise KaizoraError(f"{manifest_path} must set only one of 'resources.memory' and 'resources.memory_mb'.")
    result: dict[str, Any] = {}
    if "cpu" in value:
        result["cpu"] = _positive_number(value["cpu"], "resources.cpu", maximum=128)
    if "memory" in value:
        memory = value["memory"]
        if not isinstance(memory, str) or not MEMORY_RE.fullmatch(memory.strip()):
            raise KaizoraError(f"{manifest_path} field 'resources.memory' must be a size such as '512m'.")
        result["memory"] = memory.strip().lower()
    elif "memory_mb" in value:
        memory_mb = _positive_number(value["memory_mb"], "resources.memory_mb", maximum=1_048_576)
        if not memory_mb.is_integer():
            raise KaizoraError(f"{manifest_path} field 'resources.memory_mb' must be a whole number.")
        result["memory"] = f"{int(memory_mb)}m"
    return result


def _validate_health(value: Any, manifest_path: Path) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise KaizoraError(f"{manifest_path} field 'health' must be an object.")
    unknown = sorted(set(value) - {"path", "timeout", "port"})
    if unknown:
        raise KaizoraError(f"{manifest_path} has unsupported health field(s): {', '.join(unknown)}.")
    path = value.get("path", "/")
    if not isinstance(path, str) or not path.startswith("/") or any(ord(ch) < 32 for ch in path):
        raise KaizoraError(f"{manifest_path} field 'health.path' must start with '/'.")
    timeout = value.get("timeout", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 60:
        raise KaizoraError(f"{manifest_path} field 'health.timeout' must be an integer from 1 to 60.")
    port = value.get("port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
        raise KaizoraError(f"{manifest_path} field 'health.port' must be an integer from 1 to 65535.")
    return {"path": path, "timeout": timeout, "port": port}


def _validate_backup(value: Any, manifest_path: Path) -> dict[str, bool]:
    defaults = {"project": True, "database": False, "volumes": False}
    if value is None:
        return defaults
    if not isinstance(value, dict):
        raise KaizoraError(f"{manifest_path} field 'backup' must be an object.")
    unknown = sorted(set(value) - set(defaults))
    if unknown:
        raise KaizoraError(f"{manifest_path} has unsupported backup field(s): {', '.join(unknown)}.")
    result: dict[str, bool] = {}
    for key, default in defaults.items():
        item = value.get(key, default)
        if not isinstance(item, bool):
            raise KaizoraError(f"{manifest_path} field 'backup.{key}' must be true or false.")
        result[key] = item
    return result


def _compose_path(project_path: Path, value: Any, manifest_path: Path | None) -> Path:
    if value is None:
        candidates = ("docker-compose.yml", "compose.yaml", "compose.yml")
        for candidate in candidates:
            candidate_path = project_path / candidate
            if candidate_path.is_file():
                return ensure_within(candidate_path, project_path, "Compose file")
        if manifest_path:
            raise KaizoraError(f"{manifest_path} has no Compose file; expected docker-compose.yml or compose.yaml.")
        raise KaizoraError(f"Project {project_path.name} has no kaizora.json or supported Compose file.")
    if not isinstance(value, str) or not value.strip() or any(ord(ch) < 32 for ch in value):
        raise KaizoraError("Manifest field 'compose_file' must be a relative file path.")
    relative = Path(value)
    if relative.is_absolute():
        raise KaizoraError("Manifest field 'compose_file' must be relative to the project directory.")
    resolved = ensure_within(project_path / relative, project_path, "Compose file")
    if not resolved.is_file():
        raise KaizoraError(f"Compose file is missing: {resolved}")
    return resolved


def load_project(projects_path: Path, slug: str) -> Project:
    if not SLUG_RE.fullmatch(slug):
        raise KaizoraError(f"Invalid project slug: {slug!r}.")
    projects_path = projects_path.expanduser().resolve()
    if not projects_path.is_dir():
        raise KaizoraError(f"Projects directory is missing: {projects_path}")
    project_path = ensure_within(projects_path / slug, projects_path, "Project path")
    if not project_path.is_dir():
        raise KaizoraError(f"Project does not exist: {slug}")

    manifest_path = project_path / MANIFEST_NAME
    local_manifest_path = project_path / ".kaizora.local.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        manifest_path = ensure_within(manifest_path, project_path, "Manifest path")
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KaizoraError(f"Could not read {manifest_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise KaizoraError(f"{manifest_path} must contain a JSON object.")
        unknown = sorted(set(raw) - SUPPORTED_FIELDS)
        if unknown:
            raise KaizoraError(f"{manifest_path} has unsupported field(s): {', '.join(unknown)}.")
        manifest_file: Path | None = manifest_path
    else:
        raw = {}
        manifest_file = None

    if local_manifest_path.exists() or local_manifest_path.is_symlink():
        local_manifest_path = ensure_within(local_manifest_path, project_path, "Local manifest path")
        try:
            local_raw = json.loads(local_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KaizoraError(f"Could not read {local_manifest_path}: {exc}") from exc
        if not isinstance(local_raw, dict):
            raise KaizoraError(f"{local_manifest_path} must contain a JSON object.")
        unknown = sorted(set(local_raw) - SUPPORTED_FIELDS)
        if unknown:
            raise KaizoraError(f"{local_manifest_path} has unsupported field(s): {', '.join(unknown)}.")
        raw.update(local_raw)
        manifest_file = manifest_file or local_manifest_path

    compose_file = _compose_path(project_path, raw.get("compose_file"), manifest_file)
    if manifest_file is None:
        normalized = {
            "name": slug,
            "display_name": slug.replace("-", " ").title(),
            "slug": slug,
            "runtime": "docker",
            "domain": raw.get("domain"),
            "compose_file": compose_file.name,
            "repository": None,
            "branch": None,
            "resources": {},
            "health": {"path": "/", "timeout": 10, "port": None},
            "backup": {"project": True, "database": False, "volumes": False},
        }
        domain = normalized["domain"]
        if domain is not None and (
            not isinstance(domain, str) or not domain.strip() or not DOMAIN_RE.fullmatch(domain.strip())
        ):
            raise KaizoraError(f"{local_manifest_path} field 'domain' must be a hostname without a URL scheme or path.")
        normalized["domain"] = domain.strip().lower() if domain else None
        normalized["repository"] = raw.get("repository")
        normalized["branch"] = raw.get("branch")
        if normalized["repository"]:
            if not isinstance(normalized["repository"], str) or not isinstance(normalized["branch"], str) or not normalized["branch"].strip():
                raise KaizoraError(f"{manifest_file} needs a valid 'repository' and 'branch'.")
        return Project(project_path, normalized, compose_file)

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or any(ord(ch) < 32 for ch in name):
        raise KaizoraError(f"{manifest_file} needs a non-empty 'name'.")
    declared_slug = raw.get("slug", slug)
    if declared_slug != slug:
        raise KaizoraError(f"{manifest_file} field 'slug' must match its directory name ({slug}).")
    display_name = raw.get("display_name", name)
    if not isinstance(display_name, str) or not display_name.strip() or any(ord(ch) < 32 for ch in display_name):
        raise KaizoraError(f"{manifest_file} field 'display_name' must be a non-empty string.")
    runtime = raw.get("runtime", "docker")
    if not isinstance(runtime, str) or not runtime.strip():
        raise KaizoraError(f"{manifest_file} field 'runtime' must be a non-empty string.")
    if runtime.strip().lower() not in {"docker", "static"}:
        raise KaizoraError(f"{manifest_file} field 'runtime' must be 'docker' or 'static'.")
    domain = raw.get("domain")
    if domain is not None and (
        not isinstance(domain, str)
        or not domain.strip()
        or not DOMAIN_RE.fullmatch(domain.strip())
    ):
        raise KaizoraError(f"{manifest_file} field 'domain' must be a hostname without a URL scheme or path.")
    repository = raw.get("repository")
    branch = raw.get("branch")
    if repository is not None and (not isinstance(repository, str) or not repository.strip()):
        raise KaizoraError(f"{manifest_file} field 'repository' must be a non-empty URL when set.")
    if repository is not None:
        if any(ord(ch) < 32 for ch in repository) or any(ch.isspace() for ch in repository) or "?" in repository or "#" in repository:
            raise KaizoraError(f"{manifest_file} field 'repository' must not contain credentials or control characters.")
        if "://" in repository:
            try:
                parsed_repository = urlsplit(repository)
                host = parsed_repository.hostname
                _ = parsed_repository.port
            except ValueError:
                raise KaizoraError(f"{manifest_file} field 'repository' is not a valid Git URL.") from None
            userinfo_allowed = parsed_repository.scheme == "ssh" and parsed_repository.username == "git" and parsed_repository.password is None
            if (
                parsed_repository.scheme not in {"https", "http", "ssh"}
                or not host
                or not parsed_repository.path.strip("/")
                or parsed_repository.query
                or parsed_repository.fragment
                or ((parsed_repository.username or parsed_repository.password) and not userinfo_allowed)
            ):
                raise KaizoraError(f"{manifest_file} field 'repository' must be a valid HTTPS, HTTP, or SSH Git URL without embedded credentials.")
        elif not re.fullmatch(r"(?:[^@/:]+@)?[^/:]+:.+", repository):
            raise KaizoraError(f"{manifest_file} field 'repository' must be a valid Git remote URL.")
    if branch is not None and (not isinstance(branch, str) or not branch.strip() or any(ord(ch) < 32 for ch in branch)):
        raise KaizoraError(f"{manifest_file} field 'branch' must be a non-empty ref when set.")
    if repository and not branch:
        raise KaizoraError(f"{manifest_file} needs a 'branch' when 'repository' is set.")

    normalized = {
        "name": name.strip(),
        "display_name": display_name.strip(),
        "slug": slug,
        "runtime": runtime.strip().lower(),
        "domain": domain.strip().lower() if domain else None,
        "compose_file": str(compose_file.relative_to(project_path)),
        "repository": repository.strip() if repository else None,
        "branch": branch.strip() if branch else None,
        "resources": _validate_resources(raw.get("resources"), manifest_file),
        "health": _validate_health(raw.get("health"), manifest_file),
        "backup": _validate_backup(raw.get("backup"), manifest_file),
    }
    return Project(project_path, normalized, compose_file)


def discover_projects(projects_path: Path) -> tuple[list[Project], list[str]]:
    projects_path = projects_path.expanduser().resolve()
    if not projects_path.exists():
        return [], []
    if not projects_path.is_dir():
        return [], [f"{projects_path} is not a directory."]
    projects: list[Project] = []
    errors: list[str] = []
    for entry in sorted(projects_path.iterdir(), key=lambda item: item.name.lower()):
        if not entry.is_dir():
            continue
        try:
            projects.append(load_project(projects_path, entry.name))
        except KaizoraError as exc:
            errors.append(str(exc))
    return projects, errors
