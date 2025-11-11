"""Routing service for managing iptables rules and device routing."""

import subprocess
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

TAILSCALE_INTERFACE = "tailscale0"
PIA_INTERFACE = "pia"


class RoutingService:
    """Service for managing iptables routing rules."""

    def __init__(self):
        self.enabled_devices: set[str] = set()

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

    async def enable_device_routing(self, device_ip: str) -> bool:
        """Enable routing for a specific device IP through PIA.

        Args:
            device_ip: Device IP address

        Returns:
            True if successful
        """
        try:
            # Add policy routing rule to route this device through PIA
            # Use routing table 100 for PIA-routed devices
            table_id = 100

            # Check if route already exists in table 100
            result = subprocess.run(
                ["ip", "rule", "list"],
                capture_output=True,
                text=True,
                check=True
            )

            rule_exists = f"from {device_ip} lookup {table_id}" in result.stdout

            if not rule_exists:
                # Add routing rule: traffic from device_ip should use table 100
                subprocess.run(
                    ["ip", "rule", "add", "from", device_ip, "table", str(table_id)],
                    check=True,
                    capture_output=True
                )
                logger.info(f"Added routing rule for {device_ip} to use table {table_id}")

            # Add default route via PIA in table 100
            # Check if route already exists (table might not exist yet, that's OK)
            result = subprocess.run(
                ["ip", "route", "show", "table", str(table_id)],
                capture_output=True,
                text=True,
                check=False
            )

            # Add route if it doesn't exist or table doesn't exist yet
            if result.returncode != 0 or "default dev pia" not in result.stdout:
                # Try to add the route (will create table if it doesn't exist)
                result = subprocess.run(
                    ["ip", "route", "add", "default", "dev", PIA_INTERFACE, "table", str(table_id)],
                    capture_output=True,
                    text=True,
                    check=False
                )

                # If it failed because route already exists, that's OK
                if result.returncode == 0:
                    logger.info(f"Added default route via PIA in table {table_id}")
                elif "File exists" not in result.stderr:
                    # Only raise if it's not a "route exists" error
                    raise subprocess.CalledProcessError(result.returncode, result.args, result.stderr)

            # Add MASQUERADE rule for NAT
            result = subprocess.run(
                ["iptables", "-t", "nat", "-C", "POSTROUTING", "-s", device_ip, "-o", PIA_INTERFACE, "-j", "MASQUERADE"],
                capture_output=True,
                check=False
            )

            if result.returncode != 0:
                subprocess.run(
                    ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", device_ip, "-o", PIA_INTERFACE, "-j", "MASQUERADE"],
                    check=True,
                    capture_output=True
                )
                logger.info(f"Added MASQUERADE rule for {device_ip}")

            self.enabled_devices.add(device_ip)
            logger.info(f"Successfully enabled PIA routing for device {device_ip}")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to enable routing for device {device_ip}: {e}")
            return False

    async def disable_device_routing(self, device_ip: str) -> bool:
        """Disable routing for a specific device IP through PIA.

        Args:
            device_ip: Device_ip address

        Returns:
            True if successful
        """
        try:
            table_id = 100

            # Remove policy routing rule
            subprocess.run(
                ["ip", "rule", "del", "from", device_ip, "table", str(table_id)],
                capture_output=True,
                check=False
            )
            logger.info(f"Removed routing rule for {device_ip}")

            # Remove MASQUERADE rule
            result = subprocess.run(
                ["iptables", "-t", "nat", "-D", "POSTROUTING", "-s", device_ip, "-o", PIA_INTERFACE, "-j", "MASQUERADE"],
                capture_output=True,
                check=False
            )

            if result.returncode == 0:
                logger.info(f"Removed MASQUERADE rule for {device_ip}")
            else:
                logger.warning(f"MASQUERADE rule for {device_ip} not found (may already be removed)")

            self.enabled_devices.discard(device_ip)
            logger.info(f"Successfully disabled PIA routing for device {device_ip}")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to disable routing for device {device_ip}: {e}")
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
