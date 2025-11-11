"""Devices API router for Tailscale device management."""

from fastapi import APIRouter, HTTPException
import logging
import json

from app.models import (
    TailscaleDeviceList,
    TailscaleDevice,
    DeviceRoutingToggle,
    SuccessResponse,
    TailscaleDevicesDB,
    DeviceRoutingDB,
    ConnectionLogDB,
)
from app.services import (
    get_tailscale_service,
    get_routing_service,
    get_tailscale_ssh_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/devices", tags=["devices"])


@router.get("")
async def get_devices() -> TailscaleDeviceList:
    """Get list of all Tailscale devices.

    Returns:
        List of Tailscale devices with routing status
    """
    try:
        # Fetch devices from Tailscale
        tailscale_service = get_tailscale_service()
        devices = await tailscale_service.get_devices()

        # Update database
        for device in devices:
            await TailscaleDevicesDB.upsert(
                device["id"],
                device["hostname"],
                json.dumps(device["ip_addresses"]),
                device.get("os"),
                device.get("last_seen"),
                device["online"]
            )

        # Get routing status for each device
        device_list = []
        for device in devices:
            routing_enabled = await DeviceRoutingDB.is_enabled(device["id"])

            device_list.append(TailscaleDevice(
                id=device["id"],
                hostname=device["hostname"],
                ip_addresses=device["ip_addresses"],
                os=device.get("os"),
                last_seen=device.get("last_seen"),
                online=device["online"],
                routing_enabled=routing_enabled
            ))

        return TailscaleDeviceList(devices=device_list)

    except Exception as e:
        logger.error(f"Failed to get devices: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{device_id}/toggle")
async def toggle_device_routing(device_id: str, toggle: DeviceRoutingToggle) -> SuccessResponse:
    """Toggle PIA routing for a specific device.

    Args:
        device_id: Tailscale device ID
        toggle: Routing enabled status

    Returns:
        Success response
    """
    try:
        # Get device info
        device = await TailscaleDevicesDB.get_by_id(device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")

        # Parse IP addresses
        ip_addresses = json.loads(device["ip_addresses"])
        if not ip_addresses:
            raise HTTPException(status_code=400, detail="Device has no IP addresses")

        # Use first IP address (Tailscale usually assigns one primary IP)
        device_ip = ip_addresses[0]

        # Update routing
        routing_service = get_routing_service()

        if toggle.enabled:
            # Enable routing
            success = await routing_service.enable_device_routing(device_ip)
            action = "enabled"
        else:
            # Disable routing
            success = await routing_service.disable_device_routing(device_ip)
            action = "disabled"

        if not success:
            raise Exception(f"Failed to {action} routing")

        # Update database
        await DeviceRoutingDB.set_enabled(device_id, toggle.enabled)

        # Log event
        await ConnectionLogDB.add(
            "device_routing",
            "success",
            message=f"Routing {action} for device {device['hostname']} ({device_ip})"
        )

        logger.info(f"Routing {action} for device {device['hostname']} ({device_ip})")

        # Get exit node status and prepare response
        tailscale_service = get_tailscale_service()
        exit_node_status = await tailscale_service.get_exit_node_status()
        container_ip = exit_node_status.get("tailscale_ip")

        response_message = f"Routing {action} for device {device['hostname']}"

        # If enabling routing, attempt SSH automation or provide manual command
        if toggle.enabled and container_ip:
            device_os = device.get("os", "").lower()
            device_hostname = device.get("hostname")

            # Try SSH automation for Linux devices
            ssh_result = None
            if device_os == "linux":
                ssh_service = get_tailscale_ssh_service()
                ssh_result = await ssh_service.set_exit_node_via_ssh(
                    device_target=device_ip,
                    exit_node_ip=container_ip,
                    username="root",
                    device_hostname=device_hostname
                )

            if ssh_result and ssh_result.get("success"):
                # SSH automation succeeded
                response_message = f"Routing enabled and exit node configured automatically for {device['hostname']}"
                logger.info(f"Successfully configured exit node via SSH for {device_hostname}")
            else:
                # SSH failed or not attempted - provide manual command
                manual_command = f"tailscale set --exit-node={container_ip}"

                if device_os == "ios":
                    response_message += f". Open Tailscale app → Exit Node → Select 'pia'"
                elif ssh_result:
                    # SSH was attempted but failed
                    error_msg = ssh_result.get("error", "Unknown error")
                    response_message += f". SSH failed ({error_msg}). Run manually on {device_hostname}: {manual_command}"
                    logger.warning(f"SSH automation failed for {device_hostname}: {error_msg}")
                else:
                    # SSH not attempted (non-Linux)
                    response_message += f". Run this command on {device_hostname}: {manual_command}"

        elif toggle.enabled:
            response_message += ". Warning: Container is not advertising as exit node"

        # If disabling, attempt SSH to clear exit node
        elif not toggle.enabled and container_ip:
            device_os = device.get("os", "").lower()
            device_hostname = device.get("hostname")

            if device_os == "linux":
                ssh_service = get_tailscale_ssh_service()
                ssh_result = await ssh_service.disable_exit_node_via_ssh(
                    device_target=device_ip,
                    username="root",
                    device_hostname=device_hostname
                )

                if ssh_result and ssh_result.get("success"):
                    response_message = f"Routing disabled and exit node cleared for {device['hostname']}"
                else:
                    response_message += f". Run this on {device_hostname} to clear exit node: tailscale set --exit-node="

        return SuccessResponse(message=response_message)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to toggle device routing: {e}")
        await ConnectionLogDB.add(
            "device_routing",
            "error",
            message=f"Failed to toggle device routing: {str(e)}"
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
async def get_devices_status() -> dict:
    """Get routing status for all devices.

    Returns:
        Dictionary of device IDs to routing status
    """
    try:
        routing_configs = await DeviceRoutingDB.get_all()

        status = {}
        for config in routing_configs:
            status[config["device_id"]] = {
                "enabled": config["enabled"],
                "updated_at": config["updated_at"]
            }

        return {"devices": status}

    except Exception as e:
        logger.error(f"Failed to get devices status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sync")
async def sync_devices() -> SuccessResponse:
    """Force sync of Tailscale devices.

    Returns:
        Success response
    """
    try:
        tailscale_service = get_tailscale_service()
        devices = await tailscale_service.get_devices()

        # Update database
        for device in devices:
            await TailscaleDevicesDB.upsert(
                device["id"],
                device["hostname"],
                json.dumps(device["ip_addresses"]),
                device.get("os"),
                device.get("last_seen"),
                device["online"]
            )

        logger.info(f"Synced {len(devices)} Tailscale devices")
        return SuccessResponse(message=f"Synced {len(devices)} devices")

    except Exception as e:
        logger.error(f"Failed to sync devices: {e}")
        raise HTTPException(status_code=500, detail=str(e))
