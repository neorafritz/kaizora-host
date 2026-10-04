"""Local panel operations for project files, managed databases, and Cloudflare."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .config import NodeConfig
from .errors import KaizoraError
from .manifest import DOMAIN_RE, Project, ensure_within


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_EDIT_BYTES = 1024 * 1024
PROTECTED_NAMES = {".git", "credentials", "tokens", ".kaizora.local.json"}
SECRET_SUFFIXES = (".secret", ".token", ".key", ".pem", ".p12", ".pfx")


def _protected(relative: Path) -> bool:
    for part in relative.parts:
        name = part.lower()
        if name in PROTECTED_NAMES or name == ".env" or name.startswith(".env."):
            return True
        if name.endswith(SECRET_SUFFIXES) or name in {"id_rsa", "id_ed25519"}:
            return True
    return False


def safe_project_path(project: Project, relative: str = "") -> Path:
    if not isinstance(relative, str) or "\\" in relative or any(ord(char) < 32 for char in relative):
        raise KaizoraError("Invalid project file path.")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {".", ".."} for part in pure.parts):
        raise KaizoraError("File path must stay inside this project.")
    if _protected(Path(*pure.parts)):
        raise KaizoraError("Secret and credential files are hidden from the file manager.")
    target = project.path.joinpath(*pure.parts)
    try:
        ensure_within(target, project.path, "Project file path")
    except KaizoraError:
        raise
    current = project.path
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise KaizoraError("The file manager does not follow symbolic links.")
    return target


def list_project_files(project: Project, relative: str = "") -> tuple[Path, list[tuple[str, bool, int]]]:
    directory = safe_project_path(project, relative)
    if not directory.is_dir():
        raise KaizoraError("That project folder does not exist.")
    entries: list[tuple[str, bool, int]] = []
    try:
        for child in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            child_relative = child.relative_to(project.path)
            if _protected(child_relative) or child.is_symlink():
                continue
            if child.is_dir():
                entries.append((child.name, True, 0))
            elif child.is_file():
                entries.append((child.name, False, child.stat().st_size))
    except OSError as exc:
        raise KaizoraError("Could not read this project folder.") from exc
    return directory, entries


def write_project_file(project: Project, relative_dir: str, filename: str, content: bytes) -> Path:
    if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise KaizoraError("Choose a file name without folder separators.")
    if any(ord(char) < 32 for char in filename):
        raise KaizoraError("The file name contains unsupported characters.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise KaizoraError("Files can be at most 20 MB.")
    destination = safe_project_path(project, str(PurePosixPath(relative_dir) / filename) if relative_dir else filename)
    if _protected(destination.relative_to(project.path)):
        raise KaizoraError("Secret and credential files cannot be uploaded through the file manager.")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.chmod(0o755)
        ensure_within(destination.parent, project.path, "Upload directory")
        temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(6)}.tmp")
        with temporary.open("xb") as output:
            output.write(content)
        temporary.chmod(0o644)
        temporary.replace(destination)
    except OSError as exc:
        raise KaizoraError("Could not save the uploaded file.") from exc
    return destination


def read_project_text(project: Project, relative: str) -> str:
    target = safe_project_path(project, relative)
    try:
        if not target.is_file() or target.stat().st_size > MAX_EDIT_BYTES:
            raise KaizoraError("Only text files up to 1 MB can be edited here.")
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise KaizoraError("This file is not UTF-8 text and cannot be edited in the panel.") from exc
    except OSError as exc:
        raise KaizoraError("Could not read this project file.") from exc


def save_project_text(project: Project, relative: str, content: str) -> None:
    target = safe_project_path(project, relative)
    if len(content.encode("utf-8")) > MAX_EDIT_BYTES:
        raise KaizoraError("Text files can be at most 1 MB.")
    if not target.is_file():
        raise KaizoraError("This project file does not exist.")
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(target.stat().st_mode & 0o777)
        temporary.replace(target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise KaizoraError("Could not save this project file.") from exc


def delete_project_file(project: Project, relative: str) -> None:
    target = safe_project_path(project, relative)
    try:
        if not target.is_file():
            raise KaizoraError("Only files can be deleted from the file manager.")
        target.unlink()
    except OSError as exc:
        raise KaizoraError("Could not delete this project file.") from exc


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not slug or len(slug) > 48:
        raise KaizoraError("Use a project name that becomes 1 to 48 lowercase letters, numbers, or hyphens.")
    return slug


def _exclude_local_project_config(project_path: Path) -> None:
    git_dir = project_path / ".git"
    if not git_dir.is_dir() or git_dir.is_symlink():
        return
    info_dir = git_dir / "info"
    exclude_file = info_dir / "exclude"
    if info_dir.is_symlink() or exclude_file.is_symlink():
        raise KaizoraError("Could not protect the local project settings from Git commits.")
    try:
        info_dir.mkdir(exist_ok=True)
        existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
        if not any(line.strip().lstrip("/") == ".kaizora.local.json" for line in existing.splitlines()):
            with exclude_file.open("a", encoding="utf-8") as output:
                output.write("\n/.kaizora.local.json\n")
    except OSError as exc:
        raise KaizoraError("Could not protect the local project settings from Git commits.") from exc


def create_static_project(projects_path: Path, display_name: str, domain: str = "") -> Project:
    display_name = display_name.strip()
    if not display_name or len(display_name) > 100 or any(ord(char) < 32 for char in display_name):
        raise KaizoraError("Enter a project name from 1 to 100 characters.")
    slug = _slugify(display_name)
    domain = domain.strip().lower().rstrip(".")
    if domain and not DOMAIN_RE.fullmatch(domain):
        raise KaizoraError("Enter a domain such as site.example.com, without https:// or a path.")
    projects_path = projects_path.expanduser().resolve()
    projects_path_existed = projects_path.is_dir()
    projects_path.mkdir(parents=True, exist_ok=True)
    if not projects_path_existed:
        projects_path.chmod(0o755)
    target = projects_path / slug
    if target.exists() or target.is_symlink():
        raise KaizoraError(f"A project named {slug} already exists.")

    staging = Path(tempfile.mkdtemp(prefix=".kaizora-project-", dir=projects_path))
    manifest: dict[str, Any] = {
        "name": slug,
        "display_name": display_name,
        "runtime": "static",
        "domain": domain or None,
        "resources": {"cpu": 0.25, "memory": "256m"},
        "health": {"path": "/", "timeout": 10, "port": 80},
        "backup": {"project": True, "database": True, "volumes": True},
    }
    compose = f"""services:
  web:
    image: nginx:alpine
    container_name: kz-{slug}-web
    restart: unless-stopped
    expose:
      - \"80\"
    mem_limit: 256m
    cpus: 0.25
    volumes:
      - ./public:/usr/share/nginx/html:ro
    networks:
      - kaizora-network

