# KZ-HOME-01 setup and deployment guide

This guide prepares the real Windows PC as an Ubuntu or Kali WSL2 hosting node. Ubuntu is the recommended target. The bootstrap accepts Kali, but its Docker package setup has not yet been validated on KZ-HOME-01. The Codex cloud environment is not that PC; no Windows settings, WSL startup, tunnel token, firewall, port forwarding, or Windows reboot behavior has been configured or tested here.

Target milestone: **Kaizora Hosting v0.1 — KZ-HOME-01 READY FOR REAL NODE VALIDATION**.

## Runtime flow

```text
Windows
  ↓
WSL2 Linux (Ubuntu recommended; Kali supported)
  ↓
systemd
  ↓
Docker Engine
  ↓
Cloudflare Tunnel
  ↓
project containers on kaizora-network
```

Public traffic should follow:

```text
Internet → Cloudflare → Cloudflare Tunnel → private Docker network → application
```

Do not configure router port forwarding. Databases must not publish ports on the Windows host.

## 1. Prepare Ubuntu or Kali WSL2

The documented production path uses Ubuntu. If Kali is already installed, it can be used after Docker Engine and Compose v2 pass the checks below. From Windows PowerShell, start the chosen distribution with `wsl -d Ubuntu` or `wsl -d kali-linux`, then confirm it is WSL2:

```bash
uname -a
cat /etc/os-release
```

Keep the repository and project data in the Linux filesystem (for example, under `/home/<user>` or `/srv`), not under `/mnt/c`, to avoid slower and less reliable Docker bind mounts.

## 2. Enable systemd in WSL

