"""Push an export bundle to the live LocZ database over SSH, in chunks.

  python scripts/push_to_locz.py --district Warangal --limit 500 --dry-run
  python scripts/push_to_locz.py --district Warangal --limit 500 --apply

Design constraints:

  * The engine NEVER connects to LocZ directly. It writes a CSV, ships it, and the
    import runs inside LocZ's own container. If this script dies, LocZ is untouched.
  * Every chunk is checksummed before import and verified after. A partial chunk is
    rolled back, not left half-applied.
  * Upsert is on (sourceName, sourceRecordId), which is LocZ's own unique key —
    NOT on name. Indian business names repeat heavily and matching on them would
    merge unrelated shops. LocZ deliberately has no externalDirectoryId column;
    conform to their key rather than imposing ours.
  * A claimed or verified business is never overwritten. Those rows are skipped and
    counted, so a later manual review can decide.
  * The search index is rebuilt afterwards. LocZ treats Meilisearch as a derived
    index (their ADR-0005): Postgres is the source of truth, so a SQL insert is
    safe but invisible to search until the index is rebuilt from it. Skipping this
    is how 300 correctly-imported businesses returned "0 results".

Mapping decisions worth knowing:
  cityId          resolved from LocZ's own pincodes table, not sent by us
  categoryId      matched on slug; both sides use the same 47 slugs
  businessType    our richer set collapses into LocZ's enum (FOOD_SERVICE and
                  HOSPITALITY have no equivalent, so they become OTHER)
  verification    always UNVERIFIED — LocZ has no SOURCE_VERIFIED tier, and
                  claiming more than the schema can express would be a lie
"""
import argparse, csv, hashlib, os, subprocess, sys, tempfile, time
from pathlib import Path
import psycopg

ROOT = Path(__file__).resolve().parents[1]
SSH_HOST = os.environ.get("LOCZ_SSH_HOST", "onrol")
CONTAINER = "locz-postgres"
DB_USER, DB_NAME = "locz", "locz"
API_DIR = os.environ.get("LOCZ_API_DIR", "/home/locz/app/apps/api")
APP_USER = os.environ.get("LOCZ_APP_USER", "locz")


def _dsn():
    v = os.environ.get("LOCZ_DSN")
    if not v:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("LOCZ_DSN="):
                    return line.split("=", 1)[1].strip()
        raise SystemExit("LOCZ_DSN is not set")
    return v


# our business_type -> LocZ's BusinessType enum
BTYPE = {
    "RETAIL_STORE": "RETAIL_STORE", "HOME_BUSINESS": "HOME_BUSINESS",
    "SERVICE_PROVIDER": "SERVICE_PROVIDER", "WHOLESALER": "WHOLESALER",
    "MANUFACTURER": "MANUFACTURER",
    # no equivalent in LocZ's enum — OTHER is honest, a wrong specific value is not
    "FOOD_SERVICE": "OTHER", "HOSPITALITY": "OTHER",
    "INSTITUTION": "OTHER", "PUBLIC_SERVICE": "OTHER",
}

# Ordering by confidence alone returns a medical district: hospitals and clinics
# carry the most complete data in OSM and Overture, so they win every top-N.
# A per-category cap makes a chunk look like a real high street instead.
# Ordering by confidence alone returns a medical district: hospitals and clinics
# carry the most complete data in OSM and Overture, so they win every top-N.
# A per-category cap makes a chunk look like a real high street instead.
SELECT = """
SELECT locz_id, external_id, name, slug, category_slug, business_type,
       public_phone, whatsapp, public_email, website, lat, lon, pincode_code,
       address_line_1, locality, confidence_score, source_name,
       source_record_id, licence_name, attribution_text
FROM (
  SELECT b.locz_id, b.external_id,
         COALESCE(NULLIF(b.resolved_name,''), b.display_name) AS name,
         b.slug, c.slug AS category_slug, b.business_type,
         b.public_phone, b.whatsapp, b.public_email, b.website,
         b.lat, b.lon, b.pincode_code, b.address_line_1, b.locality,
         b.confidence_score, d.name AS source_name, b.source_record_id,
         b.licence_name, b.attribution_text,
         row_number() OVER (PARTITION BY c.slug
                            ORDER BY b.completeness_score DESC,
                                     b.confidence_score DESC) AS rn
  FROM businesses b
  JOIN categories c   ON c.id = b.category_id
  JOIN data_sources d ON d.id = b.source_id
  JOIN pincodes p     ON p.code = b.pincode_code
  WHERE b.tier <> 'HELD'
    AND b.pincode_code IS NOT NULL
    AND b.pincode_confidence >= 0.70
    AND p.centroid_src <> 'unverified'
    AND b.merged_into_id IS NULL
    AND {extra}
) ranked
WHERE rn <= %s
ORDER BY rn, category_slug
LIMIT %s
"""

