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
#       TCP 5349   — TURNS (TLS)
#       UDP 49152-65535 — TURN relay range
#
# NOTE: getUserMedia() requires HTTPS for non-localhost origins.  For
# production, place the server behind an HTTPS reverse proxy (e.g.
# nginx or Caddy with a TLS certificate from Let's Encrypt).
#
# Usage:
#   export OPENAI_API_KEY="sk-..."
#   export TURN_PASSWORD="some-secure-password"
#   bash deploy.sh
#
set -euo pipefail

EXTERNAL_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 || echo "")
if [ -z "$EXTERNAL_IP" ]; then
    echo "Could not detect EC2 public IP.  Set EXTERNAL_IP manually."
    echo "  export EXTERNAL_IP=1.2.3.4"
    exit 1
fi

TURN_PASSWORD="${TURN_PASSWORD:-$(openssl rand -base64 24)}"
OPENAI_API_KEY="${OPENAI_API_KEY:?Set OPENAI_API_KEY before running this script}"
INSTALL_DIR="/opt/easycat"

echo "=== EasyCat WebRTC Deployment ==="
echo "  EC2 public IP:   $EXTERNAL_IP"
echo "  TURN password:   $TURN_PASSWORD"
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

# Enable coturn daemon.
sudo sed -i 's/#TURNSERVER_ENABLED=1/TURNSERVER_ENABLED=1/' /etc/default/coturn

# Write config.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
sudo cp "$SCRIPT_DIR/coturn.conf" /etc/turnserver.conf
sudo sed -i "s/__EXTERNAL_IP__/$EXTERNAL_IP/" /etc/turnserver.conf
sudo sed -i "s/__TURN_PASSWORD__/$TURN_PASSWORD/" /etc/turnserver.conf

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
    sudo cp -a "$REPO_ROOT/." "$INSTALL_DIR/"
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
TURN_SERVER_URL=turn:$EXTERNAL_IP:3478
TURN_USERNAME=easycat
TURN_CREDENTIAL=$TURN_PASSWORD
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
echo "  Client URL:      http://$EXTERNAL_IP:8080/webrtc_client.html"
echo "  Signaling URL:   http://$EXTERNAL_IP:8080/offer"
echo "  TURN server:     turn:$EXTERNAL_IP:3478"
echo "  TURN user:       easycat"
echo "  TURN password:   $TURN_PASSWORD"
echo ""
echo "  Check status:    sudo systemctl status easycat-webrtc"
echo "  View logs:       sudo journalctl -u easycat-webrtc -f"
echo "  TURN logs:       sudo tail -f /var/log/turnserver.log"
echo ""
echo "Security Group reminder — ensure these ports are open:"
echo "  TCP 8080, TCP/UDP 3478, TCP 5349, UDP 49152-65535"
echo ""
echo "NOTE: For remote access, getUserMedia() requires HTTPS."
echo "  Place this server behind nginx/Caddy with a TLS certificate."
