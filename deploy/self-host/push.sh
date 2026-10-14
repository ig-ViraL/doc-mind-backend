#!/usr/bin/env bash
# Run on your Mac:  bash push.sh ubuntu@<VM_IP> [ssh-key]
# Copies this bundle plus both projects' source (pg-mcp-server is private, so no git clone on the VM).
set -euo pipefail
HOST="${1:?usage: bash push.sh ubuntu@VM_IP [ssh-key]}"
KEY="${2:-$HOME/.ssh/id_ed25519}"
W="$HOME/Desktop/Work"
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new"
EX=(--exclude .git --exclude .venv --exclude node_modules --exclude __pycache__ --exclude .env --exclude .env.local --exclude .neon --exclude .pytest_cache)
rsync -az -e "$SSH" --exclude app --exclude .env "$W/oracle-deploy/" "$HOST:~/deploy/"
rsync -az -e "$SSH" "${EX[@]}" "$W/doc-mind-backend/" "$HOST:~/deploy/app/doc-mind-backend/"
rsync -az -e "$SSH" "${EX[@]}" "$W/pg-mcp-server/" "$HOST:~/deploy/app/pg-mcp-server/"
echo "Synced. On the VM:  cd ~/deploy && bash setup.sh"
