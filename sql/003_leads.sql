-- Merchant acquisition pipeline.
--
-- A lead is NOT a directory listing. It is an internal record of a business we
-- intend to contact. Leads never appear in an export; only a business that has
-- consented and onboarded becomes a claimed LocZ profile.

DO $$ BEGIN
  CREATE TYPE lead_status AS ENUM (
    'discovered','duplicate','incomplete','ready_for_outreach','contacted',
    'interested','declined','unreachable','onboarding_started','approved',
    'converted','do_not_contact');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE consent_status AS ENUM (
    'not_requested','requested','granted','refused','withdrawn');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS business_leads (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id       uuid REFERENCES businesses(id) ON DELETE SET NULL,
  external_id       text UNIQUE NOT NULL,
  name              text NOT NULL,
  category_id       integer REFERENCES categories(id),
  pincode_code      varchar(6) REFERENCES pincodes(code),
  locality          text,
  district          text,
  state             text,
  contact_phone     text,
  phone_line_type   text,
  contact_email     text,
  website           text,
  lat               double precision,
  lon              double precision,
  -- scoring
  score             smallint NOT NULL DEFAULT 0,
  score_breakdown   jsonb,
  -- workflow
  status            lead_status NOT NULL DEFAULT 'discovered',
  consent           consent_status NOT NULL DEFAULT 'not_requested',
  consent_at        timestamptz,
  consent_method    text,
  assigned_to       text,
  contact_attempts  smallint NOT NULL DEFAULT 0,
  last_contacted_at timestamptz,
  do_not_contact    boolean NOT NULL DEFAULT false,
  locz_business_id  text,
  notes             text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS leads_queue ON business_leads (status, score DESC)
  WHERE NOT do_not_contact;
CREATE INDEX IF NOT EXISTS leads_pin_cat ON business_leads (pincode_code, category_id);
CREATE INDEX IF NOT EXISTS leads_phone ON business_leads (contact_phone)
  WHERE contact_phone IS NOT NULL;

-- Suppression list. Survives lead deletion: a refusal must outlive the record
-- that caused it, or the next import re-contacts someone who said no.
CREATE TABLE IF NOT EXISTS contact_suppression (
  phone       text PRIMARY KEY,
  reason      text NOT NULL,
  added_by    text,
  added_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lead_events (
  id          bigserial PRIMARY KEY,
  lead_id     uuid NOT NULL REFERENCES business_leads(id) ON DELETE CASCADE,
  actor       text NOT NULL,
  event       text NOT NULL,          -- called | visited | emailed | consented | refused
  outcome     text,
  note        text,
  at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lead_events_lead ON lead_events (lead_id, at DESC);

-- Coverage gaps: what a registry says exists vs what we have mapped.
CREATE TABLE IF NOT EXISTS coverage_gaps (
  pincode_code   varchar(6) NOT NULL REFERENCES pincodes(code),
  category_id    integer NOT NULL REFERENCES categories(id),
  mapped         integer NOT NULL DEFAULT 0,
  contactable    integer NOT NULL DEFAULT 0,
  demand_signal  integer NOT NULL DEFAULT 0,   -- from LocZ zero-result searches
  gap            integer NOT NULL DEFAULT 0,
  priority       smallint NOT NULL DEFAULT 0,
  computed_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (pincode_code, category_id)
);
CREATE INDEX IF NOT EXISTS gaps_priority ON coverage_gaps (priority DESC, gap DESC);
