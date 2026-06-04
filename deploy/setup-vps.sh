#!/bin/bash
# Run this ONCE on your VPS to set up the Infinity service.
# Usage: bash setup-vps.sh

set -e

REPO_URL="https://github.com/ZeroSanan/Infinity.git"
DEPLOY_PATH="$HOME/infinity"
SERVICE_NAME="infinity"

echo "==> Cloning repository..."
git clone "$REPO_URL" "$DEPLOY_PATH"
cd "$DEPLOY_PATH"

echo "==> Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Creating .env from template..."
cp .env.example .env
echo ""
echo "  *** ACTION REQUIRED: Edit $DEPLOY_PATH/.env and fill in your API keys before starting the service. ***"
echo ""

echo "==> Installing systemd service..."
sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null <<EOF
[Unit]
Description=Infinity DCA Trading Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$DEPLOY_PATH
EnvironmentFile=$DEPLOY_PATH/.env
ExecStart=$DEPLOY_PATH/venv/bin/python main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_NAME

echo "==> Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit your .env file:  nano $DEPLOY_PATH/.env"
echo "  2. Start the service:    sudo systemctl start $SERVICE_NAME"
echo "  3. Check status:         sudo systemctl status $SERVICE_NAME"
echo "  4. View logs:            journalctl -u $SERVICE_NAME -f"
