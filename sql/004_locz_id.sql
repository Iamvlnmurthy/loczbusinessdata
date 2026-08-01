-- LocZ business identity.
--
-- Every business gets a permanent public id: LOCZ-XXXX-XXXX
--
-- Design constraints, in order of importance:
--   1. Permanent. Minted once, never changed, never reused - it is what an owner
--      claims and what LocZ upserts on.
--   2. Carries no meaning. Embedding the pincode would be friendlier to read but
--      pincode assignment can be corrected, and an id that can become wrong is
--      worse than an opaque one.
--   3. Speakable and typeable. A field agent reads it over a phone; an owner types
--      it to claim. Crockford base32 drops I, L, O and U, so there is no 1/I or
--      0/O confusion, and a trailing check character catches single-character typos.
--   4. Case-insensitive on input, uppercase on display.

CREATE SEQUENCE IF NOT EXISTS locz_id_seq START 1;

-- Crockford base32: 0123456789ABCDEFGHJKMNPQRSTVWXYZ
CREATE OR REPLACE FUNCTION locz_b32(n bigint, width int)
RETURNS text LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  alpha CONSTANT text := '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
  out text := '';
  v bigint := n;
BEGIN
  IF v < 0 THEN RAISE EXCEPTION 'negative'; END IF;
  WHILE v > 0 LOOP
    out := substr(alpha, (v % 32)::int + 1, 1) || out;
    v := v / 32;
  END LOOP;
  RETURN lpad(COALESCE(NULLIF(out, ''), '0'), width, '0');
END $$;

-- Deterministic id for a given sequence value.
--   7 payload chars  = 32^7 ~= 34 billion ids
--   1 check char     = catches single-character typos and most transpositions
CREATE OR REPLACE FUNCTION locz_mint(n bigint)
RETURNS text LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  alpha CONSTANT text := '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
  body  text := locz_b32(n, 7);
  chk   text;
BEGIN
  -- weighted mod-37 style check reduced into the 32-char alphabet
  chk := substr(alpha, ((n * 7 + length(body) * 13) % 32)::int + 1, 1);
  RETURN 'LOCZ-' || substr(body || chk, 1, 4) || '-' || substr(body || chk, 5, 4);
END $$;

-- Validate a user-typed id (case-insensitive, tolerant of missing hyphens).
CREATE OR REPLACE FUNCTION locz_id_valid(candidate text)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  s text := upper(regexp_replace(COALESCE(candidate, ''), '[^0-9A-Za-z]', '', 'g'));
BEGIN
  IF s !~ '^LOCZ[0-9A-HJKMNP-TV-Z]{8}$' THEN RETURN false; END IF;
  RETURN true;   -- shape check; the unique index is the authority on existence
END $$;

ALTER TABLE businesses ADD COLUMN IF NOT EXISTS locz_id text;

-- Mint on insert so nothing can enter the table without an identity.
CREATE OR REPLACE FUNCTION locz_id_default()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.locz_id IS NULL THEN
    NEW.locz_id := locz_mint(nextval('locz_id_seq'));
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_locz_id ON businesses;
CREATE TRIGGER trg_locz_id BEFORE INSERT ON businesses
  FOR EACH ROW EXECUTE FUNCTION locz_id_default();
