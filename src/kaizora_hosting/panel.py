"""Local-only browser control panel for a Kaizora Hosting node."""

from __future__ import annotations

import html
import mimetypes
import secrets
import subprocess
import sys
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from . import cli as engine
from . import panel_admin as admin
from .errors import KaizoraError
from .manifest import discover_projects, load_project


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
PANEL_CSS = """
:root{color-scheme:dark;--bg:#0a1020;--panel:#111a2c;--panel2:#17233a;--line:#263550;--text:#edf3ff;--muted:#91a1ba;--blue:#65a7ff;--green:#43d9a3;--red:#ff7185;--amber:#ffca70}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
a{color:var(--blue);text-decoration:none}button{font:inherit}.layout{min-height:100vh;display:grid;grid-template-columns:230px 1fr}
aside{padding:28px 18px;background:#0d1628;border-right:1px solid var(--line)}.brand{font-size:20px;font-weight:750;letter-spacing:.2px;margin:0 10px 4px}.brand span{color:var(--blue)}.subbrand{color:var(--muted);font-size:12px;margin:0 10px 34px}
nav a{display:block;padding:11px 12px;border-radius:9px;color:#becbe0;margin:4px 0}nav a.active,nav a:hover{background:#192844;color:#fff}.aside-note{font-size:12px;color:var(--muted);margin:38px 10px 0}
main{padding:34px clamp(18px,4vw,54px);max-width:1500px;width:100%}.topline{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:28px}.eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:1.4px;color:var(--blue);font-weight:700}.title{font-size:29px;line-height:1.2;margin:5px 0}.subtitle{color:var(--muted);margin:5px 0 0}.local-pill,.status{border:1px solid var(--line);background:var(--panel);border-radius:999px;padding:6px 11px;color:var(--muted);font-size:12px;white-space:nowrap}
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:20px 0 8px}.metric,.service,.project,.empty,.output{background:var(--panel);border:1px solid var(--line);border-radius:13px}.metric{padding:17px}.metric-label{color:var(--muted);font-size:12px}.metric-value{font-size:21px;font-weight:700;margin-top:8px;overflow-wrap:anywhere}.metric-note{color:var(--muted);font-size:12px;margin:0 0 28px}.section-head{display:flex;justify-content:space-between;align-items:end;margin:30px 0 13px}.section-head h2{font-size:18px;margin:0}.section-head p{font-size:12px;color:var(--muted);margin:0}
.services{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.service{padding:16px;display:flex;justify-content:space-between;align-items:center}.service-name{color:var(--muted);font-size:13px}.service-value{font-weight:650;margin-top:2px}.good{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--amber)}
.projects{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px}.project{padding:19px}.project-head{display:flex;justify-content:space-between;gap:10px;align-items:start}.project h3{margin:0;font-size:17px}.slug{color:var(--muted);font-size:12px;margin-top:3px}.domain{margin:15px 0;color:#c6d5ec;overflow-wrap:anywhere}.project-meta{display:flex;gap:18px;color:var(--muted);font-size:12px;flex-wrap:wrap}.actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:17px}.actions form{margin:0}.btn{background:#1c2b45;border:1px solid #314766;border-radius:7px;color:#eaf2ff;padding:7px 10px;cursor:pointer;font-size:12px}.btn:hover{border-color:var(--blue);background:#233b5e}.btn.primary{background:#2264b8;border-color:#347dd9}.btn.danger{color:#ffb4bf}.btn.link{display:inline-block}.empty{padding:28px;text-align:center;color:var(--muted)}.empty strong{display:block;color:var(--text);font-size:16px;margin-bottom:5px}
.form-card,.file-list,.info-card{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:20px;margin:15px 0}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.field{display:flex;flex-direction:column;gap:6px;margin:0 0 14px}.field label{font-size:13px;color:#c6d5ec}.field input,.field select,.field textarea{width:100%;background:#0b1425;color:var(--text);border:1px solid #314766;border-radius:7px;padding:10px;font:inherit}.field textarea{min-height:55vh;font-family:ui-monospace,monospace;font-size:13px}.hint{color:var(--muted);font-size:12px;margin:4px 0 14px}.file-row{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:10px;align-items:center;border-bottom:1px solid var(--line);padding:10px 0}.file-row:last-child{border-bottom:0}.file-actions{display:flex;gap:7px}.table-note{color:var(--muted);font-size:12px}.code-box{display:block;white-space:pre-wrap;overflow-wrap:anywhere;background:#08111f;border:1px solid var(--line);border-radius:8px;padding:14px;margin:10px 0}.danger-text{color:var(--amber)}
.flash{padding:13px 15px;border:1px solid var(--line);border-radius:9px;margin-bottom:16px;white-space:pre-wrap;overflow-wrap:anywhere}.flash.success{border-color:#276e5c;color:#b1f3dc;background:#102921}.flash.error{border-color:#733b47;color:#ffc2cc;background:#2b171e}.output{padding:18px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;color:#d5e1f3}.foot{color:var(--muted);font-size:12px;margin-top:30px}
@media(max-width:900px){.layout{grid-template-columns:1fr}aside{padding:15px 18px;border-right:0;border-bottom:1px solid var(--line)}.subbrand{margin-bottom:12px}nav{display:flex;gap:5px;overflow:auto}.aside-note{display:none}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:600px){main{padding:24px 15px}.topline{align-items:start;flex-direction:column}.projects,.services{grid-template-columns:1fr}.metric-value{font-size:17px}}
"""


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def is_local_host_header(value: str, port: int) -> bool:
    try:
        parsed = urlsplit("//" + value)
        return parsed.hostname in LOCAL_HOSTS and parsed.port in (None, port)
    except ValueError:
        return False


