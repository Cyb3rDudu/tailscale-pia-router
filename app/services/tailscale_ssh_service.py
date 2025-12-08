"""Tailscale SSH service for remotely configuring exit nodes."""

import subprocess
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class TailscaleSSHService:
    """Service to remotely configure Tailscale exit nodes via SSH."""

    async def set_exit_node_via_ssh(
        self,
        device_target: str,
        exit_node_ip: str,
        username: str = "root",
        timeout: int = 30,
        device_hostname: str = None
    ) -> Dict[str, any]:
        """Remotely set exit node on a device via Tailscale SSH.

        Args:
            device_target: Tailscale IP or hostname to SSH to (e.g., "100.104.92.91" or "nas")
            exit_node_ip: Exit node IP (e.g., "100.112.7.98")
            username: SSH username (default: root)
            timeout: Command timeout in seconds
            device_hostname: Optional hostname for logging (if device_target is an IP)

        Returns:
            Dict with success status and output/error
        """
        try:
            # Use hostname for logging if provided, otherwise use target
            log_name = device_hostname or device_target

            # Command to set exit node on remote device
            cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                f"{username}@{device_target}",
                f"tailscale set --exit-node={exit_node_ip} --exit-node-allow-lan-access"
            ]

            logger.info(f"Setting exit node on {log_name} to {exit_node_ip} via SSH")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )

            if result.returncode == 0:
                logger.info(f"Successfully set exit node on {log_name}")
                return {
                    "success": True,
                    "device": log_name,
                    "output": result.stdout.strip(),
                    "method": "ssh"
                }
            else:
                logger.error(f"Failed to set exit node on {log_name}: {result.stderr}")
                return {
                    "success": False,
                    "device": log_name,
                    "error": result.stderr.strip(),
                    "method": "ssh"
                }

        except subprocess.TimeoutExpired:
            logger.error(f"SSH command timed out for {log_name}")
            return {
                "success": False,
                "device": log_name,
                "error": "SSH connection timeout",
                "method": "ssh"
            }
        except Exception as e:
            logger.error(f"Exception setting exit node on {log_name}: {e}")
            return {
                "success": False,
                "device": log_name,
                "error": str(e),
                "method": "ssh"
            }

    async def disable_exit_node_via_ssh(
        self,
        device_target: str,
        username: str = "root",
        device_hostname: str = None
    ) -> Dict[str, any]:
        """Disable exit node on remote device via SSH.

        Args:
            device_target: Tailscale IP or hostname to SSH to
            username: SSH username
            device_hostname: Optional hostname for logging (if device_target is an IP)

        Returns:
            Dict with success status and output/error
        """
        try:
            # Use hostname for logging if provided, otherwise use target
            log_name = device_hostname or device_target

            cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                f"{username}@{device_target}",
                "tailscale set --exit-node="
            ]

            logger.info(f"Disabling exit node on {log_name} via SSH")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                logger.info(f"Successfully disabled exit node on {log_name}")
                return {
                    "success": True,
                    "device": log_name,
                    "output": result.stdout.strip(),
                    "method": "ssh"
                }
            else:
                logger.error(f"Failed to disable exit node on {log_name}: {result.stderr}")
                return {
                    "success": False,
                    "device": log_name,
                    "error": result.stderr.strip(),
                    "method": "ssh"
                }

        except subprocess.TimeoutExpired:
            logger.error(f"SSH command timed out for {log_name}")
            return {
                "success": False,
                "device": log_name,
                "error": "SSH connection timeout",
                "method": "ssh"
            }
        except Exception as e:
            logger.error(f"Exception disabling exit node on {log_name}: {e}")
            return {
                "success": False,
                "device": log_name,
                "error": str(e),
                "method": "ssh"
            }

    async def get_exit_node_via_ssh(
        self,
        device_target: str,
        username: str = "root",
        device_hostname: str = None
    ) -> Optional[str]:
        """Get current exit node setting on remote device via SSH.

        Args:
            device_target: Tailscale IP or hostname to SSH to
            username: SSH username
            device_hostname: Optional hostname for logging (if device_target is an IP)

        Returns:
            Exit node IP if set, empty string if no exit node, None if check failed
        """
        try:
            # Use hostname for logging if provided, otherwise use target
            log_name = device_hostname or device_target

            cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=5",
                f"{username}@{device_target}",
                "tailscale status --json 2>/dev/null | grep -oP '\"ExitNodeOption\":\\s*\"\\K[^\"]*' || echo ''"
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                exit_node = result.stdout.strip()
                logger.debug(f"Current exit node on {log_name}: {exit_node if exit_node else 'none'}")
                return exit_node
            else:
                logger.warning(f"Failed to get exit node from {log_name}: {result.stderr}")
                return None

        except subprocess.TimeoutExpired:
            logger.warning(f"SSH timeout getting exit node from {log_name}")
            return None
        except Exception as e:
            logger.warning(f"Exception getting exit node from {log_name}: {e}")
            return None

    async def check_ssh_connectivity(
        self,
        device_target: str,
        username: str = "root",
        device_hostname: str = None
    ) -> bool:
        """Test if SSH connection works to device.

        Args:
            device_target: Tailscale IP or hostname to SSH to
            username: SSH username
            device_hostname: Optional hostname for logging (if device_target is an IP)

        Returns:
            True if SSH connection successful
        """
        try:
            # Use hostname for logging if provided, otherwise use target
            log_name = device_hostname or device_target

            result = subprocess.run(
                [
                    "ssh",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "ConnectTimeout=5",
                    f"{username}@{device_target}",
                    "echo test"
                ],
                capture_output=True,
                timeout=10
            )
            success = result.returncode == 0
            logger.debug(f"SSH connectivity check for {log_name}: {success}")
            return success
        except Exception as e:
            logger.debug(f"SSH connectivity check failed for {log_name}: {e}")
            return False


# Global service instance
_tailscale_ssh_service: Optional[TailscaleSSHService] = None


def get_tailscale_ssh_service() -> TailscaleSSHService:
    """Get or create Tailscale SSH service instance."""
    global _tailscale_ssh_service
    if _tailscale_ssh_service is None:
        _tailscale_ssh_service = TailscaleSSHService()
    return _tailscale_ssh_service
