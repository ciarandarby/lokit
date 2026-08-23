MATCH_QUERY = """
WITH input AS (
    SELECT
        %s::text AS source_match_text,
        %s::text AS placeholder_signature,
        %s::text AS source_locale,
        %s::text AS target_locale,
        %s::text AS previous_source,
        %s::text AS next_source,
        %s::boolean AS check_ice,
        %s::boolean AS require_context,
        %s::int AS max_results,
        %s::float AS threshold
),
params AS (
    SELECT
        input.*,
        md5(source_match_text) AS source_match_hash,
        md5(
            source_match_text || '|' ||
            lower(previous_source) || '|' ||
            lower(next_source)
        ) AS match_context_hash
    FROM input
),
ice AS (
    SELECT
        tu.id::text AS id,
        tu.unit_key,
        tu.source_text,
        tu.target_text,
        tu.status,
        tu.previous_source,
        tu.next_source,
        tu.source_match_text,
        tu.placeholder_signature,
        1.0::float AS score,
        'ice'::text AS kind
    FROM translation_units tu, params p
    WHERE p.check_ice
      AND md5(tu.source_match_text) = p.source_match_hash
      AND tu.source_match_text = p.source_match_text
      AND tu.placeholder_signature = p.placeholder_signature
      AND (
          NOT p.require_context
          OR md5(
              tu.source_match_text || '|' ||
              lower(tu.previous_source) || '|' ||
              lower(tu.next_source)
          ) = p.match_context_hash
      )
      AND (
          NOT p.require_context
          OR (
              lower(tu.previous_source) = lower(p.previous_source)
              AND lower(tu.next_source) = lower(p.next_source)
          )
      )
      AND tu.source_locale = p.source_locale
      AND tu.target_locale = p.target_locale
      AND tu.target_text IS NOT NULL
    ORDER BY tu.usage_count DESC, tu.updated_at DESC
    LIMIT 1
),
exact AS (
    SELECT
        tu.id::text AS id,
        tu.unit_key,
        tu.source_text,
        tu.target_text,
        tu.status,
        tu.previous_source,
        tu.next_source,
        tu.source_match_text,
        tu.placeholder_signature,
        1.0::float AS score,
        'exact'::text AS kind
    FROM translation_units tu, params p
    WHERE md5(tu.source_match_text) = p.source_match_hash
      AND tu.source_match_text = p.source_match_text
      AND tu.placeholder_signature = p.placeholder_signature
      AND tu.source_locale = p.source_locale
      AND tu.target_locale = p.target_locale
      AND tu.target_text IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM ice WHERE ice.id = tu.id::text)
    ORDER BY tu.usage_count DESC, tu.updated_at DESC
    LIMIT (SELECT max_results FROM params)
),
fuzzy AS (
    SELECT
        tu.id::text AS id,
        tu.unit_key,
        tu.source_text,
        tu.target_text,
        tu.status,
        tu.previous_source,
        tu.next_source,
        tu.source_match_text,
        tu.placeholder_signature,
        similarity(tu.source_match_text, p.source_match_text)::float AS score,
        'fuzzy'::text AS kind
    FROM translation_units tu, params p
    WHERE tu.source_match_text %% p.source_match_text
      AND tu.placeholder_signature = p.placeholder_signature
      AND tu.source_locale = p.source_locale
      AND tu.target_locale = p.target_locale
      AND tu.target_text IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM ice)
      AND NOT EXISTS (SELECT 1 FROM exact)
      AND similarity(tu.source_match_text, p.source_match_text) >= p.threshold
    ORDER BY similarity(tu.source_match_text, p.source_match_text) DESC, tu.usage_count DESC
    LIMIT (SELECT max_results FROM params)
)
SELECT * FROM ice
UNION ALL SELECT * FROM exact
UNION ALL SELECT * FROM fuzzy;
"""

