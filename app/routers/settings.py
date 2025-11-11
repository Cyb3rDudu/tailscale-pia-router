"""Settings API router for PIA and Tailscale configuration."""

from fastapi import APIRouter, HTTPException
from typing import Dict
import logging

from app.models import (
    PIACredentials,
    TailscaleAPIKey,
    RegionSelect,
    PIARegionList,
    PIARegion,
    SuccessResponse,
    SettingsDB,
    PIARegionsDB,
    ConnectionLogDB,
)
from app.services import (
    get_pia_service,
    get_tailscale_service,
    get_routing_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.post("/pia")
async def save_pia_credentials(credentials: PIACredentials) -> SuccessResponse:
    """Save PIA credentials.

    Args:
        credentials: PIA username and password

    Returns:
        Success response
    """
    try:
        # Test credentials by getting a token
        pia_service = get_pia_service()
        token = await pia_service.get_auth_token(
            credentials.username,
            credentials.password
        )

        # Save credentials to database
        await SettingsDB.set_json("pia_credentials", {
            "username": credentials.username,
            "password": credentials.password
        })

        # Log event
        await ConnectionLogDB.add("config", "success", message="PIA credentials saved")

        logger.info("PIA credentials saved and validated")
        return SuccessResponse(message="PIA credentials saved successfully")

    except Exception as e:
        logger.error(f"Failed to save PIA credentials: {e}")
        await ConnectionLogDB.add("config", "error", message=f"Failed to save PIA credentials: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/pia")
async def get_pia_credentials() -> Dict:
    """Get PIA credentials (password masked).

    Returns:
        PIA credentials with masked password
    """
    try:
        credentials = await SettingsDB.get_json("pia_credentials")

        if not credentials:
            return {"configured": False, "username": None}

        return {
            "configured": True,
            "username": credentials.get("username"),
            "password": "********"  # Masked
        }

    except Exception as e:
        logger.error(f"Failed to get PIA credentials: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tailscale")
async def save_tailscale_api_key(api_key: TailscaleAPIKey) -> SuccessResponse:
    """Save Tailscale API key.

    Args:
        api_key: Tailscale API key

    Returns:
        Success response
    """
    try:
        # Configure service
        tailscale_service = get_tailscale_service()
        tailscale_service.set_api_key(api_key.api_key)

        # Test API key by fetching tailnet
        tailnet = await tailscale_service.get_tailnet_name()
        if not tailnet:
            raise ValueError("Failed to validate API key")

        # Save to database
        await SettingsDB.set("tailscale_api_key", api_key.api_key)

        # Log event
        await ConnectionLogDB.add("config", "success", message="Tailscale API key saved")

        logger.info("Tailscale API key saved and validated")
        return SuccessResponse(message="Tailscale API key saved successfully")

    except Exception as e:
        logger.error(f"Failed to save Tailscale API key: {e}")
        await ConnectionLogDB.add("config", "error", message=f"Failed to save Tailscale API key: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/tailscale")
async def get_tailscale_settings() -> Dict:
    """Get Tailscale settings (API key masked).

    Returns:
        Tailscale settings with masked API key
    """
    try:
        api_key = await SettingsDB.get("tailscale_api_key")

        return {
            "configured": bool(api_key),
            "api_key": "********" if api_key else None
        }

    except Exception as e:
        logger.error(f"Failed to get Tailscale settings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/regions")
async def get_regions() -> PIARegionList:
    """Get list of available PIA regions.

    Returns:
        List of PIA regions
    """
    try:
        # Try to get from database first
        regions = await PIARegionsDB.get_all()

        if not regions:
            # Fetch from PIA API and cache
            pia_service = get_pia_service()
            fresh_regions = await pia_service.fetch_server_list()

            # Save to database
            for region in fresh_regions:
                await PIARegionsDB.upsert(
                    region["id"],
                    region["name"],
                    region["country"],
                    region["dns"],
                    region["port_forward"],
                    region["geo"],
                    region["servers"]
                )

            regions = await PIARegionsDB.get_all()

        # Convert to response format
        region_list = [
            PIARegion(
                id=r["id"],
                name=r["name"],
                country=r["country"],
                dns=r.get("dns"),
                port_forward=r["port_forward"],
                geo=r["geo"]
            )
            for r in regions
        ]

        return PIARegionList(regions=region_list)

    except Exception as e:
        logger.error(f"Failed to get regions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/regions/refresh")
async def refresh_regions() -> SuccessResponse:
    """Refresh PIA regions from API.

    Returns:
        Success response
    """
    try:
        pia_service = get_pia_service()
        regions = await pia_service.fetch_server_list()

        # Update database
        for region in regions:
            await PIARegionsDB.upsert(
                region["id"],
                region["name"],
                region["country"],
                region["dns"],
                region["port_forward"],
                region["geo"],
                region["servers"]
            )

        logger.info(f"Refreshed {len(regions)} PIA regions")
        return SuccessResponse(message=f"Refreshed {len(regions)} regions")

    except Exception as e:
        logger.error(f"Failed to refresh regions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/region/select")
async def select_region(selection: RegionSelect) -> SuccessResponse:
    """Select a PIA region.

    Args:
        selection: Region ID to select

    Returns:
        Success response
    """
    try:
        # Get region data
        region = await PIARegionsDB.get_by_id(selection.region_id)
        if not region:
            raise HTTPException(status_code=404, detail="Region not found")

        # Get PIA credentials
        credentials = await SettingsDB.get_json("pia_credentials")
        if not credentials:
            raise HTTPException(status_code=400, detail="PIA credentials not configured")

        # Get auth token
        pia_service = get_pia_service()
        token = await pia_service.get_auth_token(
            credentials["username"],
            credentials["password"]
        )

        # Generate WireGuard config
        config = await pia_service.generate_wireguard_config(
            selection.region_id,
            region,
            token
        )

        # Write config
        await pia_service.write_wireguard_config(config)

        # Save selected region
        await SettingsDB.set("selected_region", selection.region_id)

        # Log event
        await ConnectionLogDB.add(
            "region_change",
            "success",
            region_id=selection.region_id,
            message=f"Selected region: {region['name']}"
        )

        logger.info(f"Selected PIA region: {region['name']}")
        return SuccessResponse(message=f"Selected region: {region['name']}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to select region: {e}")
        await ConnectionLogDB.add(
            "region_change",
            "error",
            message=f"Failed to select region: {str(e)}"
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/connection/toggle")
async def toggle_connection() -> Dict:
    """Toggle PIA VPN connection (connect/disconnect).

    Returns:
        Connection status
    """
    try:
        pia_service = get_pia_service()
        status = await pia_service.get_status()

        if status["connected"]:
            # Disconnect
            success = await pia_service.disconnect()

            if success:
                # Clean up routing rules
                routing_service = get_routing_service()
                await routing_service.cleanup_rules()

                await ConnectionLogDB.add("disconnect", "success", message="Disconnected from PIA VPN")
                logger.info("Disconnected from PIA VPN")
                return {"action": "disconnect", "success": True, "connected": False}
            else:
                raise Exception("Failed to disconnect")

        else:
            # Connect
            # Check if region is selected
            selected_region = await SettingsDB.get("selected_region")
            if not selected_region:
                raise HTTPException(status_code=400, detail="No region selected")

            success = await pia_service.connect()

            if success:
                # Setup routing
                routing_service = get_routing_service()
                await routing_service.enable_ip_forwarding()
                await routing_service.setup_base_rules()

                await ConnectionLogDB.add(
                    "connect",
                    "success",
                    region_id=selected_region,
                    message="Connected to PIA VPN"
                )
                logger.info("Connected to PIA VPN")
                return {"action": "connect", "success": True, "connected": True}
            else:
                raise Exception("Failed to connect")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to toggle connection: {e}")
        await ConnectionLogDB.add(
            "connection_error",
            "error",
            message=f"Failed to toggle connection: {str(e)}"
        )
        raise HTTPException(status_code=500, detail=str(e))
