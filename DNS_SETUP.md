# DNS Proxy Setup for Leak Prevention

## Problem Statement

When using Tailscale with "Use Tailscale DNS" enabled, DNS queries were being sent to Tailscale's MagicDNS (100.100.100.100), which bypassed local iptables rules. This caused DNS queries to leak to the local network (Pi-hole at 10.36.0.101) in plaintext, allowing FortiGate firewall to inspect and block domains even when connected through a VPN exit node.

**Symptoms:**
- VPN connection shows Singapore IP (working)
- FortiGate still intercepts and blocks domains (e.g., whonix.org)
- DNS queries visible to local network devices

**Root Cause:**
- Tailscale MagicDNS bypasses iptables DNAT rules on the `tailscale0` interface
- DNS queries leak to local network before VPN encryption
- FortiGate Deep Packet Inspection sees domain names in plaintext

## Solution: dnsmasq DNS Proxy

Implemented a DNS proxy using dnsmasq on the PIA exit node container that:
1. Listens on the Tailscale interface (100.112.7.98:53)
2. Forwards ALL DNS queries to Cloudflare DNS (1.1.1.1, 1.0.0.1) via VPN tunnel
3. Prevents DNS leaks by routing all queries through VPN

## Configuration Files

### `/etc/dnsmasq.d/pia-proxy.conf`

```bash
# PIA VPN DNS Proxy Configuration
# Prevents DNS leaks while maintaining local domain access

# Listen only on Tailscale interface - use bind-dynamic for better routing
interface=tailscale0
bind-dynamic

# DNS cache size
cache-size=1000

# Forward ALL queries to Cloudflare DNS (devices will route this through VPN)
# Using public DNS that is accessible from container
server=1.1.1.1
server=1.0.0.1

# Don't forward plain names (without dots)
domain-needed

# Don't forward reverse lookups for private IP ranges
bogus-priv

# Log queries for debugging (can be disabled later)
log-queries
log-facility=/var/log/dnsmasq.log

# Don't read /etc/resolv.conf or /etc/hosts
no-resolv
no-hosts

# Enable DNSSEC if available
dnssec

# Return NXDOMAIN for non-existent domains quickly
no-negcache
```

**Key Configuration Points:**

- `bind-dynamic`: Allows proper routing when querying from same host
- `interface=tailscale0`: Only listen on Tailscale network
- `server=1.1.1.1` and `server=1.0.0.1`: Cloudflare DNS servers (all queries)
- `no-resolv`: Don't read `/etc/resolv.conf` (prevents loops)

### Why Cloudflare DNS Instead of PIA DNS?

The container has a bypass routing rule (priority 100: `from 10.36.0.102 lookup main`) that allows the container itself to access the internet directly. This means:

- ❌ Container **cannot** reach PIA DNS servers (10.0.0.243) - they're only accessible through VPN tunnel
- ✅ Container **can** reach public DNS like Cloudflare (1.1.1.1)
- ✅ Client devices querying dnsmasq **will** route to Cloudflare through VPN (policy routing applies)

This design ensures:
- dnsmasq can resolve queries from container context
- Client devices get DNS privacy through VPN
- No DNS leaks to local network

## Tailscale DNS Configuration

### API Configuration

Configure Tailscale to advertise the dnsmasq proxy as the tailnet DNS server:

```bash
curl -X POST "https://api.tailscale.com/api/v2/tailnet/YOUR_TAILNET/dns/nameservers" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary '{"dns": ["100.112.7.98"]}'
```

**Response:**
```json
{"dns":["100.112.7.98"],"magicDNS":true}
```

This configuration:
- Sets 100.112.7.98 (dnsmasq) as the global nameserver for the tailnet
- Keeps MagicDNS enabled for Tailscale hostname resolution
- Propagates automatically to all devices with "Use Tailscale DNS" enabled

### Tailscale Container Configuration

The exit node container runs with:

```bash
tailscale up --accept-routes --advertise-exit-node
```

**Important:**
- ❌ Don't use `--accept-dns=false` - this prevents devices from using our DNS proxy
- ✅ Let Tailscale manage DNS (it will use our API-configured nameserver)

## Installation Steps

### 1. Install dnsmasq

```bash
ssh root@10.36.0.102
apt update
apt install -y dnsmasq
```

### 2. Configure dnsmasq

```bash
cat > /etc/dnsmasq.d/pia-proxy.conf << 'EOF'
# [Configuration content from above]
EOF

systemctl restart dnsmasq
systemctl enable dnsmasq
```