UPSERT_UNITS_QUERY = """
INSERT INTO translation_units (
    id,
    unit_key,
    source_text,
    source_match_text,
    placeholder_signature,
    placeholder_index_version,
    target_text,
    source_locale,
    target_locale,
    status,
    previous_source,
    next_source,
    project,
    domain,
    usage_count,
    plural_variant,
    plural_count,
    plural_category,
    extensions
)
SELECT
    id,
    unit_key,
    source_text,
    source_match_text,
    placeholder_signature,
    placeholder_index_version,
    target_text,
    source_locale,
    target_locale,
    status,
    previous_source,
    next_source,
    project,
    domain,
    usage_count,
    plural_variant,
    plural_count,
    plural_category,
    extensions
FROM tmp_lokit_units
ON CONFLICT ON CONSTRAINT uq_translation_units_dedup DO UPDATE SET
    unit_key = EXCLUDED.unit_key,
    source_text = EXCLUDED.source_text,
    source_match_text = EXCLUDED.source_match_text,
    placeholder_signature = EXCLUDED.placeholder_signature,
    placeholder_index_version = EXCLUDED.placeholder_index_version,
    target_text = EXCLUDED.target_text,
    status = EXCLUDED.status,
    previous_source = EXCLUDED.previous_source,
    next_source = EXCLUDED.next_source,
    project = EXCLUDED.project,
    domain = EXCLUDED.domain,
    usage_count = GREATEST(translation_units.usage_count, EXCLUDED.usage_count) + 1,
    updated_at = now(),
    plural_variant = EXCLUDED.plural_variant,
    plural_count = EXCLUDED.plural_count,
    plural_category = EXCLUDED.plural_category,
    extensions = translation_units.extensions || EXCLUDED.extensions;
"""

UPDATE_EXISTING_UNTRANSLATED_QUERY = """
UPDATE translation_units target
SET
    status = staged.status,
    source_match_text = staged.source_match_text,
    placeholder_signature = staged.placeholder_signature,
    placeholder_index_version = staged.placeholder_index_version,
    previous_source = staged.previous_source,
    next_source = staged.next_source,
    project = staged.project,
    domain = staged.domain,
    usage_count = GREATEST(target.usage_count, staged.usage_count) + 1,
    updated_at = now(),
    plural_variant = staged.plural_variant,
    plural_count = staged.plural_count,
    plural_category = staged.plural_category,
    extensions = target.extensions || staged.extensions
FROM tmp_lokit_units staged
WHERE staged.target_text IS NULL
  AND target.target_text IS NULL
  AND target.unit_key = staged.unit_key
  AND target.source_locale = staged.source_locale
  AND target.target_locale = staged.target_locale
  AND target.source_hash = md5(lower(staged.source_text))
  AND target.context_hash = md5(
        lower(staged.source_text) || '|' ||
        lower(staged.previous_source) || '|' ||
        lower(staged.next_source)
    );
"""

MAP_EXISTING_UNTRANSLATED_QUERY = """
INSERT INTO tmp_lokit_unit_map (load_id, unit_id, source_locale)
SELECT DISTINCT ON (staged.load_id)
    staged.load_id,
    target.id,
    target.source_locale
FROM tmp_lokit_units staged
JOIN translation_units target
  ON staged.target_text IS NULL
 AND target.target_text IS NULL
 AND target.unit_key = staged.unit_key
 AND target.source_locale = staged.source_locale
 AND target.target_locale = staged.target_locale
 AND target.source_hash = md5(lower(staged.source_text))
 AND target.context_hash = md5(
        lower(staged.source_text) || '|' ||
        lower(staged.previous_source) || '|' ||
        lower(staged.next_source)
    )
ORDER BY staged.load_id, target.updated_at DESC
ON CONFLICT (load_id) DO UPDATE SET
    unit_id = EXCLUDED.unit_id,
    source_locale = EXCLUDED.source_locale;
"""

