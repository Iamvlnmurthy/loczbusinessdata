-- LocZ Pincode Business Data Engine — core schema
-- PostgreSQL 18 + PostGIS 3.6.  Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================ compliance
DO $$ BEGIN
  CREATE TYPE compliance_status AS ENUM
    ('pending_review','approved','approved_with_restrictions','blocked','expired','suspended');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS data_sources (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug                    text UNIQUE NOT NULL,
  name                    text NOT NULL,
  source_type             text NOT NULL,           -- osm | government | partner | survey | submission
  adapter_key             text NOT NULL,
  base_url                text,
  provider_name           text,
  licence_name            text,
  licence_url             text,
  attribution_text        text,
  attribution_url         text,
  commercial_use_allowed  boolean DEFAULT false,
  storage_allowed         boolean DEFAULT false,
  redistribution_allowed  boolean DEFAULT false,
  automated_access_allowed boolean DEFAULT false,
  rate_limit_per_minute   integer,
  rate_limit_per_day      integer,
  crawl_delay_seconds     numeric(6,2),
  data_retention_days     integer,
  enabled                 boolean NOT NULL DEFAULT false,
  status                  compliance_status NOT NULL DEFAULT 'pending_review',
  reviewed_by             text,
  reviewed_at             timestamptz,
  config                  jsonb NOT NULL DEFAULT '{}'::jsonb,
  notes                   text,
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),
  -- an adapter may only ever run for an approved, enabled source
  CONSTRAINT enabled_requires_approval
    CHECK (NOT enabled OR status IN ('approved','approved_with_restrictions'))
);

CREATE TABLE IF NOT EXISTS source_compliance_events (
  id          bigserial PRIMARY KEY,
  source_id   uuid NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
  from_status compliance_status,
  to_status   compliance_status NOT NULL,
  actor       text NOT NULL,
  reason      text,
  evidence    jsonb,
  at          timestamptz NOT NULL DEFAULT now()
);

-- ============================================================ geography
DO $$ BEGIN
  CREATE TYPE centroid_source AS ENUM
    ('geonames','osm_place_match','osm_postal_boundary','manual','unverified');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- One row per pincode. `code` is the natural key LocZ also uses.
CREATE TABLE IF NOT EXISTS pincodes (
  code                 varchar(6) PRIMARY KEY,
  name                 text NOT NULL,
  district_name        text NOT NULL,
  state_name           text NOT NULL,
  mandal               text,
  office_count         integer NOT NULL DEFAULT 1,
  -- GeoNames values, kept verbatim so a correction is always reversible
  geonames_lat         double precision,
  geonames_lon         double precision,
  -- the geometry the engine actually targets
  lat                  double precision,
  lon                  double precision,
  geo                  geography(Point,4326),
  search_radius_km     numeric(6,2) NOT NULL DEFAULT 3.0,
  centroid_src         centroid_source NOT NULL DEFAULT 'geonames',
  centroid_offset_km   numeric(7,2),
  -- audit flags from the centroid audit
  shares_coordinate    boolean NOT NULL DEFAULT false,
  cluster_size         integer NOT NULL DEFAULT 1,
  targetable           boolean NOT NULL DEFAULT true,
  urban_class          text,                        -- urban | semi_urban | rural
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pincodes_geo_gix   ON pincodes USING gist (geo);
CREATE INDEX IF NOT EXISTS pincodes_state_idx ON pincodes (state_name, district_name);
CREATE INDEX IF NOT EXISTS pincodes_name_trgm ON pincodes USING gin (name gin_trgm_ops);

-- Every named settlement, from the OSM extract. The Area Resolver's primary index.
CREATE TABLE IF NOT EXISTS named_places (
  id           bigserial PRIMARY KEY,
  osm_type     text NOT NULL,
  osm_id       bigint NOT NULL,
  name         text NOT NULL,
  name_en      text,
  name_hi      text,
  name_te      text,
  place_kind   text NOT NULL,                       -- city | town | village | suburb | ...
  population   integer,
  postcode     varchar(6),
  pincode_code varchar(6) REFERENCES pincodes(code),
  lat          double precision NOT NULL,
  lon          double precision NOT NULL,
  geo          geography(Point,4326) NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (osm_type, osm_id)
);
CREATE INDEX IF NOT EXISTS places_geo_gix   ON named_places USING gist (geo);
CREATE INDEX IF NOT EXISTS places_name_trgm ON named_places USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS places_kind_idx  ON named_places (place_kind);

-- Post-office rows, kept for name matching (a pincode has many office names).
CREATE TABLE IF NOT EXISTS pincode_offices (
  id           bigserial PRIMARY KEY,
  code         varchar(6) NOT NULL REFERENCES pincodes(code) ON DELETE CASCADE,
  office_name  text NOT NULL,
  mandal       text,
  lat          double precision,
  lon          double precision
);
CREATE INDEX IF NOT EXISTS offices_code_idx ON pincode_offices (code);
CREATE INDEX IF NOT EXISTS offices_name_trgm ON pincode_offices USING gin (office_name gin_trgm_ops);

-- ============================================================ taxonomy
CREATE TABLE IF NOT EXISTS categories (
  id         serial PRIMARY KEY,
  slug       text UNIQUE NOT NULL,
  name       text NOT NULL,
  parent_id  integer REFERENCES categories(id),
  sort_order integer NOT NULL DEFAULT 0,
  active     boolean NOT NULL DEFAULT true
);

-- OSM tag -> canonical category. Data, never adapter code.
CREATE TABLE IF NOT EXISTS source_category_map (
  id             serial PRIMARY KEY,
  source_type    text NOT NULL DEFAULT 'osm',
  source_key     text NOT NULL,                     -- e.g. 'shop'
  source_value   text NOT NULL,                     -- e.g. 'hardware'
  category_id    integer NOT NULL REFERENCES categories(id),
  subcategory    text,
  business_type  text,                              -- RETAIL_STORE | FOOD_SERVICE | ...
  confidence     numeric(3,2) NOT NULL DEFAULT 0.95,
  mapping_type   text NOT NULL DEFAULT 'manual',
  UNIQUE (source_type, source_key, source_value)
);

CREATE TABLE IF NOT EXISTS unmapped_source_categories (
  id           serial PRIMARY KEY,
  source_type  text NOT NULL,
  source_key   text NOT NULL,
  source_value text NOT NULL,
  occurrences  integer NOT NULL DEFAULT 1,
  first_seen   timestamptz NOT NULL DEFAULT now(),
  last_seen    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_type, source_key, source_value)
);

