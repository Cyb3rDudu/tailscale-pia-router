"""Main FastAPI application for Tailscale PIA Router."""

import logging
import json
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

    logger.info("Application startup complete")

    yield

    # Shutdown
    logger.info("Shutting down application...")


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
