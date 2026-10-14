-- Up Migration

-- EXPAND phase of moving attributes.size into a typed products.size column.
--
--   release N   (this migration): add nullable `size`, keep it and attributes.size in sync both ways with a
--                trigger, make product_document() prefer the column. Old writers that only know
--                attributes.size and new writers that set `size` both keep working.
--   backfill    scripts/backfill_size.py copies attributes.size into `size` for rows written before this
--                migration, in keyset batches (see that script for the batching rationale).
--   release N+1 packages/db/migrations-next/*_contract_product_size.sql drops the trigger and strips the
--                key from attributes. It must not ship in the same release as this file.
ALTER TABLE products ADD COLUMN size text;

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
    -- An old writer changed attributes.size: the column follows.
    NEW.size := NEW.attributes ->> 'size';
  ELSIF NEW.size IS DISTINCT FROM OLD.size THEN
    -- A new writer changed the column: mirror it for old readers of attributes.size.
    IF NEW.size IS NULL THEN
      NEW.attributes := NEW.attributes - 'size';
    ELSE
      NEW.attributes := jsonb_set(NEW.attributes, '{size}', to_jsonb(NEW.size));
    END IF;
  END IF;
  RETURN NEW;
END
$$;

-- Sorts before z_products_bump_version, so the version check sees the synced row.
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
    -- Rows not yet reached by the backfill still only carry attributes.size.
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

-- Down Migration

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

-- The trigger kept attributes.size authoritative-equivalent throughout, so dropping the column loses nothing.
DROP TRIGGER IF EXISTS products_sync_size ON products;
DROP FUNCTION IF EXISTS products_sync_size();
ALTER TABLE products DROP COLUMN IF EXISTS size;
