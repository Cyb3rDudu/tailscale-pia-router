"""Status API router for system health and connection status."""

from fastapi import APIRouter, HTTPException
import logging

from app.models import (
    PIAStatus,
    TailscaleStatus,
    SystemHealth,
    ConnectionLogList,
    ConnectionLogEntry,
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

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("/pia")
async def get_pia_status() -> PIAStatus:
    """Get PIA VPN connection status.

    Returns:
        PIA connection status
    """
    try:
        pia_service = get_pia_service()
        status = await pia_service.get_status()

        # Get selected region
        selected_region_id = await SettingsDB.get("selected_region")
        region_name = None

        if selected_region_id:
            region = await PIARegionsDB.get_by_id(selected_region_id)
            if region:
                region_name = region["name"]

        # Get public IP if connected
        public_ip = None
        if status["connected"]:
            public_ip = await pia_service.get_public_ip()

        return PIAStatus(
            connected=status["connected"],
            region_id=selected_region_id,
            region_name=region_name,
            ip_address=public_ip,
            interface=status.get("interface")
        )

    except Exception as e:
        logger.error(f"Failed to get PIA status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/vpn")
async def get_vpn_status() -> dict:
    """Get VPN connection status summary (all active connections).

    Returns:
        VPN status with active connection count and detailed connection info
    """
    try:
        pia_service = get_pia_service()
        active_connections = await pia_service.get_active_connections()

        # Get detailed info for each active connection
        connections = []
        for conn in active_connections:
            region_id = conn["region_id"]
            interface = conn["interface"]

            # Get region name
            region = await PIARegionsDB.get_by_id(region_id)
            region_name = region["name"] if region else region_id

            # Get interface details (handshake time, transfer stats)
            interface_details = await pia_service.get_interface_details(interface)

            connections.append({
                "region_id": region_id,
                "region_name": region_name,
                "interface": interface,
                "last_handshake": interface_details.get("last_handshake", "N/A"),
                "transfer_rx": interface_details.get("transfer_rx"),
                "transfer_tx": interface_details.get("transfer_tx")
            })

        return {
            "active_count": len(connections),
            "connections": connections
        }

    except Exception as e:
        logger.error(f"Failed to get VPN status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tailscale")
async def get_tailscale_status() -> TailscaleStatus:
    """Get Tailscale connection status.

    Returns:
        Tailscale status
    """
    try:
        tailscale_service = get_tailscale_service()
        status = await tailscale_service.get_local_status()

        # Get exit node status
        exit_node_status = await tailscale_service.get_exit_node_status()

        return TailscaleStatus(
            running=status["running"],
            exit_node_enabled=exit_node_status["advertised"],
            hostname=status.get("hostname"),
            tailnet=status.get("tailnet")
        )

    except Exception as e:
        logger.error(f"Failed to get Tailscale status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def get_system_health() -> SystemHealth:
    """Get overall system health check.

    Returns:
        System health status
    """
    messages = []
    healthy = True

    try:
        # Check PIA configuration
        pia_credentials = await SettingsDB.get_json("pia_credentials")
        pia_configured = bool(pia_credentials)

        if not pia_configured:
            messages.append("PIA credentials not configured")
            healthy = False

        # Check PIA connection
        pia_service = get_pia_service()
        pia_status = await pia_service.get_status()
        pia_connected = pia_status["connected"]

        if not pia_connected:
            messages.append("PIA VPN not connected")

        # Check Tailscale
        tailscale_service = get_tailscale_service()
        ts_status = await tailscale_service.get_local_status()
        tailscale_running = ts_status["running"]

        if not tailscale_running:
            messages.append("Tailscale not running")
            healthy = False

        # Check IP forwarding
        routing_service = get_routing_service()
        ip_forwarding = await routing_service.is_ip_forwarding_enabled()

        if not ip_forwarding:
            messages.append("IP forwarding not enabled")
            if pia_connected:
                healthy = False

        # If everything is OK
        if not messages:
            messages.append("All systems operational")

        return SystemHealth(
            healthy=healthy,
            pia_configured=pia_configured,
            pia_connected=pia_connected,
            tailscale_running=tailscale_running,
            ip_forwarding_enabled=ip_forwarding,
            messages=messages
        )

    except Exception as e:
        logger.error(f"Failed to get system health: {e}")
        return SystemHealth(
            healthy=False,
            pia_configured=False,
            pia_connected=False,
            tailscale_running=False,
            ip_forwarding_enabled=False,
            messages=[f"Health check failed: {str(e)}"]
        )


@router.get("/logs")
async def get_connection_logs(limit: int = 50, offset: int = 0) -> ConnectionLogList:
    """Get recent connection logs with pagination.

    Args:
        limit: Maximum number of log entries to return (default: 50)
        offset: Number of entries to skip (default: 0)

    Returns:
        List of connection log entries with pagination metadata
    """
    try:
        logs = await ConnectionLogDB.get_recent(limit, offset)
        total = await ConnectionLogDB.get_count()

        entries = [
            ConnectionLogEntry(
                id=log["id"],
                event_type=log["event_type"],
                region_id=log.get("region_id"),
                status=log["status"],
                message=log.get("message"),
                timestamp=log["timestamp"]
            )
            for log in logs
        ]

        return ConnectionLogList(
            entries=entries,
            total=total,
            limit=limit,
            offset=offset
        )

    except Exception as e:
        logger.error(f"Failed to get connection logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))
