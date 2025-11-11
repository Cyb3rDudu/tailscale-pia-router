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

## Setup

1. Install dependencies:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2. Run the application:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

3. Access the web interface at `http://pia.local:8000`

## Deployment

The application is designed to run on an LXC container acting as a Tailscale exit node with PIA VPN routing.

## License

MIT
