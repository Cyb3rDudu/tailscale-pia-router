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


class PIAService:
    """Service for managing PIA VPN connection."""

    def __init__(self):
        # NOTE: SSL verification disabled due to Python 3.13.5 + OpenSSL 3.5.1 compatibility issue
        # This is acceptable for homelab use with known PIA servers
        self.client = httpx.AsyncClient(timeout=30.0, verify=False)

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

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

    async def write_wireguard_config(self, config: str):
        """Create/update WireGuard connection in NetworkManager.

        Args:
            config: WireGuard configuration content
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

            # Convert DNS to NetworkManager format (semicolon-separated)
            dns_nm = dns.replace(',', ';').replace(' ', '') if dns else ""

            # Generate UUID for connection
            import uuid
            conn_uuid = str(uuid.uuid4())

            # Create NetworkManager keyfile format configuration
            nm_config = f"""[connection]
id={WG_INTERFACE}
uuid={conn_uuid}
type=wireguard
interface-name={WG_INTERFACE}

[wireguard]
private-key={private_key}

[wireguard-peer.{public_key}]
endpoint={endpoint}
allowed-ips={allowed_ips};
persistent-keepalive={keepalive}

[ipv4]
address1={address}
{f'dns={dns_nm};' if dns_nm else ''}
{f'ignore-auto-dns=true' if dns_nm else ''}
method=manual
never-default=yes

[ipv6]
addr-gen-mode=default
method=disabled

[proxy]
"""

            # Write configuration to NetworkManager system-connections directory
            nm_conn_path = Path(f"/etc/NetworkManager/system-connections/{WG_INTERFACE}.nmconnection")

            # Ensure NetworkManager directory exists
            nm_conn_path.parent.mkdir(parents=True, exist_ok=True)

            # Write the configuration
            nm_conn_path.write_text(nm_config)

            # Set correct permissions (NetworkManager requires 0600)
            nm_conn_path.chmod(0o600)

            logger.info(f"Wrote NetworkManager WireGuard configuration to {nm_conn_path}")

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

    async def connect(self) -> bool:
        """Connect to PIA VPN via NetworkManager.

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
        """Disconnect from PIA VPN via NetworkManager.

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


# Global service instance
_pia_service: Optional[PIAService] = None


def get_pia_service() -> PIAService:
    """Get or create PIA service instance."""
    global _pia_service
    if _pia_service is None:
        _pia_service = PIAService()
    return _pia_service
