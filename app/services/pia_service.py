"""PIA VPN service for WireGuard connection management."""

import asyncio
import httpx
import json
import subprocess
from pathlib import Path
from typing import Optional, Dict, List
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

PIA_SERVER_LIST_URL = "https://serverlist.piaservers.net/vpninfo/servers/v6"
PIA_TOKEN_URL = "https://www.privateinternetaccess.com/api/client/v2/token"
WG_CONFIG_PATH = Path("/etc/wireguard/pia.conf")
WG_INTERFACE = "pia"
WG_INTERFACE_PREFIX = "pia-"  # Prefix for per-region interfaces


class PIAService:
    """Service for managing PIA VPN connection."""

    def __init__(self):
        # NOTE: SSL verification disabled due to Python 3.13.5 + OpenSSL 3.5.1 compatibility issue
        # This is acceptable for homelab use with known PIA servers
        # Bind to container's eth0 IP to ensure traffic doesn't go through VPN interfaces
        transport = httpx.AsyncHTTPTransport(local_address="10.36.0.102", verify=False)
        self.client = httpx.AsyncClient(timeout=30.0, verify=False, transport=transport)

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    def _get_interface_name(self, region_id: str) -> str:
        """Get WireGuard interface name for a region.

        Args:
            region_id: PIA region ID

        Returns:
            Interface name (e.g., pia-de, pia-sg, pia-defra)
        """
        # Replace underscores with dashes for valid Linux interface names
        base_name = f"{WG_INTERFACE_PREFIX}{region_id.lower().replace('_', '-')}"

        # Linux interface names must be <= 15 characters
        # Truncate if too long, keeping prefix and shortening region
        if len(base_name) > 15:
            # Keep "pia-" prefix (4 chars) + up to 11 chars of region
            region_part = region_id.lower().replace('_', '-')[:11]
            base_name = f"{WG_INTERFACE_PREFIX}{region_part}"

        return base_name

    async def fetch_server_list(self) -> List[Dict]:
        """Fetch PIA server list from API.

        Returns:
            List of server regions with details
        """
        try:
            response = await self.client.get(PIA_SERVER_LIST_URL)
            response.raise_for_status()

            # PIA response format: JSON on first line, then signature
            # Split on first newline and only parse the JSON part
            response_text = response.text
            json_part = response_text.split('\n')[0]

            # Parse the JSON
            data = json.loads(json_part)
            regions = data.get("regions", [])

            parsed_regions = []
            for region in regions:
                # Only include regions that have WireGuard servers
                servers = region.get("servers", {})
                wg_servers = servers.get("wg", [])

                if not wg_servers:
                    logger.debug(f"Skipping region {region.get('id')} - no WireGuard servers")
                    continue

                parsed_regions.append({
                    "id": region.get("id"),
                    "name": region.get("name"),
                    "country": region.get("country"),
                    "dns": region.get("dns"),
                    "port_forward": region.get("port_forward", False),
                    "geo": region.get("geo", False),
                    "servers": json.dumps(servers)
                })

            logger.info(f"Fetched {len(parsed_regions)} PIA regions with WireGuard support")
            return parsed_regions

        except Exception as e:
            logger.error(f"Failed to fetch PIA server list: {e}")
            raise

    async def get_auth_token(self, username: str, password: str) -> str:
        """Get PIA authentication token.

        Args:
            username: PIA username
            password: PIA password

        Returns:
            Authentication token
        """
        try:
            # Create a temporary client with SSL verification enabled for token request
            # PIA's token endpoint may require proper SSL validation
            async with httpx.AsyncClient(
                timeout=30.0,
                verify=True,  # Enable SSL verification for token endpoint
                follow_redirects=True,
                headers={
                    "User-Agent": "curl/7.81.0"  # Mimic curl user agent
                }
            ) as token_client:
                # Use multipart/form-data (curl --form equivalent)
                response = await token_client.post(
                    PIA_TOKEN_URL,
                    files={
                        "username": (None, username),
                        "password": (None, password)
                    }
                )
                response.raise_for_status()

                data = response.json()
                token = data.get("token")

                if not token:
                    raise ValueError("No token in response")

                logger.info("Successfully obtained PIA auth token")
                return token

        except Exception as e:
            logger.error(f"Failed to get PIA auth token: {e}")
            raise

    def _generate_wireguard_keys(self) -> tuple[str, str]:
        """Generate WireGuard private and public keys.

        Returns:
            Tuple of (private_key, public_key)
        """
        try:
            # Generate private key
            result = subprocess.run(
                ["wg", "genkey"],
                capture_output=True,
                text=True,
                check=True
            )
            private_key = result.stdout.strip()

            # Generate public key from private key
            result = subprocess.run(
                ["wg", "pubkey"],
                input=private_key,
                capture_output=True,
                text=True,
                check=True
            )
            public_key = result.stdout.strip()

            return private_key, public_key

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to generate WireGuard keys: {e}")
            raise

    async def generate_wireguard_config(
        self,
        region_id: str,
        region_data: Dict,
        username: str,
        password: str
    ) -> str:
        """Generate WireGuard configuration for a region.

        Args:
            region_id: PIA region ID
            region_data: Region data from database
            username: PIA username
            password: PIA password

        Returns:
            WireGuard configuration content
        """
        try:
            # Generate WireGuard keys
            private_key, public_key = self._generate_wireguard_keys()

            # Parse servers data
            servers = json.loads(region_data.get("servers", "{}"))
            wg_servers = servers.get("wg", [])

            if not wg_servers:
                raise ValueError(f"No WireGuard servers found for region {region_id}")

            # Use first available server
            server = wg_servers[0]
            server_ip = server.get("ip")
            server_cn = server.get("cn")

            if not server_ip or not server_cn:
                raise ValueError(f"No server IP or CN found for region {region_id}")

            # Call PIA's /addKey endpoint to register our public key and get server details
            # Try two authentication methods:
            # 1. Token-based (official method, requires token API access)
            # 2. Basic Auth with username/password (fallback for blocked networks)
            logger.info(f"Registering with PIA WireGuard server {server_cn} ({server_ip})")

            addkey_data = None
            auth_method = None

            # Method 1: Try token-based authentication
            try:
                logger.info("Attempting token-based authentication")
                token = await self.get_auth_token(username, password)

                # Connect to IP address but set Host header for proper routing
                addkey_response = await self.client.get(
                    f"https://{server_ip}:1337/addKey",
                    params={
                        "pt": token,
                        "pubkey": public_key
                    },
                    headers={"Host": f"{server_cn}:1337"},
                    timeout=10.0
                )
                addkey_response.raise_for_status()
                addkey_data = addkey_response.json()
                auth_method = "token"
                logger.info("Token-based authentication successful")

            except Exception as e:
                logger.warning(f"Token-based authentication failed: {e}")
                logger.info("Falling back to direct Basic Auth")

                # Method 2: Try Basic Auth directly with WireGuard server
                # Connect to IP address to bypass DNS issues
                try:
                    addkey_response = await self.client.get(
                        f"https://{server_ip}:1337/addKey",
                        params={"pubkey": public_key},
                        auth=(username, password),
                        headers={"Host": f"{server_cn}:1337"},
                        timeout=10.0
                    )
                    addkey_response.raise_for_status()
                    addkey_data = addkey_response.json()
                    auth_method = "basic"
                    logger.info("Basic Auth authentication successful")

                except Exception as e2:
                    logger.error(f"Basic Auth also failed: {e2}")
                    raise Exception(
                        f"Both authentication methods failed. "
                        f"Token error: {e}. Basic Auth error: {e2}"
                    )

            # Validate response
            if not addkey_data:
                raise ValueError("Failed to authenticate with PIA server")

            if addkey_data.get("status") != "OK":
                raise ValueError(f"PIA addKey returned non-OK status: {addkey_data}")

            # Extract server details from response
            server_public_key = addkey_data.get("server_key")
            peer_ip = addkey_data.get("peer_ip")
            server_port = addkey_data.get("server_port", 1337)
            dns_servers = addkey_data.get("dns_servers", [])

            if not server_public_key:
                raise ValueError(f"No server_key in addKey response: {addkey_data}")

            if not peer_ip:
                raise ValueError(f"No peer_ip in addKey response: {addkey_data}")

            logger.info(
                f"Successfully registered with PIA server using {auth_method} auth, "
                f"assigned IP: {peer_ip}"
            )

            # Use endpoint from response or fallback
            endpoint = f"{server_ip}:{server_port}"

            # Use DNS servers from response, fallback to region data or PIA defaults
            if dns_servers:
                dns_setting = ",".join(dns_servers)
            else:
                dns_setting = region_data.get("dns", "209.222.18.222,209.222.18.218")

            # Generate config using values from PIA's addKey response
            config = f"""[Interface]
PrivateKey = {private_key}
Address = {peer_ip}
DNS = {dns_setting}

[Peer]
PublicKey = {server_public_key}
Endpoint = {endpoint}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""

            logger.info(f"Generated WireGuard config for region {region_id}")
            return config

        except Exception as e:
            logger.error(f"Failed to generate WireGuard config: {e}")
            raise

    async def write_wireguard_config(self, config: str, region_id: str):
        """Create/update WireGuard connection in NetworkManager.

        Args:
            config: WireGuard configuration content
            region_id: PIA region ID for interface naming
        """
        try:
            # Parse config to extract key parameters
            private_key = None
            address = None
            dns = None
            endpoint = None
            public_key = None
            allowed_ips = None
            keepalive = None

            for line in config.split('\n'):
                line = line.strip()
                if line.startswith('PrivateKey'):
                    private_key = line.split('=', 1)[1].strip()
                elif line.startswith('Address'):
                    address = line.split('=', 1)[1].strip()
                elif line.startswith('DNS'):
                    dns = line.split('=', 1)[1].strip()
                elif line.startswith('Endpoint'):
                    endpoint = line.split('=', 1)[1].strip()
                elif line.startswith('PublicKey'):
                    public_key = line.split('=', 1)[1].strip()
                elif line.startswith('AllowedIPs'):
                    allowed_ips = line.split('=', 1)[1].strip()
                elif line.startswith('PersistentKeepalive'):
                    keepalive = line.split('=', 1)[1].strip()

            if not all([private_key, address, endpoint, public_key]):
                raise ValueError("Missing required WireGuard parameters")

            # Get interface name for this region
            interface_name = self._get_interface_name(region_id)

            # Generate UUID for connection
            import uuid
            conn_uuid = str(uuid.uuid4())

            # Create NetworkManager keyfile format configuration
            # NOTE: DNS is intentionally NOT configured to avoid DNS resolution issues
            # with Tailscale API and other services. The system DNS will be used.
            nm_config = f"""[connection]
