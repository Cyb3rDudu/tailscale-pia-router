# Deployment Guide

This guide covers deploying the Tailscale PIA Router to an LXC container.

## Container Setup

### 1. Create LXC Container

The container should be created with:
- **Template**: Debian 13 or Ubuntu 22.04+
- **Resources**: 1 CPU, 512MB RAM minimum
- **Network**: Bridge with static IP
- **TUN device**: Required for VPN support

Example Proxmox configuration:
```bash
# Enable TUN device in container config
echo "lxc.cgroup2.devices.allow: c 10:200 rwm" >> /etc/pve/lxc/102.conf
echo "lxc.mount.entry: /dev/net dev/net none bind,create=dir" >> /etc/pve/lxc/102.conf
```

### 2. Install Prerequisites

SSH into the container and install required packages:

```bash
# Update system
apt update && apt upgrade -y

# Install required packages
apt install -y \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    git \
    wireguard-tools \
    iptables \
    avahi-daemon \
    build-essential

# Enable and start avahi for .local domain
systemctl enable --now avahi-daemon
```

### 3. Install Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh

# Start Tailscale and advertise as exit node
tailscale up --auth-key=<YOUR_AUTH_KEY> --advertise-exit-node
```

Get an auth key from: https://login.tailscale.com/admin/settings/keys

## Application Deployment

### Method 1: Automated Deployment (Recommended)

1. **Clone the repository**:
```bash
cd /opt
git clone <your-repo-url> tailscale-pia-router
cd tailscale-pia-router
```

2. **Run deployment script**:
```bash
chmod +x scripts/deploy.sh
./scripts/deploy.sh
```

The script will:
- Create a Python virtual environment
- Install dependencies
- Initialize the database
- Configure routing
- Install and start the systemd service

3. **Verify deployment**:
```bash
systemctl status pia-router
journalctl -u pia-router -n 50
```

### Method 2: Manual Deployment

1. **Clone and setup Python environment**:
```bash
cd /opt
git clone <your-repo-url> tailscale-pia-router
cd tailscale-pia-router

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

2. **Initialize database**:
```bash
python -m app.init_db
```

3. **Setup routing**:
```bash
chmod +x scripts/setup_routing.sh
./scripts/setup_routing.sh
```

4. **Install systemd service**:
```bash
cp deploy/pia-router.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable pia-router
systemctl start pia-router
```

5. **Verify**:
```bash
systemctl status pia-router
```

## Initial Configuration

1. **Access the web interface**:
   - Via mDNS: `http://pia.local:8000`
   - Via IP: `http://<container-ip>:8000`

2. **Configure PIA credentials**:
   - Click "Settings" in the header
   - Enter your PIA username (e.g., `p1234567`)
   - Enter your PIA password
   - Click "Save PIA Credentials"
   - Wait for validation to complete

3. **Configure Tailscale API Key** (Optional but recommended):
   - Go to [Tailscale Keys](https://login.tailscale.com/admin/settings/keys)
   - Create a new API key
   - Enter the key in Settings
   - Click "Save Tailscale API Key"

4. **Select VPN Region**:
   - Return to main dashboard
   - Select a region from the dropdown (e.g., "US New York")
   - Click "Connect"
   - Wait for connection to establish

5. **Enable Device Routing**:
   - Scroll down to the device list
   - Toggle the switch for devices you want to route through PIA
   - Verify routing is working by checking the device's public IP

## Verification

### Check VPN Connection

```bash
# Check WireGuard status
wg show pia

# Check public IP (should be PIA exit node)
curl https://api.ipify.org
```

### Check Routing

```bash
# View IP forwarding
sysctl net.ipv4.ip_forward

# View NAT rules
iptables -t nat -L -n -v

# View forward rules
iptables -L FORWARD -n -v
```

### Check Tailscale

```bash
# Check Tailscale status
tailscale status

# Verify exit node advertisement
tailscale status | grep "exit node"
```

### Check Application

```bash
# View service status
systemctl status pia-router

# View logs
journalctl -u pia-router -f

# Check health endpoint
curl http://localhost:8000/health
```

## Updating

To update the application after pulling new code:

```bash
cd /opt/tailscale-pia-router
./scripts/update.sh
```

Or manually:

```bash
cd /opt/tailscale-pia-router
git pull
source venv/bin/activate
pip install -r requirements.txt
systemctl restart pia-router
```

## Troubleshooting

### Service won't start

Check logs:
```bash
journalctl -u pia-router -n 100 --no-pager
```

Common issues:
- Python dependencies missing: Re-run `pip install -r requirements.txt`
- Database not initialized: Run `python -m app.init_db`
- Port 8000 in use: Change port in service file

### VPN connection fails

Check WireGuard:
```bash
# Test WireGuard installation
which wg-quick

# Check if TUN device exists
ls -l /dev/net/tun

# View WireGuard logs
journalctl -u wg-quick@pia -f
```

If TUN device is missing, add to container config:
```bash
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net dev/net none bind,create=dir
```

### Routing not working

Check iptables:
```bash
# View all NAT rules
iptables -t nat -L -n -v

# View FORWARD chain
iptables -L FORWARD -n -v

# Check IP forwarding
sysctl net.ipv4.ip_forward
```

Reset routing:
```bash
./scripts/setup_routing.sh
```

### mDNS not working

Check avahi:
```bash
systemctl status avahi-daemon

# Test mDNS resolution
avahi-browse -a -t
```

Restart avahi:
```bash
systemctl restart avahi-daemon
```

## Security Considerations

1. **No Authentication**: The web interface has no authentication. It's designed for internal networks only.
   - Use firewall rules to restrict access
   - Consider adding reverse proxy with authentication

2. **Plain-text Passwords**: PIA credentials are stored unencrypted in SQLite
   - Database file: `data/app.db`
   - Only readable by root
   - Acceptable for homelab use

3. **Root Privileges**: Application runs as root to manage:
   - WireGuard interfaces
   - iptables rules
   - System configuration

4. **API Keys**: Tailscale API key stored in database
   - Use read-only API keys when possible
   - Rotate keys periodically

## Advanced Configuration

### Custom Port

Edit `/etc/systemd/system/pia-router.service`:
```ini
ExecStart=/opt/tailscale-pia-router/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Then:
```bash
systemctl daemon-reload
systemctl restart pia-router
```

### Reverse Proxy

Example Nginx configuration:
```nginx
server {
    listen 80;
    server_name pia.local;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### Persistent iptables Rules

To save rules across reboots:
```bash
apt install iptables-persistent
iptables-save > /etc/iptables/rules.v4
```

## Backup and Restore

### Backup

```bash
# Backup database
cp /opt/tailscale-pia-router/data/app.db /backup/app.db.$(date +%F)

# Backup WireGuard configs
cp /etc/wireguard/pia.conf /backup/pia.conf.$(date +%F)
```

### Restore

```bash
# Restore database
cp /backup/app.db.2024-01-15 /opt/tailscale-pia-router/data/app.db

# Restart service
systemctl restart pia-router
```

## Monitoring

View logs in real-time:
```bash
journalctl -u pia-router -f
```

Check connection status:
```bash
curl http://localhost:8000/api/status/health | jq
```

Monitor WireGuard:
```bash
watch -n 5 wg show pia
```

## Support

For issues:
1. Check logs: `journalctl -u pia-router -f`
2. Verify health: `curl http://localhost:8000/api/status/health`
3. Check GitHub issues
4. Review CLAUDE.md for architecture details
