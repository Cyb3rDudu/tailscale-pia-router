"""Main FastAPI application for Tailscale PIA Router."""

import logging
import json
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

from app.models import init_database, SettingsDB, TailscaleDevicesDB, DeviceRoutingDB, PIARegionsDB
from app.routers import settings, devices, status
from app.services import get_tailscale_service, get_pia_service, get_routing_service

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Get paths
BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Ensure static directory exists
STATIC_DIR.mkdir(exist_ok=True)

# Templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


async def restore_routing_rules() -> int:
    """Restore routing rules for all enabled devices on startup.

    Returns:
        Number of devices restored
    """
    restored = 0

    try:
        # Ensure container traffic always uses main table (not VPN)
        import subprocess
        result = subprocess.run(
            ["ip", "rule", "list"],
            capture_output=True,
            text=True,
            check=True
        )

        # Check if container routing rule exists
        if "from 10.36.0.102 lookup main" not in result.stdout:
            subprocess.run(
                ["ip", "rule", "add", "from", "10.36.0.102", "table", "main", "priority", "100"],
                check=True,
                capture_output=True
            )
            logger.info("Added container routing rule to prevent VPN interference")


        # Get all routing configurations
        routing_configs = await DeviceRoutingDB.get_all()

        # Get PIA credentials
        pia_credentials = await SettingsDB.get_json("pia_credentials")
        if not pia_credentials:
            logger.warning("PIA credentials not configured, skipping routing restoration")
            return 0

        # Get services
        pia_service = get_pia_service()
        routing_service = get_routing_service()

        # Restore routing for each enabled device
        for config in routing_configs:
            if not config.get("enabled") or not config.get("region_id"):
                continue

            device_id = config["device_id"]
            region_id = config["region_id"]

            # Get device info
            device = await TailscaleDevicesDB.get_by_id(device_id)
            if not device:
                logger.warning(f"Device {device_id} not found, skipping")
                continue

            # Parse IP addresses
            ip_addresses = json.loads(device["ip_addresses"])
            if not ip_addresses:
                logger.warning(f"Device {device['hostname']} has no IP addresses, skipping")
                continue

            device_ip = ip_addresses[0]

            # Get region info
            region = await PIARegionsDB.get_by_id(region_id)
            if not region:
                logger.warning(f"Region {region_id} not found for device {device['hostname']}, skipping")
                continue

            try:
                # Ensure VPN connection
                success = await pia_service.ensure_region_connection(
                    region_id=region_id,
                    region_data=region,
                    username=pia_credentials["username"],
                    password=pia_credentials["password"]
                )

                if not success:
                    logger.error(f"Failed to connect to region {region['name']} for device {device['hostname']}")
                    continue

                # Enable routing
                pia_interface = pia_service._get_interface_name(region_id)
                success = await routing_service.enable_device_routing(device_ip, pia_interface)

                if success:
                    logger.info(f"Restored routing for {device['hostname']} ({device_ip}) -> {region['name']}")
                    restored += 1
                else:
                    logger.error(f"Failed to enable routing for device {device['hostname']}")

            except Exception as e:
                logger.error(f"Failed to restore routing for device {device['hostname']}: {e}")
                continue

    except Exception as e:
        logger.error(f"Error in restore_routing_rules: {e}")

    return restored


