#!/usr/bin/env bash
#
# deploy.sh — Set up an EC2 instance for EasyCat WebRTC voice chat.
#
# Prerequisites:
#   - Ubuntu 22.04+ EC2 instance (t3.medium or larger recommended)
#   - SSH access
#   - The following ports open in your Security Group:
#       TCP 8080   — WebRTC signaling + static files (HTTP)
#       TCP 3478   — TURN/STUN
#       UDP 3478   — TURN/STUN
#       UDP 49152-65535 — TURN relay range
#     Optional: TCP 5349 only after you configure coturn cert/pkey for TURNS.
#
# NOTE: getUserMedia() requires HTTPS for non-localhost origins.  For
# production, place the server behind an HTTPS reverse proxy (e.g.
# nginx or Caddy with a TLS certificate from Let's Encrypt).
#
# Usage:
#   export OPENAI_API_KEY="sk-..."
#   export TURN_PASSWORD="some-secure-password"
#   export WEBRTC_SIGNALING_TOKEN="some-secure-signaling-token"  # optional; generated if unset
#   bash deploy.sh
#
set -euo pipefail

detect_external_ip() {
    local token=""
    token="$(
        curl -fsS --max-time 2 -X PUT \
            -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" \
            http://169.254.169.254/latest/api/token 2>/dev/null || true
    )"
    if [ -n "$token" ]; then
        curl -fsS --max-time 2 \
            -H "X-aws-ec2-metadata-token: $token" \
            http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true
        return
    fi

    curl -fsS --max-time 2 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true
}

EXTERNAL_IP="${EXTERNAL_IP:-$(detect_external_ip)}"
if [ -z "$EXTERNAL_IP" ]; then
    echo "Could not detect EC2 public IP.  Set EXTERNAL_IP manually."
    echo "  export EXTERNAL_IP=1.2.3.4"
    exit 1
fi

TURN_PASSWORD="${TURN_PASSWORD:-$(openssl rand -base64 24)}"
# Keep the generated browser bootstrap token URL-safe. URLSearchParams treats
# an unescaped "+" in an encoded value as a space, which corrupts standard Base64
# tokens before the client forwards them in the Authorization header.
WEBRTC_SIGNALING_TOKEN="${WEBRTC_SIGNALING_TOKEN:-$(openssl rand -hex 32)}"
OPENAI_API_KEY="${OPENAI_API_KEY:?Set OPENAI_API_KEY before running this script}"
INSTALL_DIR="/opt/easycat"

echo "=== EasyCat WebRTC Deployment ==="
echo "  EC2 public IP:   $EXTERNAL_IP"
echo "  TURN password:   $TURN_PASSWORD"
echo "  Signaling token: configured (stored in $INSTALL_DIR/.env)"
echo "  Install dir:     $INSTALL_DIR"
echo ""

# ── 1. System packages ───────────────────────────────────────────

echo "[1/6] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3.11 python3.11-venv python3.11-dev \
    coturn \
    libopus0 libopus-dev \
    libvpx-dev \
    pkg-config \
    build-essential

# ── 2. coturn ─────────────────────────────────────────────────────

echo "[2/6] Configuring coturn..."

# Enable coturn daemon across Ubuntu package default variants.
if sudo grep -Eq '^#?TURNSERVER_ENABLED=' /etc/default/coturn; then
    sudo sed -i -E 's/^#?TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn
else
    echo 'TURNSERVER_ENABLED=1' | sudo tee -a /etc/default/coturn > /dev/null
fi

# Write config. Use Python templating so generated base64 TURN passwords
# containing "/" or "&" cannot break sed replacement syntax.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python3.11 - "$SCRIPT_DIR/coturn.conf" "$EXTERNAL_IP" "$TURN_PASSWORD" <<'PY' | sudo tee /etc/turnserver.conf > /dev/null
import sys
from pathlib import Path

template = Path(sys.argv[1]).read_text(encoding="utf-8")
rendered = (
    template
    .replace("__EXTERNAL_IP__", sys.argv[2])
    .replace("__TURN_PASSWORD__", sys.argv[3])
)
sys.stdout.write(rendered)
PY

sudo systemctl restart coturn
sudo systemctl enable coturn
echo "  coturn started on $EXTERNAL_IP:3478"

# ── 3. Application user & directory ──────────────────────────────

