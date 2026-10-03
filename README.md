# Kaizora Hosting

Kaizora Hosting is an internal single-node deployment platform for Kaizora Tech. This repository implements the next project milestone from the project document: a small `kz` CLI for operating Docker Compose projects on node `KZ-HOME-01`.

The current scope is manual deployment and operations for trusted internal projects. It is not a public customer hosting panel. The CLI supports project discovery, node/project status, Git fast-forward pulls, Compose deployment, start/stop/restart, logs, and a source/configuration archive. Database and Docker volume backups, customer access, billing, automated GitHub webhooks, and a web dashboard remain future work.

## Requirements

- Linux, intended for Ubuntu under WSL2.
- Python 3.10 or newer; the CLI uses only the Python standard library.
- Docker Engine and the Docker Compose v2 plugin for project operations.
- `kaizora-network` Docker network for Compose projects.
- A Cloudflare Tunnel token only when the tunnel service is deployed.

The cloud development environment can validate the Python CLI and Compose configuration, but it does not configure a separate Windows/WSL host or create a Cloudflare tunnel. Never commit the real tunnel token or project `.env` files.

## Local development

From this repository:

```bash
python3 bin/kz --help
KZ_HOSTING_ROOT="$PWD" python3 bin/kz projects
```

The project root defaults to `/srv/kaizora-hosting` on the hosting node. Set `KZ_HOSTING_ROOT` or pass `--root` before the subcommand to use another directory.

## Prepare a WSL2 host

Install and start Docker Engine with the official Docker instructions for Ubuntu. Enable systemd inside Ubuntu by adding this to `/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

Then run `wsl --shutdown` from PowerShell and reopen Ubuntu. Check `systemctl status` and `docker info` before continuing. This repository intentionally does not run host package installation from the cloud development container.

Clone this repository under the Linux filesystem, for example `/srv/kaizora-hosting`, then prepare the base directories and private Docker network:

```bash
sudo mkdir -p /srv/kaizora-hosting
sudo chown "$USER:$USER" /srv/kaizora-hosting
git clone https://github.com/neorafritz/kaizora-host.git /srv/kaizora-hosting
cd /srv/kaizora-hosting
bash scripts/prepare-host.sh
```

Install the CLI for the current user:

```bash
mkdir -p "$HOME/.local/bin"
ln -sfn /srv/kaizora-hosting/bin/kz "$HOME/.local/bin/kz"
```

Ensure `$HOME/.local/bin` is on `PATH`. The CLI does not run as root; Docker access should be configured for the WSL user through the host's normal Docker setup.

## Configure the Cloudflare Tunnel

Create the token file on the host; do not commit it:

```bash
cd /srv/kaizora-hosting/infrastructure/cloudflare
cp .env.example .env
chmod 600 .env
${EDITOR:-nano} .env
docker compose --file compose.yaml up -d
```

The tunnel uses the existing `kaizora-network` network and makes outbound connections to Cloudflare. Configure hostnames in the Cloudflare Zero Trust tunnel settings to route to project services, such as `http://kz-kaizora-test-static-web-1:80` after checking the actual container name with `docker ps`.

No router port forwarding is needed. Project Compose files should use `expose`, not public `ports`, and database services must not publish a host port.

## Add and deploy a project

Create `projects/<slug>/kaizora.json` and a Compose file. The manifest requires `name` and a matching lowercase-hyphen `slug`; it can also declare `runtime`, `domain`, `compose_file`, resource information, and a Git `repository` plus `branch`.

Every project service must:

- Join the external `kaizora-network` network.
- Set `restart: unless-stopped`.
- Set CPU and memory limits (`cpus` and `mem_limit`, or Compose deploy limits).
- Avoid `privileged` mode and Docker socket mounts.
- Keep databases off host-published ports.

The sample project is `projects/kaizora-test-static`. Deploy it after preparing the host network:

```bash
kz projects
kz deploy kaizora-test-static
kz status kaizora-test-static
kz logs kaizora-test-static --tail 100
```

`kz deploy` performs a fast-forward-only pull when the manifest has a Git repository, validates the normalized Compose configuration and security limits, builds and starts the services, then waits for the containers to report running/healthy. It does not configure Cloudflare DNS or tunnel routes.

## CLI commands

```text
kz projects [--json]
kz status [project]
kz deploy <project> [--timeout seconds]
kz start <project>
kz stop <project>
kz restart <project>
kz logs <project> [--follow] [--tail lines] [service ...]
kz backup <project>
```

`kz backup` writes a mode-0600 tarball under `backups/project-configs/`. It excludes `.env` files, Git metadata, caches, and symlinks. It is a project-file/configuration snapshot; it does not back up database contents or Docker volumes.

## Manual operations and limits

Use Docker's own tools for resource usage and low-level inspection:

```bash
docker ps
docker stats
docker compose --file infrastructure/cloudflare/compose.yaml logs -f
```

The node is intended for internal, demo, portfolio, and staging projects. It does not provide an SLA, high availability, multi-node orchestration, user accounts, automated webhooks, a control panel, or managed database backups. Keep the host awake, back up important databases separately, and deploy only repositories you trust.