async def reconciliation_loop():
    """Background task that continuously reconciles NetworkManager state with database.

    This loop runs every 5 seconds and ensures that:
    - VPN connections that should be up are actually up
    - Routing rules match the database configuration
    - Failed connections are automatically restored
    - Device exit nodes match expected configuration (endpoint drift detection)
    """
    import subprocess
    from app.services import get_tailscale_ssh_service

    logger.info("Starting reconciliation loop...")

    while True:
        try:
            await asyncio.sleep(5)  # Check every 5 seconds

            # Get all routing configurations from database
            routing_configs = await DeviceRoutingDB.get_all()

            # Get PIA credentials
            pia_credentials = await SettingsDB.get_json("pia_credentials")
            if not pia_credentials:
                continue

            # Get services
            pia_service = get_pia_service()
            routing_service = get_routing_service()
            tailscale_service = get_tailscale_service()
            ssh_service = get_tailscale_ssh_service()

            # Get the expected exit node IP (PIA container)
            exit_node_status = await tailscale_service.get_exit_node_status()
            expected_exit_node_ip = exit_node_status.get("tailscale_ip")

            # Track which connections should be active
            expected_connections = set()

            # Collect devices that need drift checking (for parallel execution)
            devices_to_check_drift = []

            # Check each enabled device
            for config in routing_configs:
                if not config.get("enabled") or not config.get("region_id"):
                    continue

                device_id = config["device_id"]
                region_id = config["region_id"]
                expected_connections.add(region_id)

                # Get device info
                device = await TailscaleDevicesDB.get_by_id(device_id)
                if not device:
                    continue

                # Parse IP addresses
                ip_addresses = json.loads(device["ip_addresses"])
                if not ip_addresses:
                    continue

                device_ip = ip_addresses[0]

                # Get region info
                region = await PIARegionsDB.get_by_id(region_id)
                if not region:
                    continue

                # Check if VPN connection is actually up
                interface_name = pia_service._get_interface_name(region_id)

                # Check if interface exists
                result = subprocess.run(
                    ["ip", "link", "show", interface_name],
                    capture_output=True,
                    check=False
                )

                interface_exists = result.returncode == 0

                if not interface_exists:
                    logger.warning(f"Reconciliation: Interface {interface_name} missing for {device['hostname']}, restoring...")

                    # Restore VPN connection
                    success = await pia_service.ensure_region_connection(
                        region_id=region_id,
                        region_data=region,
                        username=pia_credentials["username"],
                        password=pia_credentials["password"]
                    )

                    if not success:
                        logger.error(f"Reconciliation: Failed to restore VPN for {device['hostname']}")
                        continue

                    logger.info(f"Reconciliation: Restored VPN connection {interface_name}")

                # Check if routing rule exists
                result = subprocess.run(
                    ["ip", "rule", "list"],
                    capture_output=True,
                    text=True,
                    check=True
                )

                # Get table ID for this device
                if device_ip in routing_service.device_table_map:
                    table_id = routing_service.device_table_map[device_ip]
                    rule_exists = f"from {device_ip} lookup {table_id}" in result.stdout

                    if not rule_exists:
                        logger.warning(f"Reconciliation: Routing rule missing for {device['hostname']} ({device_ip}), restoring...")

                        # Restore routing rule
                        success = await routing_service.enable_device_routing(device_ip, interface_name)

                        if success:
                            logger.info(f"Reconciliation: Restored routing for {device['hostname']}")
                        else:
                            logger.error(f"Reconciliation: Failed to restore routing for {device['hostname']}")

                # Collect device for parallel drift checking (only Linux devices)
                device_os = device.get("os", "").lower()
                if device_os == "linux" and expected_exit_node_ip:
                    devices_to_check_drift.append({
                        "device": device,
                        "device_ip": device_ip,
                        "expected_exit_node_ip": expected_exit_node_ip
                    })

            # Perform drift checks in parallel to avoid timing issues with many devices
            if devices_to_check_drift:
                async def check_and_fix_drift(drift_info):
                    """Check drift for a single device and fix if needed."""
                    device = drift_info["device"]
                    device_ip = drift_info["device_ip"]
                    expected = drift_info["expected_exit_node_ip"]

                    try:
                        # Get current exit node on device via SSH
                        current_exit_node = await ssh_service.get_exit_node_via_ssh(
                            device_target=device_ip,
                            username="root",
                            device_hostname=device['hostname']
                        )

                        # Check for drift (None means SSH failed, skip in that case)
                        if current_exit_node is not None and current_exit_node != expected:
                            logger.warning(
                                f"Reconciliation: Exit node drift detected on {device['hostname']} "
                                f"(current: {current_exit_node or 'none'}, expected: {expected}), restoring..."
                            )

                            # Restore correct exit node
                            ssh_result = await ssh_service.set_exit_node_via_ssh(
                                device_target=device_ip,
                                exit_node_ip=expected,
                                username="root",
                                device_hostname=device['hostname']
                            )

                            if ssh_result and ssh_result.get("success"):
                                logger.info(f"Reconciliation: Restored exit node on {device['hostname']}")
                            else:
                                logger.error(f"Reconciliation: Failed to restore exit node on {device['hostname']}")

                    except Exception as e:
                        logger.debug(f"Reconciliation: Could not check exit node on {device['hostname']}: {e}")

                # Execute all drift checks in parallel
                await asyncio.gather(
                    *[check_and_fix_drift(info) for info in devices_to_check_drift],
                    return_exceptions=True
                )

        except Exception as e:
            logger.error(f"Error in reconciliation loop: {e}", exc_info=True)
            # Continue the loop even if there's an error
            continue


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events."""
    # Startup
    logger.info("Starting Tailscale PIA Router application...")

    # Initialize database
    try:
        await init_database()
        logger.info("Database initialized")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")

    # Load Tailscale API key if configured
    try:
        api_key = await SettingsDB.get("tailscale_api_key")
        if api_key:
            tailscale_service = get_tailscale_service()
            tailscale_service.set_api_key(api_key)
            logger.info("Tailscale API key loaded")
    except Exception as e:
        logger.error(f"Failed to load Tailscale API key: {e}")

    # Restore routing rules for enabled devices
    try:
        logger.info("Restoring routing rules for enabled devices...")
        restored_count = await restore_routing_rules()
        logger.info(f"Restored routing for {restored_count} devices")
    except Exception as e:
        logger.error(f"Failed to restore routing rules: {e}")

    # Start background reconciliation loop
    reconciliation_task = asyncio.create_task(reconciliation_loop())
    logger.info("Background reconciliation loop started")

    logger.info("Application startup complete")

    yield

    # Shutdown
    logger.info("Shutting down application...")
    reconciliation_task.cancel()
    try:
        await reconciliation_task
    except asyncio.CancelledError:
        logger.info("Reconciliation loop stopped")


# Create FastAPI app
app = FastAPI(
    title="Tailscale PIA Router",
    description="Web application to manage PIA VPN as a Tailscale exit node",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(settings.router)
app.include_router(devices.router)
app.include_router(status.router)

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main dashboard page."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings page."""
    return templates.TemplateResponse("settings.html", {"request": request})


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
