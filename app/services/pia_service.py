"""PIA VPN service for WireGuard connection management."""

import asyncio
import httpx
import json
import subprocess
import base64
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
                parsed_regions.append({
                    "id": region.get("id"),
                    "name": region.get("name"),
                    "country": region.get("country"),
                    "dns": region.get("dns"),
                    "port_forward": region.get("port_forward", False),
                    "geo": region.get("geo", False),
                    "servers": json.dumps(region.get("servers", {}))
                })

            logger.info(f"Fetched {len(parsed_regions)} PIA regions")
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
            # Use basic auth
            auth = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers = {"Authorization": f"Basic {auth}"}

            response = await self.client.post(PIA_TOKEN_URL, headers=headers)
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
        username: str = None,
        password: str = None
    ) -> str:
        """Generate WireGuard configuration for a region.

        Args:
            region_id: PIA region ID
            region_data: Region data from database
            username: PIA username (optional, for future use)
            password: PIA password (optional, for future use)

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

            if not server_ip:
                raise ValueError(f"No server IP found for region {region_id}")

            # Get server public key from server data or meta
            server_public_key = server.get("pk")
            if not server_public_key:
                # Try getting from meta
                meta = servers.get("meta", [])
                if meta:
                    server_public_key = meta[0].get("pubkey")

            if not server_public_key:
                raise ValueError(f"No server public key found for region {region_id}")

            endpoint = f"{server_ip}:1337"

            # DNS servers
            dns_servers = region_data.get("dns", "209.222.18.222,209.222.18.218")

            # Generate config
            config = f"""[Interface]
PrivateKey = {private_key}
Address = 10.0.0.2/32
DNS = {dns_servers}

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
        """Write WireGuard configuration to file.

        Args:
            config: WireGuard configuration content
        """
        try:
            # Ensure directory exists
            WG_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

            # Write config
            WG_CONFIG_PATH.write_text(config)

            # Set permissions (only root can read)
            WG_CONFIG_PATH.chmod(0o600)

            logger.info(f"Wrote WireGuard config to {WG_CONFIG_PATH}")

        except Exception as e:
            logger.error(f"Failed to write WireGuard config: {e}")
            raise

    async def connect(self) -> bool:
        """Connect to PIA VPN via WireGuard.

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

            # Bring up WireGuard interface
            result = subprocess.run(
                ["wg-quick", "up", WG_INTERFACE],
                capture_output=True,
                text=True,
                check=True
            )

            logger.info(f"PIA VPN connected: {result.stdout}")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to connect PIA VPN: {e.stderr}")
            return False

    async def disconnect(self) -> bool:
        """Disconnect from PIA VPN.

        Returns:
            True if disconnection successful
        """
        try:
            result = subprocess.run(
                ["wg-quick", "down", WG_INTERFACE],
                capture_output=True,
                text=True,
                check=True
            )

            logger.info(f"PIA VPN disconnected: {result.stdout}")
            return True

        except subprocess.CalledProcessError as e:
            # If interface doesn't exist, that's fine
            if "does not exist" in e.stderr or "Cannot find device" in e.stderr:
                logger.info("PIA VPN already disconnected")
                return True

            logger.error(f"Failed to disconnect PIA VPN: {e.stderr}")
            return False

    async def get_status(self) -> Dict:
        """Get PIA VPN connection status.

        Returns:
            Status dictionary with connection info
        """
        try:
            result = subprocess.run(
                ["wg", "show", WG_INTERFACE],
                capture_output=True,
                text=True,
                check=False
            )

            if result.returncode != 0:
                return {
                    "connected": False,
                    "interface": None,
                    "endpoint": None,
                    "latest_handshake": None,
                    "transfer": None
                }

            # Parse wg show output
            output = result.stdout
            lines = output.strip().split("\n")

            status = {
                "connected": True,
                "interface": WG_INTERFACE,
                "endpoint": None,
                "latest_handshake": None,
                "transfer": None
            }

            for line in lines:
                line = line.strip()
                if line.startswith("endpoint:"):
                    status["endpoint"] = line.split("endpoint:", 1)[1].strip()
                elif line.startswith("latest handshake:"):
                    status["latest_handshake"] = line.split("latest handshake:", 1)[1].strip()
                elif line.startswith("transfer:"):
                    status["transfer"] = line.split("transfer:", 1)[1].strip()

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
