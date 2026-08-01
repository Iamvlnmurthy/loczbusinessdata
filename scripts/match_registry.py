"""Match mapped businesses against Telangana's business registers.

The map knows WHERE a shop is. The register knows WHETHER it is real and WHAT it
trades. Matching them gives verification, a declared category, and a measured gap.

Driven from the business side (~200k Telangana rows) rather than the register side
(5.7M), because the earlier Overture dedup ran 25 minutes by driving from the large
side. Same join, opposite direction, a fraction of the work.

Deliberately conservative. A false verification is worse than no verification: it
tells a user the state vouched for a business when it did not.

Matching is on EXACT normalised name within the same district, not fuzzy
similarity. The first attempt used trigram similarity and spilled 15 GB of temp
in seven minutes: Hyderabad district alone holds 1.86M register entries, so every
business there scanned an enormous candidate set and sorted all of it. Exact
equality is a hash join — seconds instead of hours — and it trades recall for
precision, which is the right trade when the output is a verification claim.

Both sides are normalised the same way (lowercase, punctuation stripped, and the
noise words Indian business names repeat: sri, shri, new, m/s, pvt, ltd).
"""
import os
from pathlib import Path
import re, sys
import psycopg

def _dsn():
    """Connection string comes from the environment. No default: a hardcoded
    fallback password ends up in version control, which is how this file used to
    leak one to a public repository."""
    v = os.environ.get("LOCZ_DSN")
    if not v:
        env = Path(__file__).resolve().parents[1] / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("LOCZ_DSN="):
                    return line.split("=", 1)[1].strip()
        raise SystemExit("LOCZ_DSN is not set. Copy .env.example to .env and fill it in.")
    return v


DSN = _dsn()
SIM = 0.62