networks:
  kaizora-network:
    external: true
"""
    try:
        public_path = staging / "public"
        public_path.mkdir()
        index_file = public_path / "index.html"
        index_file.write_text(
            "<!doctype html>\n<html lang=\"id\"><head><meta charset=\"utf-8\"><title>"
            + display_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            + "</title></head><body><h1>"
            + display_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            + "</h1><p>Website Kaizora Hosting siap diisi.</p></body></html>\n",
            encoding="utf-8",
        )
        public_path.chmod(0o755)
        index_file.chmod(0o644)
        (staging / "compose.yaml").write_text(compose, encoding="utf-8")
        (staging / "kaizora.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        staging.chmod(0o755)
        staging.replace(target)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise KaizoraError("Could not create the project folder.") from exc
    return Project(target, manifest | {"slug": slug, "branch": None, "repository": None, "compose_file": "compose.yaml"}, target / "compose.yaml")


def create_git_project(
    projects_path: Path, display_name: str, repository: str, branch: str, domain: str = ""
) -> Project:
    display_name = display_name.strip()
    repository = repository.strip()
    branch = branch.strip()
    if not display_name or len(display_name) > 100:
        raise KaizoraError("Enter a project name from 1 to 100 characters.")
    if not repository or any(char.isspace() or ord(char) < 32 for char in repository) or "?" in repository or "#" in repository:
        raise KaizoraError("Enter a public Git repository URL without embedded credentials.")
    try:
        parsed = urlsplit(repository)
        if parsed.scheme not in {"https", "ssh"} or not parsed.hostname or not parsed.path.strip("/"):
            raise ValueError
        if parsed.username not in (None, "git") or parsed.password or parsed.query or parsed.fragment:
            raise ValueError
    except ValueError:
        raise KaizoraError("Use a HTTPS or SSH Git URL without a username token or password.") from None
    if not branch or any(char.isspace() or ord(char) < 32 for char in branch):
        raise KaizoraError("Enter a Git branch such as main.")
    domain = domain.strip().lower().rstrip(".")
    if domain and not DOMAIN_RE.fullmatch(domain):
        raise KaizoraError("Enter a domain such as site.example.com, without https:// or a path.")
    slug = _slugify(display_name)
    projects_path = projects_path.expanduser().resolve()
    projects_path.mkdir(parents=True, exist_ok=True)
    target = projects_path / slug
    if target.exists() or target.is_symlink():
        raise KaizoraError(f"A project named {slug} already exists.")
    git = shutil.which("git")
    if not git:
        raise KaizoraError("Git is not installed on this node.")
    try:
        valid_branch = subprocess.run(
            [git, "check-ref-format", "--branch", branch], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=10,
        )
        if valid_branch.returncode:
            raise KaizoraError("The Git branch name is not valid.")
        result = subprocess.run(
            [git, "clone", "--depth", "1", "--single-branch", "--branch", branch, "--", repository, str(target)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=180,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise KaizoraError("Git clone timed out. Check the repository URL and node internet access.") from exc
    except OSError as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise KaizoraError("Could not run Git on this node.") from exc
    if result.returncode:
        shutil.rmtree(target, ignore_errors=True)
        raise KaizoraError("Could not clone that repository and branch. Check that the repository is reachable by this node.")
    manifest_path = target / "kaizora.json"
    local_manifest_path = target / ".kaizora.local.json"
    try:
        if manifest_path.is_symlink():
            raise KaizoraError("Repository kaizora.json cannot be a symbolic link.")
        if manifest_path.exists():
            current = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(current, dict):
                raise KaizoraError("Repository kaizora.json must contain a JSON object.")
            local = {"repository": repository, "branch": branch}
            if domain:
                local["domain"] = domain
        else:
            local = {
                "name": slug,
                "display_name": display_name,
                "slug": slug,
                "runtime": "docker",
                "repository": repository,
                "branch": branch,
                "domain": domain or None,
                "resources": {"cpu": 1.0, "memory": "1g"},
                "health": {"path": "/", "timeout": 10},
                "backup": {"project": True, "database": True, "volumes": True},
            }
        local_manifest_path.write_text(json.dumps(local, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        _exclude_local_project_config(target)
    except KaizoraError:
        shutil.rmtree(target, ignore_errors=True)
        raise
    except (OSError, json.JSONDecodeError) as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise KaizoraError("Could not prepare the project manifest from this repository.") from exc
    from .manifest import load_project

    try:
        return load_project(projects_path, slug)
    except KaizoraError:
        shutil.rmtree(target, ignore_errors=True)
        raise


def create_project_folder(project: Project, relative: str, name: str) -> Path:
    if not name or name in {".", ".."} or "/" in name or "\\" in name or any(ord(c) < 32 for c in name):
        raise KaizoraError("Enter a folder name without slashes.")
    destination = safe_project_path(project, str(PurePosixPath(relative) / name) if relative else name)
    try:
        destination.mkdir()
        destination.chmod(0o755)
    except FileExistsError as exc:
        raise KaizoraError("A file or folder with that name already exists.") from exc
    except OSError as exc:
        raise KaizoraError("Could not create this project folder.") from exc
    return destination


def update_project_domain(project: Project, domain: str) -> None:
    domain = domain.strip().lower().rstrip(".")
    if domain and not DOMAIN_RE.fullmatch(domain):
        raise KaizoraError("Enter a domain such as site.example.com, without https:// or a path.")
    local_path = project.path / ".kaizora.local.json"
    try:
        if local_path.is_symlink():
            raise KaizoraError("Refusing to replace a linked local project settings file.")
        data = {
            key: project.manifest.get(key)
            for key in (
                "name", "display_name", "slug", "runtime", "repository", "branch", "compose_file",
                "resources", "health", "backup",
            )
            if key in project.manifest
        }
        data["domain"] = domain or None
        temporary = local_path.with_name(".kaizora.local.json.tmp")
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(local_path)
        _exclude_local_project_config(project.path)
    except KaizoraError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise KaizoraError("Could not update the project domain.") from exc


def _database_files(node: NodeConfig, project: Project) -> tuple[Path, Path]:
    directory = node.databases_path / project.slug
    try:
        ensure_within(directory, node.databases_path, "Database path")
    except KaizoraError:
        raise
    return directory, directory / ".env"


def _atomic_secret(path: Path, content: str) -> None:
    if path.parent.is_symlink():
        raise KaizoraError("Refusing to write a secret inside a linked directory.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise KaizoraError("Refusing to replace a linked secret file.")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".secret-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
        temporary.replace(path)
        path.chmod(0o600)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise KaizoraError("Could not save the protected credentials file.") from exc


def _docker_compose(directory: Path, arguments: list[str]) -> None:
    docker = shutil.which("docker")
    if not docker:
        raise KaizoraError("Docker is not installed on this node.")
    command = [
        docker, "compose", "--project-name", f"kzdb-{directory.name}",
        "--project-directory", str(directory), "--file", str(directory / "compose.yaml"), *arguments,
    ]
    try:
        result = subprocess.run(
            command, cwd=directory, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise KaizoraError("Database operation timed out; check the database status before retrying.") from exc
    except OSError as exc:
        raise KaizoraError("Could not run Docker Compose for the database.") from exc
    if result.returncode:
        raise KaizoraError("Database operation failed. Check Docker and the kaizora-network, then retry.")


def create_database(node: NodeConfig, project: Project, engine: str) -> dict[str, str]:
    if engine not in {"mysql", "mariadb", "postgres"}:
        raise KaizoraError("Choose MySQL, MariaDB, or PostgreSQL.")
    directory, secret_file = _database_files(node, project)
    if directory.exists() or directory.is_symlink():
        raise KaizoraError(f"A managed database already exists for {project.slug}.")
    username = "kz_" + project.slug.replace("-", "_")[:40]
    database = "kaizora_" + project.slug.replace("-", "_")[:40]
    password = secrets.token_urlsafe(32)
    root_password = secrets.token_urlsafe(32)
    env_values = {
        "DB_NAME": database,
        "DB_USER": username,
        "DB_PASSWORD": password,
        "DB_ROOT_PASSWORD": root_password,
    }
    image, data_path = {
        "mysql": ("mysql:8.4", "/var/lib/mysql"),
        "mariadb": ("mariadb:11", "/var/lib/mysql"),
        "postgres": ("postgres:17-alpine", "/var/lib/postgresql/data"),
    }[engine]
    if engine == "postgres":
        environment = """      POSTGRES_DB: ${DB_NAME}
      POSTGRES_USER: ${DB_USER}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
