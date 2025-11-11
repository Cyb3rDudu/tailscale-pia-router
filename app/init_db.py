"""Database initialization script."""

import asyncio
from app.models.database import init_database


async def main():
    """Initialize the database."""
    print("Initializing database...")
    await init_database()
    print("Database initialized successfully!")
    print(f"Database location: data/app.db")


if __name__ == "__main__":
    asyncio.run(main())
