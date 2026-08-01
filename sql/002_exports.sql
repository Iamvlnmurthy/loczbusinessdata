-- Export bookkeeping. An export is a reproducible artefact: same filters +
-- same data version must yield the same checksum.

CREATE TABLE IF NOT EXISTS exports (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  schema_version  text NOT NULL,
  format          text NOT NULL,
  filters         jsonb NOT NULL DEFAULT '{}'::jsonb,
  record_count    integer NOT NULL DEFAULT 0,
  excluded_count  integer NOT NULL DEFAULT 0,
  exclusion_reasons jsonb,
  source_summary  jsonb,
  licence_summary jsonb,
  attribution     text,
  file_path       text,
  sha256          text,
  status          text NOT NULL DEFAULT 'building',
  created_by      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz
);

-- what actually went out, so a later export can diff and LocZ can be reconciled
CREATE TABLE IF NOT EXISTS export_records (
  export_id    uuid NOT NULL REFERENCES exports(id) ON DELETE CASCADE,
  business_id  uuid NOT NULL REFERENCES businesses(id),
  external_id  text NOT NULL,
  PRIMARY KEY (export_id, business_id)
);
CREATE INDEX IF NOT EXISTS export_records_biz ON export_records (business_id);
