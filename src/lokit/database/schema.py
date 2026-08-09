"""Stable PostgreSQL schema helpers for migration tools such as Alembic."""

from lokit.db.schema import CREATE_EXTENSIONS, CREATE_META_TABLE, CURRENT_VERSION, schema_for_partitioning

DATABASE_SCHEMA_VERSION = CURRENT_VERSION

__all__ = ["DATABASE_SCHEMA_VERSION", "database_schema_statements"]


def database_schema_statements(
    partitioned: bool = True,
    include_extensions: bool = True,
) -> tuple[str, ...]:
    """Return the ordered, individually executable statements for the current schema."""
    sections: list[str] = []
    if include_extensions:
        sections.append(CREATE_EXTENSIONS)
    sections.append(CREATE_META_TABLE)
    sections.append(schema_for_partitioning(partitioned))
    sections.append(_metadata_statement(partitioned))
    statements: list[str] = []
    for section in sections:
        statements.extend(_split_statements(section))
    return tuple(statements)


def _split_statements(sql: str) -> tuple[str, ...]:
    return tuple(f"{statement.strip()};" for statement in sql.split(";") if statement.strip())


def _metadata_statement(partitioned: bool) -> str:
    partitioned_value = "true" if partitioned else "false"
    return f"""
    INSERT INTO _lokit_meta (key, value) VALUES
        ('schema_version', '{DATABASE_SCHEMA_VERSION}'),
        ('created_at', now()::text),
        ('partitioned', '{partitioned_value}')
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
    """
