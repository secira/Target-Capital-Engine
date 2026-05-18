#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# tc-execution-engine — EC2 deployment script (future use)
#
# Prerequisites (manual, done once):
#   1. AWS account + EC2 instance running Amazon Linux 2 / Ubuntu 22.04
#   2. Elastic IP allocated and associated with the instance
#   3. Security group: inbound 22 (SSH from your IP), 8080 (HTTPS from broker CIDRs)
#   4. Instance profile with SSM read access (for secrets)
#   5. SSH key pair — set EC2_KEY_PATH below
#   6. Broker IP-whitelisting paperwork submitted with the Elastic IP
#
# Usage:
#   EC2_HOST=<elastic-ip>  EC2_KEY_PATH=~/.ssh/tc-engine.pem  bash scripts/deploy_ec2.sh
# ---------------------------------------------------------------------------
set -euo pipefail

EC2_HOST="${EC2_HOST:?Set EC2_HOST to the Elastic IP or hostname}"
EC2_KEY_PATH="${EC2_KEY_PATH:?Set EC2_KEY_PATH to the path of your SSH private key}"
EC2_USER="${EC2_USER:-ubuntu}"
REMOTE_DIR="/opt/tc-execution-engine"
APP_SERVICE="tc-execution-engine"
GIT_REPO="${GIT_REPO:-}"  # optional: if set, pulls latest from git on the remote

SSH_CMD="ssh -i $EC2_KEY_PATH -o StrictHostKeyChecking=no $EC2_USER@$EC2_HOST"

echo "==> Deploying tc-execution-engine to $EC2_USER@$EC2_HOST:$REMOTE_DIR"

# ---------------------------------------------------------------------------
# 1. Sync code to EC2 (rsync, excluding secrets and venv)
# ---------------------------------------------------------------------------
echo "==> Syncing code…"
rsync -avz --progress \
  --exclude ".git" \
  --exclude ".env" \
  --exclude "__pycache__" \
  --exclude "*.pyc" \
  --exclude ".venv" \
  --exclude "halt_state.db" \
  -e "ssh -i $EC2_KEY_PATH -o StrictHostKeyChecking=no" \
  . "$EC2_USER@$EC2_HOST:$REMOTE_DIR/"

# ---------------------------------------------------------------------------
# 2. Remote: install Python, create venv, install dependencies, restart service
# ---------------------------------------------------------------------------
echo "==> Installing dependencies and restarting service on remote…"
$SSH_CMD bash <<REMOTE
  set -euo pipefail

  # Install Python 3.11 if not present
  if ! python3.11 --version &>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y python3.11 python3.11-venv python3.11-dev build-essential libpq-dev
  fi

  cd "$REMOTE_DIR"

  # Create / update virtualenv
  if [ ! -d .venv ]; then
    python3.11 -m venv .venv
  fi
  source .venv/bin/activate
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt

  # Write secrets from AWS SSM (requires instance profile with ssm:GetParameter)
  if command -v aws &>/dev/null; then
    echo "==> Pulling secrets from SSM…"
    aws ssm get-parameter --name /tc-engine/DATABASE_URL      --with-decryption --query Parameter.Value --output text > .ssm_db_url
    aws ssm get-parameter --name /tc-engine/EXECUTION_HMAC_SECRET --with-decryption --query Parameter.Value --output text > .ssm_hmac
    aws ssm get-parameter --name /tc-engine/BROKER_ENCRYPTION_KEY --with-decryption --query Parameter.Value --output text > .ssm_bmk
    aws ssm get-parameter --name /tc-engine/ADMIN_TOKEN       --with-decryption --query Parameter.Value --output text > .ssm_admin
    cat > .env <<EOF
DATABASE_URL=\$(cat .ssm_db_url)
EXECUTION_HMAC_SECRET=\$(cat .ssm_hmac)
BROKER_ENCRYPTION_KEY=\$(cat .ssm_bmk)
ADMIN_TOKEN=\$(cat .ssm_admin)
PORT=8080
LOG_LEVEL=INFO
EOF
    rm -f .ssm_*
    chmod 600 .env
  else
    echo "WARN: AWS CLI not found — skipping SSM secret pull. Ensure .env is present."
  fi

  # Install / restart systemd service
  sudo tee /etc/systemd/system/$APP_SERVICE.service > /dev/null <<UNIT
[Unit]
Description=tc-execution-engine FastAPI service
After=network.target

[Service]
Type=simple
User=$EC2_USER
WorkingDirectory=$REMOTE_DIR
EnvironmentFile=$REMOTE_DIR/.env
ExecStart=$REMOTE_DIR/.venv/bin/gunicorn main:app -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8080 --workers 2 --timeout 30 --access-logfile - --error-logfile -
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

  sudo systemctl daemon-reload
  sudo systemctl enable $APP_SERVICE
  sudo systemctl restart $APP_SERVICE
  sleep 3
  sudo systemctl status $APP_SERVICE --no-pager
REMOTE

echo ""
echo "==> Smoke-testing remote /healthz…"
sleep 2
curl -sf "http://$EC2_HOST:8080/healthz" | python3 -m json.tool || echo "WARN: /healthz not reachable yet — check firewall / startup logs"

echo ""
echo "Deploy complete."
echo "Remember to:"
echo "  1. Submit broker IP-whitelisting requests with the Elastic IP: $EC2_HOST"
echo "  2. Set up nginx + Let's Encrypt for HTTPS (optional for Railway testing, required for production)"
echo "  3. Test with: bash tests/smoke.sh http://$EC2_HOST:8080"
