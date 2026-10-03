# Cloudflare Tunnel for Kaizora Hosting

This template runs `cloudflared` as `kaizora-cloudflared` on the external `kaizora-network`. It publishes no host ports. Add the real `CF_TUNNEL_TOKEN` only to a local `.env` file on KZ-HOME-01; `.env` is ignored by Git.

```bash
cp .env.example .env
chmod 600 .env
nano .env
docker compose --file docker-compose.yml up -d
docker compose --file docker-compose.yml ps
```

Configure each hostname in Cloudflare Zero Trust to target the matching application service and internal container port. Keep app and database services private; do not use router port forwarding. A running tunnel container does not by itself prove that Cloudflare routing is healthy, so validate the domain from outside the node. `kz health` reports `not configured` when this named container is absent and `unknown` when it cannot establish a health state.

To stop the tunnel:

```bash
docker compose --file docker-compose.yml down
```

Never commit `.env`, a tunnel token, or any other credentials.
