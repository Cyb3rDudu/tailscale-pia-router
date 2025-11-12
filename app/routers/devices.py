"""Devices API router for Tailscale device management."""

from fastapi import APIRouter, HTTPException, BackgroundTasks
import logging
import json
import asyncio

from app.models import (
    TailscaleDeviceList,
    TailscaleDevice,
    DeviceRoutingToggle,
    DeviceRegionSelect,
    SuccessResponse,
    TailscaleDevicesDB,
    DeviceRoutingDB,
    ConnectionLogDB,
    PIARegionsDB,
    SettingsDB,
)
from app.services import (
    get_tailscale_service,
    get_routing_service,
    get_tailscale_ssh_service,
    get_pia_service,
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
        # Check PIA connection status
        pia_service = get_pia_service()
        pia_status = await pia_service.get_status()
        pia_connected = pia_status.get("connected", False)

        # Fetch devices from Tailscale
        tailscale_service = get_tailscale_service()
        routing_service = get_routing_service()
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
            device_os = device.get("os", "").lower()

            # Determine if device should be auto-managed (macOS/iOS)
            is_auto_managed = device_os in ["macos", "ios"]

            # Get current routing status and region
            routing_enabled = await DeviceRoutingDB.is_enabled(device["id"])
            region_id = await DeviceRoutingDB.get_region(device["id"])

            # Get region name if region is set
            region_name = None
            if region_id:
                region = await PIARegionsDB.get_by_id(region_id)
                if region:
                    region_name = region["name"]

            # Auto-enable/disable routing for GUI clients based on region selection
            if is_auto_managed:
                if region_id and not routing_enabled:
                    # GUI device has region selected, enable routing to that region
                    device_ip = device["ip_addresses"][0] if device["ip_addresses"] else None
                    if device_ip:
                        # Get PIA interface for the selected region
                        pia_service = get_pia_service()
                        pia_interface = pia_service._get_interface_name(region_id)

                        # Check if region connection is active, if not it will be created
                        region_data = await PIARegionsDB.get_by_id(region_id)
                        if region_data:
                            # Ensure connection exists
                            pia_credentials = await SettingsDB.get_json("pia_credentials")
                            if pia_credentials:
                                await pia_service.ensure_region_connection(
                                    region_id=region_id,
                                    region_data=region_data,
                                    username=pia_credentials["username"],
                                    password=pia_credentials["password"]
                                )

                        await routing_service.enable_device_routing(device_ip, pia_interface)
                        await DeviceRoutingDB.set_enabled(device["id"], True)
                        routing_enabled = True
                        logger.info(f"Auto-enabled routing for {device['hostname']} to region {region_id}")
                elif not region_id and routing_enabled:
                    # GUI device has no region selected, disable routing
                    device_ip = device["ip_addresses"][0] if device["ip_addresses"] else None
                    if device_ip:
                        await routing_service.disable_device_routing(device_ip)
                        await DeviceRoutingDB.set_enabled(device["id"], False)
                        routing_enabled = False
                        logger.info(f"Auto-disabled routing for {device['hostname']} (no region selected)")

            device_list.append(TailscaleDevice(
                id=device["id"],
                hostname=device["hostname"],
                ip_addresses=device["ip_addresses"],
                os=device.get("os"),
                last_seen=device.get("last_seen"),
                online=device["online"],
                routing_enabled=routing_enabled,
                auto_managed=is_auto_managed,
                region_id=region_id,
                region_name=region_name
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
        pia_service = get_pia_service()

        if toggle.enabled:
            # Get the device's selected region
            region_id = await DeviceRoutingDB.get_region(device_id)
            if not region_id:
                raise HTTPException(
                    status_code=400,
                    detail="Please select a region for this device first"
                )

            # Get region data
            region = await PIARegionsDB.get_by_id(region_id)
            if not region:
                raise HTTPException(status_code=404, detail="Selected region not found")

            # Get PIA credentials
            pia_credentials = await SettingsDB.get_json("pia_credentials")
            if not pia_credentials:
                raise HTTPException(status_code=400, detail="PIA credentials not configured")

            # Ensure connection to the region
            success = await pia_service.ensure_region_connection(
                region_id=region_id,
                region_data=region,
                username=pia_credentials["username"],
                password=pia_credentials["password"]
            )

            if not success:
                raise Exception(f"Failed to connect to region {region['name']}")

            # Enable routing with the specific PIA interface
            pia_interface = pia_service._get_interface_name(region_id)
            success = await routing_service.enable_device_routing(device_ip, pia_interface)
            action = "enabled"
        else:
            # Disable routing
            success = await routing_service.disable_device_routing(device_ip)
            action = "disabled"

        if not success:
            raise Exception(f"Failed to {action} routing")

        # Update database
        await DeviceRoutingDB.set_enabled(device_id, toggle.enabled)

        # If disabling, check if we need to clean up unused VPN connections
        if not toggle.enabled:
            region_id = await DeviceRoutingDB.get_region(device_id)
            if region_id:
                # Check if any other devices are using this region
                devices_using_region = await DeviceRoutingDB.get_devices_by_region(region_id)
                if not devices_using_region:
                    # No other devices using this region, disconnect VPN
                    pia_service = get_pia_service()
                    await pia_service.disconnect_region(region_id)
                    logger.info(f"Disconnected unused VPN region {region_id}")

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


async def _establish_vpn_and_routing(
    device_id: str,
    region_id: str,
    region_data: dict,
    device_ip: str,
    device_hostname: str,
    username: str,
    password: str
):
    """Background task to establish VPN connection and enable routing."""
    try:
        logger.info(f"Background task: Establishing VPN for {device_hostname} -> {region_data['name']}")

        pia_service = get_pia_service()
        routing_service = get_routing_service()

        # Ensure connection to region
        success = await pia_service.ensure_region_connection(
            region_id=region_id,
            region_data=region_data,
            username=username,
            password=password
        )

        if not success:
            logger.error(f"Background task failed: Could not connect to {region_data['name']}")
            await ConnectionLogDB.add(
                "device_region",
                "error",
                region_id=region_id,
                message=f"Failed to connect to {region_data['name']} for device {device_hostname}"
            )
            return

        # Enable routing
        pia_interface = pia_service._get_interface_name(region_id)
        await routing_service.enable_device_routing(device_ip, pia_interface)

        # Mark as enabled in database
        await DeviceRoutingDB.set_enabled(device_id, True)

        logger.info(f"Background task complete: {device_hostname} now routing through {region_data['name']}")

        await ConnectionLogDB.add(
            "device_region",
            "success",
            region_id=region_id,
            message=f"Region set to {region_data['name']} for device {device_hostname}"
        )

    except Exception as e:
        logger.error(f"Background task error for {device_hostname}: {e}")
        await ConnectionLogDB.add(
            "device_region",
            "error",
            message=f"Background task failed for {device_hostname}: {str(e)}"
        )


@router.post("/{device_id}/region")
async def set_device_region(
    device_id: str,
    region_select: DeviceRegionSelect,
    background_tasks: BackgroundTasks
) -> SuccessResponse:
    """Set PIA region for a specific device, or clear it.

    For GUI devices (macOS/iOS): Auto-enables routing when region is selected, auto-disables when cleared.
    For servers (Linux): Only updates region, routing must be manually toggled.

    Args:
        device_id: Tailscale device ID
        region_select: Region selection (None/empty to clear)

    Returns:
        Success response
    """
    try:
        # Get device info
        device = await TailscaleDevicesDB.get_by_id(device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")

        # Get old region before updating
        old_region_id = await DeviceRoutingDB.get_region(device_id)
        device_os = device.get("os", "").lower()
        is_gui_device = device_os in ["macos", "ios"]

        # Case 1: Clearing region (None or empty string)
        if not region_select.region_id:
            # Disable routing
            ip_addresses = json.loads(device["ip_addresses"])
            if ip_addresses:
                device_ip = ip_addresses[0]
                routing_service = get_routing_service()
                await routing_service.disable_device_routing(device_ip)

            # Clear region from database
            await DeviceRoutingDB.set_region(device_id, None)
            await DeviceRoutingDB.set_enabled(device_id, False)

            # Clean up old VPN if unused
            if old_region_id:
                devices_using_old_region = await DeviceRoutingDB.get_devices_by_region(old_region_id)
                if not devices_using_old_region:
                    pia_service = get_pia_service()
                    await pia_service.disconnect_region(old_region_id)
                    logger.info(f"Disconnected unused VPN region {old_region_id}")

            await ConnectionLogDB.add(
                "device_region",
                "success",
                message=f"Region cleared for device {device['hostname']}"
            )

            return SuccessResponse(message=f"Region cleared and routing disabled for {device['hostname']}")

        # Case 2: Setting a specific region
        # Validate region
        region = await PIARegionsDB.get_by_id(region_select.region_id)
        if not region:
            raise HTTPException(status_code=404, detail="Region not found")

        # Update region in database
        await DeviceRoutingDB.set_region(device_id, region_select.region_id)

        # Check if old region needs cleanup
        if old_region_id and old_region_id != region_select.region_id:
            devices_using_old_region = await DeviceRoutingDB.get_devices_by_region(old_region_id)
            if not devices_using_old_region:
                # No devices using old region anymore, disconnect VPN
                pia_service = get_pia_service()
                await pia_service.disconnect_region(old_region_id)
                logger.info(f"Disconnected unused VPN region {old_region_id}")

        # Setting a region always enables routing (simplified model)
        # The region selection IS the routing toggle

        # Get PIA credentials
        pia_credentials = await SettingsDB.get_json("pia_credentials")
        if not pia_credentials:
            raise HTTPException(status_code=400, detail="PIA credentials not configured")

        # Parse IP addresses
        ip_addresses = json.loads(device["ip_addresses"])
        if not ip_addresses:
            raise HTTPException(status_code=400, detail="Device has no IP addresses")

        device_ip = ip_addresses[0]

        # Check if VPN for this region is already active
        pia_service = get_pia_service()
        active_connections = await pia_service.get_active_connections()
        region_already_active = any(
            conn["region_id"] == region_select.region_id
            for conn in active_connections
        )

        if region_already_active:
            # Region is already connected, enable routing immediately
            routing_service = get_routing_service()
            pia_interface = pia_service._get_interface_name(region_select.region_id)
            await routing_service.enable_device_routing(device_ip, pia_interface)
            await DeviceRoutingDB.set_enabled(device_id, True)

            logger.info(f"Enabled routing for {device['hostname']} to use existing region {region['name']}")

            await ConnectionLogDB.add(
                "device_region",
                "success",
                region_id=region_select.region_id,
                message=f"Region set to {region['name']} for device {device['hostname']}"
            )

            message = f"Region set to {region['name']} and routing enabled for {device['hostname']}"
        else:
            # Region not connected yet, start background task to establish VPN
            logger.info(f"Starting background task to establish VPN for {device['hostname']} -> {region['name']}")

            background_tasks.add_task(
                _establish_vpn_and_routing,
                device_id=device_id,
                region_id=region_select.region_id,
                region_data=region,
                device_ip=device_ip,
                device_hostname=device['hostname'],
                username=pia_credentials["username"],
                password=pia_credentials["password"]
            )

            message = f"Establishing VPN connection to {region['name']} for {device['hostname']}. This may take 10-30 seconds. Routing will be enabled automatically when connected."

        # Add device-specific instructions to message
        if is_gui_device and region_already_active:
            message += " Select this container as exit node in Tailscale app."
        elif not is_gui_device and region_already_active:
            message += " SSH to device and set Tailscale exit node."

        return SuccessResponse(message=message)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to set device region: {e}")
        await ConnectionLogDB.add(
            "device_region",
            "error",
            message=f"Failed to set device region: {str(e)}"
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