def is_local_origin(value: str, port: int) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS and parsed.port == port
    except ValueError:
        return False


def cli_command(root: Path, action: str, slug: str, *extra: str) -> list[str]:
    if action not in {"deploy", "start", "stop", "restart", "backup", "logs", "backups"}:
        raise KaizoraError("Panel action is not supported.")
    project = load_project(engine.load_node_config(root).projects_path, slug)
    entrypoint = Path(__file__).resolve().parents[2] / "bin" / "kz"
    if not entrypoint.is_file():
        raise KaizoraError("Could not locate the installed kz command.")
    return [sys.executable, str(entrypoint), "--root", str(root), action, project.slug, *extra]


def run_cli(root: Path, action: str, slug: str, *extra: str, timeout: int = 1800) -> tuple[int, str]:
    command = cli_command(root, action, slug, *extra)
    try:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise KaizoraError(f"{action.capitalize()} timed out; check the project status before retrying.") from exc
    except OSError as exc:
        raise KaizoraError(f"Could not run {action}: {exc}") from exc
    return result.returncode, result.stdout.strip()


def _status_class(value: str) -> str:
    normalized = value.lower()
    if normalized in {"healthy", "running", "configured"}:
        return "good"
    if normalized in {"unhealthy", "stopped", "unavailable"}:
        return "bad"
    return "warn"


def _layout(title: str, body: str, *, active: str = "Dashboard", notice: str = "", kind: str = "success") -> str:
    nav = "".join(
        f'<a class="{"active" if name == active else ""}" href="{href}">{name}</a>'
        for name, href in (("Dashboard", "/"), ("Projects", "/#projects"), ("Cloudflare", "/cloudflare"))
    )
    flash = f'<div class="flash {"error" if kind == "error" else "success"}">{_esc(notice)}</div>' if notice else ""
    return f"""<!doctype html>
<html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)} · Kaizora Hosting</title><style>{PANEL_CSS}</style></head>
<body><div class="layout"><aside><p class="brand">KAIZORA <span>HOSTING</span></p><p class="subbrand">Node control panel</p><nav>{nav}</nav><p class="aside-note">Panel lokal untuk node ini. Jangan publikasikan melalui tunnel.</p></aside>
<main><div class="topline"><div><div class="eyebrow">Kaizora Hosting · KZ-HOME-01</div><h1 class="title">{_esc(title)}</h1><p class="subtitle">Kelola layanan hosting dari satu tempat.</p></div><span class="local-pill">● &nbsp; Akses lokal</span></div>{flash}{body}
<p class="foot">Panel ini hanya menerima koneksi dari PC node. Akses internet dan multi-user belum diaktifkan.</p></main></div></body></html>"""


