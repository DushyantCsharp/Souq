-- Up Migration

-- Fat event payload for outbox rows and the reindex backfill. One definition so the relay/indexer path and
-- scripts/reindex.py produce byte-identical documents. Returns NULL for an unknown SKU.
-- category_levels holds every ltree prefix (electronics, electronics.audio, electronics.audio.headphones)
-- so a category filter is a single term query on any level.
CREATE FUNCTION product_document(sku text) RETURNS jsonb
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT jsonb_build_object(
    'sku',             p.sku,
    'brand',           p.brand,
    'category_path',   p.category_path::text,
    'category_levels', (SELECT jsonb_agg(subpath(p.category_path, 0, i)::text ORDER BY i)
                          FROM generate_series(1, nlevel(p.category_path)) AS i),
    'price',           p.price,
    'currency',        p.currency,
    'colour',          p.colour,
    'size',            p.attributes ->> 'size',
    'attributes',      p.attributes,
    'title_en',        en.title,
    'title_ar',        ar.title,
    'description_en',  en.description,
    'description_ar',  ar.description,
    'updated_at',      p.updated_at,
    'version',         p.version
  )
  FROM products p
  LEFT JOIN product_texts en ON en.sku = p.sku AND en.lang = 'en'
  LEFT JOIN product_texts ar ON ar.sku = p.sku AND ar.lang = 'ar'
  WHERE p.sku = $1
$$;

-- Down Migration

DROP FUNCTION IF EXISTS product_document(text);