echo "[3/6] Setting up application..."

sudo useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin easycat 2>/dev/null || true
sudo mkdir -p "$INSTALL_DIR"

# Clone or copy the repo.  If running from within the repo, copy it.
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
if [ -d "$REPO_ROOT/src/easycat" ]; then
    tar -C "$REPO_ROOT" \
        --exclude='./.agents' \
        --exclude='./.claude' \
        --exclude='./.codex' \
        --exclude='./.coverage' \
        --exclude='./.coverage.*' \
        --exclude='./.easycat' \
        --exclude='./.env' \
        --exclude='./.env.*' \
        --exclude='./.git' \
        --exclude='./.hypothesis' \
        --exclude='./.mypy_cache' \
        --exclude='./.mutmut-cache' \
        --exclude='./.pipecat-bench' \
        --exclude='./.pytest_cache' \
        --exclude='./.ruff_cache' \
        --exclude='./.uv-cache' \
        --exclude='./.venv' \
        --exclude='./coverage.xml' \
        --exclude='./htmlcov' \
        --exclude='./mutants' \
        --exclude='./site' \
        --exclude='__pycache__' \
        --exclude='*.key' \
        --exclude='*.pem' \
        --exclude='*.pyc' \
        --exclude='*.pyo' \
        -cf - . | sudo tar -C "$INSTALL_DIR" -xf -
else
    echo "  Place the easycat repository at $INSTALL_DIR"
fi

# ── 4. Python environment ────────────────────────────────────────

echo "[4/6] Creating Python venv..."

sudo python3.11 -m venv "$INSTALL_DIR/.venv"
sudo "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
sudo "$INSTALL_DIR/.venv/bin/pip" install "$INSTALL_DIR[webrtc,openai-agents,openai]"

# ── 5. Environment file ──────────────────────────────────────────

echo "[5/6] Writing environment file..."

sudo tee "$INSTALL_DIR/.env" > /dev/null <<EOF
OPENAI_API_KEY=$OPENAI_API_KEY
SIGNALING_HOST=0.0.0.0
SIGNALING_PORT=8080
WEBRTC_SIGNALING_TOKEN=$WEBRTC_SIGNALING_TOKEN
TURN_SERVER_URL=turn:$EXTERNAL_IP:3478
TURN_USERNAME=easycat
TURN_CREDENTIAL=$TURN_PASSWORD
WEBRTC_EXPOSE_ICE_CREDENTIALS=0
EOF

sudo chmod 600 "$INSTALL_DIR/.env"
sudo chown -R easycat:easycat "$INSTALL_DIR"

# ── 6. systemd service ───────────────────────────────────────────

echo "[6/6] Installing systemd service..."

sudo cp "$SCRIPT_DIR/easycat-webrtc.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable easycat-webrtc
sudo systemctl start easycat-webrtc

echo ""
echo "=== Deployment complete ==="
echo ""
echo "  Backend HTTP URL: http://$EXTERNAL_IP:8080/webrtc_client.html"
echo "  Browser URL:      https://<your-domain>/webrtc_client.html#token=<WEBRTC_SIGNALING_TOKEN>"
echo "                    Percent-encode the value first if you supplied a custom token."
echo "  Signaling URL:    https://<your-domain>                     (after TLS proxy)"
echo "  TURN server:     turn:$EXTERNAL_IP:3478"
echo "  TURN user:       easycat"
echo "  TURN password:   $TURN_PASSWORD"
echo "  Browser TURN entries are omitted from /config by default (server-side relay only)."
echo "  Clients that require their own relay need short-lived TURN credentials."
echo "  Set WEBRTC_EXPOSE_ICE_CREDENTIALS=1 only for trusted demos or short-lived credentials."
echo ""
echo "  Check status:    sudo systemctl status easycat-webrtc"
echo "  View logs:       sudo journalctl -u easycat-webrtc -f"
echo "  TURN logs:       sudo tail -f /var/log/turnserver.log"
echo ""
echo "Security Group reminder — ensure these ports are open:"
echo "  TCP 8080, TCP/UDP 3478, UDP 49152-65535"
echo "  Optional TURNS: TCP 5349 after coturn cert/pkey are configured"
echo ""
echo "NOTE: For remote access, getUserMedia() requires HTTPS."
echo "  Place this server behind nginx/Caddy with a TLS certificate."