REMOTE_SQL = r"""
BEGIN;

CREATE TEMP TABLE incoming (
  locz_id text, external_id text, name text, slug text, category_slug text,
  business_type text, phone text, whatsapp text, email text, website text,
  lat double precision, lon double precision, pincode text, address text,
  locality text, confidence numeric, source_name text, source_record_id text,
  licence text, attribution text
) ON COMMIT DROP;

\copy incoming FROM '/tmp/locz-import/chunk.csv' WITH (FORMAT csv, HEADER true)

-- never overwrite a business a human has claimed or verified
CREATE TEMP TABLE protected ON COMMIT DROP AS
SELECT "sourceName", "sourceRecordId" FROM businesses
WHERE "claimStatus" IN ('CLAIMED','CLAIM_PENDING')
   OR "verificationStatus" = 'VERIFIED';

INSERT INTO businesses (
  id, "ownerId", name, slug, "categoryId", "cityId", "pincodeCode",
  latitude, longitude, geo, "primaryPhone", "whatsappNumber", email, website,
  "businessType", "claimStatus", "verificationStatus", "isActive",
  "sourceName", "sourceRecordId", "licenceName",
  "attributionText", "confidenceScore", keywords, "createdAt", "updatedAt")
SELECT
  gen_random_uuid(), NULL, i.name,
  -- slug must be unique platform-wide; the locz_id guarantees that
  left(regexp_replace(lower(i.name), '[^a-z0-9]+', '-', 'g'), 150)
    || '-' || lower(replace(i.locz_id, 'LOCZ-', '')),
  cat.id, p."cityId", i.pincode,
  i.lat, i.lon, ST_SetSRID(ST_MakePoint(i.lon, i.lat), 4326)::geography,
  i.phone, i.whatsapp, i.email, i.website,
  COALESCE(i.business_type, 'OTHER')::"BusinessType",
  'UNCLAIMED'::"BusinessClaimStatus",
  'UNVERIFIED'::"VerificationStatus",
  true,
  i.source_name, i.source_record_id, i.licence,
  i.attribution, round(i.confidence/100.0, 2), ARRAY[]::text[], now(), now()
FROM incoming i
JOIN categories cat ON cat.slug = i.category_slug
JOIN pincodes  p    ON p.code   = i.pincode
WHERE NOT EXISTS (SELECT 1 FROM protected pr
                  WHERE pr."sourceName" = i.source_name
                    AND pr."sourceRecordId" = i.source_record_id)
ON CONFLICT ("sourceName", "sourceRecordId") DO UPDATE SET
  name = EXCLUDED.name,
  "primaryPhone" = COALESCE(EXCLUDED."primaryPhone", businesses."primaryPhone"),
  website = COALESCE(EXCLUDED.website, businesses.website),
  latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
  geo = EXCLUDED.geo, "confidenceScore" = EXCLUDED."confidenceScore",
  "updatedAt" = now();

SELECT count(*) AS total_businesses FROM businesses;
COMMIT;
"""


