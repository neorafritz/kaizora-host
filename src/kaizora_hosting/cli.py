"""Manage Kaizora Hosting projects with Docker Compose."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from .config import load_node_config
from .errors import KaizoraError
from .manifest import Project, discover_projects, load_project


DEFAULT_ROOT = Path("/srv/kaizora-hosting")
NETWORK_NAME = "kaizora-network"
DATABASE_SERVICE_RE = re.compile(r"(?:^|[-_])(db|database|mysql|mariadb|postgres|postgresql)(?:$|[-_])", re.I)


def hosting_root(args: argparse.Namespace) -> Path:
    configured = args.root or os.environ.get("KZ_HOSTING_ROOT")
    return Path(configured or DEFAULT_ROOT).expanduser().resolve()


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


def run_capture(
    command: Sequence[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 15,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise KaizoraError(f"Command timed out: {Path(command[0]).name}.") from exc
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
    result = run_capture([*compose_prefix(project), "ps", "--all", "--format", "json"], cwd=project.path)
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
    if any(state == "restarting" for state in states):
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
    config = load_node_config(hosting_root(args))
    projects, errors = discover_projects(config.projects_path)
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
        print(f"NODE  {config.name}")
        print_project_table(projects, stats, bool(version))
    return 1 if errors else 0


def command_status(args: argparse.Namespace) -> int:
    config = load_node_config(hosting_root(args))
    projects, errors = discover_projects(config.projects_path)
    for error in errors:
        print(f"WARN: {error}", file=sys.stderr)

    version, issue = docker_info()
    print("KAIZORA HOSTING")
    print(f"NODE  {config.name}")
    if version:
        print(f"DOCKER  {version}")
    else:
        print(f"DOCKER  unavailable: {issue}")

    if args.project:
        try:
            project = load_project(config.projects_path, args.project)
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
    try:
        if "://" in value:
            parsed = urlsplit(value)
            host = parsed.hostname or ""
            path = parsed.path.lstrip("/")
        else:
            match = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", value)
            if not match:
                return None
            host, path = match.groups()
    except ValueError:
        return None
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
        raise KaizoraError("Git pull failed; check network access and the project's Git credential setup.")
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


def _is_database_service(name: str, service: dict[str, Any]) -> bool:
    image = str(service.get("image") or "").lower()
    return bool(DATABASE_SERVICE_RE.search(name)) or any(
        engine in image for engine in ("mysql", "mariadb", "postgres", "postgresql")
    )


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
    total_cpu = 0.0
    total_memory = 0
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
        memory_limit = service.get("mem_limit") or (limits.get("memory") if isinstance(limits, dict) else None)
        cpu_limit = service.get("cpus") or (limits.get("cpus") if isinstance(limits, dict) else None)
        if not memory_limit:
            issues.append(f"{name}: memory limit is required")
        else:
            try:
                total_memory += _memory_bytes(memory_limit)
            except ValueError:
                issues.append(f"{name}: memory limit is invalid")
        if not cpu_limit:
            issues.append(f"{name}: CPU limit is required")
        else:
            try:
                total_cpu += float(cpu_limit)
            except (TypeError, ValueError):
                issues.append(f"{name}: CPU limit is invalid")
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
        if _is_database_service(str(name), service) and ports:
            issues.append(f"{name}: database ports must not be published on the host")

    if issues:
        raise KaizoraError("Compose security/readiness checks failed:\n  - " + "\n  - ".join(issues))
    resources = project.manifest.get("resources", {})
    if "cpu" in resources and total_cpu > resources["cpu"] + 1e-9:
        raise KaizoraError(f"Compose CPU limits total {total_cpu:g}, above kaizora.json resources.cpu ({resources['cpu']:g}).")
    if "memory" in resources:
        budget = _memory_bytes(resources["memory"])
        if total_memory > budget:
            raise KaizoraError(f"Compose memory limits total {total_memory} bytes, above kaizora.json resources.memory ({resources['memory']}).")
    return config


def _memory_bytes(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("not a memory value")
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    match = re.fullmatch(r"([0-9]+)(b|k|kb|kib|m|mb|mib|g|gb|gib|t|tb|tib)?", text)
    if not match:
        raise ValueError("not a memory value")
    amount = int(match.group(1))
    suffix = match.group(2) or "b"
    exponent = {"b": 0, "k": 1, "kb": 1, "kib": 1, "m": 2, "mb": 2, "mib": 2,
                "g": 3, "gb": 3, "gib": 3, "t": 4, "tb": 4, "tib": 4}[suffix]
    return amount * (1024 ** exponent)


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
        if len(rows) >= expected_services and last_state == "UNHEALTHY":
            raise KaizoraError("A container reported unhealthy after deployment.")
        if len(rows) >= expected_services and last_state == "RUNNING":
            health = [str(row.get("Health", row.get("health", ""))).lower() for row in rows]
            if not any(value in {"starting", "unhealthy"} for value in health):
                return
        if time.monotonic() >= deadline:
            raise KaizoraError(f"Services did not become ready within {timeout}s (last state: {last_state}).")
        time.sleep(2)


def command_deploy(args: argparse.Namespace) -> int:
    node = load_node_config(hosting_root(args))
    project = load_project(node.projects_path, args.project)
    if args.dry_run:
        print(f"Dry run for {project.title} ({project.slug})")
        if project.manifest.get("repository"):
            print(f"- Fast-forward Git branch {project.manifest['branch']}")
        print("- Validate Compose security and readiness settings")
        print("- Build project images")
        print("- Start/update containers and remove orphans")
        print(f"- Wait up to {args.timeout}s for services to become ready")
        return 0
    require_engine()
    update_from_git(project)
    config = validate_compose(project)
    print(f"Deploying {project.slug}...")
    run_compose(project, "up", "--detach", "--build", "--remove-orphans")
    wait_until_running(project, len(config["services"]), args.timeout)
    print(f"Deployment ready: {project.domain}")
    return 0


def command_lifecycle(args: argparse.Namespace) -> int:
    node = load_node_config(hosting_root(args))
    project = load_project(node.projects_path, args.project)
    require_engine()
    if args.action == "deploy":
        return command_deploy(args)
    run_compose(project, args.action)
    return 0


def command_logs(args: argparse.Namespace) -> int:
    node = load_node_config(hosting_root(args))
    project = load_project(node.projects_path, args.project)
    require_engine()
    command = [*compose_prefix(project), "logs", "--tail", str(args.tail)]
    if args.follow:
        command.append("--follow")
    command.extend(args.services)
    run_stream(command, cwd=project.path)
    return 0


def command_backup(args: argparse.Namespace) -> int:
    node = load_node_config(hosting_root(args))
    project = load_project(node.projects_path, args.project)
    from .backup import create_backup

    return create_backup(project, node, print)


def _host_memory() -> str:
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
        total = values["MemTotal"]
        available = values.get("MemAvailable", values.get("MemFree", 0))
        used = total - available
        gib = 1024 ** 3
        return f"{used / gib:.1f} / {total / gib:.1f} GB"
    except (OSError, ValueError, KeyError, IndexError):
        return "unknown"


def _host_disk(path: Path) -> str:
    try:
        usage = shutil.disk_usage(path if path.exists() else Path("/"))
        gib = 1024 ** 3
        return f"{(usage.total - usage.free) / gib:.0f} / {usage.total / gib:.0f} GB"
    except OSError:
        return "unknown"


def _project_rows(projects: list[Project], docker_ready: bool) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    if not docker_ready:
        return rows, errors
    for project in projects:
        try:
            rows[project.slug] = compose_rows(project)
        except KaizoraError as exc:
            rows[project.slug] = []
            errors[project.slug] = str(exc)
    return rows, errors


def command_node(args: argparse.Namespace) -> int:
    node = load_node_config(hosting_root(args))
    projects, errors = discover_projects(node.projects_path)
    version, docker_issue = docker_info()
    rows, row_errors = _project_rows(projects, bool(version))
    running = sum(project_state(rows.get(project.slug, [])) in {"RUNNING", "UNHEALTHY", "STARTING", "PARTIAL"} for project in projects)
    stopped = sum(project_state(rows.get(project.slug, [])) == "STOPPED" for project in projects)
    unknown = len(projects) if not version else sum(
        project.slug in row_errors or project_state(rows.get(project.slug, [])) == "UNKNOWN"
        for project in projects
    )
    print("KAIZORA HOSTING\n")
    print("Node\n────────────────────────")
    print(f"Name          {node.name}")
    print(f"ID            {node.node_id}")
    print(f"Type          {node.type.title()}")
    print(f"Environment   {node.environment}")
    print(f"\nProjects      {len(projects)}")
    print(f"Running       {running}")
    print(f"Stopped       {stopped}")
    if unknown:
        print(f"Unknown       {unknown}")
    print(f"\nDocker        {'Running' if version else 'Unavailable'}")
    if not version and docker_issue:
        print(f"              {docker_issue}")
    if errors:
        for error in errors:
            print(f"Warning: {error}", file=sys.stderr)
    if row_errors:
        for slug, error in row_errors.items():
            print(f"Warning: {slug}: {error}", file=sys.stderr)
    return 0 if version and not errors and not row_errors else 1


def _container_inspect(container_id: str) -> dict[str, Any] | None:
    result = run_capture([docker_path(), "inspect", container_id], timeout=8)
    if result.returncode:
        return None
    try:
        values = json.loads(result.stdout)
        return values[0] if values and isinstance(values[0], dict) else None
    except (json.JSONDecodeError, IndexError, TypeError):
        return None


def _probe_project_http(project: Project, rows: list[dict[str, Any]]) -> bool | None:
    configured_port = project.manifest.get("health", {}).get("port")
    candidates: list[tuple[int, str]] = []
    for row in rows:
        container_id = str(row.get("ID", row.get("Id", "")))
        if not container_id:
            continue
        detail = _container_inspect(container_id)
        if not detail:
            continue
        state = detail.get("State", {})
        if not state.get("Running"):
            continue
        config = detail.get("Config", {})
        exposed = config.get("ExposedPorts") or {}
        ports: list[int] = []
        if configured_port is not None:
            ports = [configured_port]
        else:
            for raw_port in exposed:
                try:
                    ports.append(int(str(raw_port).split("/", 1)[0]))
                except ValueError:
                    continue
        networks = detail.get("NetworkSettings", {}).get("Networks", {})
        net = networks.get(NETWORK_NAME, {})
        address = net.get("IPAddress")
        try:
            ipaddress.ip_address(address)
        except (ValueError, TypeError):
            continue
        for port in ports:
            candidates.append((port, str(address)))

    preferred = {80: 0, 443: 1, 8000: 2, 8080: 3, 3000: 4}
    candidates.sort(key=lambda item: (preferred.get(item[0], 10), item[0]))
    deadline = time.monotonic() + min(project.manifest.get("health", {}).get("timeout", 10), 10)
    attempted = False
    for port, address in candidates[:8]:
        health_path = project.manifest.get("health", {}).get("path", "/")
        timeout = min(project.manifest.get("health", {}).get("timeout", 10), deadline - time.monotonic())
        if timeout <= 0:
            break
        attempted = True
        try:
            with build_opener(ProxyHandler({})).open(f"http://{address}:{port}{health_path}", timeout=timeout) as response:
                if 200 <= response.status < 400:
                    return True
        except Exception:
            continue
    return False if attempted else None


def _project_health(project: Project, rows: list[dict[str, Any]], docker_ready: bool) -> str:
    if not docker_ready:
        return "unknown"
    if not rows:
        return "stopped"
    health = [str(row.get("Health", row.get("health", ""))).lower() for row in rows]
    if "unhealthy" in health:
        return "unhealthy"
    if "starting" in health:
        return "starting"
    state = project_state(rows)
    if state == "STOPPED":
        return "stopped"
    if state != "RUNNING":
        return "unhealthy" if state == "PARTIAL" else "unknown"
    configured_health = [value for value in health if value]
    if len(configured_health) == len(rows) and all(value == "healthy" for value in configured_health):
        return "healthy"
    probe = _probe_project_http(project, rows)
    if probe is True:
        return "healthy"
    if probe is False:
        return "unhealthy"
    return "running"


def command_health(args: argparse.Namespace) -> int:
    node = load_node_config(hosting_root(args))
    projects, discovery_errors = discover_projects(node.projects_path)
    version, _ = docker_info()
    docker_ready = bool(version)
    rows_by_slug, row_errors = _project_rows(projects, docker_ready)
    print("KAIZORA HOSTING HEALTH\n")
    print("Node\n" + node.name)
    print("\nHost\n────────────────────────")
    print(f"RAM             {_host_memory()}")
    print(f"Disk            {_host_disk(node.config_path.parent)}")
    print("\nServices\n────────────────────────")
    print(f"Docker          {'healthy' if docker_ready else 'unhealthy'}")
    cloudflared = run_capture([docker_path(), "inspect", "kaizora-cloudflared"], timeout=5) if docker_ready else None
    if not docker_ready or cloudflared is None or cloudflared.returncode:
        cloud_status = "not configured"
    else:
        detail = _container_inspect("kaizora-cloudflared") or {}
        state = detail.get("State", {})
        cloud_health = state.get("Health", {}).get("Status")
        cloud_status = str(cloud_health or ("unknown" if state.get("Running") else "unhealthy"))
    print(f"Cloudflare      {cloud_status}")
    print("\nProjects\n────────────────────────")
    counts = {"healthy": 0, "unhealthy": 0, "stopped": 0, "running": 0, "starting": 0, "unknown": 0}
    for project in projects:
        state = "unknown" if project.slug in row_errors else _project_health(project, rows_by_slug.get(project.slug, []), docker_ready)
        if state in counts:
            counts[state] += 1
        print(f"{project.title:<24} {state}")
    print("\nSummary")
    print(f"{counts['healthy']} Healthy")
    print(f"{counts['unhealthy']} Unhealthy")
    print(f"{counts['stopped']} Stopped")
    if counts["running"] or counts["starting"] or counts["unknown"]:
        print(f"{counts['running']} Running without health signal")
        print(f"{counts['starting']} Starting")
        print(f"{counts['unknown']} Unknown")
    if discovery_errors:
        for error in discovery_errors:
            print(f"Warning: {error}", file=sys.stderr)
    if row_errors:
        for slug, error in row_errors.items():
            print(f"Warning: {slug}: {error}", file=sys.stderr)
    return 1 if not docker_ready or discovery_errors or row_errors or counts["unhealthy"] or cloud_status == "unhealthy" else 0


def command_backups(args: argparse.Namespace) -> int:
    node = load_node_config(hosting_root(args))
    project = load_project(node.projects_path, args.project)
    from .backup import list_backups

    list_backups(project, node.backups_path)
    return 0


def command_panel(args: argparse.Namespace) -> int:
    from .panel import run_panel

    return run_panel(hosting_root(args), args.port)


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
    deploy.add_argument("--dry-run", action="store_true", help="Show deployment actions without changing containers or Git state.")
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

    backup = commands.add_parser("backup", help="Back up project files, database, and project volumes.")
    backup.add_argument("project")
    backup.set_defaults(handler=command_backup)

    backups = commands.add_parser("backups", help="List backup snapshots for a project.")
    backups.add_argument("project")
    backups.set_defaults(handler=command_backups)

    node = commands.add_parser("node", help="Show node identity and project counts.")
    node.set_defaults(handler=command_node)

    health = commands.add_parser("health", help="Check Docker and project health.")
    health.set_defaults(handler=command_health)

    panel = commands.add_parser("panel", help="Start the local-only browser control panel.")
    panel.add_argument("--port", type=int, default=8787, help="Local panel port (default: 8787).")
    panel.set_defaults(handler=command_panel)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "timeout", 1) < 1:
        parser.error("--timeout must be at least 1 second")
    if getattr(args, "tail", 1) < 0:
        parser.error("--tail cannot be negative")
    if getattr(args, "port", 1) < 1 or getattr(args, "port", 65535) > 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        return int(args.handler(args))
    except KaizoraError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: Operating system error: {exc.strerror or exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
