# Routing Architecture

This document explains how the PIA router's routing system works, requirements for proper operation, and how to add new devices.

## Table of Contents

- [Overview](#overview)
- [Architecture Components](#architecture-components)
- [Routing Requirements](#routing-requirements)
- [How Routing Works](#how-routing-works)
- [Adding New Devices](#adding-new-devices)
- [Troubleshooting](#troubleshooting)

## Overview

The PIA router operates as a Tailscale exit node with selective per-device VPN routing. This allows you to:

1. Use the container as a Tailscale exit node for all devices
2. Selectively route specific devices through PIA VPN endpoints
3. Keep non-routed devices on the local network (no VPN)

**Key Design Principle**: By default, all traffic uses the local gateway. Only devices explicitly enabled for routing use VPN.

## Architecture Components

### 1. Tailscale Exit Node

The container advertises itself as a Tailscale exit node using:

```bash
tailscale up --advertise-exit-node
```

This makes the container available as `pia` in your Tailscale network. Devices can select it as their exit node in the Tailscale app.

### 2. WireGuard VPN Interfaces

Each PIA region connection creates a WireGuard interface:

```
pia-sg-singapo     # Singapore
pia-japan          # Japan
pia-us-alaska-p    # Alaska
```

NetworkManager manages these interfaces using configuration files in `/etc/NetworkManager/system-connections/`.

### 3. Linux Policy Routing

Policy routing uses multiple routing tables:

- **Main table**: Default gateway (10.36.0.1) for local traffic
- **Table 52**: Tailscale device routes
- **Table 51965**: WireGuard routing table (managed by NetworkManager)
- **Table 100-199**: Per-device VPN routing tables

### 4. iptables Rules

Three critical rule types:

1. **FORWARD rules**: Control packet forwarding between interfaces
2. **MASQUERADE rules**: NAT outbound traffic
3. **DNS interception**: Prevent DNS leaks

## Routing Requirements

### Container Requirements

#### 1. IP Forwarding Enabled

```bash
sysctl -w net.ipv4.ip_forward=1
```

Make permanent in `/etc/sysctl.conf`:

```
net.ipv4.ip_forward=1
```

#### 2. TUN Device Support

LXC container must have TUN device access. In Proxmox, edit container config (`/etc/pve/lxc/<VMID>.conf`):

```
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```

#### 3. Required System Packages

```bash
apt install -y \
    tailscale \
    wireguard-tools \
    network-manager \
    iptables \
    iproute2
```

#### 4. Tailscale Exit Node Advertisement

Must be advertising as exit node:

```bash
tailscale status --json | grep AdvertiseExitNode
# Should show: "AdvertiseExitNode": true
```

### Network Requirements

#### 1. Container Network

- Static IP: `10.36.0.102/22`
- Gateway: `10.36.0.1`
- DNS: `8.8.8.8, 1.1.1.1`
- Hostname: `pia`

#### 2. mDNS (Optional)

For `pia.local` hostname resolution:

```bash
apt install -y avahi-daemon
```

#### 3. Tailscale Access

Container must be able to reach:
- `controlplane.tailscale.com` (control plane)
- `login.tailscale.com` (admin console)

## How Routing Works

### Traffic Flow for Non-Routed Devices

```
┌─────────────┐
│   iPhone    │  1. Selects PIA as Tailscale exit node
│ 100.108.X.X │
└──────┬──────┘
       │ 2. Traffic encapsulated by Tailscale
       ▼
┌─────────────────────────────────────────────────┐
│              PIA Container (pia)                 │
│                                                  │
│  ┌──────────────┐   ┌──────────────────────┐   │
│  │ tailscale0   │──▶│ Routing Decision     │   │
│  │ 100.112.7.98 │   │                      │   │
│  └──────────────┘   │ ip rule 30000:       │   │
│                     │ from all iif         │   │
│                     │ tailscale0           │   │
│                     │ lookup main          │   │
│                     └──────────┬───────────┘   │
│                                │                │
│                                ▼                │
│                     ┌──────────────────────┐   │
│                     │ Main Routing Table   │   │
│                     │                      │   │
│                     │ default via          │   │
│                     │ 10.36.0.1 dev eth0   │   │
│                     └──────────┬───────────┘   │
│                                │                │
│                                ▼                │
│                     ┌──────────────────────┐   │
│                     │ eth0                 │   │
│                     │ 10.36.0.102          │   │
│                     └──────────┬───────────┘   │
└─────────────────────────────────┼───────────────┘
                                  │
                                  ▼
                        ┌──────────────────┐
                        │  Gateway Router  │  3. Routes to internet
                        │  10.36.0.1       │     via local ISP
                        └──────────────────┘
```

**Result**: iPhone gets local Kuala Lumpur IP (not VPN IP)

### Traffic Flow for VPN-Routed Devices

```
┌─────────────┐
│ mothership  │  1. Routing enabled for Alaska endpoint
│ 100.86.3.102│
└──────┬──────┘
       │ 2. Traffic arrives via Tailscale
       ▼
┌──────────────────────────────────────────────────────────┐
│                  PIA Container (pia)                      │
│                                                           │
│  ┌──────────────┐   ┌─────────────────────────────┐     │
│  │ tailscale0   │──▶│ Routing Decision            │     │
│  │ 100.112.7.98 │   │                             │     │
│  └──────────────┘   │ ip rule 49:                 │     │
│                     │ from 100.86.3.102           │     │
│                     │ lookup 100                  │     │
│                     └──────────┬──────────────────┘     │
│                                │                         │
│                                ▼                         │
│                     ┌─────────────────────────────┐     │
│                     │ Routing Table 100           │     │
│                     │                             │     │
│                     │ default dev pia-us-alaska-p │     │
│                     └──────────┬──────────────────┘     │
│                                │                         │
│  ┌─────────────────────────────┼─────────────────────┐  │
│  │ iptables NAT POSTROUTING    │                     │  │
│  │                             ▼                     │  │
│  │ MASQUERADE -s 100.86.3.102 -o pia-us-alaska-p    │  │
│  └─────────────────────────────┬─────────────────────┘  │
│                                │                         │
│                                ▼                         │
│                     ┌─────────────────────────────┐     │
│                     │ pia-us-alaska-p             │     │
│                     │ WireGuard Interface         │     │
│                     └──────────┬──────────────────┘     │
└─────────────────────────────────┼──────────────────────┘
                                  │ 3. Encrypted via WireGuard
                                  ▼
                        ┌──────────────────────┐
                        │ PIA VPN Server       │  4. Exit from Alaska
                        │ Alaska, USA          │
                        └──────────────────────┘
```

**Result**: mothership gets Alaska IP

### Critical Routing Rules

#### Rule Priority Order

```bash
ip rule show
```

```
0:    from all lookup local                    # Local traffic
49:   from 100.86.3.102 lookup 100             # mothership → Alaska
100:  from 10.36.0.102 lookup main              # Container's own traffic
5210: from all fwmark 0x80000/0xff0000 lookup main  # Tailscale traffic
30000: from all iif tailscale0 lookup main     # EXIT NODE BYPASS (CRITICAL!)
31127: not from all fwmark 0xcafd lookup 51965 # WireGuard catch-all
32766: from all lookup main                    # Default
```

**Rule 30000 is CRITICAL**: It prevents WireGuard's catch-all rule (31127) from routing exit node traffic through VPN. Without this rule, ALL devices using PIA as exit node would be routed through active VPN connections.

#### iptables FORWARD Rules

Device-specific rules prevent traffic leakage:

```bash
iptables -L FORWARD -n -v
```

```
Chain FORWARD (policy ACCEPT)
# Tailscale's chain (processes exit node traffic first)
ts-forward  all  --  *  *  0.0.0.0/0  0.0.0.0/0

# Device-specific VPN forwarding (prevents leakage)
ACCEPT  all  --  tailscale0  pia-us-alaska-p  100.86.3.102  0.0.0.0/0
ACCEPT  all  --  pia-us-alaska-p  tailscale0  0.0.0.0/0  100.86.3.102  state RELATED,ESTABLISHED
```

**Key Point**: Only traffic from `100.86.3.102` is forwarded to `pia-us-alaska-p`. Other devices are blocked.

#### iptables NAT MASQUERADE Rules

Device-specific NAT prevents IP leakage:

```bash
iptables -t nat -L POSTROUTING -n -v
```

```
Chain POSTROUTING (policy ACCEPT)
# Tailscale's NAT chain
ts-postrouting  all  --  *  *  0.0.0.0/0  0.0.0.0/0

# Device-specific MASQUERADE (prevents leakage)
MASQUERADE  all  --  *  pia-us-alaska-p  100.86.3.102  0.0.0.0/0
```

**Key Point**: Only traffic from `100.86.3.102` is NAT'd on `pia-us-alaska-p`. Other devices are not affected.

## Adding New Devices

### Device Types

#### 1. Linux Servers/Containers (Automated via SSH)

**Requirements**:
- Tailscale installed and authenticated
- SSH access enabled via Tailscale
- Tagged with `tag:ssh-targets` in Tailscale ACL

**Setup**:

1. **Enable Tailscale SSH** on the target device:
   ```bash
   ssh user@device
   tailscale set --ssh --accept-risk=lose-ssh
   ```

2. **Tag the device** in Tailscale admin console:
   - Go to https://login.tailscale.com/admin/machines
   - Find the device
   - Click **...** → **Edit ACL tags**
   - Add: `tag:ssh-targets`
   - Click **Save**

3. **Enable routing** in the web UI:
   - Open http://pia.local:8000
   - Find the device in the table
   - Select VPN endpoint from dropdown
   - Toggle routing **ON**
   - System will automatically SSH and configure exit node

**Result**: Device will automatically be configured with:
```bash
tailscale set --exit-node=100.112.7.98 --exit-node-allow-lan-access
```

#### 2. macOS and iOS Devices (Manual)

**Requirements**:
- Tailscale app installed
- Device on same Tailscale network

**Setup**:

1. **Select PIA as exit node** in Tailscale app:
   - Open Tailscale app
   - Select exit node: `pia`
   - This routes traffic through container's local IP (not VPN yet)

2. **Enable VPN routing** in web UI:
   - Open http://pia.local:8000
   - Find the device (e.g., "iphone", "mothership")
   - Select VPN endpoint from dropdown
   - Toggle routing **ON**
   - System creates routing rules on container

3. **Verify** routing:
   - Check IP: https://ifconfig.me
   - Should show selected VPN region IP

**Manual Configuration** (if needed):

If SSH automation fails, manually run on the device:

```bash
tailscale set --exit-node=100.112.7.98 --exit-node-allow-lan-access
```

Replace `100.112.7.98` with container's Tailscale IP (shown in web UI).

#### 3. Windows Devices (Manual)

**Requirements**:
- Tailscale app installed
- Device on same Tailscale network

**Setup**:

1. **Select PIA as exit node**:
   - Open Tailscale app
   - Click on Tailscale icon in system tray
   - **Exit Node** → Select `pia`

2. **Enable VPN routing** in web UI (same as macOS/iOS above)

3. **Verify** routing

### Container-Specific Setup

If adding a **Proxmox LXC container** that needs routing:

#### 1. Install Tailscale in the Container

```bash
# SSH to the container
ssh root@container-ip

# Install Tailscale
curl -fsSL https://tailscale.com/install.sh | sh

# Authenticate
tailscale up
```

#### 2. Enable Tailscale SSH (for automation)

```bash
tailscale set --ssh --accept-risk=lose-ssh
```

#### 3. Tag the Container

In Tailscale admin console:
- Add tag: `tag:ssh-targets`

#### 4. Enable Routing via Web UI

- Select endpoint
- Toggle ON
- System auto-configures via SSH

## Troubleshooting

### Issue: Device Not Routing Through VPN

**Symptoms**: Device shows routing enabled in UI but gets local IP instead of VPN IP

**Diagnosis**:

1. **Check routing rule exists**:
   ```bash
   ssh root@10.36.0.102
   ip rule show | grep <device-tailscale-ip>
   ```
   Should show: `from <device-ip> lookup <table-id>`

2. **Check routing table**:
   ```bash
   ip route show table <table-id>
   ```
   Should show: `default dev pia-<region>`

3. **Check FORWARD rules**:
   ```bash
   iptables -L FORWARD -n -v | grep <device-ip>
   ```
   Should show device-specific ACCEPT rules

4. **Check MASQUERADE**:
   ```bash
   iptables -t nat -L POSTROUTING -n -v | grep <device-ip>
   ```
   Should show device-specific MASQUERADE rule

**Fix**:

```bash
# Restart PIA router service
systemctl restart pia-router

# Re-enable routing in web UI
```

### Issue: All Devices Route Through VPN (Implicit Assignment)

**Symptoms**: Devices without routing enabled get VPN IP when using PIA as exit node

**Diagnosis**:

1. **Check bypass rule**:
   ```bash
   ip rule show | grep 30000
   ```
   Should show: `30000: from all iif tailscale0 lookup main`

2. **Test routing decision**:
   ```bash
   ip route get 8.8.8.8 from <device-ip> iif tailscale0
   ```
   Should show: `via 10.36.0.1 dev eth0` (NOT pia-*)

**Fix**:

```bash
# Add bypass rule manually
ip rule add from all iif tailscale0 lookup main priority 30000

# Or restart service (will add automatically)
systemctl restart pia-router
```

### Issue: DNS Leaks

**Symptoms**: DNS queries go to local DNS (10.36.0.101) instead of PIA DNS

**Diagnosis**:

```bash
# Check DNS interception rules
iptables -t nat -L PREROUTING -n -v | grep 'dpt:53'
```

**Fix**:

DNS interception is automatically configured. If missing:

```bash
# Restart service
systemctl restart pia-router
```

### Issue: Container Can't Reach Tailscale Control Plane

**Symptoms**: Container shows as offline in Tailscale dashboard

**Diagnosis**:

```bash
# Test connectivity
ping -c 2 controlplane.tailscale.com

# Check Tailscale status
tailscale status --self=true
```

**Fix**:

```bash
# Restart Tailscale daemon
systemctl restart tailscaled

# Wait 10 seconds
sleep 10

# Verify online
tailscale status | head -1
```

### Issue: LAN Access Lost When Using Exit Node

**Symptoms**: Device can't access local network (NAS, printer, etc.) when VPN enabled

**Cause**: Missing `--exit-node-allow-lan-access` flag

**Fix**:

All exit node configurations should include this flag:

```bash
tailscale set --exit-node=100.112.7.98 --exit-node-allow-lan-access
```

The PIA router automatically includes this flag in all commands. If missing, update and restart:

```bash
systemctl restart pia-router
```

## Advanced Configuration

### Custom Routing Table IDs

Device routing tables use IDs 100-199 by default. To change:

Edit `/opt/tailscale-pia-router/app/services/routing_service.py`:

```python
# Change this range
table_id = 100 + hash(device_ip) % 100
```

### Bypass Specific Destinations

To route specific destinations (e.g., internal services) through local gateway even when VPN enabled:

```bash
# Add route in device's routing table
ip route add 192.168.1.0/24 via 10.36.0.1 table 100

# Make permanent by adding to routing service
```

### Monitor Routing in Real-Time

```bash
# Watch routing decisions
watch -n 1 'ip route get 8.8.8.8 from <device-ip> iif tailscale0'

# Watch NAT counters
watch -n 1 'iptables -t nat -L POSTROUTING -n -v'

# Watch active connections
watch -n 1 'wg show'
```

## Security Considerations

### 1. No Authentication on Web UI

The web interface has no login. It's designed for internal network use only.

**Mitigation**:
- Only accessible on Tailscale network and local LAN
- Use firewall rules to restrict access if needed

### 2. Plain-Text Password Storage

PIA credentials stored unencrypted in SQLite database.

**Mitigation**:
- Database only accessible by root user
- Container filesystem isolated in LXC
- Acceptable for homelab use

### 3. Root Privileges Required

Application runs as root to manage iptables and WireGuard.

**Mitigation**:
- Runs in isolated LXC container
- No untrusted input to shell commands
- SQL injection prevented by parameterized queries

### 4. SSH Root Access

Tailscale SSH grants root access to tagged devices.

**Mitigation**:
- Tailscale ACL restricts which devices can SSH
- Only `tag:pia-router` → `tag:ssh-targets` allowed
- WireGuard encryption for all SSH traffic

## Performance Considerations

### 1. Concurrent Connections

The system supports unlimited concurrent VPN connections. Each region creates a separate WireGuard interface.

**Observed Performance**:
- 10+ devices: No noticeable impact
- CPU usage: ~5% idle, ~15% under load
- Memory: ~200MB total

### 2. Throughput

Limited by:
1. WireGuard encryption overhead (~5-10%)
2. Container's network interface (usually 1Gbps in Proxmox)
3. PIA server capacity

**Typical Speeds**:
- Download: 300-800 Mbps (depending on region)
- Upload: 100-400 Mbps
- Latency: +10-50ms (region-dependent)

### 3. Connection Startup

Time from "toggle routing ON" to functional VPN:
- Linux (SSH automation): 2-5 seconds
- macOS/iOS (manual): Instant (routing rules already in place)

## Backup and Recovery

### Backup Critical Data

```bash
# Backup database
cp /opt/tailscale-pia-router/data/app.db /backup/app.db

# Backup NetworkManager configurations
tar -czf /backup/nm-configs.tar.gz /etc/NetworkManager/system-connections/

# Backup routing state
ip rule save > /backup/ip-rules.txt
ip route show table all > /backup/ip-routes.txt
```

### Disaster Recovery

If container is lost:

1. **Recreate container** with same IP (10.36.0.102)
2. **Reinstall packages**:
   ```bash
   apt install -y tailscale wireguard-tools network-manager iptables
   ```
3. **Authenticate Tailscale**:
   ```bash
   tailscale up --advertise-exit-node
   ```
4. **Deploy application**:
   ```bash
   cd /opt
   git clone https://github.com/Cyb3rDudu/tailscale-pia-router.git
   cd tailscale-pia-router
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
5. **Restore database** (or reconfigure via web UI):
   ```bash
   cp /backup/app.db data/app.db
   ```
6. **Start service**:
   ```bash
   systemctl start pia-router
   ```

## Additional Resources

- [Tailscale SSH Documentation](https://tailscale.com/kb/1193/tailscale-ssh)
- [WireGuard Quick Start](https://www.wireguard.com/quickstart/)
- [Linux Policy Routing Guide](https://tldp.org/HOWTO/Adv-Routing-HOWTO/lartc.rpdb.html)
- [PIA Manual Connections](https://www.privateinternetaccess.com/helpdesk/kb/articles/manual-connection-and-port-forwarding-scripts)