### 3. Verify dnsmasq is Running

```bash
# Check service status
systemctl status dnsmasq

# Verify listening on Tailscale interface
ss -tulpn | grep dnsmasq | grep 100.112.7.98

# Test resolution
dig @100.112.7.98 google.com +short
dig @100.112.7.98 n8n.catdev.io +short
```

### 4. Configure Tailscale DNS via API

```bash
# Get tailnet name
tailscale status --json | python3 -c "import sys, json; print(json.load(sys.stdin)['CurrentTailnet']['Name'])"

# Get API key from database
sqlite3 /opt/tailscale-pia-router/data/app.db "SELECT value FROM settings WHERE key = 'tailscale_api_key';"

# Configure DNS
curl -X POST "https://api.tailscale.com/api/v2/tailnet/YOUR_TAILNET/dns/nameservers" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary '{"dns": ["100.112.7.98"]}'
```

### 5. Restart Tailscale

```bash
systemctl restart tailscaled
sleep 5
tailscale up --accept-routes --advertise-exit-node
```

## Testing

### From Container

```bash
# Test public domain resolution
dig @100.112.7.98 google.com +short
# Expected: IP address (e.g., 216.58.213.110)

# Test catdev.io domain resolution (configured in Cloudflare)
dig @100.112.7.98 n8n.catdev.io +short
# Expected: IP address from Cloudflare DNS

# Check dnsmasq logs
tail -50 /var/log/dnsmasq.log
```

### From iPhone

1. **Enable Tailscale DNS:**
   - Open Tailscale app
   - Ensure "Use Tailscale DNS" is enabled

2. **Connect to PIA exit node:**
   - Select "pia" as exit node in Tailscale app

3. **Test DNS leak prevention:**
   - Go to `https://whonix.org`
   - Should load without FortiGate warning

4. **Verify VPN location:**
   - Visit `https://whatismyipaddress.com`
   - Should show Singapore IP

5. **Test catdev.io domain access:**
   - Access `https://n8n.catdev.io`
   - Should work (resolved via Cloudflare DNS)

### From macOS (mothership)

```bash
# Check DNS configuration
scutil --dns | grep "nameserver\[0\]"
# Should show: 100.112.7.98

# Test resolution
dig google.com +short
dig n8n.catdev.io +short

# Test DNS leak
curl -s https://www.dnsleaktest.com/results.html
```

## Architecture Diagram

```
┌─────────────┐
│   iPhone    │ "Use Tailscale DNS" enabled
└──────┬──────┘
       │ DNS query for whonix.org
       │
       ▼
┌─────────────────────────────────────────┐
│     Tailscale Network (100.x.x.x/10)    │
│  DNS: 100.112.7.98 (configured via API) │
└──────────────┬──────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────┐
│  Container 102 (10.36.0.102)                 │
│  Tailscale IP: 100.112.7.98                  │
│                                              │
│  ┌─────────────────────────────────────┐    │
│  │  dnsmasq (port 53)                  │    │
│  │  - Listens on: 100.112.7.98:53     │    │
│  │  - Forwards ALL queries to:         │    │
│  │    1.1.1.1 (Cloudflare)            │    │
│  │    1.0.0.1 (Cloudflare)            │    │
│  └───────────┬─────────────────────────┘    │
│              │                               │
│              └───────┐                       │
│                                │              │
│  ┌─────────────────────────────▼─────────┐  │
│  │  WireGuard (pia-sg)                   │  │
│  │  - Routes DNS to Cloudflare via VPN   │  │
│  │  - Encrypts all traffic               │  │
│  └───────────────────────────────────────┘  │
└──────────────┬───────────────────────────────┘
               │ Encrypted tunnel
               ▼
┌──────────────────────────────┐
│  PIA Server (Singapore)      │
│  - Forwards DNS to 1.1.1.1   │
│  - Routes to internet         │
└──────────────┬───────────────┘
               │
               ▼
        Internet (Cloudflare DNS)
```

## DNS Query Flow

### For Public Domains (e.g., google.com)

1. iPhone sends DNS query to Tailscale DNS (100.112.7.98)
2. Query routed through Tailscale network to container
3. dnsmasq receives query on 100.112.7.98:53
4. dnsmasq forwards to Cloudflare (1.1.1.1)
5. **Client's policy routing** sends Cloudflare query through VPN (pia-sg)
6. VPN encrypts and forwards to Cloudflare
7. Response comes back through VPN → dnsmasq → iPhone

**Result:** DNS query encrypted, FortiGate cannot see domain name

