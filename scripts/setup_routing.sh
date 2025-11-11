#!/bin/bash
# Routing setup script for Tailscale PIA Router
# This script sets up the necessary iptables rules and IP forwarding

set -e

echo "Setting up routing for Tailscale PIA Router..."

# Enable IP forwarding
echo "Enabling IP forwarding..."
sysctl -w net.ipv4.ip_forward=1
sysctl -w net.ipv6.conf.all.forwarding=1

# Make IP forwarding persistent
echo "Making IP forwarding persistent..."
if ! grep -q "net.ipv4.ip_forward=1" /etc/sysctl.conf; then
    echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
fi
if ! grep -q "net.ipv6.conf.all.forwarding=1" /etc/sysctl.conf; then
    echo "net.ipv6.conf.all.forwarding=1" >> /etc/sysctl.conf
fi

# Create iptables rules directory if it doesn't exist
mkdir -p /etc/iptables

echo "Routing setup complete!"
echo ""
echo "Note: Individual device routing and PIA NAT rules will be managed"
echo "automatically by the application when you connect to PIA VPN and"
echo "toggle device routing."