def render_dashboard(root: Path, token: str, *, notice: str = "", kind: str = "success") -> str:
    node = engine.load_node_config(root)
    projects, discovery_errors = discover_projects(node.projects_path)
    docker_version, docker_issue = engine.docker_info()
    docker_ready = bool(docker_version)
    rows_by_slug, row_errors = engine._project_rows(projects, docker_ready)
    stats = engine.docker_stats() if docker_ready else {}

    cloudflare = "not configured"
    if admin.cloudflare_token_configured(node.infrastructure_path):
        cloudflare = "stopped"
    if docker_ready and cloudflare != "not configured":
        try:
            inspected = engine.run_capture([engine.docker_path(), "inspect", "kaizora-cloudflared"], timeout=5)
            if inspected.returncode == 0:
                detail = engine._container_inspect("kaizora-cloudflared") or {}
                state = detail.get("State", {})
                cloudflare = "running" if state.get("Running") else str(state.get("Status") or "stopped")
        except KaizoraError:
            cloudflare = "unknown"

    running = sum(
        engine._project_health(project, rows_by_slug.get(project.slug, []), docker_ready)
        in {"healthy", "running", "starting"}
        for project in projects
    )
    windows_drives = engine._windows_drive_paths()
    if windows_drives:
        disk_metrics = [
            (f"Disk Windows {drive.name.upper()}:", engine._host_disk(drive)) for drive in windows_drives
        ]
        disk_note = "Angka menunjukkan kapasitas tiap drive Windows. Data Kali/WSL memakai ruang pada drive tempat distro tersimpan."
    else:
        disk_path = engine._host_disk_path(node.config_path.parent)
        disk_metrics = [("Disk node", engine._host_disk(disk_path))]
        disk_note = "RAM dan disk menunjukkan kapasitas yang terlihat oleh node ini."
    metrics = "".join(
        f'<div class="metric"><div class="metric-label">{_esc(label)}</div><div class="metric-value">{_esc(value)}</div></div>'
        for label, value in [
            ("Projects", len(projects)),
            ("Running", running),
            ("RAM Kali/WSL", engine._host_memory()),
            *disk_metrics,
        ]
    )
    services = "".join(
        f'<div class="service"><div><div class="service-name">{_esc(name)}</div><div class="service-value {_status_class(status)}">{_esc(status)}</div></div><span class="status">{_esc(detail)}</span></div>'
        for name, status, detail in (
            ("Docker Engine", "healthy" if docker_ready else "unhealthy", docker_version or docker_issue or "unavailable"),
            ("Cloudflare Tunnel", cloudflare, "container status" if cloudflare not in {"not configured", "unknown"} else "belum disiapkan"),
        )
    )

    if projects:
        cards: list[str] = []
        for project in projects:
            state = "unknown" if project.slug in row_errors else engine._project_health(
                project, rows_by_slug.get(project.slug, []), docker_ready
            )
            memory = engine.memory_for(project, stats)
            actions = []
            for action, label, style in (
                ("deploy", "Deploy", "primary"),
                ("start", "Start", ""),
                ("stop", "Stop", "danger"),
                ("restart", "Restart", ""),
                ("backup", "Backup", ""),
            ):
                actions.append(
                    f'<form method="post" action="/action"><input type="hidden" name="csrf" value="{_esc(token)}">'
                    f'<input type="hidden" name="project" value="{_esc(project.slug)}">'
                    f'<input type="hidden" name="action" value="{action}"><button class="btn {style}" type="submit">{label}</button></form>'
                )
            safe_slug = _esc(project.slug)
            file_path = "public" if project.manifest.get("runtime") == "static" else ""
            files_url = f"/files?project={quote(project.slug)}&amp;path={quote(file_path)}"
            cards.append(
                f'<article class="project"><div class="project-head"><div><h3>{_esc(project.title)}</h3>'
                f'<div class="slug">{safe_slug}</div></div><span class="status {_status_class(state)}">{_esc(state)}</span></div>'
                f'<div class="domain">{_esc(project.domain)}</div><div class="project-meta"><span>RAM { _esc(memory) }</span>'
                f'<span>Runtime {_esc(project.manifest.get("runtime", "docker"))}</span></div><div class="actions">{"".join(actions)}'
                f'<a class="btn link" href="{files_url}">File Manager</a>'
                f'<a class="btn link" href="/domain?project={quote(project.slug)}">Domain</a>'
                f'<a class="btn link" href="/database?project={quote(project.slug)}">Database</a>'
                f'<a class="btn link" href="/logs?project={safe_slug}">Logs</a>'
                f'<a class="btn link" href="/backups?project={safe_slug}">Snapshots</a></div></article>'
            )
        project_content = f'<div class="projects">{"".join(cards)}</div>'
    else:
        project_content = (
            f'<div class="empty"><strong>Belum ada project</strong>Buat project pertama dari panel ini, lalu unggah file atau deploy dari Git.'
            f'<p><a class="btn primary" href="/projects/new">Tambah Project</a></p></div>'
        )

    warnings = "".join(f'<div class="flash error">{_esc(message)}</div>' for message in [*discovery_errors, *row_errors.values()])
    if not docker_ready:
        warnings += f'<div class="flash error">Docker belum siap: {_esc(docker_issue or "unknown error")}</div>'
    body = (
        f'<section class="metrics">{metrics}</section><p class="metric-note">{_esc(disk_note)}</p>'
        f'<div class="section-head"><h2>Services</h2><p>{_esc(node.name)} · {_esc(node.environment)}</p></div>'
        f'<section class="services">{services}</section><div class="section-head" id="projects"><div><h2>Projects</h2><p>{len(projects)} terdaftar · {running} berjalan</p></div>'
        f'<a class="btn primary" href="/projects/new">Tambah Project</a></div>'
        f'{warnings}{project_content}'
    )
    return _layout("Dashboard", body, notice=notice, kind=kind)


def _hidden_fields(token: str, action: str, slug: str = "") -> str:
    project = f'<input type="hidden" name="project" value="{_esc(slug)}">' if slug else ""
    return (
        f'<input type="hidden" name="csrf" value="{_esc(token)}">'
        f'<input type="hidden" name="action" value="{_esc(action)}">{project}'
    )


