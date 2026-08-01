"""Phone validation and shared-number suppression.

Two defects found in the round-1 loader:
  * the landline fallback accepted 8-11 digits, letting through short numbers and
    toll-free 1800/1860 lines mangled with a +91 prefix;
  * a number reused across many businesses (a call centre or franchise line) was
    published as if it were that shop's own phone.

A number that is not the business's own contact is worse than no number: the user
calls and reaches someone who cannot help. Both classes are demoted, not deleted -
the original value stays in provenance.
"""
import os, re
import psycopg

DSN = os.environ.get("LOCZ_DSN",
                     "host=127.0.0.1 port=5433 dbname=locz_engine user=postgres "
                     "password=LocZEngine_2026!")
SHARE_LIMIT = 3          # a genuine shop line may cover a couple of branches, not more

DDL = """
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS phone_status text;
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS phone_raw text;
"""

# keep the original before we blank anything
BACKUP = "UPDATE businesses SET phone_raw = COALESCE(phone_raw, public_phone) WHERE public_phone IS NOT NULL"

CLASSIFY = r"""
UPDATE businesses SET phone_status = CASE
  WHEN public_phone IS NULL                              THEN NULL
  -- toll-free / premium: national lines, never a local shop's own number
  WHEN public_phone ~ '^\+91(1800|1860|1900|140)'        THEN 'tollfree'
  WHEN public_phone ~ '^\+91[6-9][0-9]{9}$'              THEN 'valid_mobile'
  WHEN public_phone ~ '^\+91[1-5][0-9]{9}$'              THEN 'valid_landline'
  WHEN public_phone ~ '(\d)\1{5,}'                       THEN 'suspicious_pattern'
  ELSE 'malformed' END
"""

MARK_SHARED = f"""
WITH shared AS (
  SELECT public_phone FROM businesses
  WHERE public_phone IS NOT NULL AND phone_status IN ('valid_mobile','valid_landline')
  GROUP BY 1 HAVING count(*) > {SHARE_LIMIT}
)
UPDATE businesses b SET phone_status = 'shared_number'
FROM shared s WHERE b.public_phone = s.public_phone
"""

# anything not a clean, exclusive line stops being a published contact
SUPPRESS = """
UPDATE businesses SET public_phone = NULL, phone_line_type = NULL
WHERE phone_status IS NOT NULL
  AND phone_status NOT IN ('valid_mobile','valid_landline')
"""

RESCORE = """
UPDATE businesses SET
  completeness_score = 45
    + CASE WHEN public_phone      IS NOT NULL THEN 20 ELSE 0 END
    + CASE WHEN address_line_1    IS NOT NULL THEN 15 ELSE 0 END
    + CASE WHEN opening_hours_raw IS NOT NULL THEN 10 ELSE 0 END
    + CASE WHEN website           IS NOT NULL THEN 10 ELSE 0 END,
  tier = CASE
    WHEN pincode_code IS NULL OR pincode_confidence < 0.55 THEN 'HELD'::export_tier
    WHEN public_phone IS NOT NULL                          THEN 'CONTACTABLE'::export_tier
    ELSE 'LOCATABLE'::export_tier END
"""


def main():
    conn = psycopg.connect(DSN)
    cur = conn.cursor()
    cur.execute(DDL); conn.commit()
    cur.execute(BACKUP); conn.commit()
    print(f"originals preserved in phone_raw : {cur.rowcount:,}")

    cur.execute(CLASSIFY); conn.commit()
    cur.execute(MARK_SHARED)
    print(f"marked shared_number             : {cur.rowcount:,}")
    conn.commit()

    cur.execute("""SELECT phone_status, count(*) FROM businesses
                   WHERE phone_status IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""")
    print("\nclassification:")
    for st, n in cur.fetchall():
        print(f"  {st:20s} {n:7,}")

    cur.execute(SUPPRESS)
    print(f"\nphones suppressed (not published): {cur.rowcount:,}")
    conn.commit()

    cur.execute(RESCORE); conn.commit()
    cur.execute("""SELECT tier::text, count(*), round(100.0*count(*)/sum(count(*)) over (),1)
                   FROM businesses GROUP BY 1 ORDER BY 2 DESC""")
    print("\ntiers after correction:")
    for t, n, p in cur.fetchall():
        print(f"  {t:12s} {n:8,}  {p}%")
    cur.execute("SELECT count(*) FROM businesses WHERE public_phone IS NOT NULL")
    print(f"\npublishable phones               : {cur.fetchone()[0]:,}")
    cur.execute("""SELECT max(c) FROM (SELECT count(*) c FROM businesses
                   WHERE public_phone IS NOT NULL GROUP BY public_phone) x""")
    print(f"max businesses on one number     : {cur.fetchone()[0]}")
    conn.close()


if __name__ == "__main__":
    main()
