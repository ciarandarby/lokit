from __future__ import annotations

import hashlib
import re


CURRENT_VERSION = 1

CREATE_EXTENSIONS = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
"""

CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS _lokit_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SCHEMA_V1_PARTITIONED = """
CREATE TABLE IF NOT EXISTS translation_units (
    id UUID DEFAULT gen_random_uuid(),
    unit_key TEXT NOT NULL,
    source_text TEXT NOT NULL,
    target_text TEXT,
    source_locale TEXT NOT NULL,
    target_locale TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unknown',
    previous_source TEXT NOT NULL DEFAULT '',
    next_source TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL GENERATED ALWAYS AS (md5(lower(source_text))) STORED,
    context_hash TEXT NOT NULL GENERATED ALWAYS AS (
        md5(
            lower(source_text) || '|' ||
            lower(COALESCE(previous_source, '')) || '|' ||
            lower(COALESCE(next_source, ''))
        )
    ) STORED,
    project TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    usage_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    plural_variant TEXT NOT NULL DEFAULT '',
    plural_count INTEGER,
    plural_category TEXT NOT NULL DEFAULT '',
    extensions JSONB NOT NULL DEFAULT '{}',
    CONSTRAINT pk_translation_units PRIMARY KEY (id, source_locale),
    CONSTRAINT uq_translation_units_dedup UNIQUE (
        source_hash,
        target_text,
        source_locale,
        target_locale,
        context_hash
    )
) PARTITION BY LIST (source_locale);

