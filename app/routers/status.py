"""Status API router for system health and connection status."""

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
import logging
import asyncio
import json
import httpx

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
                "endpoint_ip": interface_details.get("endpoint_ip"),
                "last_handshake": interface_details.get("last_handshake", "N/A"),
                "transfer_rx": interface_details.get("transfer_rx"),
                "transfer_tx": interface_details.get("transfer_tx"),
                "transfer_rx_bytes": interface_details.get("transfer_rx_bytes", 0),
                "transfer_tx_bytes": interface_details.get("transfer_tx_bytes", 0)
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

        # Check VPN connections (multi-region support)
        pia_service = get_pia_service()
        active_connections = await pia_service.get_active_connections()
        vpn_connected = len(active_connections) > 0

        if not vpn_connected:
            messages.append("No VPN connections active")

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
            if vpn_connected:
                healthy = False

        # If everything is OK
        if not messages:
            messages.append("All systems operational")

        return SystemHealth(
            healthy=healthy,
            pia_configured=pia_configured,
            pia_connected=vpn_connected,
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


@router.get("/geolocation/{ip}")
async def get_geolocation(ip: str):
    """Proxy geolocation requests to ipapi.co from server-side to avoid rate limiting.

    Args:
        ip: IP address to geolocate

    Returns:
        Geolocation data from ipapi.co
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"https://ipapi.co/{ip}/json/")

            if response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail="Geolocation service unavailable")

            data = response.json()

            # Validate response
            if not data.get("country_code") or "latitude" not in data or "longitude" not in data:
                raise HTTPException(status_code=500, detail="Invalid geolocation data")

            return data

    except httpx.TimeoutException:
        logger.error(f"Timeout fetching geolocation for {ip}")
        raise HTTPException(status_code=504, detail="Geolocation service timeout")
    except Exception as e:
        logger.error(f"Failed to fetch geolocation for {ip}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.websocket("/ws/vpn-status")
async def websocket_vpn_status(websocket: WebSocket):
    """WebSocket endpoint for real-time VPN status streaming.

    Sends VPN connection status updates every 250ms (4 samples/second) including:
    - Active connections with throughput data
    - Interface details (handshake, transfer stats)
    """
    await websocket.accept()
    logger.info("WebSocket client connected for VPN status streaming")

    try:
        while True:
            try:
                # Get VPN status with detailed connection info
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
                        "endpoint_ip": interface_details.get("endpoint_ip"),
                        "last_handshake": interface_details.get("last_handshake", "N/A"),
                        "transfer_rx": interface_details.get("transfer_rx"),
                        "transfer_tx": interface_details.get("transfer_tx"),
                        "transfer_rx_bytes": interface_details.get("transfer_rx_bytes", 0),
                        "transfer_tx_bytes": interface_details.get("transfer_tx_bytes", 0)
                    })

                # Send data to client
                await websocket.send_json({
                    "active_count": len(connections),
                    "connections": connections,
                    "timestamp": asyncio.get_event_loop().time()
                })

                # Wait 250ms before next update (4 samples/second)
                await asyncio.sleep(0.25)

            except Exception as e:
                logger.error(f"Error in WebSocket update loop: {e}")
                # Send error to client but keep connection alive
                await websocket.send_json({
                    "error": str(e),
                    "active_count": 0,
                    "connections": []
                })
                await asyncio.sleep(0.25)

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        try:
            await websocket.close()
        except:
            pass