DELETE_MAPPED_STAGED_UNITS_QUERY = """
DELETE FROM tmp_lokit_units staged
USING tmp_lokit_unit_map map
WHERE staged.load_id = map.load_id;
"""

MAP_LOADED_UNITS_QUERY = """
INSERT INTO tmp_lokit_unit_map (load_id, unit_id, source_locale)
SELECT DISTINCT ON (staged.load_id)
    staged.load_id,
    tu.id,
    tu.source_locale
FROM tmp_lokit_units staged
JOIN translation_units tu
  ON tu.source_locale = staged.source_locale
 AND tu.target_locale = staged.target_locale
 AND tu.source_hash = md5(lower(staged.source_text))
 AND tu.context_hash = md5(
        lower(staged.source_text) || '|' ||
        lower(staged.previous_source) || '|' ||
        lower(staged.next_source)
    )
 AND tu.target_text IS NOT DISTINCT FROM staged.target_text
ORDER BY staged.load_id, (tu.id = staged.id) DESC, tu.updated_at DESC
ON CONFLICT (load_id) DO UPDATE SET
    unit_id = EXCLUDED.unit_id,
    source_locale = EXCLUDED.source_locale;
"""

DELETE_MAPPED_TAGS_QUERY = """
DELETE FROM unit_tags target
USING tmp_lokit_unit_map map
WHERE target.unit_id = map.unit_id
  AND target.source_locale = map.source_locale;
"""

DELETE_MAPPED_PARTS_QUERY = """
DELETE FROM segment_parts target
USING tmp_lokit_unit_map map
WHERE target.unit_id = map.unit_id
  AND target.source_locale = map.source_locale;
"""

DELETE_MAPPED_COMMENTS_QUERY = """
DELETE FROM unit_comments target
USING tmp_lokit_unit_map map
WHERE target.unit_id = map.unit_id
  AND target.source_locale = map.source_locale;
"""

INSERT_TAGS_QUERY = """
INSERT INTO unit_tags (
    unit_id,
    source_locale,
    tag_id,
    tag_type,
    position,
    tag_order,
    attribute_data,
    pair_id,
    original_name,
    original_text,
    attributes,
    is_source
)
SELECT
    map.unit_id,
    tag.source_locale,
    tag.tag_id,
    tag.tag_type,
    tag.position,
    tag.tag_order,
    tag.attribute_data,
    tag.pair_id,
    tag.original_name,
    tag.original_text,
    tag.attributes,
    tag.is_source
FROM tmp_lokit_tags tag
JOIN tmp_lokit_unit_map map ON map.load_id = tag.load_id
ON CONFLICT (unit_id, source_locale, tag_id, is_source) DO UPDATE SET
    tag_type = EXCLUDED.tag_type,
    position = EXCLUDED.position,
    tag_order = EXCLUDED.tag_order,
    attribute_data = EXCLUDED.attribute_data,
    pair_id = EXCLUDED.pair_id,
    original_name = EXCLUDED.original_name,
    original_text = EXCLUDED.original_text,
    attributes = EXCLUDED.attributes;
"""

INSERT_PARTS_QUERY = """
INSERT INTO segment_parts (
    unit_id,
    source_locale,
    is_source,
    position,
    part_type,
    value
)
SELECT
    map.unit_id,
    part.source_locale,
    part.is_source,
    part.position,
    part.part_type,
    part.value
FROM tmp_lokit_parts part
JOIN tmp_lokit_unit_map map ON map.load_id = part.load_id
ON CONFLICT (unit_id, source_locale, is_source, position) DO UPDATE SET
    part_type = EXCLUDED.part_type,
    value = EXCLUDED.value;
"""

INSERT_COMMENTS_QUERY = """
INSERT INTO unit_comments (
    unit_id,
    source_locale,
    context,
    timestamp,
    context_key,
    system,
    project,
    creator_id,
    extensions
)
SELECT
    map.unit_id,
    comment.source_locale,
    comment.context,
    comment.timestamp,
    comment.context_key,
    comment.system,
    comment.project,
    comment.creator_id,
    comment.extensions
FROM tmp_lokit_comments comment
JOIN tmp_lokit_unit_map map ON map.load_id = comment.load_id;
"""

