-- Up Migration

-- infra/postgres/init creates these for the compose stack; repeated here so a bare database (testcontainers,
-- CI) migrates without depending on that init script.
CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Slugify a human category path ("Clothing, Shoes & Jewelry > Men > Shoes") into an ltree
-- (clothing_shoes_and_jewelry.men.shoes). Lives in SQL so every writer (feeds import, seeds) shares one
-- implementation; souq_scripts.category.slugify_category mirrors it and a parity test keeps them equal.
-- ASCII-only on purpose: ltree label validity for non-ASCII letters depends on the database locale, so a
-- path that slugifies to an empty label returns NULL and the feed importer rejects it as invalid_category.
CREATE FUNCTION category_ltree(path text) RETURNS ltree
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
DECLARE
  parts  text[] := string_to_array(path, '>');
  labels text[] := '{}';
  part   text;
  label  text;
BEGIN
  IF btrim(path) = '' OR cardinality(parts) > 10 THEN
    RETURN NULL;
  END IF;
  FOREACH part IN ARRAY parts LOOP
    -- translate() instead of lower(): lower() is locale-dependent for non-ASCII input.
    label := translate(btrim(part), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz');
    label := replace(label, '&', ' and ');
    label := btrim(regexp_replace(label, '[^a-z0-9]+', '_', 'g'), '_');
    IF label = '' OR length(label) > 255 THEN
      RETURN NULL;
    END IF;
    labels := labels || label;
  END LOOP;
  RETURN array_to_string(labels, '.')::ltree;
END
$$;

CREATE TABLE products (
  sku           text PRIMARY KEY CHECK (sku ~ '^[A-Za-z0-9][A-Za-z0-9._-]*$' AND length(sku) <= 64),
  brand         text,
  category_path ltree NOT NULL CHECK (nlevel(category_path) BETWEEN 1 AND 10),
  price         numeric(12, 2) NOT NULL CHECK (price >= 0),
  currency      char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  colour        text,
  attributes    jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(attributes) = 'object'),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  -- OpenSearch indexes with version_type=external using this value, so it must only ever grow.
  version       bigint NOT NULL DEFAULT 1 CHECK (version > 0)
);

CREATE INDEX products_category_path_gist ON products USING gist (category_path);
CREATE INDEX products_brand_idx ON products (brand);
-- Rollback catch-up in scripts/reindex.py re-sends rows changed since the alias swap.
CREATE INDEX products_updated_at_idx ON products (updated_at);

CREATE TABLE product_texts (
  sku         text NOT NULL REFERENCES products (sku) ON DELETE CASCADE,
  lang        text NOT NULL CHECK (lang IN ('en', 'ar')),
  title       text NOT NULL CHECK (title <> ''),
  description text,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (sku, lang)
);

-- products.version must increment on every *effective* change so that a re-imported, unchanged feed row
-- neither bumps the version nor produces a newer document. Enforced here rather than in each writer:
--   * any column change other than version/created_at/updated_at  -> version = OLD.version + 1
--   * no column change but the writer raised version (the product_texts trigger does this when a title or
--     description changes)                                         -> version = OLD.version + 1
--   * otherwise (no-op upsert)                                     -> version and updated_at unchanged
-- Writers never choose the number themselves, so concurrent writers cannot skip or regress it.
-- Maintenance backfills that do not change the product document (e.g. scripts/backfill_size.py) set
-- `SET LOCAL souq.skip_version_bump = 'on'` so they do not emit a phantom version.
CREATE FUNCTION products_bump_version() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF current_setting('souq.skip_version_bump', true) = 'on' THEN
    NEW.version := OLD.version;
    NEW.updated_at := OLD.updated_at;
  ELSIF (to_jsonb(NEW) - ARRAY['version', 'created_at', 'updated_at'])
          IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['version', 'created_at', 'updated_at'])
        OR NEW.version > OLD.version THEN
    NEW.version := OLD.version + 1;
    NEW.updated_at := now();
  ELSE
    NEW.version := OLD.version;
    NEW.updated_at := OLD.updated_at;
  END IF;
  NEW.created_at := OLD.created_at;
  RETURN NEW;
END
$$;

-- Named with a z_ prefix: BEFORE triggers fire alphabetically, and this one must see the row after any
-- other BEFORE trigger (e.g. the size sync added by the expand migration) has finished rewriting it.
CREATE TRIGGER z_products_bump_version
  BEFORE UPDATE ON products
  FOR EACH ROW EXECUTE FUNCTION products_bump_version();

-- A title/description change is a change to the product document, so it bumps the parent's version.
CREATE FUNCTION product_texts_touch_product() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE'
     AND (NEW.title, NEW.description) IS NOT DISTINCT FROM (OLD.title, OLD.description)
     AND NEW.sku = OLD.sku THEN
    RETURN NULL;
  END IF;
  IF TG_OP = 'UPDATE' AND NEW.sku <> OLD.sku THEN
    UPDATE products SET version = version + 1 WHERE sku = OLD.sku;
  END IF;
  UPDATE products SET version = version + 1
   WHERE sku = CASE WHEN TG_OP = 'DELETE' THEN OLD.sku ELSE NEW.sku END;
  RETURN NULL;
END
$$;

CREATE TRIGGER product_texts_touch_product
  AFTER INSERT OR UPDATE OR DELETE ON product_texts
  FOR EACH ROW EXECUTE FUNCTION product_texts_touch_product();

CREATE FUNCTION product_texts_set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.title, NEW.description) IS DISTINCT FROM (OLD.title, OLD.description) THEN
    NEW.updated_at := now();
  ELSE
    NEW.updated_at := OLD.updated_at;
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER product_texts_set_updated_at
  BEFORE UPDATE ON product_texts
  FOR EACH ROW EXECUTE FUNCTION product_texts_set_updated_at();

-- Down Migration

DROP TABLE IF EXISTS product_texts;
DROP FUNCTION IF EXISTS product_texts_set_updated_at();
DROP FUNCTION IF EXISTS product_texts_touch_product();
DROP TABLE IF EXISTS products;
DROP FUNCTION IF EXISTS products_bump_version();
DROP FUNCTION IF EXISTS category_ltree(text);
-- Extensions are left installed: the compose init script owns them and other objects may depend on them.
