#!/bin/bash
# Update script for Tailscale PIA Router
# Run this script to update the application after pulling new code

set -e

INSTALL_DIR="/opt/tailscale-pia-router"

echo "==================================="
echo "Tailscale PIA Router Update"
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
    exit 1
fi

cd "$INSTALL_DIR"

echo "1. Pulling latest code..."
git pull

echo "2. Activating virtual environment..."
source venv/bin/activate

echo "3. Updating Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "4. Restarting service..."
systemctl restart pia-router

echo "5. Checking service status..."
sleep 2
systemctl status pia-router --no-pager

echo ""
echo "==================================="
echo "Update complete!"
echo "==================================="
echo ""
