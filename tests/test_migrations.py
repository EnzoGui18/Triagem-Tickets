from decimal import Decimal
from uuid import uuid4

from conftest import run_alembic
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

DOMAIN_TABLES = {
    "tickets",
    "classifier_versions",
    "classifications",
    "processing_jobs",
    "classification_attempts",
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


async def _classification_columns(database_url: str) -> set[str]:
    test_engine = create_async_engine(database_url)
    async with test_engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_columns("classifications")
        )
    await test_engine.dispose()
    return {str(column["name"]) for column in columns}


async def _index_names(database_url: str, table_name: str) -> set[str]:
    test_engine = create_async_engine(database_url)
    async with test_engine.connect() as connection:
        indexes = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_indexes(table_name)
        )
    await test_engine.dispose()
    return {str(index["name"]) for index in indexes}


async def _insert_classification_with_usage(database_url: str) -> str:
    test_engine = create_async_engine(database_url)
    identifier = uuid4()
    async with test_engine.begin() as connection:
        ticket_id = await connection.scalar(
            text(
                "INSERT INTO tickets "
                "(external_id, customer_external_id, customer_name, customer_email, subject, "
                "message, channel, source_created_at, payload_hash) "
                "VALUES (:external_id, 'customer-migration', 'Cliente', 'cliente@example.com', "
                "'Assunto', 'Mensagem', 'web', now(), :payload_hash) RETURNING id"
            ),
            {"external_id": f"cost-migration-{identifier}", "payload_hash": "e" * 64},
        )
        version_id = await connection.scalar(
            text(
                "INSERT INTO classifier_versions "
                "(provider, model_name, version, system_prompt, output_schema_version) "
                "VALUES ('migration-test', 'model', :version, 'prompt', '1') RETURNING id"
            ),
            {"version": str(identifier)},
        )
        classification_id = await connection.scalar(
            text(
                "INSERT INTO classifications "
                "(ticket_id, classifier_version_id, category, priority, sentiment, "
                "suggested_response, input_tokens, output_tokens) "
                "VALUES (:ticket_id, :version_id, 'outros', 'media', 'neutro', "
                "'Resposta', 31, 12) RETURNING id"
            ),
            {"ticket_id": ticket_id, "version_id": version_id},
        )
    await test_engine.dispose()
    return str(classification_id)


async def _classification_costs(
    database_url: str,
    classification_id: str,
) -> tuple[Decimal, Decimal, Decimal]:
    test_engine = create_async_engine(database_url)
    async with test_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT input_cost, output_cost, total_cost FROM classifications "
                    "WHERE id = CAST(:classification_id AS uuid)"
                ),
                {"classification_id": classification_id},
            )
        ).one()
    await test_engine.dispose()
    return row[0], row[1], row[2]


async def test_migrations_upgrade_and_downgrade(migrated_database_url: str) -> None:
    assert DOMAIN_TABLES <= await _table_names(migrated_database_url)
    assert {
        "provider_model",
        "raw_output",
        "input_cost",
        "output_cost",
        "total_cost",
    } <= await _classification_columns(migrated_database_url)
    assert "ix_classifications_ticket_latest" in await _index_names(
        migrated_database_url,
        "classifications",
    )
    assert "ix_processing_jobs_lease_recovery" in await _index_names(
        migrated_database_url,
        "processing_jobs",
    )

    try:
        await run_alembic(migrated_database_url, "downgrade", "20261017_0003")
        assert {
            "input_cost",
            "output_cost",
            "total_cost",
        }.isdisjoint(await _classification_columns(migrated_database_url))

        classification_id = await _insert_classification_with_usage(migrated_database_url)
        await run_alembic(migrated_database_url, "upgrade", "head")
        assert await _classification_costs(migrated_database_url, classification_id) == (
            Decimal("0.00001240"),
            Decimal("0.00001920"),
            Decimal("0.00003160"),
        )

        await run_alembic(migrated_database_url, "downgrade", "20261016_0001")
        assert DOMAIN_TABLES.isdisjoint(await _table_names(migrated_database_url))
        assert await _domain_enum_names(migrated_database_url) == set()
    finally:
        await run_alembic(migrated_database_url, "upgrade", "head")