CREATE TABLE IF NOT EXISTS tu_default PARTITION OF translation_units DEFAULT;
"""

SCHEMA_V1_FLAT = """
CREATE TABLE IF NOT EXISTS translation_units (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    unit_key TEXT NOT NULL,
    source_text TEXT NOT NULL,
    target_text TEXT,
    source_locale TEXT NOT NULL,
    target_locale TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unknown',
    previous_source TEXT NOT NULL DEFAULT '',
    next_source TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL GENERATED ALWAYS AS (md5(lower(source_text))) STORED,
    context_hash TEXT NOT NULL GENERATED ALWAYS AS (
        md5(
            lower(source_text) || '|' ||
            lower(COALESCE(previous_source, '')) || '|' ||
            lower(COALESCE(next_source, ''))
        )
    ) STORED,
    project TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    usage_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    plural_variant TEXT NOT NULL DEFAULT '',
    plural_count INTEGER,
    plural_category TEXT NOT NULL DEFAULT '',
    extensions JSONB NOT NULL DEFAULT '{}',
    CONSTRAINT uq_translation_units_identity UNIQUE (id, source_locale),
    CONSTRAINT uq_translation_units_dedup UNIQUE (
        source_hash,
        target_text,
        source_locale,
        target_locale,
        context_hash
    )
);
"""

SCHEMA_V1_SHARED = """
CREATE INDEX IF NOT EXISTS idx_tu_source_trgm
    ON translation_units USING GIN (source_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_tu_source_hash ON translation_units (source_hash);
CREATE INDEX IF NOT EXISTS idx_tu_ice ON translation_units (source_hash, context_hash);
CREATE INDEX IF NOT EXISTS idx_tu_locale ON translation_units (source_locale, target_locale);
CREATE INDEX IF NOT EXISTS idx_tu_project ON translation_units (project) WHERE project != '';
CREATE INDEX IF NOT EXISTS idx_tu_domain ON translation_units (domain) WHERE domain != '';

CREATE TABLE IF NOT EXISTS unit_tags (
    unit_id UUID NOT NULL,
    source_locale TEXT NOT NULL,
    tag_id TEXT NOT NULL,
    tag_type TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    tag_order INTEGER NOT NULL DEFAULT 0,
    attribute_data TEXT NOT NULL DEFAULT '',
    pair_id TEXT NOT NULL DEFAULT '',
    original_name TEXT NOT NULL DEFAULT '',
    original_text TEXT NOT NULL DEFAULT '',
    attributes JSONB NOT NULL DEFAULT '{}',
    is_source BOOLEAN NOT NULL,
    PRIMARY KEY (unit_id, source_locale, tag_id, is_source),
    FOREIGN KEY (unit_id, source_locale)
        REFERENCES translation_units (id, source_locale)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS segment_parts (
    unit_id UUID NOT NULL,
    source_locale TEXT NOT NULL,
    is_source BOOLEAN NOT NULL,
    position INTEGER NOT NULL,
    part_type TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (unit_id, source_locale, is_source, position),
    FOREIGN KEY (unit_id, source_locale)
        REFERENCES translation_units (id, source_locale)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS unit_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    unit_id UUID NOT NULL,
    source_locale TEXT NOT NULL,
    context TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT '',
    context_key TEXT NOT NULL DEFAULT '',
    system TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    creator_id TEXT NOT NULL DEFAULT '',
    extensions JSONB NOT NULL DEFAULT '{}',
    FOREIGN KEY (unit_id, source_locale)
        REFERENCES translation_units (id, source_locale)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_unit_tags_unit
    ON unit_tags (unit_id, source_locale);
CREATE INDEX IF NOT EXISTS idx_segment_parts_unit
    ON segment_parts (unit_id, source_locale);
CREATE INDEX IF NOT EXISTS idx_unit_comments_unit
    ON unit_comments (unit_id, source_locale);
"""

CREATE_TEMP_UNITS = """
CREATE TEMP TABLE tmp_lokit_units (
    load_id UUID PRIMARY KEY,
    id UUID NOT NULL,
    unit_key TEXT NOT NULL,
    source_text TEXT NOT NULL,
    target_text TEXT,
    source_locale TEXT NOT NULL,
    target_locale TEXT NOT NULL,
    status TEXT NOT NULL,
    previous_source TEXT NOT NULL,
    next_source TEXT NOT NULL,
    project TEXT NOT NULL,
    domain TEXT NOT NULL,
    usage_count INTEGER NOT NULL,
    plural_variant TEXT NOT NULL,
    plural_count INTEGER,
    plural_category TEXT NOT NULL,
    extensions JSONB NOT NULL
) ON COMMIT DROP;
"""

CREATE_TEMP_TAGS = """
CREATE TEMP TABLE tmp_lokit_tags (
    load_id UUID NOT NULL,
    source_locale TEXT NOT NULL,
    tag_id TEXT NOT NULL,
    tag_type TEXT NOT NULL,
    position INTEGER NOT NULL,
    tag_order INTEGER NOT NULL,
    attribute_data TEXT NOT NULL,
    pair_id TEXT NOT NULL,
    original_name TEXT NOT NULL,
    original_text TEXT NOT NULL,
    attributes JSONB NOT NULL,
    is_source BOOLEAN NOT NULL
) ON COMMIT DROP;
"""

CREATE_TEMP_PARTS = """
CREATE TEMP TABLE tmp_lokit_parts (
    load_id UUID NOT NULL,
    source_locale TEXT NOT NULL,
    is_source BOOLEAN NOT NULL,
    position INTEGER NOT NULL,
    part_type TEXT NOT NULL,
    value TEXT NOT NULL
) ON COMMIT DROP;
"""

CREATE_TEMP_COMMENTS = """
CREATE TEMP TABLE tmp_lokit_comments (
    load_id UUID NOT NULL,
    source_locale TEXT NOT NULL,
    context TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    context_key TEXT NOT NULL,
    system TEXT NOT NULL,
    project TEXT NOT NULL,
    creator_id TEXT NOT NULL,
    extensions JSONB NOT NULL
) ON COMMIT DROP;
"""

CREATE_TEMP_UNIT_MAP = """
CREATE TEMP TABLE tmp_lokit_unit_map (
    load_id UUID PRIMARY KEY,
    unit_id UUID NOT NULL,
    source_locale TEXT NOT NULL
) ON COMMIT DROP;
"""

MIGRATIONS: dict[int, str] = {1: ""}

_PARTITION_SAFE_RE = re.compile("[^a-z0-9_]+")


def schema_for_partitioning(partitioned: bool) -> str:
    if partitioned:
        return SCHEMA_V1_PARTITIONED + SCHEMA_V1_SHARED
    return SCHEMA_V1_FLAT + SCHEMA_V1_SHARED


def partition_name_for_locale(source_locale: str) -> str:
    normalized = _PARTITION_SAFE_RE.sub("_", source_locale.lower()).strip("_")
    slug = normalized[:36] if normalized else "empty"
    digest = hashlib.md5(source_locale.encode("utf-8")).hexdigest()[:8]
    return f"tu_{slug}_{digest}"
