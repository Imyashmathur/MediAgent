import os
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

if os.getenv("APP_ENV", "development") != "development":
    raise RuntimeError("Only APP_ENV=development is supported by this development build")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./mediagent.db")
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def enable_sqlite_foreign_keys(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
Session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncIterator[AsyncSession]:
    async with Session() as db:
        yield db
