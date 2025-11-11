# Tailscale PIA Router

A web application to manage Private Internet Access (PIA) VPN exit nodes for Tailscale devices.

## Features

- Configure PIA credentials via web interface
- Select PIA exit nodes (region selection)
- Control which Tailscale devices use the PIA exit node
- Real-time connection status monitoring
- Automatic failover and reconnection
- SQLite database for configuration persistence

## Architecture

- **Backend**: FastAPI (Python)
- **Frontend**: HTML + Alpine.js + Tailwind CSS
- **Database**: SQLite
- **VPN**: PIA WireGuard
- **Network**: Tailscale exit node

## Quick Start

### Local Development

1. Install dependencies:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2. Initialize the database:
```bash
python -m app.init_db
```

3. Run the application:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

4. Access the web interface at `http://localhost:8000`

### Production Deployment

The application is designed to run on an LXC container acting as a Tailscale exit node with PIA VPN routing.

#### Prerequisites

- Debian/Ubuntu-based LXC container with TUN device support
- Tailscale installed and configured as exit node
- Required packages: `python3`, `python3-venv`, `wireguard-tools`, `iptables`, `avahi-daemon`

#### Automated Deployment

1. Clone the repository to `/opt/tailscale-pia-router`:
```bash
cd /opt
git clone <your-repo-url> tailscale-pia-router
cd tailscale-pia-router
```

2. Run the deployment script:
```bash
chmod +x scripts/deploy.sh
./scripts/deploy.sh
```

3. The application will be available at:
   - `http://pia.local:8000` (via mDNS)
   - `http://<container-ip>:8000`

#### Manual Deployment

If you prefer manual deployment:

1. Setup Python environment:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2. Initialize database:
```bash
python -m app.init_db
```

3. Setup routing:
```bash
chmod +x scripts/setup_routing.sh
./scripts/setup_routing.sh
```

4. Install and start systemd service:
```bash
cp deploy/pia-router.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pia-router
```

## Usage

1. **Configure PIA Credentials**
   - Navigate to Settings page
   - Enter your PIA username and password
   - Click "Save PIA Credentials"

2. **Configure Tailscale API Key** (Optional)
   - Get an API key from [Tailscale Admin Console](https://login.tailscale.com/admin/settings/keys)
   - Enter the API key in Settings
   - This enables advanced device management features

3. **Select VPN Region**
   - On the main dashboard, select a PIA region from the dropdown
   - Click "Connect" to establish the VPN connection

4. **Enable Device Routing**
   - View all Tailscale devices in the device list
   - Toggle the switch for each device you want to route through PIA
   - Devices with routing enabled will have their traffic exit through the PIA VPN

## Management Commands

```bash
# View service status
systemctl status pia-router

# View logs
journalctl -u pia-router -f

# Restart service
systemctl restart pia-router

# Update application
cd /opt/tailscale-pia-router
./scripts/update.sh
```

## Troubleshooting

### VPN won't connect
- Check PIA credentials in Settings
- Verify WireGuard is installed: `which wg-quick`
- Check logs: `journalctl -u pia-router -f`

### Devices not routing through VPN
- Ensure IP forwarding is enabled: `sysctl net.ipv4.ip_forward`
- Check iptables rules: `iptables -t nat -L -n -v`
- Verify PIA VPN is connected
- Check device routing status in dashboard

### Can't access web interface
- Check service is running: `systemctl status pia-router`
- Verify port 8000 is accessible
- Check firewall rules if applicable

## License

MIT
