"""Project, database, and project-scoped Docker volume backups."""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path
from typing import Any, Callable

from .config import NodeConfig
from .errors import KaizoraError
from .manifest import Project


EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", "credentials", "tokens"}
DATABASE_RE = re.compile(r"(?:^|[-_])(db|database|mysql|mariadb|postgres|postgresql)(?:$|[-_])", re.I)
SAFE_VOLUME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
MYSQL_DUMP = (
    'set -eu; '
    'user="${MYSQL_USER:-${MARIADB_USER:-root}}"; '
    'if [ "$user" = root ]; then '
    'export MYSQL_PWD="${MYSQL_PWD:-${MYSQL_ROOT_PASSWORD:-${MARIADB_ROOT_PASSWORD:-}}}"; '
    'else export MYSQL_PWD="${MYSQL_PWD:-${MYSQL_PASSWORD:-${MARIADB_PASSWORD:-}}}"; fi; '
    'db="${MYSQL_DATABASE:-${MARIADB_DATABASE:-}}"; '
    'if [ -n "$db" ]; then exec mysqldump --single-transaction --routines --events --user="$user" "$db"; '
    'else exec mysqldump --single-transaction --routines --events --all-databases --user="$user"; fi'
)
MARIADB_DUMP = MYSQL_DUMP.replace("mysqldump", "mariadb-dump")
POSTGRES_DUMP = (
    'set -eu; export PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-}}"; '
    'db="${POSTGRES_DB:-${POSTGRES_USER:-postgres}}"; user="${POSTGRES_USER:-postgres}"; '
    'exec pg_dump --no-password --no-owner --no-acl --username "$user" "$db"'
)


def _docker() -> str:
    docker = shutil.which("docker")
    if not docker:
        raise KaizoraError("Docker CLI was not found. Install Docker Engine and the Compose plugin first.")
    return docker


def _compose_prefix(project: Project) -> list[str]:
    return [
        _docker(), "compose", "--project-name", project.compose_name,
        "--project-directory", str(project.path), "--file", str(project.compose_file),
    ]


def _compose_config(project: Project) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [*_compose_prefix(project), "config", "--format", "json"],
            cwd=project.path, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=False, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KaizoraError("Could not read the project's normalized Compose configuration.") from exc
    if result.returncode:
        raise KaizoraError("Could not read the project's normalized Compose configuration.")
    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise KaizoraError("Docker Compose returned invalid normalized configuration.") from exc
    if not isinstance(config, dict):
        raise KaizoraError("Docker Compose configuration must be an object.")
    return config


def _exclude(relative: Path) -> bool:
    if any(part in EXCLUDED_DIRS for part in relative.parts):
        return True
    name = relative.name.lower()
    return (
        name == ".env"
        or name.startswith(".env.")
        or name.endswith((".env", ".secret", ".token", ".key", ".pem", ".p12", ".pfx"))
        or name in {"id_rsa", "id_ed25519"}
    )


def _archive_project(project: Project, target: Path) -> None:
    try:
        with tarfile.open(target, "w:gz") as archive:
            for current, directories, files in os.walk(project.path, followlinks=False):
                current_path = Path(current)
                relative_dir = current_path.relative_to(project.path)
                directories[:] = [
                    name for name in sorted(directories)
                    if not _exclude(relative_dir / name) and not (current_path / name).is_symlink()
                ]
                if relative_dir != Path("."):
                    archive.add(current_path, arcname=str(Path(project.slug) / relative_dir), recursive=False)
                for filename in sorted(files):
                    item = current_path / filename
                    relative = item.relative_to(project.path)
                    if _exclude(relative) or item.is_symlink() or not item.is_file():
                        continue
                    archive.add(item, arcname=str(Path(project.slug) / relative), recursive=False)
        target.chmod(0o600)
    except (OSError, tarfile.TarError) as exc:
        target.unlink(missing_ok=True)
        raise KaizoraError("Could not create the project files archive.") from exc


