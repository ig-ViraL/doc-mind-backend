#!/usr/bin/env bash
# Run once on a fresh Ubuntu ARM VM:  bash setup.sh
set -euo pipefail

# 1. Docker + firewall (Oracle Ubuntu images ship with restrictive iptables)
if ! command -v docker >/dev/null; then curl -fsSL https://get.docker.com | sudo sh; sudo usermod -aG docker "$USER"; fi
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || true

# 2. Secrets file
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s/^DEMO_DB_ADMIN_PASSWORD=.*/DEMO_DB_ADMIN_PASSWORD=$(openssl rand -hex 16)/" .env
  sed -i "s/^DEMO_DB_READONLY_PASSWORD=.*/DEMO_DB_READONLY_PASSWORD=$(openssl rand -hex 16)/" .env
  sed -i "s/^MCP_SHARED_SECRET=.*/MCP_SHARED_SECRET=$(openssl rand -hex 32)/" .env
  echo ">> Edit .env now: set DOCMIND_DATABASE_URL and OPENROUTER_API_KEY, then re-run: bash setup.sh"
  exit 0
fi
set -a; . ./.env; set +a
: "${DOCMIND_DATABASE_URL:?set DOCMIND_DATABASE_URL in .env}" "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY in .env}"

# 3. Demo database: seed sample data, then create a read-only role for the MCP server
sudo docker compose up -d demo-db
until sudo docker compose exec -T demo-db pg_isready -U postgres -d demo >/dev/null 2>&1; do sleep 2; done
sudo docker run --rm --network "$(basename "$PWD")_default" \
  -e DATABASE_URL="postgresql://postgres:${DEMO_DB_ADMIN_PASSWORD}@demo-db:5432/demo" \
  -v "$PWD/seed:/seed:ro" python:3.12-slim \
  sh -c "pip install -q psycopg2-binary && python /seed/seed_10_tables.py"
sudo docker compose exec -T demo-db psql -U postgres -d demo -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_readonly') THEN
    CREATE ROLE mcp_readonly LOGIN PASSWORD '${DEMO_DB_READONLY_PASSWORD}';
  END IF;
END \$\$;
GRANT CONNECT ON DATABASE demo TO mcp_readonly;
GRANT USAGE ON SCHEMA public TO mcp_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_readonly;
ALTER ROLE mcp_readonly SET default_transaction_read_only = on;
SQL

# 4. Everything else
sudo docker compose up -d --build
echo ">> Done. Check:  curl https://${DOCMIND_HOST}/health   and   curl -i https://${MCP_HOST}/mcp"
echo ">> Put this in Vercel (Production):  MCP_SHARED_SECRET=${MCP_SHARED_SECRET}"
