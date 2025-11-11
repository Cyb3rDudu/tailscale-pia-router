"""Tailscale service for device management and status monitoring."""

import asyncio
import subprocess
import json
import httpx
import certifi
from typing import Optional, Dict, List
import logging

logger = logging.getLogger(__name__)

TAILSCALE_API_BASE = "https://api.tailscale.com/api/v2"


class TailscaleService:
    """Service for managing Tailscale integration."""

    def __init__(self):
        self.api_key: Optional[str] = None
        self.client: Optional[httpx.AsyncClient] = None

    def set_api_key(self, api_key: str):
        """Set Tailscale API key.

        Args:
            api_key: Tailscale API key
        """
        self.api_key = api_key
        if self.client:
            asyncio.create_task(self.client.aclose())

        self.client = httpx.AsyncClient(
            base_url=TAILSCALE_API_BASE,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
            verify=certifi.where()
        )
        logger.info("Tailscale API key configured")

    async def close(self):
        """Close the HTTP client."""
        if self.client:
            await self.client.aclose()

    async def get_local_status(self) -> Dict:
        """Get local Tailscale status via CLI.

        Returns:
            Status dictionary
        """
        try:
            result = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True,
                text=True,
                check=True
            )

            data = json.loads(result.stdout)

            # Extract key information
            status = {
                "running": True,
                "hostname": data.get("Self", {}).get("HostName"),
                "tailnet": data.get("MagicDNSSuffix", "").replace(".ts.net.", ""),
                "exit_node_enabled": data.get("Self", {}).get("ExitNode", False),
                "peer_count": len(data.get("Peer", {}))
            }

            logger.debug(f"Local Tailscale status: {status}")
            return status

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to get Tailscale status: {e.stderr}")
            return {
                "running": False,
                "hostname": None,
                "tailnet": None,
                "exit_node_enabled": False,
                "peer_count": 0
            }
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Tailscale status: {e}")
            return {
                "running": False,
                "hostname": None,
                "tailnet": None,
                "exit_node_enabled": False,
                "peer_count": 0
            }

    async def get_tailnet_name(self) -> Optional[str]:
        """Get the tailnet name from local status.

        Returns:
            Tailnet name or None
        """
        status = await self.get_local_status()
        tailnet = status.get("tailnet")

        if not tailnet:
            # Fallback: try to extract from hostname
            hostname = status.get("hostname")
            if hostname and ".ts.net" in hostname:
                tailnet = hostname.split(".")[1]

        return tailnet

    async def get_devices_from_api(self) -> List[Dict]:
        """Get devices from Tailscale API.

        Returns:
            List of devices
        """
        if not self.client or not self.api_key:
            logger.warning("Tailscale API not configured")
            return []

        try:
            # Get tailnet name
            tailnet = await self.get_tailnet_name()
            if not tailnet:
                logger.error("Could not determine tailnet name")
                return []

            # Fetch devices
            response = await self.client.get(f"/tailnet/{tailnet}/devices")
            response.raise_for_status()

            data = response.json()
            devices = data.get("devices", [])

            # Parse devices
            parsed_devices = []
            for device in devices:
                parsed_devices.append({
                    "id": device.get("id"),
                    "hostname": device.get("hostname"),
                    "name": device.get("name"),
                    "ip_addresses": device.get("addresses", []),
                    "os": device.get("os"),
                    "last_seen": device.get("lastSeen"),
                    "online": not device.get("expires")  # If no expiry, it's online
                })

            logger.info(f"Fetched {len(parsed_devices)} devices from Tailscale API")
            return parsed_devices

        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch devices from Tailscale API: {e}")
            return []

    async def get_devices_from_cli(self) -> List[Dict]:
        """Get devices from local Tailscale CLI as fallback.

        Returns:
            List of devices
        """
        try:
            result = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True,
                text=True,
                check=True
            )

            data = json.loads(result.stdout)
            peers = data.get("Peer", {})

            devices = []
            for peer_id, peer in peers.items():
                devices.append({
                    "id": peer_id,
                    "hostname": peer.get("HostName"),
                    "name": peer.get("DNSName", "").split(".")[0],
                    "ip_addresses": peer.get("TailscaleIPs", []),
                    "os": peer.get("OS"),
                    "last_seen": peer.get("LastSeen"),
                    "online": peer.get("Online", False)
                })

            # Add self
            self_info = data.get("Self", {})
            if self_info:
                devices.append({
                    "id": self_info.get("ID"),
                    "hostname": self_info.get("HostName"),
                    "name": self_info.get("DNSName", "").split(".")[0],
                    "ip_addresses": self_info.get("TailscaleIPs", []),
                    "os": self_info.get("OS"),
                    "last_seen": None,
                    "online": True
                })

            logger.info(f"Fetched {len(devices)} devices from Tailscale CLI")
            return devices

        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            logger.error(f"Failed to get devices from CLI: {e}")
            return []

    async def get_devices(self) -> List[Dict]:
        """Get all Tailscale devices (try API first, fallback to CLI).

        Returns:
            List of devices
        """
        # Try API first if configured
        if self.api_key:
            devices = await self.get_devices_from_api()
            if devices:
                return devices

        # Fallback to CLI
        return await self.get_devices_from_cli()

    async def is_exit_node_advertised(self) -> bool:
        """Check if this node is advertising as an exit node.

        Returns:
            True if exit node is advertised
        """
        try:
            result = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True,
                text=True,
                check=True
            )

            data = json.loads(result.stdout)
            self_info = data.get("Self", {})

            # Check if AdvertiseExitNode is true
            return self_info.get("AdvertiseExitNode", False)

        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            logger.error(f"Failed to check exit node status: {e}")
            return False

    async def advertise_exit_node(self, enable: bool = True) -> bool:
        """Advertise or un-advertise as exit node.

        Args:
            enable: True to advertise, False to un-advertise

        Returns:
            True if successful
        """
        try:
            flag = "--advertise-exit-node" if enable else "--advertise-exit-node=false"

            result = subprocess.run(
                ["tailscale", "up", flag],
                capture_output=True,
                text=True,
                check=True
            )

            action = "advertised" if enable else "un-advertised"
            logger.info(f"Exit node {action}: {result.stdout}")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to advertise exit node: {e.stderr}")
            return False

    async def get_exit_node_status(self) -> Dict:
        """Get exit node status details.

        Returns:
            Exit node status dictionary
        """
        try:
            result = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True,
                text=True,
                check=True
            )

            data = json.loads(result.stdout)
            self_info = data.get("Self", {})

            return {
                "advertised": self_info.get("AdvertiseExitNode", False),
                "routes": self_info.get("AllowedIPs", []),
                "online": self_info.get("Online", False)
            }

        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            logger.error(f"Failed to get exit node status: {e}")
            return {
                "advertised": False,
                "routes": [],
                "online": False
            }


# Global service instance
_tailscale_service: Optional[TailscaleService] = None


def get_tailscale_service() -> TailscaleService:
    """Get or create Tailscale service instance."""
    global _tailscale_service
    if _tailscale_service is None:
        _tailscale_service = TailscaleService()
    return _tailscale_service