def _db_services(config: dict[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    services = config.get("services", {})
    if not isinstance(services, dict):
        return found
    for name, raw in services.items():
        if not isinstance(raw, dict):
            continue
        image = str(raw.get("image") or "").lower().split("@", 1)[0]
        service_name = str(name)
        database_like = bool(DATABASE_RE.search(service_name)) or any(
            engine in image for engine in ("mysql", "mariadb", "postgres", "postgresql")
        )
        if not database_like:
            continue
        if "mariadb" in image:
            engine = "mariadb"
        elif "mysql" in image:
            engine = "mysql"
        elif "postgres" in image or "postgresql" in image:
            engine = "postgres"
        else:
            engine = "unsupported"
        found.append((service_name, engine))
    return found


def _stream_process_to_gzip(command: list[str], target: Path, timeout: int, failure_message: str) -> None:
    process: subprocess.Popen[bytes] | None = None
    timer: threading.Timer | None = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        timer = threading.Timer(timeout, process.kill)
        timer.daemon = True
        timer.start()
        if process.stdout is None:
            raise KaizoraError(failure_message)
        with target.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            shutil.copyfileobj(process.stdout, compressed, length=1024 * 1024)
        return_code = process.wait(timeout=5)
        if return_code:
            raise KaizoraError(failure_message)
        target.chmod(0o600)
    except (OSError, subprocess.SubprocessError, KaizoraError) as exc:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        target.unlink(missing_ok=True)
        if isinstance(exc, KaizoraError):
            raise
        raise KaizoraError(failure_message) from exc
    finally:
        if timer:
            timer.cancel()
        if process and process.stdout:
            process.stdout.close()


def _archive_database(project: Project, service: str, engine: str, target: Path) -> None:
    if engine not in {"mysql", "mariadb", "postgres"}:
        raise KaizoraError(f"Database service {service} uses an unsupported engine; supported engines are MySQL, MariaDB, and PostgreSQL.")
    script = POSTGRES_DUMP if engine == "postgres" else MARIADB_DUMP if engine == "mariadb" else MYSQL_DUMP
    message = f"Database backup failed for service {service}; check that the database is running and its in-container dump client is available."
    _stream_process_to_gzip(
        [*_compose_prefix(project), "exec", "-T", service, "sh", "-ec", script],
        target, 600, message,
    )


def _named_volumes(config: dict[str, Any]) -> list[tuple[str, str]]:
    top_volumes = config.get("volumes", {})
    services = config.get("services", {})
    found: dict[str, str] = {}
    if not isinstance(services, dict):
        return []
    for service in services.values():
        if not isinstance(service, dict):
            continue
        mounts = service.get("volumes", [])
        if not isinstance(mounts, list):
            continue
        for mount in mounts:
            if not isinstance(mount, dict) or mount.get("type") != "volume":
                continue
            logical = str(mount.get("source") or "")
            if not logical:
                continue
            details = top_volumes.get(logical, {}) if isinstance(top_volumes, dict) else {}
            actual = str(details.get("name") or logical) if isinstance(details, dict) else logical
            if actual and SAFE_VOLUME_RE.fullmatch(actual):
                found[logical] = actual
    return sorted(found.items())


def _archive_volume(actual_name: str, target: Path) -> None:
    command = [
        _docker(), "run", "--rm", "--network", "none",
        "--mount", f"type=volume,src={actual_name},dst=/source,readonly",
        "busybox:1.37", "tar", "-cf", "-", "-C", "/source", ".",
    ]
    _stream_process_to_gzip(
        command, target, 600,
        f"Could not archive Docker volume {actual_name}; ensure busybox:1.37 is available and the volume can be read.",
    )


def _metadata(project: Project, config: dict[str, Any] | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    git = shutil.which("git")
    if git:
        for key, args in (
            ("commit", ["rev-parse", "HEAD"]),
            ("branch", ["rev-parse", "--abbrev-ref", "HEAD"]),
        ):
            try:
                result = subprocess.run(
                    [git, "-C", str(project.path), *args], text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            value = result.stdout.strip()
            if result.returncode == 0 and value and value != "HEAD":
                metadata[key] = value
    if config:
        services = config.get("services", {})
        if isinstance(services, dict):
            names: set[str] = set()
            try:
                result = subprocess.run(
                    [*_compose_prefix(project), "ps", "--all", "--format", "json"], cwd=project.path,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=8,
                )
                if result.returncode == 0:
                    output = result.stdout.strip()
                    try:
                        rows = json.loads(output) if output.startswith("[") else [json.loads(line) for line in output.splitlines() if line.strip()]
                    except json.JSONDecodeError:
                        rows = []
                    for row in rows if isinstance(rows, list) else []:
                        if isinstance(row, dict):
                            name = row.get("Name", row.get("name"))
                            if name:
                                names.add(str(name))
            except (OSError, subprocess.TimeoutExpired):
                pass
            if names:
                metadata["container_names"] = sorted(names)
    return metadata


def _write_json(path: Path, value: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        path.chmod(0o600)
    except OSError as exc:
        raise KaizoraError("Could not write backup manifest.") from exc


def create_backup(project: Project, node: NodeConfig, emit: Callable[[str], None]) -> int:
    base = node.backups_path / project.slug
    try:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d_%H%M%S")
        snapshot = base / stamp
        suffix = 2
        while snapshot.exists():
            snapshot = base / f"{stamp}-{suffix:02d}"
            suffix += 1
        snapshot.mkdir(mode=0o700)
    except OSError as exc:
        raise KaizoraError(f"Could not create backup directory: {exc.strerror or exc}") from exc

    manifest: dict[str, Any] = {
        "project": project.slug,
        "node": node.node_id,
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "backup": {
            "project": {"status": "skipped"},
            "database": {"status": "skipped"},
            "volumes": {"status": "skipped"},
        },
        "metadata": {},
    }
    failures: list[str] = []
    emit("[1/4] Project files")
    if project.manifest["backup"]["project"]:
        try:
            _archive_project(project, snapshot / "project.tar.gz")
            manifest["backup"]["project"] = {"status": "success"}
            emit("      success")
        except KaizoraError as exc:
            manifest["backup"]["project"] = {"status": "failed", "error": str(exc)}
            failures.append(str(exc))
            emit("      failed")
    else:
        emit("      skipped (disabled in manifest)")

    config: dict[str, Any] | None = None
    config_error: str | None = None
    try:
        config = _compose_config(project)
        manifest["metadata"] = _metadata(project, config)
    except KaizoraError as exc:
        config_error = str(exc)

    emit("[2/4] Database")
    if project.manifest["backup"]["database"]:
        if config_error:
            message = "Could not inspect Compose configuration for database backup."
            manifest["backup"]["database"] = {"status": "failed", "error": message}
            failures.append(message)
            emit("      failed")
        else:
            databases = _db_services(config or {})
            if not databases:
                emit("      skipped (no supported database service)")
            elif len(databases) != 1:
                message = "Database backup supports one database service per project snapshot."
                manifest["backup"]["database"] = {"status": "failed", "error": message}
                failures.append(message)
                emit("      failed")
            else:
                service, engine = databases[0]
                try:
                    _archive_database(project, service, engine, snapshot / "database.sql.gz")
                    manifest["backup"]["database"] = {"status": "success", "engine": engine}
                    emit("      success")
                except KaizoraError as exc:
                    manifest["backup"]["database"] = {"status": "failed", "error": str(exc)}
                    failures.append(str(exc))
                    emit("      failed")
    else:
        emit("      skipped (disabled in manifest)")

    emit("[3/4] Persistent volumes")
    if project.manifest["backup"]["volumes"]:
        if config_error:
            message = "Could not inspect Compose configuration for volume backup."
            manifest["backup"]["volumes"] = {"status": "failed", "error": message}
            failures.append(message)
            emit("      failed")
        else:
            volumes = _named_volumes(config or {})
            if not volumes:
                emit("      skipped (no project named volumes)")
            else:
                volume_dir = snapshot / "volumes"
                try:
                    volume_dir.mkdir(mode=0o700)
                except OSError:
                    message = "Could not create the volume backup directory."
                    manifest["backup"]["volumes"] = {"status": "failed", "error": message}
                    failures.append(message)
                    emit("      failed")
                else:
                    volume_results: list[dict[str, str]] = []
                    for logical, actual in volumes:
                        filename = re.sub(r"[^a-zA-Z0-9_.-]+", "-", logical).strip("-.") or "volume"
                        try:
                            _archive_volume(actual, volume_dir / f"{filename}.tar.gz")
                            volume_results.append({"name": logical, "status": "success"})
                        except KaizoraError as exc:
                            volume_results.append({"name": logical, "status": "failed"})
                            failures.append(str(exc))
                    if any(item["status"] == "failed" for item in volume_results):
                        manifest["backup"]["volumes"] = {"status": "failed", "items": volume_results}
                        emit("      failed")
                    else:
                        manifest["backup"]["volumes"] = {"status": "success", "items": volume_results}
                        emit("      success")
    else:
        emit("      skipped (disabled in manifest)")

    emit("[4/4] Backup manifest")
    manifest["size_bytes"] = sum(path.stat().st_size for path in snapshot.rglob("*") if path.is_file())
    manifest_path = snapshot / "manifest.json"
    _write_json(manifest_path, manifest)
    emit(f"      {'written' if manifest_path.is_file() else 'failed'}: {manifest_path}")
    if failures:
        for failure in failures:
            emit(f"Error: {failure}")
        return 1
    emit(f"Backup complete: {snapshot}")
    return 0


def list_backups(project: Project, backups_path: Path) -> None:
    snapshots: list[tuple[dt.datetime, int, str, str]] = []
    project_dir = backups_path / project.slug
    if project_dir.is_dir():
        for folder in sorted(project_dir.iterdir(), reverse=True):
            if not folder.is_dir():
                continue
            try:
                value = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
                created = dt.datetime.fromisoformat(value["created_at"])
                size = int(value.get("size_bytes", 0))
                backup = value.get("backup", {})
                database = "yes" if backup.get("database", {}).get("status") == "success" else "no"
                volumes = "yes" if backup.get("volumes", {}).get("status") == "success" else "no"
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
            snapshots.append((created, size, database, volumes))
    print("KAIZORA HOSTING BACKUPS\n")
    print(f"Project\n{project.title}\n")
    print("DATE                  SIZE        DATABASE      VOLUMES")
    print("──────────────────────────────────────────────────────────")
    for created, size, database, volumes in snapshots:
        date = created.astimezone().strftime("%Y-%m-%d %H:%M")
        if size >= 1024 ** 3:
            size_text = f"{size / 1024 ** 3:.1f} GB"
        elif size >= 1024 ** 2:
            size_text = f"{size / 1024 ** 2:.1f} MB"
        elif size >= 1024:
            size_text = f"{size / 1024:.1f} KB"
        else:
            size_text = f"{size} B"
        print(f"{date:<22}{size_text:<12}{database:<14}{volumes}")
    if not snapshots:
        print("No backup snapshots found.")
