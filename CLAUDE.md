# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a web application that manages Private Internet Access (PIA) VPN as a Tailscale exit node. It runs on an LXC container (hostname: `pia`, IP: 10.36.0.102) and provides a web interface to configure PIA credentials, select exit node regions, and control which Tailscale devices route through the VPN.

**Key Flow**: Client Device → Tailscale → Container 102 → PIA VPN → Internet

## Development Commands

### Local Development
```bash
# Setup virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run development server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Access at http://localhost:8000 or http://pia.local:8000
```

### Deployment to Container
```bash
# SSH to container
ssh root@10.36.0.102

# Update code (if already deployed)
cd /opt/tailscale-pia-router
git pull
source venv/bin/activate
pip install -r requirements.txt

# Restart service
systemctl restart pia-router

# View logs
journalctl -u pia-router -f
```

### Database Operations
```bash
# Initialize database (first time)
python -m app.init_db

# Database location
data/app.db
```

## Architecture

### Technology Stack
- **Backend**: FastAPI (Python) - async API framework
- **Frontend**: Alpine.js + Tailwind CSS (CDN-based, no build process)
- **Database**: SQLite via aiosqlite
- **VPN**: PIA WireGuard (`wg-quick`)
- **Network**: Tailscale exit node + iptables routing

### Core Components

#### 1. Database Layer (`app/models/`)
SQLite database with tables:
- `settings`: PIA credentials, Tailscale API key, selected region
- `pia_regions`: Available PIA servers (synced from PIA API)
- `tailscale_devices`: Device list from Tailscale API
- `device_routing`: Per-device routing toggle state
- `connection_log`: VPN connection history

#### 2. Services (`app/services/`)

**PIA Service** (`pia_service.py`):
- Fetches server list: `https://serverlist.piaservers.net/vpninfo/servers/v6`
- Generates WireGuard config at `/etc/wireguard/pia.conf`
- Token-based auth: `POST https://privateinternetaccess.com/api/client/v2/token`
- VPN control: `wg-quick up/down pia`, `wg show pia`
- Must enable IP forwarding: `sysctl -w net.ipv4.ip_forward=1`

**Tailscale Service** (`tailscale_service.py`):
- Local status: `tailscale status --json`
- API integration: `https://api.tailscale.com/api/v2/`
- Device management via Tailscale API
- Exit node advertisement monitoring

**Routing Service** (`routing_service.py`):
- Per-device iptables MASQUERADE rules
- Policy routing tables for selective device routing
- Automatic failover when PIA disconnects
- Only enabled devices route through PIA; others use standard Tailscale routing

#### 3. API Routes (`app/routers/`)

**Settings Routes** (`settings.py`):
- `POST /api/settings/pia` - Configure PIA credentials
- `GET /api/settings/pia` - Get config (password masked)
- `POST /api/settings/tailscale` - Save Tailscale API key
- `GET /api/regions` - List PIA regions
- `POST /api/region/select` - Change region
- `POST /api/connection/toggle` - Connect/disconnect VPN

**Device Routes** (`devices.py`):
- `GET /api/devices` - List Tailscale devices
- `POST /api/devices/{device_id}/toggle` - Toggle PIA routing per device
- `GET /api/devices/status` - Routing status for all devices

**Status Routes** (`status.py`):
- `GET /api/status/pia` - VPN connection status
- `GET /api/status/tailscale` - Exit node status
- `GET /api/status/health` - System health check

#### 4. Web Interface (`app/templates/`, `app/static/`)

**Main Dashboard** (`index.html`):
- Status cards showing PIA/Tailscale connection state
- Region selector dropdown
- Connect/Disconnect button
- Device list with per-device toggle switches
- Real-time polling (every 5 seconds)

**Settings Page** (`settings.html`):
- PIA credentials form
- Tailscale API key input
- Advanced VPN settings (DNS, MTU, keepalive)

Uses Alpine.js for reactivity, Tailwind CSS for styling. No build step required.

### PIA Authentication Flow
1. Send username/password to `POST https://privateinternetaccess.com/api/client/v2/token` (Basic Auth)
2. Receive token for WireGuard connection
3. Use token as password in WireGuard config with server's public key

### WireGuard Configuration
Generated at `/etc/wireguard/pia.conf`:
```ini
[Interface]
PrivateKey = <generated with `wg genkey`>
Address = <from PIA API response>
DNS = 209.222.18.222, 209.222.18.218

[Peer]
PublicKey = <from PIA server>
Endpoint = <region>.privateinternetaccess.com:1337
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
```

## Deployment Architecture

### System Service
Managed via systemd at `/etc/systemd/system/pia-router.service`:
- Runs as root (required for iptables/WireGuard management)
- Auto-restart on failure
- Depends on `tailscaled.service`
- Working directory: `/opt/pia-router`

### Network Configuration
- Container: LXC 102 on Proxmox (10.36.0.102/22)
- Gateway: 10.36.0.1
- DNS: 8.8.8.8, 1.1.1.1
- mDNS: Accessible via `pia.local` (avahi-daemon)
- TUN device support enabled for VPN

### Required System Packages
Already installed on container:
- Python 3.13 + pip + venv
- wireguard-tools
- iptables
- avahi-daemon (mDNS)
- build-essential
- curl, git

## Important Design Decisions

1. **No Authentication**: Web interface has no login (internal network only, user-acceptable)
2. **Plain-text Passwords**: Stored unencrypted in SQLite (user-acceptable for homelab)
3. **No Build Process**: Frontend uses CDN resources (Alpine.js, Tailwind CSS)
4. **Root Privileges**: Application runs as root to manage WireGuard and iptables
5. **Polling Updates**: UI polls status every 5 seconds (no WebSocket complexity)

## Testing Approach

Since this manages network infrastructure:
1. Test PIA service independently (can mock WireGuard commands)
2. Test Tailscale API integration with real API calls
3. Test routing logic without actual iptables changes (dry-run mode)
4. Integration testing requires deployment to actual container

## User Context

- Homelab setup with Proxmox host at 10.36.0.2 (hostname: carrier)
- Part of larger infrastructure with Nginx Proxy Manager on container 100
- User is technical and prefers complete, production-ready solutions
- Development machine: macOS (hostname: mothership)
- GitHub access via SSH key
