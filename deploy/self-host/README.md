# Self-hosting DocMind + pg-mcp-server

One VM, one command: Docker Compose runs DocMind, the PostgreSQL MCP server, a seeded demo Postgres and Caddy (automatic HTTPS).

Written for an Oracle Cloud Always Free ARM VM (Ubuntu 24.04), but it works on any Linux host with Docker.

```bash
# from your laptop: copy the bundle and both projects to the VM
bash push.sh ubuntu@<VM_IP> ~/.ssh/<key>

# on the VM
cd ~/deploy && bash setup.sh      # first run writes .env, then stops
nano .env                         # DOCMIND_DATABASE_URL (e.g. Neon) + OPENROUTER_API_KEY
bash setup.sh                     # seeds the demo DB, builds, starts, prints the MCP shared secret
```

`push.sh` expects `doc-mind-backend/` and `pg-mcp-server/` next to each other under `~/Desktop/Work`; edit the `W=` line to match your layout. Secrets live only in `.env` on the VM and are never committed.
