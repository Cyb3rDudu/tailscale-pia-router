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
        return SuccessResponse(message=f"Routing {action} for device {device['hostname']}")

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