def render_new_project_page(root: Path, token: str) -> str:
    body = f"""<div class="section-head"><div><h2>Website statis</h2><p>Mulai dari HTML/CSS/JS, lalu unggah file lewat File Manager.</p></div></div>
<form class="form-card" method="post" action="/action">
{_hidden_fields(token, "create_static_project")}
<div class="form-grid"><div class="field"><label>Nama project</label><input name="name" required maxlength="100" placeholder="Website Kaizora"></div>
<div class="field"><label>Domain (opsional)</label><input name="domain" placeholder="site.kaizoratech.com"></div></div>
<p class="hint">Panel membuat folder file website, konfigurasi Docker, dan halaman awal. Domain publik perlu diarahkan melalui Cloudflare Tunnel setelah disiapkan.</p>
<button class="btn primary" type="submit">Buat website</button></form>
<div class="section-head"><div><h2>Project dari Git</h2><p>Untuk aplikasi yang sudah memiliki Docker Compose.</p></div></div>
<form class="form-card" method="post" action="/action">
{_hidden_fields(token, "create_git_project")}
<div class="form-grid"><div class="field"><label>Nama project</label><input name="name" required maxlength="100" placeholder="Kaizora Lean"></div>
<div class="field"><label>Domain (opsional)</label><input name="domain" placeholder="lean.kaizoratech.com"></div></div>
<div class="form-grid"><div class="field"><label>Git repository</label><input name="repository" required placeholder="https://github.com/organisasi/project.git"></div>
<div class="field"><label>Branch</label><input name="branch" required value="main"></div></div>
<p class="hint">Repository harus menyertakan file Docker Compose dan dapat diakses dari node. Jangan masukkan token/password ke URL repository.</p>
<button class="btn primary" type="submit">Hubungkan project</button></form>"""
    return _layout("Tambah Project", body, active="Projects")


def render_files_page(root: Path, token: str, slug: str, relative: str = "", *, notice: str = "", kind: str = "success") -> str:
    node = engine.load_node_config(root)
    project = load_project(node.projects_path, slug)
    directory, entries = admin.list_project_files(project, relative)
    current = directory.relative_to(project.path).as_posix()
    if current == ".":
        current = ""
    parent = PurePosixPath(current).parent.as_posix() if current else ""
    if parent == ".":
        parent = ""
    project_q = quote(project.slug)
    path_q = quote(current)
    rows: list[str] = []
    if current:
        rows.append(
            f'<div class="file-row"><a href="/files?project={project_q}&amp;path={quote(parent)}">↰ Kembali</a>'
            '<span class="table-note">Folder induk</span><span></span></div>'
        )
    for name, is_dir, size in entries:
        item_path = f"{current}/{name}" if current else name
        encoded = quote(item_path)
        if is_dir:
            rows.append(
                f'<div class="file-row"><a href="/files?project={project_q}&amp;path={encoded}">📁 {_esc(name)}/</a>'
                '<span class="table-note">Folder</span><span></span></div>'
            )
        else:
            size_label = f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} B"
            rows.append(
                f'<div class="file-row"><span>📄 {_esc(name)}</span><span class="table-note">{size_label}</span>'
                '<span class="file-actions">'
                f'<a class="btn link" href="/file?project={project_q}&amp;path={encoded}">Edit</a>'
                f'<a class="btn link" href="/download?project={project_q}&amp;path={encoded}">Unduh</a>'
                f'<form method="post" action="/action" onsubmit="return confirm(\'Hapus file ini?\')">'
                f'{_hidden_fields(token, "delete_file", project.slug)}<input type="hidden" name="path" value="{_esc(item_path)}">'
                '<button class="btn danger" type="submit">Hapus</button></form></span></div>'
            )
    listing = "".join(rows) if rows else '<p class="hint">Folder ini belum berisi file.</p>'
    body = f"""<div class="section-head"><div><h2>{_esc(project.title)} · File Manager</h2><p>Folder: /{_esc(current)}</p></div><a href="/">Kembali</a></div>
<form class="form-card" method="post" action="/action">
{_hidden_fields(token, "create_folder", project.slug)}<input type="hidden" name="path" value="{_esc(current)}">
<div class="form-grid"><div class="field"><label>Nama folder baru</label><input name="name" required placeholder="assets"></div><div class="field" style="align-self:end"><button class="btn" type="submit">Buat folder</button></div></div></form>
<form class="form-card" method="post" action="/action" enctype="multipart/form-data">
{_hidden_fields(token, "upload_file", project.slug)}<input type="hidden" name="path" value="{_esc(current)}">
<div class="field"><label>Unggah file (maksimal 20 MB)</label><input type="file" name="file" required></div><button class="btn primary" type="submit">Unggah</button></form>
<div class="file-list">{listing}</div>
<p class="hint">File rahasia seperti .env, token, private key, dan folder .git disembunyikan dari File Manager.</p>"""
    return _layout("File Manager", body, active="Projects", notice=notice, kind=kind)