id={interface_name}
uuid={conn_uuid}
type=wireguard
interface-name={interface_name}

[wireguard]
private-key={private_key}

[wireguard-peer.{public_key}]
endpoint={endpoint}
allowed-ips={allowed_ips};
persistent-keepalive={keepalive}

[ipv4]
address1={address}
dns-priority=100
ignore-auto-dns=yes
method=manual
never-default=yes

[ipv6]
addr-gen-mode=default
method=disabled

[proxy]
"""

            # Write configuration to NetworkManager system-connections directory
            nm_conn_path = Path(f"/etc/NetworkManager/system-connections/{interface_name}.nmconnection")

            # Ensure NetworkManager directory exists
            nm_conn_path.parent.mkdir(parents=True, exist_ok=True)

            # Write the configuration
            nm_conn_path.write_text(nm_config)

            # Set correct permissions (NetworkManager requires 0600)
            nm_conn_path.chmod(0o600)

            logger.info(f"Wrote NetworkManager WireGuard configuration for {interface_name} to {nm_conn_path}")

            # Reload NetworkManager to pick up the new connection
            subprocess.run(
                ["nmcli", "connection", "reload"],
                check=True,
                capture_output=True
            )
            logger.info("Reloaded NetworkManager connections")

        except Exception as e:
            logger.error(f"Failed to configure WireGuard in NetworkManager: {e}")
            raise

    def _add_server_bypass_rule(self, server_ip: str) -> bool:
        """Add routing rule to bypass VPN for traffic to PIA server itself.

        This prevents a routing loop where WireGuard traffic to the PIA server
        would be routed through the VPN tunnel, breaking the handshake.

        Args:
            server_ip: PIA server IP address

        Returns:
            True if rule added successfully
        """
        try:
            # Check if rule already exists
            result = subprocess.run(
                ["ip", "rule", "list"],
                capture_output=True,
                text=True,
                check=True
            )

            if f"to {server_ip} lookup main" in result.stdout:
                logger.debug(f"Bypass rule for {server_ip} already exists")
                return True

            # Find the lowest available priority between 50-99
            # We use this range to ensure these rules take precedence over VPN routing
            used_priorities = []
            for line in result.stdout.split('\n'):
                if 'lookup main' in line and 'to ' in line:
                    parts = line.split(':')
                    if parts:
                        try:
                            priority = int(parts[0])
                            if 50 <= priority <= 99:
                                used_priorities.append(priority)
                        except ValueError:
                            pass

            # Find first available priority
            priority = 50
            while priority in used_priorities and priority < 100:
                priority += 1

            if priority >= 100:
                logger.warning("No available priority slots for bypass rules (50-99 full)")
                priority = 50  # Reuse, will update existing rule

            # Add routing rule to bypass VPN for this server
            subprocess.run(
                ["ip", "rule", "add", "to", server_ip, "lookup", "main", "priority", str(priority)],
                capture_output=True,
                text=True,
                check=True
            )

            logger.info(f"Added routing bypass rule for PIA server {server_ip} at priority {priority}")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to add bypass rule for {server_ip}: {e.stderr}")
            return False

    async def connect_region(self, region_id: str) -> bool:
        """Connect to PIA VPN for a specific region via NetworkManager.

        Args:
            region_id: PIA region ID

        Returns:
            True if connection successful
        """
        try:
            interface_name = self._get_interface_name(region_id)

            # Enable IP forwarding
            subprocess.run(
                ["sysctl", "-w", "net.ipv4.ip_forward=1"],
                check=True,
                capture_output=True
            )

            # Bring up WireGuard connection via NetworkManager
            result = subprocess.run(
                ["nmcli", "connection", "up", interface_name],
                capture_output=True,
                text=True,
                check=True
            )

            logger.info(f"PIA VPN connected to {region_id} via NetworkManager: {result.stdout}")

            # Get server IP from WireGuard interface to add bypass rule
            try:
                wg_show = subprocess.run(
                    ["wg", "show", interface_name, "endpoints"],
                    capture_output=True,
                    text=True,
                    check=True
                )

                # Parse output: "peer_key   ip:port"
                for line in wg_show.stdout.strip().split('\n'):
                    if line and '\t' in line:
                        endpoint = line.split('\t')[1].strip()
                        if ':' in endpoint:
                            server_ip = endpoint.split(':')[0]
                            # Add routing bypass rule for this server
                            self._add_server_bypass_rule(server_ip)
                            break

            except Exception as e:
                logger.warning(f"Could not add bypass rule for {region_id}: {e}")

            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to connect PIA VPN to {region_id}: {e.stderr}")
            return False

    async def disconnect_region(self, region_id: str) -> bool:
        """Disconnect from PIA VPN for a specific region via NetworkManager and delete the connection.

        Args:
            region_id: PIA region ID

        Returns:
            True if disconnection successful
        """
        try:
            interface_name = self._get_interface_name(region_id)

            # First disconnect if active
            result = subprocess.run(
                ["nmcli", "connection", "down", interface_name],
                capture_output=True,
                text=True,
                check=False  # Don't fail if not active
            )

            # Then delete the connection configuration
            result = subprocess.run(
                ["nmcli", "connection", "delete", interface_name],
                capture_output=True,
                text=True,
                check=False  # Don't fail if doesn't exist
            )

            logger.info(f"PIA VPN connection {interface_name} disconnected and deleted")
            return True

        except Exception as e:
            logger.error(f"Failed to disconnect PIA VPN from {region_id}: {e}")
            return False

    async def connect(self) -> bool:
        """Connect to PIA VPN via NetworkManager (legacy single connection).

        Returns:
            True if connection successful
        """
        try:
            # Enable IP forwarding
            subprocess.run(
                ["sysctl", "-w", "net.ipv4.ip_forward=1"],
                check=True,
                capture_output=True
            )

            # Bring up WireGuard connection via NetworkManager
            result = subprocess.run(
                ["nmcli", "connection", "up", WG_INTERFACE],
                capture_output=True,
                text=True,
                check=True
            )

            logger.info(f"PIA VPN connected via NetworkManager: {result.stdout}")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to connect PIA VPN: {e.stderr}")
            return False

    async def disconnect(self) -> bool:
        """Disconnect from PIA VPN via NetworkManager (legacy single connection).

        Returns:
            True if disconnection successful
        """
        try:
            result = subprocess.run(
                ["nmcli", "connection", "down", WG_INTERFACE],
                capture_output=True,
                text=True,
                check=True
            )

            logger.info(f"PIA VPN disconnected via NetworkManager: {result.stdout}")
            return True

        except subprocess.CalledProcessError as e:
            # If connection doesn't exist or already down, that's fine
            if "not an active connection" in e.stderr.lower() or "no active connection" in e.stderr.lower():
                logger.info("PIA VPN already disconnected")
                return True

            logger.error(f"Failed to disconnect PIA VPN: {e.stderr}")
            return False

    async def get_status(self) -> Dict:
        """Get PIA VPN connection status from NetworkManager.

        Returns:
            Status dictionary with connection info
        """
        try:
            # Check if connection is active
            result = subprocess.run(
                ["nmcli", "connection", "show", "--active"],
                capture_output=True,
                text=True,
                check=True
            )

            is_active = WG_INTERFACE in result.stdout

            if not is_active:
                return {
                    "connected": False,
                    "interface": None,
                    "endpoint": None,
                    "latest_handshake": None,
                    "transfer": None
                }

            # Get detailed connection info
            detail_result = subprocess.run(
                ["nmcli", "connection", "show", WG_INTERFACE],
                capture_output=True,
                text=True,
                check=True
            )

            status = {
                "connected": True,
                "interface": WG_INTERFACE,
                "endpoint": None,
                "latest_handshake": None,
                "transfer": None
            }

            # Parse nmcli output for endpoint
            for line in detail_result.stdout.split('\n'):
                if 'wireguard.peer' in line.lower() and 'endpoint' in line.lower():
                    # Extract endpoint from peer configuration
                    parts = line.split(':')
                    if len(parts) > 1:
                        peer_data = parts[1].strip()
                        if 'endpoint=' in peer_data:
                            endpoint_part = peer_data.split('endpoint=')[1].split(',')[0]
                            status["endpoint"] = endpoint_part.strip()

            # Try to get WireGuard stats if wg command is available
            try:
                wg_result = subprocess.run(
                    ["wg", "show", WG_INTERFACE],
                    capture_output=True,
                    text=True,
                    check=False
                )

                if wg_result.returncode == 0:
                    for line in wg_result.stdout.split('\n'):
                        line = line.strip()
                        if line.startswith("latest handshake:"):
                            status["latest_handshake"] = line.split("latest handshake:", 1)[1].strip()
                        elif line.startswith("transfer:"):
                            status["transfer"] = line.split("transfer:", 1)[1].strip()
            except:
                pass  # WireGuard stats are optional

            return status

        except Exception as e:
            logger.error(f"Failed to get PIA status: {e}")
            return {
                "connected": False,
                "interface": None,
                "endpoint": None,
                "latest_handshake": None,
                "transfer": None
            }

    async def get_public_ip(self) -> Optional[str]:
        """Get public IP address when connected to VPN.

        Returns:
            Public IP address or None
        """
        try:
            response = await self.client.get("https://api.ipify.org?format=json")
            response.raise_for_status()
            data = response.json()
            return data.get("ip")
        except Exception as e:
            logger.error(f"Failed to get public IP: {e}")
            return None

    async def get_active_connections(self) -> List[Dict]:
        """Get list of all active PIA connections.

        Returns:
            List of active connection info dicts with region_id and interface
        """
        try:
            # Get list of active interface names from nmcli
            result = subprocess.run(
                ["nmcli", "connection", "show", "--active"],
                capture_output=True,
                text=True,
                check=True
            )

            active_interfaces = set()
            for line in result.stdout.split('\n'):
                if WG_INTERFACE_PREFIX in line:
                    parts = line.split()
                    if len(parts) > 0:
                        interface_name = parts[0]
                        if interface_name.startswith(WG_INTERFACE_PREFIX):
                            active_interfaces.add(interface_name)

            # Get region_ids from database for enabled devices
            from app.models import DeviceRoutingDB
            routing_configs = await DeviceRoutingDB.get_all()

            active_connections = []
            seen_regions = set()

            for config in routing_configs:
                if config.get("enabled") and config.get("region_id"):
                    region_id = config["region_id"]
                    # Skip if already added
                    if region_id in seen_regions:
                        continue

                    # Check if this region's interface is actually active
                    interface_name = self._get_interface_name(region_id)
                    if interface_name in active_interfaces:
                        active_connections.append({
                            "region_id": region_id,
                            "interface": interface_name,
                            "connected": True
                        })
                        seen_regions.add(region_id)

            logger.info(f"Found {len(active_connections)} active PIA connections")
            return active_connections

        except Exception as e:
            logger.error(f"Failed to get active connections: {e}")
            return []

    async def get_interface_details(self, interface_name: str) -> Dict:
        """Get detailed information about a WireGuard interface.

        Args:
            interface_name: WireGuard interface name (e.g., pia-sg-singapo)

        Returns:
            Dictionary with interface details including handshake time and transfer bytes
        """
        try:
            # Get WireGuard interface stats
            result = subprocess.run(
                ["wg", "show", interface_name],
                capture_output=True,
                text=True,
                check=True
            )

            details = {
                "interface": interface_name,
                "last_handshake": None,
                "transfer_rx": None,
                "transfer_tx": None,
                "transfer_rx_bytes": 0,
                "transfer_tx_bytes": 0
            }

            # Parse wg show output
            for line in result.stdout.split('\n'):
                line = line.strip()
                if line.startswith("latest handshake:"):
                    # Extract handshake time (e.g., "1 minute, 23 seconds ago")
                    handshake_text = line.replace("latest handshake:", "").strip()
                    details["last_handshake"] = handshake_text
                elif line.startswith("transfer:"):
                    # Extract transfer stats (e.g., "12.45 MiB received, 3.21 MiB sent")
                    transfer_text = line.replace("transfer:", "").strip()
                    if "received" in transfer_text and "sent" in transfer_text:
                        parts = transfer_text.split(",")
                        if len(parts) == 2:
                            rx_str = parts[0].strip()
                            tx_str = parts[1].strip()
                            details["transfer_rx"] = rx_str
                            details["transfer_tx"] = tx_str

                            # Parse to bytes for rate calculation
                            details["transfer_rx_bytes"] = self._parse_transfer_to_bytes(rx_str)
                            details["transfer_tx_bytes"] = self._parse_transfer_to_bytes(tx_str)

            return details

        except Exception as e:
            logger.error(f"Failed to get interface details for {interface_name}: {e}")
            return {
                "interface": interface_name,
                "last_handshake": "N/A",
                "transfer_rx": None,
                "transfer_tx": None,
                "transfer_rx_bytes": 0,
                "transfer_tx_bytes": 0
            }

    def _parse_transfer_to_bytes(self, transfer_str: str) -> int:
        """Parse transfer string to bytes.

        Args:
            transfer_str: String like "57.65 MiB received" or "3.21 GiB sent"

        Returns:
            Number of bytes
        """
        if not transfer_str:
            return 0

        # Extract number and unit (e.g., "57.65 MiB received" -> ["57.65", "MiB"])
        import re
        match = re.match(r'([\d.]+)\s*([KMGT]i?B)', transfer_str, re.IGNORECASE)
        if not match:
            return 0

        value = float(match.group(1))
        unit = match.group(2).upper()

        multipliers = {
            'B': 1,
            'KB': 1000,
            'KIB': 1024,
            'MB': 1000 * 1000,
            'MIB': 1024 * 1024,
            'GB': 1000 * 1000 * 1000,
            'GIB': 1024 * 1024 * 1024,
            'TB': 1000 * 1000 * 1000 * 1000,
            'TIB': 1024 * 1024 * 1024 * 1024
        }

        return int(value * multipliers.get(unit, 1))

    async def get_region_status(self, region_id: str) -> Dict:
        """Get status of a specific region connection.

        Args:
            region_id: PIA region ID

        Returns:
            Status dictionary with connection info
        """
        try:
            interface_name = self._get_interface_name(region_id)

            # Check if connection is active
            result = subprocess.run(
                ["nmcli", "connection", "show", "--active"],
                capture_output=True,
                text=True,
                check=True
            )

            is_active = interface_name in result.stdout

            return {
                "region_id": region_id,
                "interface": interface_name,
                "connected": is_active
            }

        except Exception as e:
            logger.error(f"Failed to get region status for {region_id}: {e}")
            return {
                "region_id": region_id,
                "interface": self._get_interface_name(region_id),
                "connected": False
            }

    async def ensure_region_connection(
        self,
        region_id: str,
        region_data: Dict,
        username: str,
        password: str
    ) -> bool:
        """Ensure connection to a region is established.

        Creates config and connects if not already connected.

        Args:
            region_id: PIA region ID
            region_data: Region data from database
            username: PIA username
            password: PIA password

        Returns:
            True if connected successfully
        """
        try:
            # Check if already connected
            status = await self.get_region_status(region_id)
            if status["connected"]:
                logger.info(f"Region {region_id} already connected")
                return True

            # Generate and write config
            logger.info(f"Establishing connection to region {region_id}")
            config = await self.generate_wireguard_config(
                region_id=region_id,
                region_data=region_data,
                username=username,
                password=password
            )
            await self.write_wireguard_config(config, region_id)

            # Connect
            return await self.connect_region(region_id)

        except Exception as e:
            logger.error(f"Failed to ensure connection to region {region_id}: {e}")
            return False

    async def cleanup_unused_connections(self, active_regions: List[str]) -> None:
        """Disconnect and remove connections not in the active regions list.

        Args:
            active_regions: List of region IDs that should stay connected
        """
        try:
            # Get all active connections
            all_active = await self.get_active_connections()

            for conn in all_active:
                region_id = conn["region_id"]
                if region_id not in active_regions:
                    logger.info(f"Cleaning up unused connection to {region_id}")
                    await self.disconnect_region(region_id)

        except Exception as e:
            logger.error(f"Failed to cleanup unused connections: {e}")


# Global service instance
_pia_service: Optional[PIAService] = None


def get_pia_service() -> PIAService:
    """Get or create PIA service instance."""
    global _pia_service
    if _pia_service is None:
        _pia_service = PIAService()
    return _pia_service
