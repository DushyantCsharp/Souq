-- Up Migration

-- CONTRACT phase of the attributes.size -> products.size move. SHIP IN THE NEXT RELEASE, not alongside
-- migrations/1791849600500_expand_product_size.sql: it is only safe once every writer and reader in
-- production sets/reads the `size` column and scripts/backfill_size.py has finished. Kept in
-- migrations-next/ so `pnpm --filter @souq/db migrate up` cannot apply it early; `migrate:next` applies it.

-- The stripped key does not change what product_document() returns for `size`, so neither step is a
-- product change worth a new version or outbox event.
SET LOCAL souq.skip_version_bump = 'on';

-- Defensive catch-up for any row the backfill did not reach. Normally touches zero rows.
UPDATE products SET size = attributes ->> 'size' WHERE size IS NULL AND attributes ? 'size';

DROP TRIGGER products_sync_size ON products;
DROP FUNCTION products_sync_size();

-- One statement is fine at seed scale (~50k rows, sub-second). At catalog scale run it in keyset batches
-- (same loop as backfill_size.py) before applying this migration so this UPDATE finds nothing to do.
UPDATE products SET attributes = attributes - 'size' WHERE attributes ? 'size';

CREATE OR REPLACE FUNCTION product_document(sku text) RETURNS jsonb
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
    'size',            p.size,
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

RESET souq.skip_version_bump;

-- Down Migration

SET LOCAL souq.skip_version_bump = 'on';

UPDATE products SET attributes = jsonb_set(attributes, '{size}', to_jsonb(size)) WHERE size IS NOT NULL;

CREATE FUNCTION products_sync_size() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.size IS NULL THEN
      NEW.size := NEW.attributes ->> 'size';
    ELSIF NEW.attributes ->> 'size' IS DISTINCT FROM NEW.size THEN
      NEW.attributes := jsonb_set(NEW.attributes, '{size}', to_jsonb(NEW.size));
    END IF;
  ELSIF NEW.attributes -> 'size' IS DISTINCT FROM OLD.attributes -> 'size' THEN
    NEW.size := NEW.attributes ->> 'size';
  ELSIF NEW.size IS DISTINCT FROM OLD.size THEN
    IF NEW.size IS NULL THEN
      NEW.attributes := NEW.attributes - 'size';
    ELSE
      NEW.attributes := jsonb_set(NEW.attributes, '{size}', to_jsonb(NEW.size));
    END IF;
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER products_sync_size
  BEFORE INSERT OR UPDATE ON products
  FOR EACH ROW EXECUTE FUNCTION products_sync_size();

CREATE OR REPLACE FUNCTION product_document(sku text) RETURNS jsonb
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
    'size',            coalesce(p.size, p.attributes ->> 'size'),
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

RESET souq.skip_version_bump;