-- ============================================================ ingestion
CREATE TABLE IF NOT EXISTS raw_records (
  id                 bigserial PRIMARY KEY,
  source_id          uuid REFERENCES data_sources(id),
  source_record_id   text NOT NULL,
  payload            jsonb NOT NULL,
  payload_sha256     bytea NOT NULL,
  source_timestamp   timestamptz,
  ingested_at        timestamptz NOT NULL DEFAULT now(),
  purge_after        timestamptz,
  payload_purged_at  timestamptz,
  parse_status       text NOT NULL DEFAULT 'pending',
  UNIQUE (source_id, source_record_id)
);
CREATE INDEX IF NOT EXISTS raw_purge_idx ON raw_records (purge_after)
  WHERE payload_purged_at IS NULL;

-- ============================================================ canonical business
DO $$ BEGIN
  CREATE TYPE pincode_method AS ENUM
    ('exact_source_pincode','named_place_match','osm_postal_boundary',
     'nearest_named_place','reverse_geocoded','manual_assignment');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE export_tier AS ENUM ('CONTACTABLE','LOCATABLE','HELD');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS businesses (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  external_id         text UNIQUE NOT NULL,          -- stable: 'osm:n123456'
  display_name        text NOT NULL,
  canonical_name      text NOT NULL,
  slug                text,
  category_id         integer REFERENCES categories(id),
  subcategory         text,
  business_type       text,
  brand_name          text,

  pincode_code        varchar(6) REFERENCES pincodes(code),
  pincode_method      pincode_method,
  pincode_confidence  numeric(3,2),
  locality            text,
  mandal              text,
  city                text,
  district            text,
  state               text,
  address_line_1      text,
  address_line_2      text,

  lat                 double precision NOT NULL,
  lon                 double precision NOT NULL,
  geo                 geography(Point,4326) NOT NULL,
  location_accuracy   text NOT NULL DEFAULT 'unknown',

  public_phone        text,
  phone_line_type     text,
  public_email        text,
  website             text,
  opening_hours_raw   text,

  source_id           uuid REFERENCES data_sources(id),
  source_record_id    text,
  source_url          text,
  source_updated_at   timestamptz,
  attribution_text    text,
  licence_name        text,

  completeness_score  smallint NOT NULL DEFAULT 0,
  confidence_score    smallint NOT NULL DEFAULT 0,
  freshness_score     smallint,
  tier                export_tier NOT NULL DEFAULT 'HELD',

  claim_status        text NOT NULL DEFAULT 'unclaimed',
  verification_status text NOT NULL DEFAULT 'unverified',
  lifecycle_status    text NOT NULL DEFAULT 'imported',
  merged_into_id      uuid REFERENCES businesses(id),

  first_seen_at       timestamptz NOT NULL DEFAULT now(),
  last_seen_at        timestamptz NOT NULL DEFAULT now(),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  -- an import may never claim more than source-level verification
  CONSTRAINT import_cannot_self_verify
    CHECK (verification_status IN ('unverified','source_verified')
           OR lifecycle_status = 'manually_reviewed')
);
CREATE INDEX IF NOT EXISTS biz_geo_gix    ON businesses USING gist (geo);
CREATE INDEX IF NOT EXISTS biz_pin_cat    ON businesses (pincode_code, category_id, tier);
CREATE INDEX IF NOT EXISTS biz_name_trgm  ON businesses USING gin (canonical_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS biz_phone_idx  ON businesses (public_phone) WHERE public_phone IS NOT NULL;
CREATE INDEX IF NOT EXISTS biz_tier_idx   ON businesses (tier, pincode_code);

-- field-level provenance: which source supplied which value, under what licence
CREATE TABLE IF NOT EXISTS business_field_sources (
  id               bigserial PRIMARY KEY,
  business_id      uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  field_name       text NOT NULL,
  source_id        uuid REFERENCES data_sources(id),
  source_record_id text,
  source_value     text,
  normalised_value text,
  confidence       numeric(3,2),
  observed_at      timestamptz,
  imported_at      timestamptz NOT NULL DEFAULT now(),
  licence_name     text,
  superseded       boolean NOT NULL DEFAULT false,
  approved         boolean NOT NULL DEFAULT true
);
-- exactly one live value per field per business
CREATE UNIQUE INDEX IF NOT EXISTS bfs_current
  ON business_field_sources (business_id, field_name)
  WHERE NOT superseded AND approved;

-- ============================================================ coverage matrix
CREATE TABLE IF NOT EXISTS pincode_category_coverage (
  pincode_code   varchar(6) NOT NULL REFERENCES pincodes(code) ON DELETE CASCADE,
  category_id    integer NOT NULL REFERENCES categories(id),
  relevant       boolean NOT NULL DEFAULT true,
  min_target     smallint NOT NULL DEFAULT 2,
  target         smallint NOT NULL DEFAULT 3,
  max_seed       smallint NOT NULL DEFAULT 5,
  found          integer NOT NULL DEFAULT 0,
  contactable    integer NOT NULL DEFAULT 0,
  locatable      integer NOT NULL DEFAULT 0,
  approved       integer NOT NULL DEFAULT 0,
  exported       integer NOT NULL DEFAULT 0,
  status         text NOT NULL DEFAULT 'not_started',
  last_run_at    timestamptz,
  PRIMARY KEY (pincode_code, category_id)
);
CREATE INDEX IF NOT EXISTS coverage_status_idx ON pincode_category_coverage (status, pincode_code);

-- ============================================================ job queue (Postgres, no Redis)
CREATE TABLE IF NOT EXISTS jobs (
  id            bigserial PRIMARY KEY,
  queue         text NOT NULL DEFAULT 'default',
  kind          text NOT NULL,
  payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
  state         text NOT NULL DEFAULT 'queued',   -- queued|running|done|failed|cancelled
  priority      smallint NOT NULL DEFAULT 100,
  attempts      smallint NOT NULL DEFAULT 0,
  max_attempts  smallint NOT NULL DEFAULT 5,
  run_after     timestamptz NOT NULL DEFAULT now(),
  locked_at     timestamptz,
  locked_by     text,
  checkpoint    jsonb,
  last_error    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
-- the claim query: ORDER BY priority, run_after  FOR UPDATE SKIP LOCKED
CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs (queue, state, priority, run_after)
  WHERE state = 'queued';

-- ============================================================ audit
CREATE TABLE IF NOT EXISTS audit_log (
  id          bigserial PRIMARY KEY,
  actor       text NOT NULL,
  action      text NOT NULL,
  entity_type text,
  entity_id   text,
  before      jsonb,
  after       jsonb,
  correlation text,
  at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_entity_idx ON audit_log (entity_type, entity_id, at DESC);