"""
    elif engine == "mariadb":
        environment = """      MARIADB_DATABASE: ${DB_NAME}
      MARIADB_USER: ${DB_USER}
      MARIADB_PASSWORD: ${DB_PASSWORD}
      MARIADB_ROOT_PASSWORD: ${DB_ROOT_PASSWORD}
"""
    else:
        environment = """      MYSQL_DATABASE: ${DB_NAME}
      MYSQL_USER: ${DB_USER}
      MYSQL_PASSWORD: ${DB_PASSWORD}
      MYSQL_ROOT_PASSWORD: ${DB_ROOT_PASSWORD}
"""
    port = 5432 if engine == "postgres" else 3306
    compose = f"""services:
  database:
    image: {image}
    container_name: kz-{project.slug}-db
    restart: unless-stopped
    mem_limit: 512m
    cpus: 0.50
    environment:
{environment}    volumes:
      - database-data:{data_path}
    networks:
      - kaizora-network

volumes:
  database-data:

networks:
  kaizora-network:
    external: true
"""
    try:
        directory.mkdir(parents=True, mode=0o700)
        (directory / "compose.yaml").write_text(compose, encoding="utf-8")
        _atomic_secret(secret_file, "".join(f"{key}={value}\n" for key, value in env_values.items()))
        _docker_compose(directory, ["up", "-d"])
    except Exception:
        # Keep a failed database's protected files for diagnosis/retry, but never print credentials.
        raise
    return {
        "engine": engine,
        "host": f"kz-{project.slug}-db",
        "port": str(port),
        "database": database,
        "username": username,
        "password": password,
    }


def database_info(node: NodeConfig, project: Project) -> dict[str, str] | None:
    directory, secret_file = _database_files(node, project)
    if not secret_file.is_file() or secret_file.is_symlink():
        return None
    try:
        values: dict[str, str] = {}
        for line in secret_file.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        compose = (directory / "compose.yaml").read_text(encoding="utf-8")
        image = re.search(r"^    image: ([^\n]+)$", compose, re.M)
        engine = "postgres" if image and image.group(1).startswith("postgres:") else "mariadb" if image and image.group(1).startswith("mariadb:") else "mysql"
        slug = project.slug
        return {
            "engine": engine,
            "host": f"kz-{slug}-db",
            "port": "5432" if engine == "postgres" else "3306",
            "database": values.get("DB_NAME", ""),
            "username": values.get("DB_USER", ""),
            "password": values.get("DB_PASSWORD", ""),
        }
    except OSError as exc:
        raise KaizoraError("Could not read the managed database configuration.") from exc


def managed_database_status(node: NodeConfig, project: Project) -> str:
    info = database_info(node, project)
    if not info:
        return "not configured"
    docker = shutil.which("docker")
    if not docker:
        return "Docker unavailable"
    try:
        result = subprocess.run(
            [docker, "inspect", "--format", "{{.State.Status}}", f"kz-{project.slug}-db"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False, timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "stopped"


def start_database(node: NodeConfig, project: Project) -> None:
    directory, secret_file = _database_files(node, project)
    if not secret_file.is_file():
        raise KaizoraError("Create a database for this project first.")
    _docker_compose(directory, ["up", "-d"])


def cloudflare_token_configured(infrastructure_path: Path) -> bool:
    env_file = infrastructure_path / "cloudflare" / ".env"
    if env_file.is_symlink() or not env_file.is_file():
        return False
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "CF_TUNNEL_TOKEN" and value.strip():
                return True
    except OSError:
        return False
    return False


def save_cloudflare_token(infrastructure_path: Path, token: str) -> None:
    token = token.strip()
    if not token or any(char.isspace() for char in token) or any(ord(char) < 32 for char in token):
        raise KaizoraError("Paste the Cloudflare Tunnel token as one line.")
    env_file = infrastructure_path / "cloudflare" / ".env"
    if env_file.parent.is_symlink():
        raise KaizoraError("Refusing to save the Cloudflare token inside a linked directory.")
    _atomic_secret(env_file, f"CF_TUNNEL_TOKEN={token}\n")


def start_cloudflare(infrastructure_path: Path) -> None:
    directory = infrastructure_path / "cloudflare"
    if not cloudflare_token_configured(infrastructure_path):
        raise KaizoraError("Save a Cloudflare Tunnel token first.")
    if not (directory / "docker-compose.yml").is_file():
        raise KaizoraError("Cloudflare Tunnel template is missing; update the node installation first.")
    docker = shutil.which("docker")
    if not docker:
        raise KaizoraError("Docker is not installed on this node.")
    try:
        result = subprocess.run(
            [docker, "compose", "--project-directory", str(directory), "--file", str(directory / "docker-compose.yml"), "up", "-d"],
            cwd=directory, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KaizoraError("Could not start Cloudflare Tunnel. Check Docker and network access.") from exc
    if result.returncode:
        raise KaizoraError("Cloudflare Tunnel did not start. Check the token in Cloudflare Zero Trust and Docker status.")
