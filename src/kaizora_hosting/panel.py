"""Local-only browser control panel for a Kaizora Hosting node."""

from __future__ import annotations

import html
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from . import cli as engine
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
        for name, href in (("Dashboard", "/"), ("Projects", "/#projects"))
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
    if docker_ready:
        try:
            inspected = engine.run_capture([engine.docker_path(), "inspect", "kaizora-cloudflared"], timeout=5)
            if inspected.returncode == 0:
                detail = engine._container_inspect("kaizora-cloudflared") or {}
                state = detail.get("State", {})
                cloudflare = str(state.get("Health", {}).get("Status") or ("unknown" if state.get("Running") else "unhealthy"))
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
            ("Cloudflare Tunnel", cloudflare, "status nyata" if cloudflare not in {"not configured", "unknown"} else "belum disiapkan"),
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
            cards.append(
                f'<article class="project"><div class="project-head"><div><h3>{_esc(project.title)}</h3>'
                f'<div class="slug">{safe_slug}</div></div><span class="status {_status_class(state)}">{_esc(state)}</span></div>'
                f'<div class="domain">{_esc(project.domain)}</div><div class="project-meta"><span>RAM { _esc(memory) }</span>'
                f'<span>Runtime {_esc(project.manifest.get("runtime", "docker"))}</span></div><div class="actions">{"".join(actions)}'
                f'<a class="btn link" href="/logs?project={safe_slug}">Logs</a>'
                f'<a class="btn link" href="/backups?project={safe_slug}">Snapshots</a></div></article>'
            )
        project_content = f'<div class="projects">{"".join(cards)}</div>'
    else:
        project_content = (
            f'<div class="empty"><strong>Belum ada project</strong>Tambahkan folder project dengan <code>kaizora.json</code> ke '
            f'<code>{_esc(node.projects_path)}</code>, lalu deploy dari panel ini.</div>'
        )

    warnings = "".join(f'<div class="flash error">{_esc(message)}</div>' for message in [*discovery_errors, *row_errors.values()])
    if not docker_ready:
        warnings += f'<div class="flash error">Docker belum siap: {_esc(docker_issue or "unknown error")}</div>'
    body = (
        f'<section class="metrics">{metrics}</section><p class="metric-note">{_esc(disk_note)}</p>'
        f'<div class="section-head"><h2>Services</h2><p>{_esc(node.name)} · {_esc(node.environment)}</p></div>'
        f'<section class="services">{services}</section><div class="section-head" id="projects"><h2>Projects</h2><p>{len(projects)} terdaftar · {running} berjalan</p></div>'
        f'{warnings}{project_content}'
    )
    return _layout("Dashboard", body, notice=notice, kind=kind)


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
            try:
                if parsed.path == "/":
                    notice = query.get("notice", [""])[0][:4000]
                    kind = "error" if query.get("kind", [""])[0] == "error" else "success"
                    page = render_dashboard(root, token, notice=notice, kind=kind)
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
            if self.path != "/action":
                _response(self, 404, "Not found.", "text/plain; charset=utf-8")
                return
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                _response(self, 415, "Form data required.", "text/plain; charset=utf-8")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                size = 0
            if not 1 <= size <= 8192:
                _response(self, 413, "Invalid request size.", "text/plain; charset=utf-8")
                return
            try:
                values = parse_qs(self.rfile.read(size).decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError:
                _response(self, 400, "Invalid form data.", "text/plain; charset=utf-8")
                return
            submitted_token = values.get("csrf", [""])[0]
            if not secrets.compare_digest(submitted_token, token):
                _response(self, 403, "Invalid request token. Reload the panel and retry.", "text/plain; charset=utf-8")
                return
            slug = values.get("project", [""])[0]
            action = values.get("action", [""])[0]
            try:
                code, output = run_cli(root, action, slug)
                message = output or ("Perintah berhasil." if code == 0 else "Perintah gagal.")
                if code:
                    message = f"{message}\nExit code: {code}"
                location = "/?" + urlencode({"notice": message[:4000], "kind": "error" if code else "success"})
            except KaizoraError as exc:
                location = "/?" + urlencode({"notice": str(exc), "kind": "error"})
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

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
