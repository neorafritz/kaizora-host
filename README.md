# Kaizora Hosting

Kaizora Hosting is a small, single-node deployment engine for trusted internal projects. This repository continues the existing Python CLI and Docker Compose design for the target node **KZ-HOME-01**, with a local-only browser control panel. It does not include public customer hosting features.

The cloud workspace can run the CLI and Docker validations, but it is not the Windows/WSL2 node. Cloudflare Tunnel and Windows boot behavior must be checked on KZ-HOME-01 itself. Do not commit `.env` files, passwords, tokens, private keys, or credentials.

The local browser panel at `http://localhost:8787` is the owner interface for this node. It can create a static website or connect a Git/Compose project, manage project files, set a domain, create a private MySQL/MariaDB/PostgreSQL database, and control deployments and backups. It is intentionally local-only and has no customer accounts or billing.

## Requirements

- Linux on WSL2; Ubuntu is recommended, and Kali Linux is supported by the bootstrap checks.
- Python 3.10 or newer; runtime code uses the standard library.
- Docker Engine and the Docker Compose v2 plugin.
- Docker network `kaizora-network`.

## Development environment

From a checkout:

```bash
python3 -m compileall src
python3 bin/kz --help
KZ_HOSTING_ROOT="$PWD" python3 bin/kz projects
KZ_HOSTING_ROOT="$PWD" python3 bin/kz node
```

`--root` or `KZ_HOSTING_ROOT` selects a development hosting root. Production defaults to `/srv/kaizora-hosting`. Node configuration is read from `<root>/configs/node.json`; `KAIZORA_HOST_CONFIG=/path/to/node.json` overrides its path. A missing default config uses safe node defaults, while a missing explicit override is an error.

Run the standard-library tests with:

```bash
python3 -m unittest discover -s tests -v
```

## Production node installation

Clone the repository under the Linux filesystem, then run the bootstrap from the checkout:

```bash
git clone https://github.com/neorafritz/kaizora-host.git ~/kaizora-host
cd ~/kaizora-host
sudo bash scripts/bootstrap-node.sh
```

The script checks Ubuntu or Kali Linux, Python, Docker, Compose, and daemon access; creates missing directories and a default node config; ensures `kaizora-network` exists; installs `/usr/local/bin/kz`; and enables a local-only browser panel service when systemd is active. It does not install Docker, request a sudo password, change Windows settings, open firewall ports, or configure port forwarding. Existing node config and data are preserved. See the full [KZ-HOME-01 deployment guide](docs/KZ_HOME_01_SETUP.md).

The default layout is:

```text
/srv/kaizora-hosting/
├── engine/
├── projects/
├── configs/
├── infrastructure/
├── backups/
├── databases/
└── logs/
```

To use a custom root, set `KZ_HOSTING_ROOT` consistently when bootstrapping and operating the node. The CLI can also use a separately located config via `KAIZORA_HOST_CONFIG`.

## Add a project

Put each project directory under the configured `projects_path`. The Compose file must attach to external `kaizora-network`, use `restart: unless-stopped`, set CPU and memory limits, avoid privileged mode and Docker socket mounts, and never publish database ports to the host. Put the database and application services on private Docker networking.

Example `projects/kaizora-lean/kaizora.json`:

```json
{
  "name": "kaizora-lean",
  "display_name": "Kaizora Lean",
  "runtime": "docker",
  "branch": "main",
  "domain": "lean.kaizoratech.com",
  "resources": {
    "cpu": 0.5,
    "memory": "512m"
  },
  "health": {
    "path": "/",
    "timeout": 10
  },
  "backup": {
    "project": true,
    "database": true,
    "volumes": true
  }
}
```

The project directory name is its slug (`kaizora-lean`). `name` is required; `display_name`, `runtime`, `branch`, `domain`, `resources`, `health`, and `backup` have safe defaults. The supported `runtime` labels are `docker` and `static`; both run through the existing Compose engine. `repository` is optional; when set, `branch` is required and `kz deploy` does a fast-forward-only Git pull. `compose_file` can choose another relative Compose filename. Invalid fields produce a clear error. Old Compose-only project directories remain supported and receive conservative defaults (project file backup on, database/volume backups off).

