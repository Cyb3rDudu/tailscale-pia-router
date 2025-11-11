"""Pydantic schemas for API requests and responses."""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


# Settings Schemas
class PIACredentials(BaseModel):
    """PIA credentials."""
    username: str
    password: str


class TailscaleAPIKey(BaseModel):
    """Tailscale API key."""
    api_key: str


class RegionSelect(BaseModel):
    """Region selection request."""
    region_id: str


# PIA Region Schemas
class PIARegion(BaseModel):
    """PIA region information."""
    id: str
    name: str
    country: str
    dns: Optional[str] = None
    port_forward: bool = False
    geo: bool = False


class PIARegionList(BaseModel):
    """List of PIA regions."""
    regions: List[PIARegion]


# Tailscale Device Schemas
class TailscaleDevice(BaseModel):
    """Tailscale device information."""
    id: str
    hostname: str
    ip_addresses: List[str]
    os: Optional[str] = None
    last_seen: Optional[datetime] = None
    online: bool = False
    routing_enabled: bool = False
    auto_managed: bool = False  # True for macOS/iOS with automatic routing
    region_id: Optional[str] = None  # PIA region for this device
    region_name: Optional[str] = None  # PIA region name for display


class TailscaleDeviceList(BaseModel):
    """List of Tailscale devices."""
    devices: List[TailscaleDevice]


class DeviceRoutingToggle(BaseModel):
    """Toggle device routing."""
    enabled: bool


class DeviceRegionSelect(BaseModel):
    """Select region for a device."""
    region_id: str


# Status Schemas
class PIAStatus(BaseModel):
    """PIA connection status."""
    connected: bool
    region_id: Optional[str] = None
    region_name: Optional[str] = None
    ip_address: Optional[str] = None
    interface: Optional[str] = None


class TailscaleStatus(BaseModel):
    """Tailscale status."""
    running: bool
    exit_node_enabled: bool
    hostname: Optional[str] = None
    tailnet: Optional[str] = None


class SystemHealth(BaseModel):
    """System health check."""
    healthy: bool
    pia_configured: bool
    pia_connected: bool
    tailscale_running: bool
    ip_forwarding_enabled: bool
    messages: List[str] = Field(default_factory=list)


# Connection Log Schemas
class ConnectionLogEntry(BaseModel):
    """Connection log entry."""
    id: int
    event_type: str
    region_id: Optional[str] = None
    status: str
    message: Optional[str] = None
    timestamp: datetime


class ConnectionLogList(BaseModel):
    """List of connection log entries."""
    entries: List[ConnectionLogEntry]
    total: int = 0
    limit: int = 100
    offset: int = 0


# Response Schemas
class SuccessResponse(BaseModel):
    """Generic success response."""
    success: bool = True
    message: Optional[str] = None


class ErrorResponse(BaseModel):
    """Generic error response."""
    success: bool = False
    error: str
