"""Routing service for managing iptables rules and device routing."""

import subprocess
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

TAILSCALE_INTERFACE = "tailscale0"
PIA_INTERFACE = "pia"
PIA_INTERFACE_PREFIX = "pia-"
BASE_ROUTING_TABLE = 100  # Start routing tables from 100


class RoutingService:
    """Service for managing iptables routing rules."""

    def __init__(self):
        self.enabled_devices: set[str] = set()
        self.device_table_map: dict[str, int] = {}  # Map device_ip -> table_id
        self.next_table_id: int = BASE_ROUTING_TABLE

    async def enable_ip_forwarding(self) -> bool:
        """Enable IP forwarding.

        Returns:
            True if successful
        """
        try:
            subprocess.run(
                ["sysctl", "-w", "net.ipv4.ip_forward=1"],
                check=True,
                capture_output=True
            )
            subprocess.run(
                ["sysctl", "-w", "net.ipv6.conf.all.forwarding=1"],
                check=True,
                capture_output=True
            )
            logger.info("IP forwarding enabled")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to enable IP forwarding: {e}")
            return False

    async def is_ip_forwarding_enabled(self) -> bool:
        """Check if IP forwarding is enabled.

        Returns:
            True if IP forwarding is enabled
        """
        try:
            result = subprocess.run(
                ["sysctl", "net.ipv4.ip_forward"],
                capture_output=True,
                text=True,
                check=True
            )
            return "= 1" in result.stdout
        except subprocess.CalledProcessError:
            return False

    async def setup_base_rules(self) -> bool:
        """Setup base iptables rules for NAT.

        Returns:
            True if successful
        """
        try:
            # Enable MASQUERADE for PIA interface
            subprocess.run(
                ["iptables", "-t", "nat", "-C", "POSTROUTING", "-o", PIA_INTERFACE, "-j", "MASQUERADE"],
                capture_output=True,
                check=False
            )
            # If check failed (rule doesn't exist), add it
            result = subprocess.run(
                ["iptables", "-t", "nat", "-C", "POSTROUTING", "-o", PIA_INTERFACE, "-j", "MASQUERADE"],
                capture_output=True,
                check=False
            )
            if result.returncode != 0:
                subprocess.run(
                    ["iptables", "-t", "nat", "-A", "POSTROUTING", "-o", PIA_INTERFACE, "-j", "MASQUERADE"],
                    check=True,
                    capture_output=True
                )
                logger.info("Added MASQUERADE rule for PIA interface")

            # Allow forwarding from Tailscale to PIA
            subprocess.run(
                ["iptables", "-C", "FORWARD", "-i", TAILSCALE_INTERFACE, "-o", PIA_INTERFACE, "-j", "ACCEPT"],
                capture_output=True,
                check=False
            )
            result = subprocess.run(
                ["iptables", "-C", "FORWARD", "-i", TAILSCALE_INTERFACE, "-o", PIA_INTERFACE, "-j", "ACCEPT"],
                capture_output=True,
                check=False
            )
            if result.returncode != 0:
                subprocess.run(
                    ["iptables", "-A", "FORWARD", "-i", TAILSCALE_INTERFACE, "-o", PIA_INTERFACE, "-j", "ACCEPT"],
                    check=True,
                    capture_output=True
                )
                logger.info("Added FORWARD rule Tailscale -> PIA")

            # Allow return traffic
            subprocess.run(
                ["iptables", "-C", "FORWARD", "-i", PIA_INTERFACE, "-o", TAILSCALE_INTERFACE, "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
                capture_output=True,
                check=False
            )
            result = subprocess.run(
                ["iptables", "-C", "FORWARD", "-i", PIA_INTERFACE, "-o", TAILSCALE_INTERFACE, "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
                capture_output=True,
                check=False
            )
            if result.returncode != 0:
                subprocess.run(
                    ["iptables", "-A", "FORWARD", "-i", PIA_INTERFACE, "-o", TAILSCALE_INTERFACE, "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
                    check=True,
                    capture_output=True
                )
                logger.info("Added FORWARD rule PIA -> Tailscale (established)")

            logger.info("Base routing rules configured")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to setup base rules: {e}")
            return False

    async def enable_device_routing(self, device_ip: str, pia_interface: str) -> bool:
        """Enable routing for a specific device IP through a PIA interface.

        Args:
            device_ip: Device IP address
            pia_interface: PIA interface name (e.g., pia-de, pia-sg)

        Returns:
            True if successful
        """
        try:
            # Assign a routing table for this device if not already assigned
            if device_ip not in self.device_table_map:
                self.device_table_map[device_ip] = self.next_table_id
                self.next_table_id += 1

            table_id = self.device_table_map[device_ip]

            # Check if route already exists
            result = subprocess.run(
                ["ip", "rule", "list"],
                capture_output=True,
                text=True,
                check=True
            )

            rule_exists = f"from {device_ip} lookup {table_id}" in result.stdout

            if not rule_exists:
                # Add routing rule: traffic from device_ip should use its assigned table
                subprocess.run(
                    ["ip", "rule", "add", "from", device_ip, "table", str(table_id)],
                    check=True,
                    capture_output=True
                )
                logger.info(f"Added routing rule for {device_ip} to use table {table_id}")

            # Clear any existing routes in this table
            subprocess.run(
                ["ip", "route", "flush", "table", str(table_id)],
                capture_output=True,
                check=False
            )

            # Add exception routes BEFORE default route (more specific routes take precedence)

            # Exception 1: Tailscale network should use main routing table
            subprocess.run(
                ["ip", "route", "add", "100.64.0.0/10", "dev", TAILSCALE_INTERFACE, "table", str(table_id)],
                capture_output=True,
                check=False
            )
            logger.info(f"Added Tailscale network exception in table {table_id}")

            # Exception 2: Local network should use main routing table
            # Get default gateway from main table
            gateway_result = subprocess.run(
                ["ip", "route", "show", "default"],
                capture_output=True,
                text=True,
                check=False
            )
            if gateway_result.returncode == 0 and "via" in gateway_result.stdout:
                # Extract gateway IP and interface
                parts = gateway_result.stdout.strip().split()
                if "via" in parts:
                    gateway_idx = parts.index("via") + 1
                    gateway_ip = parts[gateway_idx]

                    # Add route for local network through default gateway
                    subprocess.run(
                        ["ip", "route", "add", "10.36.0.0/22", "via", gateway_ip, "table", str(table_id)],
                        capture_output=True,
                        check=False
                    )
                    logger.info(f"Added local network exception via {gateway_ip} in table {table_id}")

            # Add default route via PIA interface in this device's table
            result = subprocess.run(
                ["ip", "route", "add", "default", "dev", pia_interface, "table", str(table_id)],
                capture_output=True,
                text=True,
                check=False
            )

            if result.returncode == 0:
                logger.info(f"Added default route via {pia_interface} in table {table_id} for {device_ip}")
            elif "File exists" not in result.stderr:
                # Only raise if it's not a "route exists" error
                logger.warning(f"Failed to add route for {device_ip}: {result.stderr}")

            # Add MASQUERADE rule for NAT
            result = subprocess.run(
                ["iptables", "-t", "nat", "-C", "POSTROUTING", "-s", device_ip, "-o", pia_interface, "-j", "MASQUERADE"],
                capture_output=True,
                check=False
            )

            if result.returncode != 0:
                subprocess.run(
                    ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", device_ip, "-o", pia_interface, "-j", "MASQUERADE"],
                    check=True,
                    capture_output=True
                )
                logger.info(f"Added MASQUERADE rule for {device_ip} via {pia_interface}")

            # Add FORWARD rules for this PIA interface if not already present
            await self.ensure_forward_rules(pia_interface)

            self.enabled_devices.add(device_ip)
            logger.info(f"Successfully enabled routing for device {device_ip} via {pia_interface}")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to enable routing for device {device_ip}: {e}")
            return False

    async def disable_device_routing(self, device_ip: str) -> bool:
        """Disable routing for a specific device IP through PIA.

        Args:
            device_ip: Device IP address

        Returns:
            True if successful
        """
        try:
            # Get the table ID for this device
            if device_ip not in self.device_table_map:
                logger.warning(f"Device {device_ip} not in routing table map")
                return True

            table_id = self.device_table_map[device_ip]

            # Remove policy routing rule
            subprocess.run(
                ["ip", "rule", "del", "from", device_ip, "table", str(table_id)],
                capture_output=True,
                check=False
            )
            logger.info(f"Removed routing rule for {device_ip}")

            # Flush routes in this table
            subprocess.run(
                ["ip", "route", "flush", "table", str(table_id)],
                capture_output=True,
                check=False
            )

            # Remove all MASQUERADE rules for this device
            # We need to iterate and remove because we don't know which interface it was using
            while True:
                result = subprocess.run(
                    ["iptables", "-t", "nat", "-L", "POSTROUTING", "-n", "--line-numbers"],
                    capture_output=True,
                    text=True,
                    check=False
                )

                found_rule = False
                for line in result.stdout.split('\n'):
                    if device_ip in line and "MASQUERADE" in line:
                        # Extract rule number (first column)
                        parts = line.split()
                        if len(parts) > 0 and parts[0].isdigit():
                            rule_num = parts[0]
                            subprocess.run(
                                ["iptables", "-t", "nat", "-D", "POSTROUTING", rule_num],
                                capture_output=True,
                                check=False
                            )
                            logger.info(f"Removed MASQUERADE rule #{rule_num} for {device_ip}")
                            found_rule = True
                            break

                if not found_rule:
                    break

            # Remove from tracking
            del self.device_table_map[device_ip]
            self.enabled_devices.discard(device_ip)
            logger.info(f"Successfully disabled PIA routing for device {device_ip}")
            return True

        except Exception as e:
            logger.error(f"Failed to disable routing for device {device_ip}: {e}")
            return False

    async def ensure_forward_rules(self, pia_interface: str) -> bool:
        """Ensure FORWARD rules exist for a PIA interface.

        Args:
            pia_interface: PIA interface name (e.g., pia-de, pia-sg)

        Returns:
            True if successful
        """
        try:
            # Allow forwarding from Tailscale to PIA interface
            result = subprocess.run(
                ["iptables", "-C", "FORWARD", "-i", TAILSCALE_INTERFACE, "-o", pia_interface, "-j", "ACCEPT"],
                capture_output=True,
                check=False
            )

            if result.returncode != 0:
                subprocess.run(
                    ["iptables", "-A", "FORWARD", "-i", TAILSCALE_INTERFACE, "-o", pia_interface, "-j", "ACCEPT"],
                    check=True,
                    capture_output=True
                )
                logger.info(f"Added FORWARD rule Tailscale -> {pia_interface}")

            # Allow return traffic
            result = subprocess.run(
                ["iptables", "-C", "FORWARD", "-i", pia_interface, "-o", TAILSCALE_INTERFACE, "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
                capture_output=True,
                check=False
            )

            if result.returncode != 0:
                subprocess.run(
                    ["iptables", "-A", "FORWARD", "-i", pia_interface, "-o", TAILSCALE_INTERFACE, "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
                    check=True,
                    capture_output=True
                )
                logger.info(f"Added FORWARD rule {pia_interface} -> Tailscale (established)")

            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to ensure forward rules for {pia_interface}: {e}")
            return False

    async def clear_device_rules(self) -> bool:
        """Clear all device-specific routing rules.

        Returns:
            True if successful
        """
        try:
            # Flush NAT table POSTROUTING chain (be careful!)
            # This is aggressive - in production you might want to be more selective
            for device_ip in list(self.enabled_devices):
                await self.disable_device_routing(device_ip)

            logger.info("Cleared all device routing rules")
            return True

        except Exception as e:
            logger.error(f"Failed to clear device rules: {e}")
            return False

    async def cleanup_rules(self) -> bool:
        """Remove all PIA-related iptables rules.

        Returns:
            True if successful
        """
        try:
            # Remove device-specific rules
            await self.clear_device_rules()

            # Remove base rules
            subprocess.run(
                ["iptables", "-t", "nat", "-D", "POSTROUTING", "-o", PIA_INTERFACE, "-j", "MASQUERADE"],
                capture_output=True,
                check=False
            )

            subprocess.run(
                ["iptables", "-D", "FORWARD", "-i", TAILSCALE_INTERFACE, "-o", PIA_INTERFACE, "-j", "ACCEPT"],
                capture_output=True,
                check=False
            )

            subprocess.run(
                ["iptables", "-D", "FORWARD", "-i", PIA_INTERFACE, "-o", TAILSCALE_INTERFACE, "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
                capture_output=True,
                check=False
            )

            logger.info("Cleaned up routing rules")
            return True

        except Exception as e:
            logger.error(f"Failed to cleanup rules: {e}")
            return False

    async def get_active_rules(self) -> List[str]:
        """Get list of active iptables rules related to PIA.

        Returns:
            List of rule descriptions
        """
        try:
            result = subprocess.run(
                ["iptables", "-t", "nat", "-L", "POSTROUTING", "-v", "-n"],
                capture_output=True,
                text=True,
                check=True
            )

            rules = []
            for line in result.stdout.split("\n"):
                if PIA_INTERFACE in line:
                    rules.append(line.strip())

            return rules

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to get active rules: {e}")
            return []

    async def save_rules(self) -> bool:
        """Save current iptables rules to persist across reboots.

        Returns:
            True if successful
        """
        try:
            # Try iptables-save (Debian/Ubuntu)
            result = subprocess.run(
                ["which", "iptables-save"],
                capture_output=True,
                check=False
            )

            if result.returncode == 0:
                subprocess.run(
                    ["sh", "-c", "iptables-save > /etc/iptables/rules.v4"],
                    check=True,
                    capture_output=True
                )
                logger.info("Saved iptables rules")
                return True

            logger.warning("iptables-save not found, rules not persisted")
            return False

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to save rules: {e}")
            return False


# Global service instance
_routing_service: Optional[RoutingService] = None


def get_routing_service() -> RoutingService:
    """Get or create routing service instance."""
    global _routing_service
    if _routing_service is None:
        _routing_service = RoutingService()
    return _routing_service
