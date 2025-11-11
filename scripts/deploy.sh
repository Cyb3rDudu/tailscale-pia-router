#!/bin/bash
# Deployment script for Tailscale PIA Router
# Run this script on the target container after cloning the repository

set -e

INSTALL_DIR="/opt/tailscale-pia-router"

echo "==================================="
echo "Tailscale PIA Router Deployment"
echo "==================================="
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Error: This script must be run as root"
    exit 1
fi

# Change to installation directory
if [ ! -d "$INSTALL_DIR" ]; then
    echo "Error: Installation directory $INSTALL_DIR does not exist"
    echo "Please clone the repository to $INSTALL_DIR first"
    exit 1
fi

cd "$INSTALL_DIR"

echo "1. Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

echo "2. Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "3. Initializing database..."
python -m app.init_db

echo "4. Setting up routing..."
chmod +x scripts/setup_routing.sh
./scripts/setup_routing.sh

echo "5. Installing systemd service..."
cp deploy/pia-router.service /etc/systemd/system/
systemctl daemon-reload

echo "6. Enabling and starting service..."
systemctl enable pia-router
systemctl start pia-router

echo ""
echo "==================================="
echo "Deployment complete!"
echo "==================================="
echo ""
echo "The application is now running at:"
echo "  - http://pia.local:8000"
echo "  - http://10.36.0.102:8000"
echo ""
echo "Useful commands:"
echo "  - systemctl status pia-router    # Check service status"
echo "  - systemctl restart pia-router   # Restart service"
echo "  - journalctl -u pia-router -f    # View logs"
echo ""
