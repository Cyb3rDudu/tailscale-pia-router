# Tailscale SSH Setup Guide

This guide explains how to configure Tailscale SSH to enable automatic exit node configuration for Linux devices via the web UI.

## Overview

The PIA router can automatically configure exit nodes on Linux devices using Tailscale SSH. When you toggle routing for a Linux device via the web UI, the container will SSH into the device and run `tailscale set --exit-node=<container-ip>` automatically.

**Important:** macOS and iOS devices use automatic routing based on PIA connection status and do not require SSH setup.

## Prerequisites

- Tailscale admin console access
- Root/sudo access to Linux devices
- Container running with hostname `pia` (IP: 10.36.0.102)

## Step 1: Configure Tailscale ACL

1. Go to https://login.tailscale.com/admin/acls
2. Add the following ACL configuration:

```json
{
  "tagOwners": {
    "tag:pia-router": ["your-email@example.com"],
    "tag:ssh-targets": ["your-email@example.com"]
  },
  "acls": [
    {
      "action": "accept",
      "src": ["*"],
      "dst": ["*:*"]
    }
  ],
  "ssh": [
    {
      "action": "accept",
      "src": ["tag:pia-router"],
      "dst": ["tag:ssh-targets"],
      "users": ["root"]
    }
  ]
}
```

**Replace** `your-email@example.com` with your actual Tailscale account email.

3. Click **Save** to apply the ACL

## Step 2: Tag the PIA Container

1. In Tailscale admin console, go to **Machines**
2. Find the device named `pia` (your container)
3. Click the **...** menu → **Edit ACL tags**
4. Add tag: `tag:pia-router`
5. Click **Save**

## Step 3: Enable Tailscale SSH on Target Devices

SSH into each Linux device you want to auto-configure and run:

```bash
tailscale set --ssh --accept-risk=lose-ssh
```

**Supported devices:**
- Ubuntu/Debian servers
- Proxmox LXC containers
- Any Linux system with Tailscale installed

**Not supported:**
- macOS App Store version (see below)
- iOS devices (use Tailscale app UI)
- Windows (not tested)

### macOS Tailscale SSH

macOS requires the **open source tailscaled** variant to support SSH server functionality:

| Variant | SSH Server | GUI | Notes |
|---------|-----------|-----|-------|
| App Store | ❌ | ✅ | Cannot be SSH server |
| Standalone | ❌ | ✅ | Cannot be SSH server |
| Open source tailscaled | ✅ | ❌ | CLI only, no GUI |

**Recommendation for macOS:** Keep the GUI version and use automatic routing (no SSH needed). Routing automatically follows PIA connection status.

## Step 4: Tag Target Devices

For each Linux device that should receive SSH automation:

1. In Tailscale admin console, go to **Machines**
2. Find the device (e.g., `nas`, `trader`, `carrier`)
3. Click the **...** menu → **Edit ACL tags**
4. Add tag: `tag:ssh-targets`
5. Click **Save**

## Step 5: Test SSH Connectivity

From the PIA container, test SSH access:

```bash
# SSH to container first
ssh root@10.36.0.102

# Test SSH to a target device (use Tailscale IP)
ssh root@100.104.92.91 "echo 'SSH test successful'"
```

If you see "SSH test successful", the setup is complete!

## Step 6: Test Exit Node Automation

1. Open the web UI: http://pia.local:8000 or http://10.36.0.102:8000
2. Find a Linux device in the device list
3. Toggle routing **ON**
4. You should see: "Routing enabled and exit node configured automatically for {hostname}"

Verify the device is using PIA:

```bash
ssh root@{device-ip} "curl -s http://ifconfig.me"
```

This should show the PIA VPN IP address.

## Troubleshooting

### SSH Connection Fails

**Error:** `tailscale: tailnet policy does not permit you to SSH to this node`

**Solution:**
- Verify ACL is saved correctly in admin console
- Confirm `tag:pia-router` is applied to container
- Confirm `tag:ssh-targets` is applied to target device
- Check both tag owners are set to your email

### Hostname Resolution Fails

**Error:** `ssh: Could not resolve hostname nas: Name or service not known`

**Solution:** The SSH service uses IP addresses, not hostnames. This is expected and handled automatically.

### Device Not Auto-Configuring

**Symptoms:**
- Web UI shows: "SSH failed. Run manually on {hostname}: tailscale set --exit-node=..."

**Solutions:**
1. Verify Tailscale SSH is enabled on target device:
   ```bash
   ssh {device} "tailscale status --json" | grep AdvertiseSSH
   ```
   Should show `"AdvertiseSSH": true`

2. Check if device has `tag:ssh-targets`:
   ```bash
   tailscale status --json | grep -A 5 "Hostname.*{device}"
   ```

3. Test SSH from container manually:
   ```bash
   ssh root@10.36.0.102
   ssh root@{device-tailscale-ip} "tailscale set --exit-node=100.112.7.98"
   ```

### Exit Node Already Advertising

**Error:** `Cannot advertise an exit node and use an exit node at the same time`

**Solution:** Device is advertising itself as an exit node. Disable it:

```bash
ssh {device} "tailscale up --advertise-exit-node=false --accept-dns=false --ssh"
```

## Device Type Behavior

| Device Type | Routing Control | SSH Required | Notes |
|-------------|----------------|--------------|-------|
| Linux servers | Manual toggle via web UI | ✅ Yes | Automated exit node config via SSH |
| macOS | Automatic (follows PIA status) | ❌ No | Use native Tailscale GUI to select exit node |
| iOS | Automatic (follows PIA status) | ❌ No | Use Tailscale app to select exit node |

## Architecture

```
┌─────────────────┐
│   Web UI        │  User toggles routing for Linux device
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ PIA Container   │  Receives toggle request
│ (tag:pia-router)│
└────────┬────────┘
         │ SSH over Tailscale
         ▼
┌─────────────────┐
│ Linux Device    │  Executes: tailscale set --exit-node={container-ip}
│ (tag:ssh-targets)│
└─────────────────┘
         │
         ▼
    Exit node configured ✓
    Traffic → Container → PIA VPN → Internet
```

## Security Considerations

- SSH access is restricted by Tailscale ACL (only container can SSH to targets)
- Uses Tailscale SSH (WireGuard keys for auth, no password/SSH keys needed)
- Only `root` user access is granted
- Network traffic stays within Tailscale mesh (encrypted)
- Container cannot SSH to devices without `tag:ssh-targets`

## Additional Resources

- [Tailscale SSH Documentation](https://tailscale.com/kb/1193/tailscale-ssh)
- [Tailscale ACL Syntax](https://tailscale.com/kb/1018/acls)
- [Tailscale macOS Variants](https://tailscale.com/kb/1065/macos-variants)
