"""Database models and initialization for Tailscale PIA Router."""

import aiosqlite
from pathlib import Path
from typing import Optional
import json
from datetime import datetime

DATABASE_PATH = Path(__file__).parent.parent.parent / "data" / "app.db"


async def get_db():
    """Get database connection."""
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def init_database():
    """Initialize database schema."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    db = await get_db()
    try:
        # Settings table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # PIA regions table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pia_regions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                country TEXT NOT NULL,
                dns TEXT,
                port_forward BOOLEAN DEFAULT 0,
                geo BOOLEAN DEFAULT 0,
                servers TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Tailscale devices table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tailscale_devices (
                id TEXT PRIMARY KEY,
                hostname TEXT NOT NULL,
                ip_addresses TEXT NOT NULL,
                os TEXT,
                last_seen TIMESTAMP,
                online BOOLEAN DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Device routing configuration
        await db.execute("""
            CREATE TABLE IF NOT EXISTS device_routing (
                device_id TEXT PRIMARY KEY,
                enabled BOOLEAN DEFAULT 0,
                region_id TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (device_id) REFERENCES tailscale_devices(id),
                FOREIGN KEY (region_id) REFERENCES pia_regions(id)
            )
        """)

        # Connection log
        await db.execute("""
            CREATE TABLE IF NOT EXISTS connection_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                region_id TEXT,
                status TEXT NOT NULL,
                message TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create indexes
        await db.execute("CREATE INDEX IF NOT EXISTS idx_connection_log_timestamp ON connection_log(timestamp)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_connection_log_event_type ON connection_log(event_type)")

        await db.commit()
    finally:
        await db.close()


class SettingsDB:
    """Database operations for settings."""

    @staticmethod
    async def get(key: str) -> Optional[str]:
        """Get a setting value."""
        db = await get_db()
        try:
            async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
                return row["value"] if row else None
        finally:
            await db.close()

    @staticmethod
    async def set(key: str, value: str):
        """Set a setting value."""
        db = await get_db()
        try:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, datetime.utcnow().isoformat())
            )
            await db.commit()
        finally:
            await db.close()

    @staticmethod
    async def get_json(key: str) -> Optional[dict]:
        """Get a JSON setting value."""
        value = await SettingsDB.get(key)
        return json.loads(value) if value else None

    @staticmethod
    async def set_json(key: str, value: dict):
        """Set a JSON setting value."""
        await SettingsDB.set(key, json.dumps(value))


