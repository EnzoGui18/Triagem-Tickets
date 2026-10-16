from conftest import run_alembic
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

DOMAIN_TABLES = {
    "tickets",
    "classifier_versions",
    "classifications",
    "processing_jobs",
    "alert_deliveries",
}


async def _table_names(database_url: str) -> set[str]:
    test_engine = create_async_engine(database_url)
    async with test_engine.connect() as connection:
        names = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_table_names()
        )
    await test_engine.dispose()
    return set(names)


async def _domain_enum_names(database_url: str) -> set[str]:
    test_engine = create_async_engine(database_url)
    async with test_engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT typname FROM pg_type "
                "WHERE typname IN ('ticket_status', 'classification_category', "
                "'ticket_priority', 'sentiment', 'job_type', 'job_status', "
                "'alert_delivery_status')"
            )
        )
        names: set[str] = set(result.scalars())
    await test_engine.dispose()
    return names


async def test_migrations_upgrade_and_downgrade(migrated_database_url: str) -> None:
    assert DOMAIN_TABLES <= await _table_names(migrated_database_url)

    await run_alembic(migrated_database_url, "downgrade", "20261016_0001")
    try:
        assert DOMAIN_TABLES.isdisjoint(await _table_names(migrated_database_url))
        assert await _domain_enum_names(migrated_database_url) == set()
    finally:
        await run_alembic(migrated_database_url, "upgrade", "head")
