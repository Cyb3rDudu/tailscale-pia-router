# Claude Context - Tailscale PIA Router Project

## Project Overview
Building a web application to manage Private Internet Access (PIA) VPN as an exit node for Tailscale devices. This runs on LXC container 102 (hostname: pia, IP: 10.36.0.102).

## Current State

### Infrastructure Setup (COMPLETED)
- ✅ LXC Container 102 created
  - Hostname: `pia`
  - IP: `10.36.0.102/22`
  - Gateway: `10.36.0.1`
  - DNS: `8.8.8.8`, `1.1.1.1`
  - Template: `debian-13-dudu_13.1-2_amd64.tar.gz`
  - TUN device support configured for VPN

- ✅ Tailscale installed as exit node
  - Auth key used: `tskey-auth-kZxcCJvu3r11CNTRL-4hquzSpe9tE4gUYJTnLDsEjBA3C7yEQK`
  - Exit node flag: `--advertise-exit-node`
  - Status: Running and advertised

- ✅ Dependencies installed on container:
  - Python 3.13 + pip + venv
  - curl, git
  - avahi-daemon (for mDNS `.local` resolution)
  - wireguard-tools
  - iptables
  - build-essential (full gcc/g++ toolchain)

### Project Repository
- Location: `/Users/dudu/Documents/Code/tailscale-pia-router`
- Git initialized but not yet pushed to GitHub
- Structure created with `app/` directory

## What Needs to Be Built

### Core Application Components

#### 1. **Database Layer** (`app/models/`)
- SQLite database: `data/app.db`
- Tables needed:
  - `settings`: PIA username/password, Tailscale API key, current region
  - `pia_regions`: Available PIA servers (fetch from PIA API)
  - `tailscale_devices`: Synced from Tailscale API
  - `device_routing`: Which devices use PIA exit node (toggle state)
  - `connection_log`: VPN connection history and status

#### 2. **PIA Service** (`app/services/pia_service.py`)
Handles PIA WireGuard VPN:
- Fetch PIA server list from: `https://serverlist.piaservers.net/vpninfo/servers/v6`
- Generate WireGuard config for selected region
- Use PIA token-based auth (get token with username/password)
- WireGuard config location: `/etc/wireguard/pia.conf`
- Commands:
  - Connect: `wg-quick up pia`
  - Disconnect: `wg-quick down pia`
  - Status: `wg show pia`
- Enable IP forwarding: `sysctl -w net.ipv4.ip_forward=1`
- iptables NAT rules for routing Tailscale → PIA

#### 3. **Tailscale Service** (`app/services/tailscale_service.py`)
Manages Tailscale devices and routing:
- Get device list: `tailscale status --json`
- API integration (needs Tailscale API key from user)
  - Endpoint: `https://api.tailscale.com/api/v2/`
  - Get devices: `/api/v2/tailnet/{tailnet}/devices`
  - Update ACLs: `/api/v2/tailnet/{tailnet}/acl`
- Per-device routing control via ACL rules
- Exit node advertisement status

#### 4. **Routing Controller** (`app/services/routing_service.py`)
Controls which Tailscale devices route through PIA:
- iptables MASQUERADE rules
- Policy routing tables
- Subnet routing configuration
- Automatic failover (if PIA disconnects, disable routing)

#### 5. **API Endpoints** (`app/routers/`)

**Settings Routes** (`app/routers/settings.py`):
- `POST /api/settings/pia` - Save PIA credentials
- `GET /api/settings/pia` - Get PIA config (password masked)
- `POST /api/settings/tailscale` - Save Tailscale API key
- `GET /api/regions` - List available PIA regions
- `POST /api/region/select` - Change PIA region
- `POST /api/connection/toggle` - Connect/disconnect PIA

**Device Routes** (`app/routers/devices.py`):
- `GET /api/devices` - List all Tailscale devices
- `POST /api/devices/{device_id}/toggle` - Enable/disable PIA routing for device
- `GET /api/devices/status` - Get routing status for all devices

**Status Routes** (`app/routers/status.py`):
- `GET /api/status/pia` - PIA connection status
- `GET /api/status/tailscale` - Tailscale exit node status
- `GET /api/status/health` - Overall system health check