class PIARegionsDB:
    """Database operations for PIA regions."""

    @staticmethod
    async def upsert(region_id: str, name: str, country: str, dns: str,
                     port_forward: bool, geo: bool, servers: str):
        """Insert or update a PIA region."""
        db = await get_db()
        try:
            await db.execute("""
                INSERT OR REPLACE INTO pia_regions
                (id, name, country, dns, port_forward, geo, servers, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (region_id, name, country, dns, port_forward, geo, servers,
                  datetime.utcnow().isoformat()))
            await db.commit()
        finally:
            await db.close()

    @staticmethod
    async def get_all():
        """Get all PIA regions."""
        db = await get_db()
        try:
            async with db.execute("SELECT * FROM pia_regions ORDER BY name") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        finally:
            await db.close()

    @staticmethod
    async def get_by_id(region_id: str):
        """Get a PIA region by ID."""
        db = await get_db()
        try:
            async with db.execute("SELECT * FROM pia_regions WHERE id = ?", (region_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
        finally:
            await db.close()


class TailscaleDevicesDB:
    """Database operations for Tailscale devices."""

    @staticmethod
    async def upsert(device_id: str, hostname: str, ip_addresses: str,
                     os: str, last_seen: str, online: bool):
        """Insert or update a Tailscale device."""
        db = await get_db()
        try:
            await db.execute("""
                INSERT OR REPLACE INTO tailscale_devices
                (id, hostname, ip_addresses, os, last_seen, online, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (device_id, hostname, ip_addresses, os, last_seen, online,
                  datetime.utcnow().isoformat()))
            await db.commit()
        finally:
            await db.close()

    @staticmethod
    async def get_all():
        """Get all Tailscale devices."""
        db = await get_db()
        try:
            async with db.execute("SELECT * FROM tailscale_devices ORDER BY hostname") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        finally:
            await db.close()

    @staticmethod
    async def get_by_id(device_id: str):
        """Get a Tailscale device by ID."""
        db = await get_db()
        try:
            async with db.execute("SELECT * FROM tailscale_devices WHERE id = ?", (device_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
        finally:
            await db.close()


class DeviceRoutingDB:
    """Database operations for device routing configuration."""

    @staticmethod
    async def set_enabled(device_id: str, enabled: bool, region_id: Optional[str] = None):
        """Set routing enabled status for a device."""
        db = await get_db()
        try:
            # Check if row exists
            async with db.execute(
                "SELECT 1 FROM device_routing WHERE device_id = ?",
                (device_id,)
            ) as cursor:
                exists = await cursor.fetchone()

            if exists:
                # Update existing row, preserving region_id if not provided
                if region_id:
                    await db.execute("""
                        UPDATE device_routing
                        SET enabled = ?, region_id = ?, updated_at = ?
                        WHERE device_id = ?
                    """, (enabled, region_id, datetime.utcnow().isoformat(), device_id))
                else:
                    await db.execute("""
                        UPDATE device_routing
                        SET enabled = ?, updated_at = ?
                        WHERE device_id = ?
                    """, (enabled, datetime.utcnow().isoformat(), device_id))
            else:
                # Insert new row
                await db.execute("""
                    INSERT INTO device_routing (device_id, enabled, region_id, updated_at)
                    VALUES (?, ?, ?, ?)
                """, (device_id, enabled, region_id, datetime.utcnow().isoformat()))

            await db.commit()
        finally:
            await db.close()

    @staticmethod
    async def set_region(device_id: str, region_id: Optional[str]):
        """Set the region for a device (None to clear)."""
        db = await get_db()
        try:
            # Check if row exists
            async with db.execute(
                "SELECT 1 FROM device_routing WHERE device_id = ?",
                (device_id,)
            ) as cursor:
                exists = await cursor.fetchone()

            if exists:
                # Update existing row, preserving enabled state
                await db.execute("""
                    UPDATE device_routing
                    SET region_id = ?, updated_at = ?
                    WHERE device_id = ?
                """, (region_id, datetime.utcnow().isoformat(), device_id))
            else:
                # Insert new row with enabled=False by default
                await db.execute("""
                    INSERT INTO device_routing (device_id, enabled, region_id, updated_at)
                    VALUES (?, 0, ?, ?)
                """, (device_id, region_id, datetime.utcnow().isoformat()))

            await db.commit()
        finally:
            await db.close()

    @staticmethod
    async def get_region(device_id: str) -> Optional[str]:
        """Get the region for a device."""
        db = await get_db()
        try:
            async with db.execute(
                "SELECT region_id FROM device_routing WHERE device_id = ?",
                (device_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return row["region_id"] if row else None
        finally:
            await db.close()

    @staticmethod
    async def is_enabled(device_id: str) -> bool:
        """Check if routing is enabled for a device."""
        db = await get_db()
        try:
            async with db.execute(
                "SELECT enabled FROM device_routing WHERE device_id = ?",
                (device_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return bool(row["enabled"]) if row else False
        finally:
            await db.close()

    @staticmethod
    async def get_all():
        """Get all device routing configurations."""
        db = await get_db()
        try:
            async with db.execute("SELECT * FROM device_routing") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        finally:
            await db.close()

    @staticmethod
    async def get_devices_by_region(region_id: str):
        """Get all devices using a specific region."""
        db = await get_db()
        try:
            async with db.execute(
                "SELECT * FROM device_routing WHERE region_id = ? AND enabled = 1",
                (region_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        finally:
            await db.close()


class ConnectionLogDB:
    """Database operations for connection log."""

    @staticmethod
    async def add(event_type: str, status: str, region_id: Optional[str] = None,
                  message: Optional[str] = None):
        """Add a connection log entry."""
        db = await get_db()
        try:
            await db.execute("""
                INSERT INTO connection_log (event_type, region_id, status, message, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (event_type, region_id, status, message, datetime.utcnow().isoformat()))
            await db.commit()
        finally:
            await db.close()

    @staticmethod
    async def get_recent(limit: int = 100, offset: int = 0):
        """Get recent connection log entries with pagination."""
        db = await get_db()
        try:
            async with db.execute(
                "SELECT * FROM connection_log ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        finally:
            await db.close()

    @staticmethod
    async def get_count():
        """Get total count of log entries."""
        db = await get_db()
        try:
            async with db.execute("SELECT COUNT(*) as count FROM connection_log") as cursor:
                row = await cursor.fetchone()
                return row["count"] if row else 0
        finally:
            await db.close()