def render_edit_file_page(root: Path, token: str, slug: str, relative: str, *, notice: str = "", kind: str = "success") -> str:
    project = load_project(engine.load_node_config(root).projects_path, slug)
    content = admin.read_project_text(project, relative)
    body = f"""<div class="section-head"><div><h2>Edit {_esc(relative)}</h2><p>{_esc(project.title)}</p></div>
<a href="/files?project={quote(project.slug)}&amp;path={quote(PurePosixPath(relative).parent.as_posix() if PurePosixPath(relative).parent.as_posix() != '.' else '')}">Kembali ke File Manager</a></div>
<form class="form-card" method="post" action="/action">
{_hidden_fields(token, "save_file", project.slug)}<input type="hidden" name="path" value="{_esc(relative)}">
<div class="field"><label>Isi file UTF-8 (maksimal 1 MB)</label><textarea name="content" spellcheck="false">{_esc(content)}</textarea></div>
<button class="btn primary" type="submit">Simpan perubahan</button></form>"""
    return _layout("Edit File", body, active="Projects", notice=notice, kind=kind)


def render_domain_page(root: Path, token: str, slug: str, *, notice: str = "", kind: str = "success") -> str:
    node = engine.load_node_config(root)
    project = load_project(node.projects_path, slug)
    target = f"http://kz-{project.slug}-web:80" if project.manifest.get("runtime") == "static" else "alamat-container:port-internal"
    body = f"""<div class="section-head"><div><h2>Domain · {_esc(project.title)}</h2><p>Hostname untuk project ini.</p></div><a href="/">Kembali</a></div>
<form class="form-card" method="post" action="/action">
{_hidden_fields(token, "save_domain", project.slug)}
<div class="field"><label>Domain</label><input name="domain" value="{_esc(project.domain if project.domain != '-' else '')}" placeholder="site.kaizoratech.com"></div>
<p class="hint">Simpan domain di panel. Agar bisa dibuka dari internet, hostname ini juga harus ditambahkan di Cloudflare Zero Trust → Tunnels → Public Hostnames.</p>
<button class="btn primary" type="submit">Simpan domain</button></form>
<div class="info-card"><h3>Target layanan di Docker</h3><code class="code-box">{_esc(target)}</code>
<p class="hint">Gunakan target ini saat membuat Public Hostname di Cloudflare. Jangan buka port router. Untuk project Git, sesuaikan nama container dan port internal dengan Compose project.</p>
<a href="/cloudflare">Buka setup Cloudflare Tunnel</a></div>"""
    return _layout("Domain", body, active="Projects", notice=notice, kind=kind)


def render_database_page(root: Path, token: str, slug: str, *, notice: str = "", kind: str = "success") -> str:
    node = engine.load_node_config(root)
    project = load_project(node.projects_path, slug)
    info = admin.database_info(node, project)
    if info:
        db_status = admin.managed_database_status(node, project)
        details = "\n".join(
            (
                f"Engine: {info['engine']}", f"Host: {info['host']}", f"Port: {info['port']}",
                f"Database: {info['database']}", f"Username: {info['username']}", f"Password: {info['password']}",
            )
        )
        content = (
            f'<div class="info-card"><h3>Database { _esc(info["engine"]).upper() } · '
            f'<span class="{_status_class(db_status)}">{_esc(db_status)}</span></h3>'
            f'<p>Status container: <span class="{_status_class(db_status)}">{_esc(db_status)}</span>. Credential tersimpan dengan permission 0600 di node.</p>'
            f'<code class="code-box">{_esc(details)}</code>'
            f'<form method="post" action="/action">{_hidden_fields(token, "start_database", project.slug)}'
            '<button class="btn" type="submit">Jalankan / mulai ulang database</button></form>'
            '<p class="hint">Database hanya dapat diakses container pada kaizora-network. Jangan publikasikan port database.</p></div>'
        )
    else:
        content = f"""<form class="form-card" method="post" action="/action">
{_hidden_fields(token, "create_database", project.slug)}
<div class="field"><label>Jenis database</label><select name="engine"><option value="mysql">MySQL</option><option value="mariadb">MariaDB</option><option value="postgres">PostgreSQL</option></select></div>
<p class="hint">Panel membuat database privat untuk project ini, membuat password acak, dan menyimpan credential di file lokal permission 0600. Port database tidak dibuka ke internet.</p>
<button class="btn primary" type="submit">Buat database</button></form>"""
    body = f'<div class="section-head"><div><h2>Database · {_esc(project.title)}</h2><p>Database project di jaringan Docker privat.</p></div><a href="/">Kembali</a></div>{content}'
    return _layout("Database", body, active="Projects", notice=notice, kind=kind)