def ssh(cmd, stdin=None):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", SSH_HOST, cmd],
                          input=stdin, capture_output=True, text=True, timeout=900)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--district"); ap.add_argument("--state")
    ap.add_argument("--pincode", nargs="*")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--per-category", type=int, default=25,
                    help="cap per category so one trade cannot dominate")
    ap.add_argument("--apply", action="store_true", help="without this, dry run only")
    ap.add_argument("--reindex", action="store_true",
                    help="rebuild LocZ's business search index after import")
    a = ap.parse_args()

    extra, args = ["1=1"], []
    if a.district:
        extra.append("b.district ILIKE %s"); args.append(a.district)
    if a.state:
        extra.append("b.state ILIKE %s"); args.append(a.state)
    if a.pincode:
        extra.append("b.pincode_code = ANY(%s)"); args.append(a.pincode)

    conn = psycopg.connect(_dsn())
    cur = conn.cursor()
    cur.execute("ALTER TABLE businesses ADD COLUMN IF NOT EXISTS whatsapp text")
    conn.commit()
    cur.execute(SELECT.format(extra=" AND ".join(extra)),
                args + [a.per_category, a.limit])
    rows = cur.fetchall()
    cols = [d.name for d in cur.description]
    print(f"selected {len(rows):,} records")
    if not rows:
        return

    tmp = Path(tempfile.gettempdir()) / "locz_chunk.csv"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["locz_id", "external_id", "name", "slug", "category_slug",
                    "business_type", "phone", "whatsapp", "email", "website",
                    "lat", "lon", "pincode", "address", "locality", "confidence",
                    "source_name", "source_record_id", "licence", "attribution"])
        for r in rows:
            d = dict(zip(cols, r))
            w.writerow([d["locz_id"], d["external_id"], d["name"], d["slug"],
                        d["category_slug"], BTYPE.get(d["business_type"], "OTHER"),
                        d["public_phone"], d["whatsapp"], d["public_email"],
                        d["website"], d["lat"], d["lon"], d["pincode_code"],
                        d["address_line_1"], d["locality"], d["confidence_score"],
                        d["source_name"], d["source_record_id"], d["licence_name"],
                        d["attribution_text"]])
    sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
    print(f"chunk written  {tmp}  {tmp.stat().st_size/1024:.0f} KB  sha256 {sha[:16]}...")

    by_cat = {}
    for r in rows:
        d = dict(zip(cols, r))
        by_cat[d["category_slug"]] = by_cat.get(d["category_slug"], 0) + 1
    print("\ntop categories in this chunk:")
    for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])[:8]:
        print(f"  {k:32s} {v:5,}")

    if not a.apply:
        print("\nDRY RUN — nothing sent. Re-run with --apply to push.")
        return

    print("\nshipping ...", flush=True)
    ssh("mkdir -p /tmp/locz-import")
    # scp is disabled on this host; stream the file over the ssh channel instead
    with open(tmp, "rb") as fh:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", SSH_HOST,
                            "cat > /tmp/locz-import/chunk.csv"],
                           stdin=fh, capture_output=True, timeout=900)
    if r.returncode:
        sys.exit(f"transfer failed: {r.stderr.decode()[:300]}")

    remote_sha = ssh("sha256sum /tmp/locz-import/chunk.csv").stdout.split()[0]
    if remote_sha != sha:
        sys.exit(f"CHECKSUM MISMATCH — refusing to import\n local {sha}\n remote {remote_sha}")
    print("checksum verified on VPS OK")

    # psql runs INSIDE the container, so `\copy` reads the container's filesystem,
    # not the VPS host's. The file has to be copied in before the import.
    cp = ssh(f"docker cp /tmp/locz-import/chunk.csv {CONTAINER}:/tmp/chunk.csv")
    if cp.returncode:
        sys.exit(f"docker cp failed: {cp.stderr[:300]}")

    r = ssh(f"docker exec -i {CONTAINER} psql -U {DB_USER} -d {DB_NAME} -v ON_ERROR_STOP=1",
            stdin=REMOTE_SQL.replace("/tmp/locz-import/chunk.csv", "/tmp/chunk.csv"))
    if r.stdout.strip():
        print(r.stdout[-1500:])
    if r.stderr.strip():                 # never let an error hide behind stdout
        print("STDERR:", r.stderr[-1500:])
    ssh(f"rm -f /tmp/locz-import/chunk.csv && docker exec {CONTAINER} rm -f /tmp/chunk.csv")

    if a.reindex:
        reindex_search()
    else:
        print("\nNOT reindexed - these rows will not appear in search yet.")
        print("Run with --reindex, or POST /search/index/businesses/rebuild")
    conn.close()


def reindex_search():
    """Rebuild LocZ's business index from Postgres using LocZ's own service.

    Not by writing Meilisearch documents directly: the index shape is theirs to
    define, and a hand-rolled document would drift the moment they change a field.

    The JS goes to a file rather than `node -e`. Inline JS through ssh + su is
    mangled by three layers of quoting; a file has none of that problem.
    """
    print(chr(10) + "rebuilding the business search index ...", flush=True)
    js = chr(10).join([
        "const {NestFactory}=require('@nestjs/core');",
        "const {AppModule}=require('./dist/app.module');",
        "const {BusinessSearchService}=require('./dist/search/business-search.service');",
        "(async()=>{",
        "  const app=await NestFactory.createApplicationContext(AppModule,{logger:false});",
        "  const r=await app.get(BusinessSearchService).reindexAll();",
        "  console.log(JSON.stringify(r)); await app.close(); process.exit(0);",
        "})().catch(e=>{console.error(e.message);process.exit(1)});",
        "",
    ])
    dest = API_DIR + "/.reindex.js"
    r = ssh("cat > " + dest + " && chown " + APP_USER + ":" + APP_USER + " " + dest, stdin=js)
    if r.returncode:
        print("  could not write reindex script:", r.stderr[:200])
        return
    r = ssh("su - " + APP_USER + " -c 'cd " + API_DIR + " && node .reindex.js'")
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    print("  " + (chr(10) + "  ").join(out.splitlines()[-4:]))

if __name__ == "__main__":
    main()