def main():
    conn = psycopg.connect(DSN, autocommit=True)
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '90min'")
    cur.execute(f"SET pg_trgm.similarity_threshold = {SIM}")

    cur.execute("""ALTER TABLE businesses
                     ADD COLUMN IF NOT EXISTS registry_match_id bigint,
                     ADD COLUMN IF NOT EXISTS registry_trade text,
                     ADD COLUMN IF NOT EXISTS registry_score numeric(3,2)""")

    print("indexing …", flush=True)
    cur.execute("CREATE INDEX IF NOT EXISTS reg_district_lower ON registry_entries (lower(district))")
    cur.execute("ANALYZE registry_entries")

    # Telangana businesses only: the register covers one state.
    cur.execute("""DROP TABLE IF EXISTS tg_biz;
                   CREATE TABLE tg_biz AS
                   SELECT b.id, b.canonical_name, b.display_name,
                          lower(p.district_name) AS district, b.pincode_code
                   FROM businesses b
                   JOIN pincodes p ON p.code = b.pincode_code
                   WHERE p.state_name = 'Telangana' AND b.canonical_name <> ''""")
    cur.execute("SELECT count(*) FROM tg_biz")
    n_biz = cur.fetchone()[0]
    print(f"Telangana businesses mapped : {n_biz:,}")
    cur.execute("CREATE INDEX ON tg_biz (district)")
    cur.execute("ANALYZE tg_biz")

    cur.execute("SELECT count(*) FROM registry_entries")
    print(f"register entries            : {cur.fetchone()[0]:,}")

    print("indexing register for exact match …", flush=True)
    cur.execute("""CREATE INDEX IF NOT EXISTS reg_exact
                   ON registry_entries (lower(district), canonical_name)""")
    cur.execute("ANALYZE registry_entries")

    print("matching on exact normalised name + district …", flush=True)
    cur.execute("""
      DROP TABLE IF EXISTS reg_match;
      CREATE TABLE reg_match AS
      SELECT DISTINCT ON (t.id) t.id AS business_id, r.id AS rid, r.nature,
             1.00::numeric AS score
      FROM tg_biz t
      JOIN registry_entries r
        ON lower(r.district) = t.district
       AND r.canonical_name = t.canonical_name
      WHERE length(t.canonical_name) >= 8      -- a 2-3 letter name is not evidence
      ORDER BY t.id, r.id""")
    cur.execute("SELECT count(*) FROM reg_match")
    n_match = cur.fetchone()[0]
    print(f"matched                     : {n_match:,}  ({n_match/max(n_biz,1)*100:.1f}% of mapped)")

    cur.execute("""UPDATE businesses b
                   SET registry_match_id = m.rid,
                       registry_trade    = m.nature,
                       registry_score    = round(m.score::numeric, 2),
                       verification_status = 'source_verified',
                       updated_at = now()
                   FROM reg_match m WHERE b.id = m.business_id""")
    print(f"marked source_verified      : {cur.rowcount:,}")

    # ---------------- the gap: what the state records vs what we mapped
    print("\ncomputing coverage gaps per mandal …", flush=True)
    cur.execute("""
      DROP TABLE IF EXISTS mandal_gap;
      CREATE TABLE mandal_gap AS
      WITH reg AS (
        SELECT lower(district) district, lower(COALESCE(mandal,'')) mandal, count(*) registered
        FROM registry_entries GROUP BY 1,2),
      mapped AS (
        SELECT lower(p.district_name) district, lower(COALESCE(b.locality,'')) mandal,
               count(*) mapped
        FROM businesses b JOIN pincodes p ON p.code=b.pincode_code
        WHERE p.state_name='Telangana' GROUP BY 1,2)
      SELECT r.district, r.mandal, r.registered,
             COALESCE(m.mapped,0) AS mapped,
             GREATEST(r.registered - COALESCE(m.mapped,0), 0) AS gap,
             round(100.0*COALESCE(m.mapped,0)/NULLIF(r.registered,0), 1) AS coverage_pct
      FROM reg r LEFT JOIN mapped m USING (district, mandal)
      WHERE r.registered >= 20""")
    cur.execute("SELECT count(*), sum(registered), sum(mapped), sum(gap) FROM mandal_gap")
    cells, reg, mapd, gap = cur.fetchone()
    print(f"mandals with 20+ registrations : {cells:,}")
    print(f"  registered (2017-2026)       : {reg:,}")
    print(f"  mapped by us                 : {mapd:,}")
    print(f"  gap                          : {gap:,}")

    print("\nlargest gaps — where a field team would go first:")
    cur.execute("""SELECT district, mandal, registered, mapped, coverage_pct
                   FROM mandal_gap ORDER BY gap DESC LIMIT 12""")
    print(f"  {'district':18s} {'mandal':22s} {'reg':>8s} {'mapped':>7s} {'cover':>7s}")
    for d, m, r, mp, c in cur.fetchall():
        print(f"  {str(d)[:16]:18s} {str(m)[:20]:22s} {r:8,} {mp:7,} {str(c)+'%':>7s}")

    print("\nbest-covered mandals:")
    cur.execute("""SELECT district, mandal, registered, mapped, coverage_pct
                   FROM mandal_gap WHERE mapped > 50
                   ORDER BY coverage_pct DESC NULLS LAST LIMIT 8""")
    for d, m, r, mp, c in cur.fetchall():
        print(f"  {str(d)[:16]:18s} {str(m)[:20]:22s} {r:8,} {mp:7,} {str(c)+'%':>7s}")

    print("\nsample verifications (map name -> declared trade):")
    cur.execute("""SELECT b.display_name, r.name, b.registry_trade, b.registry_score
                   FROM businesses b JOIN registry_entries r ON r.id = b.registry_match_id
                   WHERE b.registry_trade IS NOT NULL ORDER BY b.registry_score DESC LIMIT 10""")
    for a, bb, tr, sc in cur.fetchall():
        print(f"  {str(a)[:30]:32s} = {str(bb)[:30]:32s} [{tr}] {sc}")
    conn.close()


if __name__ == "__main__":
    main()