def render_cloudflare_page(root: Path, token: str, *, notice: str = "", kind: str = "success") -> str:
    node = engine.load_node_config(root)
    configured = admin.cloudflare_token_configured(node.infrastructure_path)
    tunnel_status = "Token tersimpan" if configured else "Belum dikonfigurasi"
    if configured:
        try:
            result = engine.run_capture([engine.docker_path(), "inspect", "kaizora-cloudflared"], timeout=5)
            if result.returncode == 0:
                detail = engine._container_inspect("kaizora-cloudflared") or {}
                tunnel_status = "Container berjalan" if detail.get("State", {}).get("Running") else "Container berhenti"
        except KaizoraError:
            tunnel_status = "Docker belum tersedia"
    projects, _ = discover_projects(node.projects_path)
    hosts = "".join(
        f'<li><strong>{_esc(project.domain)}</strong> → '
        + (f'<code>{_esc(f"http://kz-{project.slug}-web:80")}</code>' if project.manifest.get("runtime") == "static"
           else '<span class="table-note">target container/port mengikuti Compose project</span>')
        + "</li>"
        for project in projects if project.domain != "-"
    ) or "<li>Belum ada domain project.</li>"
    token_form = f"""<form class="form-card" method="post" action="/action">
{_hidden_fields(token, "configure_cloudflare")}
<div class="field"><label>Cloudflare Tunnel token</label><input type="password" name="tunnel_token" required autocomplete="new-password" spellcheck="false"></div>
<p class="hint">Token ditulis hanya ke {_esc(node.infrastructure_path / "cloudflare" / ".env")} dengan permission 0600, lalu tunnel dijalankan. Token tidak ditampilkan atau dimasukkan ke URL.</p>
<button class="btn primary" type="submit">Simpan token dan jalankan tunnel</button></form>"""
    if configured:
        token_form = f"""<div class="info-card"><p>Token tunnel sudah tersimpan. Jika perlu mengganti token, masukkan token baru di bawah. Isian kosong tidak mengubah token lama.</p>
<form method="post" action="/action">{_hidden_fields(token, "start_cloudflare")}<button class="btn" type="submit">Jalankan / mulai ulang tunnel</button></form></div>
<form class="form-card" method="post" action="/action">{_hidden_fields(token, "configure_cloudflare")}
<div class="field"><label>Ganti token (opsional)</label><input type="password" name="tunnel_token" autocomplete="new-password" spellcheck="false"></div>
<button class="btn" type="submit">Simpan token baru dan jalankan</button></form>"""
    body = f"""<div class="section-head"><div><h2>Cloudflare Tunnel</h2><p>Jalur aman dari internet ke layanan di Docker.</p></div><a href="/">Dashboard</a></div>
<div class="info-card"><h3>Status: {_esc(tunnel_status)}</h3><p>Panel tidak membuka port router dan tidak menampilkan nilai token.</p>
<ol><li>Di Cloudflare Zero Trust, buat named tunnel untuk node ini.</li><li>Pilih Docker sebagai connector, lalu salin token yang diberikan Cloudflare.</li><li>Tempel token ke form lokal di bawah.</li><li>Tambahkan Public Hostname untuk setiap domain dan arahkan ke target container yang ditampilkan di halaman Domain.</li></ol>
<p class="danger-text">Jangan kirim token ke chat, Git, atau screenshot.</p></div>
{token_form}<div class="info-card"><h3>Hostname project</h3><ul>{hosts}</ul>
<p class="hint">Simpan domain di Kaizora tidak otomatis membuat DNS route Cloudflare. Public Hostname tetap perlu dibuat pada akun Cloudflare milikmu.</p></div>"""
    return _layout("Cloudflare Tunnel", body, active="Cloudflare", notice=notice, kind=kind)


def render_output_page(root: Path, token: str, title: str, slug: str, action: str, *, notice: str = "", kind: str = "success") -> str:
    node = engine.load_node_config(root)
    project = load_project(node.projects_path, slug)
    if action not in {"logs", "backups"}:
        raise KaizoraError("Panel page is not supported.")
    code, output = run_cli(root, action, project.slug, "--tail", "200") if action == "logs" else run_cli(root, action, project.slug)
    result_kind = "error" if code else kind
    result_notice = notice or ("Perintah gagal; periksa hasil di bawah." if code else "")
    body = (
        f'<div class="section-head"><h2>{_esc(project.title)} · {_esc(action.title())}</h2>'
        f'<a href="/">Kembali ke dashboard</a></div><pre class="output">{_esc(output or "Tidak ada keluaran.")}</pre>'
    )
    return _layout(title, body, active="Projects", notice=result_notice, kind=result_kind)


def _parse_multipart(content_type: str, body: bytes) -> dict[str, str | bytes]:
    envelope = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii", errors="strict")
        + body
    )
    message = BytesParser(policy=policy.default).parsebytes(envelope)
    if not message.is_multipart():
        raise ValueError("Multipart form boundary is missing.")
    values: dict[str, str | bytes] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            values[f"{name}_name"] = Path(filename.replace("\\", "/")).name
            values[f"{name}_bytes"] = payload
        else:
            charset = part.get_content_charset() or "utf-8"
            values[str(name)] = payload.decode(charset, errors="strict")
    return values