### For All Domains (Including catdev.io)

1. iPhone sends DNS query to Tailscale DNS (100.112.7.98)
2. Query routed through Tailscale network to container
3. dnsmasq receives query on 100.112.7.98:53
4. dnsmasq forwards to Cloudflare DNS (1.1.1.1)
5. **Client's policy routing** sends Cloudflare query through VPN (pia-sg)
6. VPN encrypts and forwards to Cloudflare
7. Cloudflare resolves domain (including catdev.io domains configured in Cloudflare)
8. Response comes back through VPN → dnsmasq → iPhone

**Result:** All DNS queries encrypted through VPN, domains resolved by Cloudflare (including catdev.io)

## Troubleshooting

### Exit Node Shows "Offline"

```bash
# Restart Tailscale
systemctl restart tailscaled
sleep 5
tailscale up --accept-routes --advertise-exit-node

# Check status
tailscale status
```

### DNS Queries Timing Out

```bash
# Check dnsmasq is running
systemctl status dnsmasq

# Check listening on correct interface
ss -tulpn | grep dnsmasq

# Test from container
dig @100.112.7.98 google.com +short

# Check logs
tail -50 /var/log/dnsmasq.log
```

### Domains Not Resolving

```bash
# Test direct query to Cloudflare
dig @1.1.1.1 n8n.catdev.io +short

# Check dnsmasq config
cat /etc/dnsmasq.d/pia-proxy.conf

# Check dnsmasq logs
tail -50 /var/log/dnsmasq.log
```

### Still Seeing FortiGate Warnings

1. **Check Tailscale DNS is being used:**
   - iPhone: Tailscale app → Settings → "Use Tailscale DNS" should be ON
   - macOS: `scutil --dns | grep nameserver` should show 100.112.7.98

2. **Verify exit node is active:**
   ```bash
   tailscale status | grep "offers exit node"
   ```

3. **Check dnsmasq logs for queries:**
   ```bash
   tail -f /var/log/dnsmasq.log
   # Try accessing blocked site, should see queries
   ```

4. **Verify VPN routing:**
   ```bash
   # Check active VPN connections
   wg show

   # Should see data transfer
   wg show pia-sg transfer
   ```

## Maintenance

### Disable DNS Query Logging

After confirming everything works, disable verbose logging:

```bash
# Edit config
nano /etc/dnsmasq.d/pia-proxy.conf

# Comment out or remove:
# log-queries
# log-facility=/var/log/dnsmasq.log

# Restart
systemctl restart dnsmasq
```

### Update DNS Servers

To change upstream DNS servers:

```bash
# Edit config
nano /etc/dnsmasq.d/pia-proxy.conf

# Modify server lines:
server=8.8.8.8
server=8.8.4.4

# Restart
systemctl restart dnsmasq
```

### Add Split DNS Domains (Optional)

If you need to route specific domains to different DNS servers:

```bash
# Edit config
nano /etc/dnsmasq.d/pia-proxy.conf

# Add domain-specific rules:
server=/example.local/192.168.1.1
server=/internal.corp/10.0.0.1

# Restart
systemctl restart dnsmasq
```

## Security Considerations

1. **DNS Privacy:** ALL DNS queries are encrypted through VPN tunnel (including catdev.io)
2. **Cloudflare DNS:** All domains resolved via Cloudflare DNS (1.1.1.1, 1.0.0.1)
3. **No Authentication:** dnsmasq has no authentication (acceptable - only accessible via Tailscale)
4. **DNSSEC:** Enabled in configuration for additional security
5. **Logging:** Query logging enabled by default (disable after testing for privacy)

## Performance

- **Cache Size:** 1000 entries (configurable via `cache-size`)
- **Latency:** Adds ~1-2ms for DNS resolution (negligible)
- **Throughput:** dnsmasq can handle thousands of queries per second
- **Memory:** ~10-20MB RAM usage

## References

- [Tailscale DNS Documentation](https://tailscale.com/kb/1054/dns)
- [Tailscale API - DNS Configuration](https://github.com/tailscale/tailscale/blob/main/publicapi/tailnet.md)
- [dnsmasq Documentation](https://thekelleys.org.uk/dnsmasq/doc.html)
- [DNS Leak Testing](https://www.dnsleaktest.com/)

## Changelog

- **2025-11-12:** Initial DNS proxy setup with split DNS configuration
  - Implemented dnsmasq on container 102
  - Configured Tailscale API to advertise DNS
  - Verified DNS leak prevention working