#### 6. **Web UI** (`app/templates/` and `app/static/`)

**Main Dashboard** (`templates/index.html`):
- Status cards: PIA status, Tailscale status, connected devices
- Region selector dropdown
- Connect/Disconnect button
- Device list with toggle switches
- Real-time status updates (polling every 5s)

**Settings Page** (`templates/settings.html`):
- PIA credentials form
- Tailscale API key input
- Advanced settings (DNS, MTU, keepalive)

**Technology Stack**:
- Alpine.js for reactivity
- Tailwind CSS for styling (CDN)
- HTMX for AJAX (or fetch API)
- No build process - pure HTML/JS

#### 7. **Main Application** (`app/main.py`)
- FastAPI app initialization
- Static file serving
- Template rendering (Jinja2)
- CORS middleware
- Background tasks for monitoring
- Startup: Initialize DB, check services
- Shutdown: Cleanup connections

### System Integration

#### Systemd Service (`deploy/pia-router.service`)
```systemd
[Unit]
Description=Tailscale PIA Router Web App
After=network.target tailscaled.service
Wants=tailscaled.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/pia-router
Environment="PATH=/opt/pia-router/venv/bin"
ExecStart=/opt/pia-router/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

#### IP Routing Script (`scripts/setup_routing.sh`)
- Sets up iptables rules
- Configures policy routing
- Enables IP forwarding
- Saves rules persistently

## Deployment Steps (TODO)

1. **Copy SSH key to container**:
   ```bash
   ssh-copy-id root@10.36.0.102
   ```

2. **Create GitHub repository**:
   ```bash
   gh repo create tailscale-pia-router --public --source=. --remote=origin
   git add .
   git commit -m "Initial commit: Project structure"
   git push -u origin master
   ```

3. **Clone to container and setup**:
   ```bash
   ssh root@10.36.0.102
   cd /opt
   git clone https://github.com/{username}/tailscale-pia-router
   cd tailscale-pia-router
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

4. **Initialize database**:
   ```bash
   python -m app.init_db
   ```

5. **Install systemd service**:
   ```bash
   cp deploy/pia-router.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now pia-router
   ```

6. **Verify mDNS**:
   - Should be accessible at `http://pia.local:8000`
   - Avahi already installed and running

## Technical Details

### PIA Authentication Flow
1. Get token: `POST https://privateinternetaccess.com/api/client/v2/token`
   - Basic auth with username:password
   - Returns token for WireGuard connection
2. Use token in WireGuard config as password

### WireGuard Config Template
```ini
[Interface]
PrivateKey = <generate with `wg genkey`>
Address = <from PIA API response>
DNS = 209.222.18.222, 209.222.18.218

[Peer]
PublicKey = <from PIA server>
Endpoint = <region>.privateinternetaccess.com:1337
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
```

### Tailscale Exit Node Routing
- Traffic flow: `Client Device → Tailscale → Container 102 → PIA → Internet`
- Only devices with routing enabled get forwarded to PIA
- Others use regular Tailscale routing

## User Requirements Recap
- Web interface accessible at `pia.local`
- No authentication required (internal network only)
- PIA region selection
- Per-device toggle for Tailscale routing
- SQLite for persistence
- Plain-text password storage (user acceptable)

## Environment
- Proxmox host: 10.36.0.2 (carrier)
- Container network: 10.36.0.0/22
- Gateway: 10.36.0.1
- User's macOS hostname: mothership
- User's GitHub: Via SSH (key already configured)

## Next Session: Start Here
1. Build all the application files (models, services, routers, UI)
2. Test locally if possible
3. Deploy to container 102
4. Push to GitHub
5. Document usage in README

## Files Created So Far
- README.md
- requirements.txt
- app/ directory structure (empty modules)
- CLAUDE_CONTEXT.md (this file)

## Important Notes
- User prefers option B: Full production-ready solution
- Speed is important but completeness is priority
- User is technical and can iterate
- This is part of a larger homelab setup with Nginx Proxy Manager on container 100