`resources.cpu` and `resources.memory` are aggregate ceilings checked against the CPU and memory limits declared for all Compose services. Each service must still declare its own limits. Health checks use Docker health state where available, then the manifest's HTTP path and timeout; without either signal the state remains `running` instead of being called healthy.

## CLI commands

```text
kz node
kz projects [--json]
kz status [project]
kz health
kz deploy <project> [--timeout seconds] [--dry-run]
kz start <project>
kz stop <project>
kz restart <project>
kz logs <project> [--follow] [--tail lines] [service ...]
kz backup <project>
kz backups <project>
kz panel
```

`kz deploy --dry-run` prints the intended steps without pulling Git or changing containers. `kz health` reports Cloudflare as `not configured` if the named tunnel container is absent; it does not infer tunnel health from a running project.

`kz backup` creates `backups/<project>/<YYYY-MM-DD_HHMMSS>/` and a `manifest.json`. It follows each `backup` boolean in `kaizora.json`, excludes `.env`, secret files, credentials directories, symlinks, Git metadata, and dependency caches, and writes archives and manifest with mode `0600`. Database dumps support one MySQL, MariaDB, or PostgreSQL service per snapshot, using the in-container environment for credentials and streaming output into gzip. Named volumes are selected only from that project's normalized Compose config and archived through a read-only temporary container on `--network none`. A missing DB or volume is `skipped`; a configured backup that fails yields an error and nonzero exit code. `kz backups` lists snapshots. Restore is intentionally not implemented until backup recovery is validated on the real node.

## Browser panel

Open `http://localhost:8787` on the Windows PC while the node is running. Use **Tambah Project** to create a static website or connect a Git repository with Docker Compose. Project cards open **File Manager**, **Domain**, and **Database**, alongside deploy, start, stop, restart, logs, and backup controls. Static-site files open directly in the `public/` folder. The file manager hides `.env`, credentials, tokens, private keys, and `.git`; uploads are limited to 20 MB and text editing to 1 MB.

The panel listens only to `127.0.0.1` and has no login yet. Do not expose it through Cloudflare, a router, or another network interface. It manages this node for its owner; customer accounts and billing are not part of this milestone.

## Cloudflare Tunnel

Open **Cloudflare** in the local panel. In Cloudflare Zero Trust, create a named tunnel, choose the Docker connector, copy its connector token, then paste it into the panel's password field. The panel saves it as `infrastructure/cloudflare/.env` with permission `0600` and starts the tunnel without showing the token again. Never put the token in Git, a URL, a screenshot, or chat.

The token only connects the node to the tunnel. In Cloudflare Zero Trust → Networks → Tunnels → this tunnel → **Public Hostnames**, add one hostname for each project and route it to the target shown under that project's **Domain** page (for example, `http://kz-my-site-web:80` for a static project). The domain field in Kaizora is local project metadata; saving it does not create a Cloudflare route. Do not enable router port forwarding. You can also configure the template manually on the node:

```bash
cd /srv/kaizora-hosting/infrastructure/cloudflare
cp .env.example .env
chmod 600 .env
${EDITOR:-nano} .env
docker compose --file docker-compose.yml up -d
```

Keep `.env` local. Configure public hostnames and service targets in Cloudflare Zero Trust. Tunnel and project containers share `kaizora-network`, so route each hostname to its project container/service and internal port. No router port forwarding is needed. Keep databases private on Docker networking; never add host-published database ports such as `3306:3306`.

## Current milestone

The repository targets **Kaizora Hosting v0.1 — KZ-HOME-01 READY FOR REAL NODE VALIDATION**. The cloud workspace can validate code, tests, Compose configuration, and its sample container. It cannot validate WSL boot, Task Scheduler, the actual Cloudflare token/tunnel, Windows reboot recovery, or KZ-HOME-01's real database and volume recovery. No credentials are stored in this repository.