COUNT_MAPPED_UNITS_QUERY = "SELECT count(*)::int FROM tmp_lokit_unit_map;"

FETCH_UNIT_BY_KEY_QUERY = """
SELECT
    id::text AS id,
    unit_key,
    source_text,
    target_text,
    source_locale,
    target_locale,
    status,
    previous_source,
    next_source,
    usage_count,
    plural_variant,
    plural_count,
    plural_category,
    extensions,
    project,
    domain
FROM translation_units
WHERE unit_key = %s
  AND (%s::text = '' OR source_locale = %s::text)
  AND (%s::text = '' OR target_locale = %s::text)
ORDER BY updated_at DESC
LIMIT 1;
"""

FETCH_UNITS_QUERY = """
SELECT
    id::text AS id,
    unit_key,
    source_text,
    target_text,
    source_locale,
    target_locale,
    status,
    previous_source,
    next_source,
    usage_count,
    plural_variant,
    plural_count,
    plural_category,
    extensions,
    project,
    domain
FROM translation_units
WHERE source_locale = %s
  AND target_locale = %s
ORDER BY unit_key;
"""

FETCH_UNITS_BY_SOURCE_QUERY = """
SELECT
    id::text AS id,
    unit_key,
    source_text,
    target_text,
    source_locale,
    target_locale,
    status,
    previous_source,
    next_source,
    usage_count,
    plural_variant,
    plural_count,
    plural_category,
    extensions,
    project,
    domain
FROM translation_units
WHERE source_locale = %s
ORDER BY
    unit_key,
    source_text,
    previous_source,
    next_source,
    target_locale,
    target_text NULLS FIRST,
    id;
"""

FETCH_UNITS_BY_SOURCE_TARGETS_QUERY = """
SELECT
    id::text AS id,
    unit_key,
    source_text,
    target_text,
    source_locale,
    target_locale,
    status,
    previous_source,
    next_source,
    usage_count,
    plural_variant,
    plural_count,
    plural_category,
    extensions,
    project,
    domain
FROM translation_units
WHERE source_locale = %s
  AND target_locale = ANY(%s::text[])
ORDER BY
    unit_key,
    source_text,
    previous_source,
    next_source,
    target_locale,
    target_text NULLS FIRST,
    id;
"""

FETCH_TAGS_FOR_UNITS_QUERY = """
SELECT
    unit_id::text AS unit_id,
    source_locale,
    tag_id,
    tag_type,
    position,
    tag_order,
    attribute_data,
    pair_id,
    original_name,
    original_text,
    attributes,
    is_source
FROM unit_tags
WHERE unit_id = ANY(%s::uuid[])
ORDER BY unit_id, is_source DESC, tag_order, tag_id;
"""

FETCH_PARTS_FOR_UNITS_QUERY = """
SELECT
    unit_id::text AS unit_id,
    source_locale,
    is_source,
    position,
    part_type,
    value
FROM segment_parts
WHERE unit_id = ANY(%s::uuid[])
ORDER BY unit_id, is_source DESC, position;
"""

FETCH_COMMENTS_FOR_UNITS_QUERY = """
SELECT
    unit_id::text AS unit_id,
    source_locale,
    context,
    timestamp,
    context_key,
    system,
    project,
    creator_id,
    extensions
FROM unit_comments
WHERE unit_id = ANY(%s::uuid[])
ORDER BY unit_id, id;
"""

FETCH_TAG_SIGNATURES_QUERY = """
SELECT unit_id::text, tag_type, pair_id
FROM unit_tags
WHERE unit_id = ANY(%s::uuid[])
  AND source_locale = %s
  AND is_source = true
ORDER BY unit_id, tag_order, tag_id;
"""