In the selected Linux distribution, edit `/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

For example, use `sudoedit /etc/wsl.conf`; preserve any existing WSL configuration sections. From **Windows PowerShell**, stop WSL so the configuration takes effect:

```powershell
wsl --shutdown
```

Start the distribution again and verify:

```bash
ps -p 1 -o comm=
systemctl is-system-running
```

PID 1 should be `systemd`. Resolve any systemd/WSL error before installing the node.

## 3. Install Docker Engine

For Ubuntu, install Docker Engine and the Compose v2 plugin using Docker's current official Ubuntu instructions. For Kali, use Kali's current package repository; do not use Ubuntu-specific repository commands. Package names can differ on Kali Rolling. After `apt update`, inspect available candidates with `apt-cache policy docker.io docker-compose docker-compose-v2 docker-compose-plugin`, then install Docker Engine and the package that provides Compose v2. Do not assume a `docker-compose` command is Compose v2. Do not continue unless `docker compose version` reports Compose v2. Do not use a convenience script from this repository to install system packages. Docker group access is effectively root access; use the host's normal security policy.

Enable and start Docker from the chosen distribution (omit `sudo` if the terminal is already running as root):

```bash
sudo systemctl enable --now docker
systemctl status docker --no-pager
docker compose version
docker info
```

`docker info` must work for the account that will operate `kz`. The bootstrap script will stop with instructions if Docker or Compose is missing or the daemon is unavailable; it will not guess a sudo password or install Docker for you.

## 4. Clone and bootstrap the node

Clone the repository from the chosen distribution and run the bootstrap:

```bash
git clone https://github.com/neorafritz/kaizora-host.git ~/kaizora-host
cd ~/kaizora-host
sudo bash scripts/bootstrap-node.sh
```

The script creates `/srv/kaizora-hosting/{engine,projects,configs,infrastructure,backups,databases,logs}`, writes `configs/node.json` only when it does not exist, ensures `kaizora-network` exists, and installs the `kz` command at `/usr/local/bin/kz`. It leaves existing configuration and project data intact. It does not configure Windows startup, Cloudflare, firewall rules, or router forwarding.

If the repository is already checked out at `/srv/kaizora-hosting`, the sample project is present in that path. Otherwise, add or clone projects into the configured `projects_path` in `configs/node.json`. Inspect the initial node configuration:

```bash
sudo cat /srv/kaizora-hosting/configs/node.json
kz node
kz projects
```

For a non-default config location, set `KAIZORA_HOST_CONFIG=/absolute/path/to/node.json` in the shell or the node service environment. A missing explicit override is an error. The node JSON contains paths and identity, not credentials.

## 5. Configure Cloudflare Tunnel

Create a local token file on KZ-HOME-01 only:

```bash
cd /srv/kaizora-hosting/infrastructure/cloudflare
cp .env.example .env
chmod 600 .env
nano .env
```

Paste `CF_TUNNEL_TOKEN` from Cloudflare Zero Trust into `.env`. Do not paste the token into chat, Git, shell history, a command argument, or a backup manifest. Confirm `.env` is ignored by Git before continuing:

```bash
git -C ~/kaizora-host check-ignore infrastructure/cloudflare/.env
```

Start the tunnel only after a token has been entered locally:

```bash
docker compose --file docker-compose.yml up -d
docker ps --filter name=kaizora-cloudflared
kz health
```

The Compose service uses `kaizora-network`, has `restart: unless-stopped`, and publishes no host port. The CLI reports `unknown` or `not configured` if tunnel health cannot be established; a running container alone does not prove Cloudflare routing works.

In Cloudflare Zero Trust, configure each public hostname to route to the matching project container/service and internal port on `kaizora-network`. Test from an external network. Do not open host firewall ports for application traffic and do not add router port forwarding.

## 6. Add a project

Create one directory per project under the node's `projects_path`, containing the application source, a Compose file, and a `kaizora.json`. Example:

```json
{
  "name": "kaizora-lean",
  "display_name": "Kaizora Lean",
  "runtime": "docker",
  "branch": "main",
  "domain": "lean.kaizoratech.com",
  "resources": {"cpu": 0.5, "memory": "512m"},
  "health": {"path": "/", "timeout": 10},
  "backup": {"project": true, "database": true, "volumes": true}
}
```

`name` is required. The directory name is the project slug. `display_name`, `runtime`, `branch`, `domain`, `resources`, `health`, `backup`, and `compose_file` are optional; defaults are validated and conservative. Git `repository` is optional, but requires `branch` when present. Resource values set aggregate ceilings; each Compose service still needs its own CPU and memory limits. HTTP health probes run only against a container's private Docker network IP with a timeout.

Every service must join external `kaizora-network`, use `restart: unless-stopped`, have CPU and memory limits, avoid privileged mode and Docker socket mounts, and avoid publishing database ports. Use `expose` or private networks for service-to-service access.

Operate projects with:

```bash
kz projects
kz status kaizora-lean
kz deploy kaizora-lean --dry-run
kz deploy kaizora-lean
kz start kaizora-lean
kz stop kaizora-lean
kz restart kaizora-lean
kz logs kaizora-lean --tail 100
kz health
```

`--dry-run` shows the steps but does not pull Git or change containers. Set application environment in a local project `.env` file with restrictive permissions; never commit it. Ensure the project Compose file does not print credentials in startup logs.

## 7. Backups

Review a project's `backup` settings. Then create and list a snapshot:

```bash
kz backup kaizora-lean
kz backups kaizora-lean
```

Each snapshot contains `manifest.json`, project files (unless disabled), an optional compressed database dump, and only named volumes referenced by that project's Compose definition. MySQL/MariaDB require `mysqldump` inside the database container; PostgreSQL requires `pg_dump` inside it. Credentials are taken from database-container environment variables and are not written to command output or the manifest. Missing database/volumes are reported as `skipped`; failures are explicit and nonzero. Project `.env`, secret files, Git internals, and credentials directories are excluded.

Before relying on backups, create a test project with a disposable database and named volume on the real node, create snapshots, inspect archive contents/permissions, then validate recovery manually in an isolated environment. `kz restore` is intentionally not available in this milestone.

## 8. Start WSL at Windows boot/login

WSL systemd starts enabled services when the distribution starts, but Windows must start the distribution. A practical per-user Task Scheduler task is:

1. Open **Task Scheduler** and choose **Create Task**.
2. Name it `Kaizora Hosting WSL`; select **Run only when user is logged on**. Use the Windows account that owns the distribution.
3. Add a trigger **At log on** for that account. If desired, add a short delay (for example, 30 seconds) to let networking settle.
4. Add an action to start the distro and Docker service:
   - Program: `C:\Windows\System32\wsl.exe`
   - Arguments: `-d Ubuntu -u root --exec /usr/bin/systemctl start docker`
   - Replace `Ubuntu` with the exact distribution name from `wsl -l -q`.
5. Save the task. Use Task Scheduler's **Run** action once to check its **Last Run Result**. Avoid placing credentials or tunnel tokens in task arguments.

With systemd enabled and Docker enabled at boot, the Docker service keeps the WSL distribution alive after startup. The Compose containers use `restart: unless-stopped`, so Docker restarts them. Verify that the account running the task can start the distribution and that Docker starts after a full Windows reboot.

## 9. Real-node acceptance test

After setup and after adding a test hostname, perform this acceptance test from outside the Windows PC:

```text
Restart Windows
↓
Do not open the chosen distribution or run any WSL/Linux command manually
↓
Wait for Windows login and the scheduled task
↓
Check the project domain from another device/network
↓
Confirm `kz health`, Docker state, and project containers after logging in
```

**Pass condition:** the project domain becomes available again after reboot without manually starting WSL, Docker, the tunnel, or project containers. Record the reboot time and external HTTP result. Also create and inspect a test database/volume backup and validate recovery separately.

This acceptance test has not been run by Codex because this environment is not the Windows/WSL2 KZ-HOME-01 machine and has no Cloudflare tunnel token. The milestone is **READY FOR REAL NODE VALIDATION**, not validated on the real node.
