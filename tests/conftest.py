import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from triagem.core.database import get_session
from triagem.main import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


async def run_alembic(database_url: str, operation: str, revision: str) -> None:
    config = _alembic_config(database_url)
    action = command.upgrade if operation == "upgrade" else command.downgrade
    await asyncio.to_thread(action, config, revision)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def temporary_database_url() -> AsyncIterator[str]:
    configured_url = os.getenv("TEST_DATABASE_URL")
    if configured_url is None:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL persistence tests")

    base_url = make_url(configured_url)
    database_name = f"triagem_test_{uuid4().hex}"
    admin_url = base_url.set(database=base_url.database or "postgres")
    database_url = base_url.set(database=database_name)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")

    async with admin_engine.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    try:
        yield database_url.render_as_string(hide_password=False)
    finally:
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            await connection.execute(text(f'DROP DATABASE "{database_name}"'))
        await admin_engine.dispose()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def migrated_database_url(temporary_database_url: str) -> AsyncIterator[str]:
    await run_alembic(temporary_database_url, "upgrade", "head")
    yield temporary_database_url
    await run_alembic(temporary_database_url, "downgrade", "base")


@pytest_asyncio.fixture
async def db_session(migrated_database_url: str) -> AsyncIterator[AsyncSession]:
    test_engine = create_async_engine(migrated_database_url)
    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            if transaction.is_active:
                await transaction.rollback()
    await test_engine.dispose()


@pytest_asyncio.fixture
async def api_client(migrated_database_url: str) -> AsyncIterator[AsyncClient]:
    test_engine = create_async_engine(migrated_database_url)
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)
        await test_engine.dispose()
