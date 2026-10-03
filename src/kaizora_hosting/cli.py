"""Manage Kaizora Hosting projects with Docker Compose."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit


DEFAULT_ROOT = Path("/srv/kaizora-hosting")
PROJECTS_DIR = "projects"
MANIFEST_NAME = "kaizora.json"
NETWORK_NAME = "kaizora-network"
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DATABASE_SERVICE_RE = re.compile(r"(?:^|[-_])(db|database|mysql|mariadb|postgres|postgresql)(?:$|[-_])", re.I)
EXCLUDED_BACKUP_NAMES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
}


class KaizoraError(Exception):
    """An expected, user-correctable CLI error."""


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
        return str(self.manifest["name"])

    @property
    def domain(self) -> str:
        return str(self.manifest.get("domain") or "-")

    @property
    def compose_name(self) -> str:
        return f"kz-{self.slug}"


def hosting_root(args: argparse.Namespace) -> Path:
    configured = args.root or os.environ.get("KZ_HOSTING_ROOT")
    return Path(configured or DEFAULT_ROOT).expanduser().resolve()


def ensure_within(path: Path, parent: Path, description: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(parent.resolve())
    except ValueError as exc:
        raise KaizoraError(f"{description} must stay inside {parent}.") from exc
    return resolved


def load_project(root: Path, slug: str) -> Project:
    if not SLUG_RE.fullmatch(slug):
        raise KaizoraError(f"Invalid project slug: {slug!r}.")

    projects_dir = root / PROJECTS_DIR
    if not projects_dir.is_dir():
        raise KaizoraError(f"Projects directory is missing: {projects_dir}")

    project_path = ensure_within(projects_dir / slug, projects_dir, "Project path")
    if not project_path.is_dir():
        raise KaizoraError(f"Project does not exist: {slug}")

    manifest_path = ensure_within(project_path / MANIFEST_NAME, project_path, "Manifest path")
    if not manifest_path.is_file():
        raise KaizoraError(f"Project {slug} has no {MANIFEST_NAME} manifest.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KaizoraError(f"Could not read {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise KaizoraError(f"{manifest_path} must contain a JSON object.")

    name = manifest.get("name")
    declared_slug = manifest.get("slug")
    if not isinstance(name, str) or not name.strip():
        raise KaizoraError(f"{manifest_path} needs a non-empty name.")
    if declared_slug != slug:
        raise KaizoraError(f"{manifest_path} slug must match its directory name ({slug}).")

    compose_value = manifest.get("compose_file", "docker-compose.yml")
    if not isinstance(compose_value, str) or not compose_value.strip():
        raise KaizoraError(f"{manifest_path} compose_file must be a relative file path.")
    compose_rel = Path(compose_value)
    if compose_rel.is_absolute():
        raise KaizoraError("compose_file must be relative to the project directory.")
    compose_file = ensure_within(project_path / compose_rel, project_path, "Compose file")
    if not compose_file.is_file():
        raise KaizoraError(f"Compose file is missing: {compose_file}")

    repository = manifest.get("repository")
    branch = manifest.get("branch")
    if repository is not None and (not isinstance(repository, str) or not repository.strip()):
        raise KaizoraError(f"{manifest_path} repository must be a non-empty URL when set.")
    if branch is not None and (not isinstance(branch, str) or not branch.strip()):
        raise KaizoraError(f"{manifest_path} branch must be a non-empty ref when set.")
    if repository and not branch:
        raise KaizoraError(f"{manifest_path} needs a branch when repository is set.")

    return Project(path=project_path, manifest=manifest, compose_file=compose_file)


def discover_projects(root: Path) -> tuple[list[Project], list[str]]:
    projects_dir = root / PROJECTS_DIR
    if not projects_dir.exists():
        return [], []
    if not projects_dir.is_dir():
        return [], [f"{projects_dir} is not a directory."]

    projects: list[Project] = []
    errors: list[str] = []
    for entry in sorted(projects_dir.iterdir(), key=lambda item: item.name.lower()):
        if not entry.is_dir():
            continue
        try:
            projects.append(load_project(root, entry.name))
        except KaizoraError as exc:
            errors.append(str(exc))
    return projects, errors


def docker_path() -> str:
    executable = shutil.which("docker")
    if not executable:
        raise KaizoraError("Docker CLI was not found. Install Docker Engine and the Compose plugin first.")
    return executable


def compose_prefix(project: Project) -> list[str]:
    return [
        docker_path(),
        "compose",
        "--project-name",
        project.compose_name,
        "--project-directory",
        str(project.path),
        "--file",
        str(project.compose_file),
    ]


def run_capture(command: Sequence[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise KaizoraError(f"Could not run {Path(command[0]).name}: {exc}") from exc


def run_stream(command: Sequence[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    try:
        result = subprocess.run(list(command), cwd=cwd, env=env, check=False)
    except OSError as exc:
        raise KaizoraError(f"Could not run {Path(command[0]).name}: {exc}") from exc
    if result.returncode:
        raise KaizoraError(f"Command failed with exit code {result.returncode}: {Path(command[0]).name}")


def parse_compose_json(output: str) -> list[dict[str, Any]]:
    text = output.strip()
    if not text:
        return []
    try:
        value = json.loads(text)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
    except json.JSONDecodeError:
        pass

    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise KaizoraError("Docker Compose returned invalid JSON for its project status.") from exc
        if isinstance(value, dict):
            rows.append(value)
    return rows


def compose_rows(project: Project) -> list[dict[str, Any]]:
    result = run_capture([*compose_prefix(project), "ps", "--format", "json"], cwd=project.path)
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "Docker Compose could not read project status."
        raise KaizoraError(message)
    return parse_compose_json(result.stdout)


def project_state(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "STOPPED"
    states = [str(row.get("State", row.get("state", ""))).lower() for row in rows]
    health = [str(row.get("Health", row.get("health", ""))).lower() for row in rows]
    if any(value == "unhealthy" for value in health):
        return "UNHEALTHY"
    if any(value == "starting" for value in health):
        return "STARTING"
    if all(state == "running" for state in states):
        return "RUNNING"
    if any(state == "running" for state in states):
        return "PARTIAL"
    if any(state in {"exited", "dead", "created"} for state in states):
        return "STOPPED"
    return "UNKNOWN"


def docker_info() -> tuple[str | None, str | None]:
    try:
        executable = docker_path()
    except KaizoraError as exc:
        return None, str(exc)
    result = run_capture([executable, "info", "--format", "{{.ServerVersion}}"])
    if result.returncode:
        message = result.stderr.strip() or "Docker daemon is not responding."
        return None, message
    return result.stdout.strip() or "available", None


def docker_stats() -> dict[str, str]:
    try:
        executable = docker_path()
    except KaizoraError:
        return {}
    result = run_capture([executable, "stats", "--no-stream", "--format", "{{json .}}"])
    if result.returncode:
        return {}
    stats: dict[str, str] = {}
    for line in result.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            name = str(row.get("Name") or row.get("Container") or "")
            memory = str(row.get("MemUsage") or "-")
            if name:
                stats[name] = memory
    return stats


def memory_for(project: Project, stats: dict[str, str]) -> str:
    prefix = f"{project.compose_name}-"
    values = [value for name, value in stats.items() if name.startswith(prefix)]
    return ", ".join(values) if values else "-"


def print_project_table(projects: list[Project], stats: dict[str, str], docker_ready: bool) -> None:
    headers = ("PROJECT", "STATUS", "MEMORY", "DOMAIN")
    rows: list[tuple[str, str, str, str]] = []
    for project in projects:
        if docker_ready:
            try:
                state = project_state(compose_rows(project))
            except KaizoraError:
                state = "UNKNOWN"
        else:
            state = "UNKNOWN"
        rows.append((project.slug, state, memory_for(project, stats), project.domain))

    widths = [max(len(headers[index]), *(len(row[index]) for row in rows)) if rows else len(headers[index]) for index in range(len(headers))]
    print("  ".join(headers[index].ljust(widths[index]) for index in range(len(headers))))
    print("  ".join("-" * widths[index] for index in range(len(headers))))
    for row in rows:
        print("  ".join(row[index].ljust(widths[index]) for index in range(len(headers))))
    if not rows:
        print("Belum ada project. Tambahkan folder dengan manifest kaizora.json di projects/.")


def command_projects(args: argparse.Namespace) -> int:
    root = hosting_root(args)
    projects, errors = discover_projects(root)
    for error in errors:
        print(f"WARN: {error}", file=sys.stderr)
    version, _ = docker_info()
    stats = docker_stats() if version else {}
    if args.json:
        print(json.dumps([
            {
                "slug": project.slug,
                "name": project.title,
                "domain": project.domain,
                "status": project_state(compose_rows(project)) if version else "UNKNOWN",
                "memory": memory_for(project, stats),
            }
            for project in projects
        ], indent=2))
    else:
        print("KAIZORA HOSTING")
        print("NODE  KZ-HOME-01")
        print_project_table(projects, stats, bool(version))
    return 1 if errors else 0


def command_status(args: argparse.Namespace) -> int:
    root = hosting_root(args)
    projects, errors = discover_projects(root)
    for error in errors:
        print(f"WARN: {error}", file=sys.stderr)

    version, issue = docker_info()
    print("KAIZORA HOSTING")
    print("NODE  KZ-HOME-01")
    if version:
        print(f"DOCKER  {version}")
    else:
        print(f"DOCKER  unavailable: {issue}")

    if args.project:
        try:
            project = load_project(root, args.project)
            rows = compose_rows(project) if version else []
            print(f"PROJECT  {project.slug}")
            print(f"STATUS   {project_state(rows) if version else 'UNKNOWN'}")
            print(f"DOMAIN   {project.domain}")
            if not version:
                return 1
        except KaizoraError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    else:
        stats = docker_stats() if version else {}
        print_project_table(projects, stats, bool(version))
    return 1 if errors or not version else 0


def git_identity(remote: str) -> tuple[str, str] | None:
    value = remote.strip()
    if not value:
        return None
    if "://" in value:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        path = parsed.path.lstrip("/")
    else:
        match = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", value)
        if not match:
            return None
        host, path = match.groups()
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path:
        return None
    return host.lower(), path.lower()


def update_from_git(project: Project) -> None:
    expected = project.manifest.get("repository")
    if not expected:
        return

    git = shutil.which("git")
    if not git:
        raise KaizoraError("Git is required because this project's manifest declares a repository.")
    top = run_capture([git, "-C", str(project.path), "rev-parse", "--show-toplevel"])
    if top.returncode or Path(top.stdout.strip()).resolve() != project.path:
        raise KaizoraError("The project repository root does not match its project directory.")

    origin = run_capture([git, "-C", str(project.path), "config", "--get", "remote.origin.url"])
    expected_identity = git_identity(str(expected))
    actual_identity = git_identity(origin.stdout) if origin.returncode == 0 else None
    if not expected_identity or expected_identity != actual_identity:
        raise KaizoraError("Git origin does not match the repository in kaizora.json.")

    branch = str(project.manifest["branch"])
    valid_branch = run_capture([git, "check-ref-format", "--branch", branch])
    if valid_branch.returncode:
        raise KaizoraError("The branch in kaizora.json is not a valid Git branch name.")

    changes = run_capture([git, "-C", str(project.path), "status", "--porcelain", "--untracked-files=all"])
    if changes.returncode:
        raise KaizoraError("Could not inspect project Git status before pulling.")
    if changes.stdout.strip():
        raise KaizoraError("Project has local changes; commit or move them before deploying from Git.")

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = run_capture([git, "-C", str(project.path), "pull", "--ff-only", "origin", branch], env=env)
    if result.returncode:
        message = result.stderr.strip() or "Git pull failed."
        raise KaizoraError(message)
    if result.stdout.strip():
        print(result.stdout.strip())


def compose_config(project: Project) -> dict[str, Any]:
    result = run_capture([*compose_prefix(project), "config", "--format", "json"], cwd=project.path)
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "Docker Compose configuration is invalid."
        raise KaizoraError(message)
    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise KaizoraError("Docker Compose returned invalid normalized configuration.") from exc
    if not isinstance(config, dict):
        raise KaizoraError("Docker Compose configuration must be an object.")
    return config


def validate_compose(project: Project) -> dict[str, Any]:
    config = compose_config(project)
    services = config.get("services")
    networks = config.get("networks")
    if not isinstance(services, dict) or not services:
        raise KaizoraError("Compose file must define at least one service.")
    if not isinstance(networks, dict) or not isinstance(networks.get(NETWORK_NAME), dict):
        raise KaizoraError(f"Compose file must use the external {NETWORK_NAME} network.")
    if not networks[NETWORK_NAME].get("external"):
        raise KaizoraError(f"{NETWORK_NAME} must be declared as an external network.")

    issues: list[str] = []
    for name, service in services.items():
        if not isinstance(service, dict):
            issues.append(f"{name}: service configuration is invalid")
            continue
        attached = service.get("networks", {})
        if isinstance(attached, list):
            network_names = set(attached)
        elif isinstance(attached, dict):
            network_names = set(attached)
        else:
            network_names = set()
        if NETWORK_NAME not in network_names:
            issues.append(f"{name}: not attached to {NETWORK_NAME}")

        deploy = service.get("deploy", {})
        limits = deploy.get("resources", {}).get("limits", {}) if isinstance(deploy, dict) else {}
        if not (service.get("mem_limit") or (isinstance(limits, dict) and limits.get("memory"))):
            issues.append(f"{name}: memory limit is required")
        if not (service.get("cpus") or (isinstance(limits, dict) and limits.get("cpus"))):
            issues.append(f"{name}: CPU limit is required")
        if service.get("restart") != "unless-stopped":
            issues.append(f"{name}: restart must be unless-stopped")
        if service.get("privileged") is True:
            issues.append(f"{name}: privileged containers are not allowed")

        volumes = service.get("volumes", [])
        for volume in volumes if isinstance(volumes, list) else []:
            source = volume.get("source", "") if isinstance(volume, dict) else str(volume).split(":", 1)[0]
            target = volume.get("target", "") if isinstance(volume, dict) else ":".join(str(volume).split(":")[1:2])
            if "/var/run/docker.sock" in source or "/var/run/docker.sock" in target:
                issues.append(f"{name}: mounting the Docker socket is not allowed")
                break

        ports = service.get("ports", [])
        if DATABASE_SERVICE_RE.search(str(name)) and ports:
            issues.append(f"{name}: database ports must not be published on the host")

    if issues:
        raise KaizoraError("Compose security/readiness checks failed:\n  - " + "\n  - ".join(issues))
    return config


def require_engine() -> None:
    _, issue = docker_info()
    if issue:
        raise KaizoraError(f"Docker daemon is unavailable: {issue}")


def run_compose(project: Project, *arguments: str) -> None:
    run_stream([*compose_prefix(project), *arguments], cwd=project.path)


def wait_until_running(project: Project, expected_services: int, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last_state = "UNKNOWN"
    while True:
        rows = compose_rows(project)
        last_state = project_state(rows)
        if len(rows) >= expected_services and last_state == "RUNNING":
            health = [str(row.get("Health", row.get("health", ""))).lower() for row in rows]
            if not any(value in {"starting", "unhealthy"} for value in health):
                return
            if any(value == "unhealthy" for value in health):
                raise KaizoraError("A container reported unhealthy after deployment.")
        if time.monotonic() >= deadline:
            raise KaizoraError(f"Services did not become ready within {timeout}s (last state: {last_state}).")
        time.sleep(2)


def command_deploy(args: argparse.Namespace) -> int:
    root = hosting_root(args)
    project = load_project(root, args.project)
    require_engine()
    update_from_git(project)
    config = validate_compose(project)
    print(f"Deploying {project.slug}...")
    run_compose(project, "up", "--detach", "--build", "--remove-orphans")
    wait_until_running(project, len(config["services"]), args.timeout)
    print(f"Deployment ready: {project.domain}")
    return 0


def command_lifecycle(args: argparse.Namespace) -> int:
    root = hosting_root(args)
    project = load_project(root, args.project)
    require_engine()
    if args.action == "deploy":
        return command_deploy(args)
    run_compose(project, args.action)
    return 0


def command_logs(args: argparse.Namespace) -> int:
    project = load_project(hosting_root(args), args.project)
    require_engine()
    command = [*compose_prefix(project), "logs", "--tail", str(args.tail)]
    if args.follow:
        command.append("--follow")
    command.extend(args.services)
    run_stream(command, cwd=project.path)
    return 0


def excluded_backup_path(relative: Path) -> bool:
    if any(part in EXCLUDED_BACKUP_NAMES for part in relative.parts):
        return True
    filename = relative.name.lower()
    if filename == ".env" or filename.startswith(".env.") or filename.endswith(".env"):
        return True
    return False


def command_backup(args: argparse.Namespace) -> int:
    root = hosting_root(args)
    project = load_project(root, args.project)
    backup_dir = root / "backups" / "project-configs"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = backup_dir / f"{project.slug}-{stamp}.tar.gz"

    try:
        with tarfile.open(archive, "w:gz") as bundle:
            for current, directories, files in os.walk(project.path, followlinks=False):
                current_path = Path(current)
                relative_dir = current_path.relative_to(project.path)
                directories[:] = [
                    name for name in sorted(directories)
                    if not excluded_backup_path(relative_dir / name)
                    and not (current_path / name).is_symlink()
                ]
                if relative_dir != Path("."):
                    bundle.add(current_path, arcname=str(Path(project.slug) / relative_dir), recursive=False)
                for filename in sorted(files):
                    item = current_path / filename
                    relative = item.relative_to(project.path)
                    if excluded_backup_path(relative) or item.is_symlink() or not item.is_file():
                        continue
                    bundle.add(item, arcname=str(Path(project.slug) / relative), recursive=False)
        archive.chmod(0o600)
    except (OSError, tarfile.TarError) as exc:
        archive.unlink(missing_ok=True)
        raise KaizoraError(f"Could not create project backup: {exc}") from exc

    print(f"Project files backed up to {archive}")
    print(".env files, Git metadata, dependency caches, symlinks, and Docker volumes are excluded.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kz", description="Manage projects on a Kaizora Hosting node.")
    parser.add_argument("--root", help="Hosting root (default: /srv/kaizora-hosting or KZ_HOSTING_ROOT).")
    commands = parser.add_subparsers(dest="command", required=True)

    projects = commands.add_parser("projects", help="List configured projects and their status.")
    projects.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    projects.set_defaults(handler=command_projects)

    status = commands.add_parser("status", help="Show node and project status.")
    status.add_argument("project", nargs="?", help="Show one project.")
    status.set_defaults(handler=command_status)

    deploy = commands.add_parser("deploy", help="Pull a Git project, build it, and start its containers.")
    deploy.add_argument("project")
    deploy.add_argument("--timeout", type=int, default=60, help="Seconds to wait for running containers (default: 60).")
    deploy.set_defaults(handler=command_deploy)

    for action in ("start", "stop", "restart"):
        command = commands.add_parser(action, help=f"{action.capitalize()} a project's containers.")
        command.add_argument("project")
        command.set_defaults(handler=command_lifecycle, action=action)

    logs = commands.add_parser("logs", help="Read or follow a project's logs.")
    logs.add_argument("project")
    logs.add_argument("--follow", "-f", action="store_true", help="Follow new log output.")
    logs.add_argument("--tail", type=int, default=100, help="Number of lines per container (default: 100).")
    logs.add_argument("services", nargs="*", help="Optional Compose service names.")
    logs.set_defaults(handler=command_logs)

    backup = commands.add_parser("backup", help="Archive project files without .env files or Docker volumes.")
    backup.add_argument("project")
    backup.set_defaults(handler=command_backup)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "timeout", 1) < 1:
        parser.error("--timeout must be at least 1 second")
    if getattr(args, "tail", 1) < 0:
        parser.error("--tail cannot be negative")
    try:
        return int(args.handler(args))
    except KaizoraError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