def _value(values: dict[str, str | bytes], key: str, default: str = "") -> str:
    item = values.get(key, default)
    return item if isinstance(item, str) else default


def _redirect(handler: BaseHTTPRequestHandler, location: str) -> None:
    handler.send_response(303)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()


def _response(handler: BaseHTTPRequestHandler, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
    handler.end_headers()
    handler.wfile.write(data)


def make_handler(root: Path, token: str):
    class PanelHandler(BaseHTTPRequestHandler):
        server_version = "KaizoraPanel"
        sys_version = ""

        def _host_allowed(self) -> bool:
            return is_local_host_header(self.headers.get("Host", ""), self.server.server_port)

        def _deny_remote_host(self) -> bool:
            if self._host_allowed():
                return False
            _response(self, 421, "Local access only.", "text/plain; charset=utf-8")
            return True

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self._deny_remote_host():
                return
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            notice = query.get("notice", [""])[0][:4000]
            kind = "error" if query.get("kind", [""])[0] == "error" else "success"
            try:
                if parsed.path == "/":
                    page = render_dashboard(root, token, notice=notice, kind=kind)
                elif parsed.path == "/projects/new":
                    page = render_new_project_page(root, token)
                elif parsed.path == "/files":
                    page = render_files_page(
                        root, token, query.get("project", [""])[0], query.get("path", [""])[0],
                        notice=notice, kind=kind,
                    )
                elif parsed.path == "/file":
                    page = render_edit_file_page(
                        root, token, query.get("project", [""])[0], query.get("path", [""])[0],
                        notice=notice, kind=kind,
                    )
                elif parsed.path == "/domain":
                    page = render_domain_page(root, token, query.get("project", [""])[0], notice=notice, kind=kind)
                elif parsed.path == "/database":
                    page = render_database_page(root, token, query.get("project", [""])[0], notice=notice, kind=kind)
                elif parsed.path == "/cloudflare":
                    page = render_cloudflare_page(root, token, notice=notice, kind=kind)
                elif parsed.path == "/download":
                    node = engine.load_node_config(root)
                    project = load_project(node.projects_path, query.get("project", [""])[0])
                    relative = query.get("path", [""])[0]
                    target = admin.safe_project_path(project, relative)
                    if not target.is_file() or target.stat().st_size > admin.MAX_UPLOAD_BYTES:
                        raise KaizoraError("That file cannot be downloaded from the panel.")
                    data = target.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                    self.send_header(
                        "Content-Disposition",
                        f"attachment; filename=\"download\"; filename*=UTF-8''{quote(target.name, safe='')}",
                    )
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                elif parsed.path in {"/logs", "/backups"}:
                    slug = query.get("project", [""])[0]
                    action = parsed.path.lstrip("/")
                    page = render_output_page(root, token, action.title(), slug, action)
                else:
                    _response(self, 404, "Not found.", "text/plain; charset=utf-8")
                    return
            except KaizoraError as exc:
                page = _layout("Kaizora Panel", f'<div class="flash error">{_esc(exc)}</div><a href="/">Kembali</a>', notice="Panel tidak dapat memuat data.", kind="error")
                _response(self, 400, page)
                return
            _response(self, 200, page)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self._deny_remote_host():
                return
            origin = self.headers.get("Origin")
            referer = self.headers.get("Referer")
            valid_source = is_local_origin(origin or "", self.server.server_port)
            if not origin and referer:
                valid_source = is_local_origin(referer, self.server.server_port)
            if not valid_source:
                _response(self, 403, "Request origin was not accepted.", "text/plain; charset=utf-8")
                return
            if urlsplit(self.path).path != "/action":
                _response(self, 404, "Not found.", "text/plain; charset=utf-8")
                return
            content_type = self.headers.get_content_type()
            if content_type not in {"application/x-www-form-urlencoded", "multipart/form-data"}:
                _response(self, 415, "Form data required.", "text/plain; charset=utf-8")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                size = 0
            if not 1 <= size <= admin.MAX_UPLOAD_BYTES + 65536:
                _response(self, 413, "Invalid request size.", "text/plain; charset=utf-8")
                return
            try:
                raw_body = self.rfile.read(size)
                if content_type == "multipart/form-data":
                    values = _parse_multipart(self.headers.get("Content-Type", ""), raw_body)
                else:
                    parsed_values = parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
                    values = {key: items[0] if items else "" for key, items in parsed_values.items()}
            except (UnicodeDecodeError, LookupError, ValueError):
                _response(self, 400, "Invalid form data.", "text/plain; charset=utf-8")
                return
            submitted_token = _value(values, "csrf")
            if not secrets.compare_digest(submitted_token, token):
                _response(self, 403, "Invalid request token. Reload the panel and retry.", "text/plain; charset=utf-8")
                return
            slug = _value(values, "project")
            action = _value(values, "action")
            path = _value(values, "path")
            target_page = "/cloudflare" if action in {"configure_cloudflare", "start_cloudflare"} else "/"
            if slug and action in {"upload_file", "delete_file", "create_folder", "save_file"}:
                target_page = "/files?" + urlencode({"project": slug, "path": path})
            elif slug and action == "save_domain":
                target_page = "/domain?" + urlencode({"project": slug})
            elif slug and action == "create_database":
                target_page = "/database?" + urlencode({"project": slug})
            elif slug and action == "start_database":
                target_page = "/database?" + urlencode({"project": slug})
            try:
                node = engine.load_node_config(root)
                if action in {"deploy", "start", "stop", "restart", "backup", "logs", "backups"}:
                    code, output = run_cli(root, action, slug)
                    message = output or ("Perintah berhasil." if code == 0 else "Perintah gagal.")
                    if code:
                        message = f"{message}\nExit code: {code}"
                    kind = "error" if code else "success"
                elif action == "create_static_project":
                    created = admin.create_static_project(node.projects_path, _value(values, "name"), _value(values, "domain"))
                    message = f"Project {created.slug} dibuat. Unggah file website, lalu klik Deploy."
                    kind = "success"
                elif action == "create_git_project":
                    created = admin.create_git_project(
                        node.projects_path, _value(values, "name"), _value(values, "repository"),
                        _value(values, "branch"), _value(values, "domain"),
                    )
                    message = f"Repository terhubung ke {created.slug}. Klik Deploy untuk menjalankannya."
                    kind = "success"
                elif action == "upload_file":
                    filename = _value(values, "file_name")
                    payload = values.get("file_bytes")
                    if not filename or not isinstance(payload, bytes):
                        raise KaizoraError("Pilih satu file untuk diunggah.")
                    created_file = admin.write_project_file(load_project(node.projects_path, slug), path, filename, payload)
                    message = f"File {created_file.name} berhasil diunggah."
                    kind = "success"
                elif action == "create_folder":
                    created_folder = admin.create_project_folder(load_project(node.projects_path, slug), path, _value(values, "name"))
                    message = f"Folder {created_folder.name} berhasil dibuat."
                    kind = "success"
                elif action == "delete_file":
                    admin.delete_project_file(load_project(node.projects_path, slug), path)
                    message = "File berhasil dihapus."
                    kind = "success"
                elif action == "save_file":
                    admin.save_project_text(load_project(node.projects_path, slug), path, _value(values, "content"))
                    message = "Perubahan file berhasil disimpan."
                    kind = "success"
                elif action == "save_domain":
                    admin.update_project_domain(load_project(node.projects_path, slug), _value(values, "domain"))
                    message = "Domain tersimpan. Buat atau perbarui Public Hostname di Cloudflare agar domain aktif."
                    kind = "success"
                elif action == "create_database":
                    admin.create_database(node, load_project(node.projects_path, slug), _value(values, "engine"))
                    message = "Database dibuat. Detail koneksi tersedia di halaman Database project ini."
                    kind = "success"
                elif action == "start_database":
                    admin.start_database(node, load_project(node.projects_path, slug))
                    message = "Database sudah diminta untuk berjalan. Periksa status container di halaman ini."
                    kind = "success"
                elif action == "configure_cloudflare":
                    tunnel_token = _value(values, "tunnel_token")
                    if tunnel_token:
                        admin.save_cloudflare_token(node.infrastructure_path, tunnel_token)
                    admin.start_cloudflare(node.infrastructure_path)
                    message = "Token tersimpan aman dan Cloudflare Tunnel sudah diminta untuk berjalan. Periksa status di halaman ini."
                    kind = "success"
                elif action == "start_cloudflare":
                    admin.start_cloudflare(node.infrastructure_path)
                    message = "Cloudflare Tunnel sudah diminta untuk berjalan. Periksa status di halaman ini."
                    kind = "success"
                else:
                    raise KaizoraError("Panel action is not supported.")
            except KaizoraError as exc:
                message = str(exc)
                kind = "error"
            except OSError:
                message = "Panel could not complete that operation. Check node permissions and available disk space."
                kind = "error"
            _redirect(self, target_page + ("&" if "?" in target_page else "?") + urlencode({"notice": message[:4000], "kind": kind}))

        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write("Kaizora panel: " + format % args + "\n")

    return PanelHandler


def run_panel(root: Path, port: int = 8787) -> int:
    if not 1 <= port <= 65535:
        raise KaizoraError("Panel port must be between 1 and 65535.")
    token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(root, token))
    server.daemon_threads = True
    print(f"Kaizora panel is local-only: http://localhost:{server.server_port}")
    print("Keep this process running; press Ctrl+C to stop the panel.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nKaizora panel stopped.")
    finally:
        server.server_close()
    return 0
