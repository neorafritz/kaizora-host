from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest import mock

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from kaizora_hosting import backup, cli, panel
from kaizora_hosting.config import default_node_config, load_node_config
from kaizora_hosting.errors import KaizoraError
from kaizora_hosting.manifest import discover_projects, load_project


class ConfigTests(unittest.TestCase):
    def test_default_node_config_uses_development_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_node_config(Path(directory))
            self.assertEqual(config.name, "KZ-HOME-01")
            self.assertEqual(config.projects_path, Path(directory) / "projects")
            self.assertEqual(config.backups_path, Path(directory) / "backups")

    def test_environment_override_loads_node_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "custom.json"
            config_file.write_text(json.dumps({"node_id": "test-node", "name": "Test Node"}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"KAIZORA_HOST_CONFIG": str(config_file)}):
                config = load_node_config(Path(directory) / "root")
            self.assertEqual(config.node_id, "test-node")
            self.assertEqual(config.projects_path, Path(directory) / "root/projects")

    def test_missing_override_is_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"KAIZORA_HOST_CONFIG": str(Path(directory) / "missing.json")}):
                with self.assertRaisesRegex(KaizoraError, "Node config was not found"):
                    load_node_config(Path(directory))

    def test_invalid_node_config_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "node.json"
            config_file.write_text('{"node_id": "INVALID ID"}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"KAIZORA_HOST_CONFIG": str(config_file)}):
                with self.assertRaisesRegex(KaizoraError, "node_id"):
                    load_node_config(Path(directory))


class ManifestTests(unittest.TestCase):
    def make_project(self, root: Path, slug: str = "sample", manifest: dict | None = None) -> Path:
        projects = root / "projects"
        path = projects / slug
        path.mkdir(parents=True)
        (path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        if manifest is not None:
            (path / "kaizora.json").write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_manifest_defaults_are_safe_and_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = self.make_project(Path(directory), manifest={"name": "sample"})
            project = load_project(project_path.parent, "sample")
            self.assertEqual(project.title, "sample")
            self.assertEqual(project.manifest["health"], {"path": "/", "timeout": 10, "port": None})
            self.assertEqual(project.manifest["backup"], {"project": True, "database": False, "volumes": False})

    def test_invalid_manifest_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = self.make_project(Path(directory), manifest={"name": "sample", "resources": {"cpu": 0}})
            with self.assertRaisesRegex(KaizoraError, "resources.cpu"):
                load_project(project_path.parent, "sample")

    def test_repository_with_embedded_token_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = self.make_project(Path(directory), manifest={
                "name": "sample", "repository": "https://token@github.com/org/repo.git", "branch": "main",
            })
            with self.assertRaisesRegex(KaizoraError, "embedded credentials"):
                load_project(project_path.parent, "sample")

    def test_manifest_compose_path_cannot_escape_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = self.make_project(Path(directory), manifest={"name": "sample", "compose_file": "../outside.yml"})
            with self.assertRaisesRegex(KaizoraError, "stay inside"):
                load_project(project_path.parent, "sample")

    def test_legacy_compose_only_project_remains_discoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = self.make_project(Path(directory), manifest=None)
            project = load_project(project_path.parent, "sample")
            projects, errors = discover_projects(project_path.parent)
            self.assertEqual(project.slug, "sample")
            self.assertEqual([item.slug for item in projects], ["sample"])
            self.assertEqual(errors, [])
            self.assertFalse(project.manifest["backup"]["database"])


class BackupTests(unittest.TestCase):
    def _project_and_node(self, root: Path, backup_options: dict | None = None):
        project_path = root / "projects" / "sample"
        project_path.mkdir(parents=True)
        (project_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        (project_path / "app.txt").write_text("app data", encoding="utf-8")
        (project_path / ".env").write_text("PASSWORD=do-not-backup", encoding="utf-8")
        (project_path / "token.secret").write_text("also private", encoding="utf-8")
        (project_path / "tls").mkdir()
        (project_path / "tls" / "server.key").write_text("private key material", encoding="utf-8")
        (project_path / "kaizora.json").write_text(json.dumps({
            "name": "sample",
            "backup": backup_options or {"project": True, "database": True, "volumes": True},
        }), encoding="utf-8")
        return load_project(root / "projects", "sample"), default_node_config(root)

    def test_backup_path_manifest_permissions_and_absent_db_volumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, node = self._project_and_node(root)
            output = io.StringIO()
            with mock.patch.object(backup, "_compose_config", return_value={"services": {}, "volumes": {}}):
                result = backup.create_backup(project, node, output.write)
            self.assertEqual(result, 0)
            snapshot = next((root / "backups" / "sample").iterdir())
            data = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(data["backup"]["project"]["status"], "success")
            self.assertEqual(data["backup"]["database"]["status"], "skipped")
            self.assertEqual(data["backup"]["volumes"]["status"], "skipped")
            self.assertEqual(stat.S_IMODE((snapshot / "manifest.json").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((snapshot / "project.tar.gz").stat().st_mode), 0o600)
            with tarfile_open(snapshot / "project.tar.gz") as archive:
                names = archive.getnames()
                self.assertTrue(any(name.endswith("app.txt") for name in names))
                self.assertFalse(any(name.endswith((".env", "token.secret", "server.key")) for name in names))

    def test_database_failure_is_recorded_and_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, node = self._project_and_node(root)
            config = {"services": {"db": {"image": "postgres:16"}}, "volumes": {}}
            with mock.patch.object(backup, "_compose_config", return_value=config), mock.patch.object(
                backup, "_archive_database", side_effect=KaizoraError("Database backup failed for service db; check dump client.")
            ):
                result = backup.create_backup(project, node, lambda _message: None)
            snapshot = next((root / "backups" / "sample").iterdir())
            manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(result, 1)
            self.assertEqual(manifest["backup"]["database"]["status"], "failed")

    def test_volume_selection_is_project_scoped(self) -> None:
        config = {
            "services": {"web": {"volumes": [{"type": "volume", "source": "storage", "target": "/data"}]}},
            "volumes": {"storage": {"name": "sample_storage"}, "unrelated": {"name": "other_project_data"}},
        }
        self.assertEqual(backup._named_volumes(config), [("storage", "sample_storage")])

    def test_database_engine_detection_supports_mysql_mariadb_postgres(self) -> None:
        config = {"services": {
            "mysql": {"image": "mysql:8"},
            "maria": {"image": "mariadb:11"},
            "postgres": {"image": "postgres:16"},
            "web": {"image": "nginx:stable-alpine"},
        }}
        self.assertEqual(backup._db_services(config), [
            ("mysql", "mysql"), ("maria", "mariadb"), ("postgres", "postgres"),
        ])

    def test_backups_command_lists_manifest_backed_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, node = self._project_and_node(root, {"project": True, "database": False, "volumes": False})
            with mock.patch.object(backup, "_compose_config", return_value={"services": {}}):
                self.assertEqual(backup.create_backup(project, node, lambda _message: None), 0)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                backup.list_backups(project, node.backups_path)
            self.assertIn("Project", output.getvalue())
            self.assertIn("DATE", output.getvalue())

    def test_gzip_stream_produces_compressed_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "dump.gz"
            backup._stream_process_to_gzip(
                [sys.executable, "-c", "print('database dump')"], target, 5, "backup failed"
            )
            with gzip.open(target, "rt", encoding="utf-8") as source:
                self.assertEqual(source.read().strip(), "database dump")


class CliTests(unittest.TestCase):
    def test_missing_docker_returns_nonzero_without_traceback(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with mock.patch.object(cli.shutil, "which", return_value=None):
                result = cli.main(["--root", directory, "status"])
        self.assertNotEqual(result, 0)
        self.assertNotIn("Traceback", output.getvalue() + errors.getvalue())
        self.assertIn("Docker CLI was not found", output.getvalue())

    def test_health_with_missing_docker_is_nonzero_and_honest(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(output):
            with mock.patch.object(cli, "docker_info", return_value=(None, "missing")):
                result = cli.main(["--root", directory, "health"])
        self.assertEqual(result, 1)
        self.assertIn("Cloudflare      not configured", output.getvalue())
        self.assertIn("Docker          unhealthy", output.getvalue())

    def test_dry_run_does_not_require_docker_or_mutate_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "projects" / "sample"
            path.mkdir(parents=True)
            (path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
            (path / "kaizora.json").write_text(json.dumps({"name": "sample"}), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output), mock.patch.object(cli, "require_engine", side_effect=AssertionError):
                result = cli.main(["--root", directory, "deploy", "sample", "--dry-run"])
            self.assertEqual(result, 0)
            self.assertIn("Dry run", output.getvalue())

    def test_database_ports_are_rejected_even_with_custom_service_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "projects" / "sample"
            path.mkdir(parents=True)
            (path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
            (path / "kaizora.json").write_text(json.dumps({"name": "sample"}), encoding="utf-8")
            project = load_project(root / "projects", "sample")
            config = {
                "networks": {"kaizora-network": {"external": True}},
                "services": {"primary": {
                    "image": "postgres:16", "networks": {"kaizora-network": None},
                    "mem_limit": "268435456", "cpus": 0.5, "restart": "unless-stopped",
                    "ports": [{"target": 5432, "published": "5432"}],
                }},
            }
            with mock.patch.object(cli, "compose_config", return_value=config):
                with self.assertRaisesRegex(KaizoraError, "database ports must not be published"):
                    cli.validate_compose(project)

    def test_stopped_container_state_fallback(self) -> None:
        self.assertEqual(cli.project_state([]), "STOPPED")
        project = mock.Mock()
        self.assertEqual(cli._project_health(project, [], True), "stopped")

    def test_invalid_project_returns_nonzero(self) -> None:
        errors = io.StringIO()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            (Path(directory) / "projects").mkdir()
            result = cli.main(["--root", directory, "status", "missing"])
        self.assertEqual(result, 1)
        self.assertIn("Project does not exist", errors.getvalue())


class PanelTests(unittest.TestCase):
    def test_panel_accepts_only_local_hosts_and_origins(self) -> None:
        self.assertTrue(panel.is_local_host_header("localhost:8787", 8787))
        self.assertTrue(panel.is_local_host_header("127.0.0.1", 8787))
        self.assertFalse(panel.is_local_host_header("example.com:8787", 8787))
        self.assertFalse(panel.is_local_host_header("localhost:9999", 8787))
        self.assertTrue(panel.is_local_origin("http://localhost:8787", 8787))
        self.assertFalse(panel.is_local_origin("https://localhost:8787", 8787))
        self.assertFalse(panel.is_local_origin("http://example.com:8787", 8787))

    def test_dashboard_is_local_and_escapes_notices(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            panel.engine, "docker_info", return_value=(None, "Docker unavailable")
        ):
            page = panel.render_dashboard(Path(directory), "csrf-token", notice="<script>bad()</script>")
        self.assertIn("Akses lokal", page)
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", page)
        self.assertNotIn("<script>bad()</script>", page)
        self.assertIn("Docker belum siap", page)

    def test_panel_cli_action_uses_validated_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "sample"
            project.mkdir(parents=True)
            (project / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
            (project / "kaizora.json").write_text(json.dumps({"name": "sample"}), encoding="utf-8")
            command = panel.cli_command(root, "stop", "sample")
            self.assertEqual(command[-2:], ["stop", "sample"])
            with self.assertRaisesRegex(KaizoraError, "not supported"):
                panel.cli_command(root, "sh", "sample")

    def test_panel_serves_dashboard_only_for_local_host_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            panel.engine, "docker_info", return_value=(None, "Docker unavailable")
        ):
            root = Path(directory)
            server = panel.ThreadingHTTPServer(("127.0.0.1", 0), panel.make_handler(root, "test-token"))
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}/"
            try:
                with urlopen(url, timeout=3) as response:
                    page = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                self.assertIn("Kaizora Hosting", page)
                self.assertIn("Akses lokal", page)
                request = Request(url, headers={"Host": "attacker.example"})
                with self.assertRaises(HTTPError) as failure:
                    urlopen(request, timeout=3)
                self.assertEqual(failure.exception.code, 421)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


def tarfile_open(path: Path):
    import tarfile

    return tarfile.open(path, "r:gz")


if __name__ == "__main__":
    unittest.main()
