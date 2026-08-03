"""Create the `addresses` rows LocZ's API reads from.

`BusinessSummaryDto.addressLine` comes from the related `addresses` row, not from a
column on `businesses`. The first import left `addressId` null, so every imported
business returned `addressLine: null` and rendered as a card with no location —
which is very likely why the browse list looked empty.

Ships (sourceRecordId, line1, locality) for the records already in LocZ, creates one
address per business, and links it. Idempotent: a business that already has an
addressId is skipped, so re-running is safe.
"""
import csv, os, subprocess, sys, tempfile
from pathlib import Path
import psycopg

ROOT = Path(__file__).resolve().parents[1]
SSH_HOST = os.environ.get("LOCZ_SSH_HOST", "onrol")
CONTAINER = "locz-postgres"
DB_USER = DB_NAME = "locz"


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


REMOTE_SQL = r"""
BEGIN;

CREATE TEMP TABLE addr_in (
  source_record_id text, line1 text, locality text
) ON COMMIT DROP;

\copy addr_in FROM '/tmp/addr.csv' WITH (FORMAT csv, HEADER true)

-- one address per business that still lacks one
WITH need AS (
  SELECT b.id AS business_id, b."cityId", b."pincodeCode",
         b.latitude, b.longitude, b.geo,
         NULLIF(a.line1, '')    AS line1,
         NULLIF(a.locality, '') AS line2
  FROM businesses b
  JOIN addr_in a ON a.source_record_id = b."sourceRecordId"
  WHERE b."addressId" IS NULL
    AND b."sourceName" IS NOT NULL
), made AS (
  INSERT INTO addresses (id, line1, line2, "cityId", "postalCode",
                         latitude, longitude, geo, "createdAt", "updatedAt")
  SELECT gen_random_uuid(), n.line1, n.line2, n."cityId", n."pincodeCode",
         n.latitude, n.longitude, n.geo, now(), now()
  FROM need n
  RETURNING id, "postalCode", latitude, longitude
)
-- link each new address back to its business by matching the coordinates we just wrote
UPDATE businesses b
SET "addressId" = m.id, "updatedAt" = now()
FROM made m
WHERE b."addressId" IS NULL
  AND b."sourceName" IS NOT NULL
  AND b.latitude = m.latitude
  AND b.longitude = m.longitude;

SELECT count(*) FILTER (WHERE "addressId" IS NOT NULL) AS with_address,
       count(*) AS total
FROM businesses WHERE "sourceName" IS NOT NULL;

COMMIT;
"""


def ssh(cmd, stdin=None):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", SSH_HOST, cmd],
                          input=stdin, capture_output=True, text=True, timeout=900)


def main():
    conn = psycopg.connect(_dsn())
    cur = conn.cursor()
    cur.execute("""SELECT source_record_id, address_line_1, locality
                   FROM businesses
                   WHERE source_record_id IS NOT NULL""")
    rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    conn.close()
    print(f"engine records available: {len(rows):,}")

    # which ones are actually in LocZ
    r = ssh(f'docker exec -i {CONTAINER} psql -U {DB_USER} -d {DB_NAME} -tAc '
            f'"SELECT \\"sourceRecordId\\" FROM businesses WHERE \\"addressId\\" IS NULL '
            f'AND \\"sourceName\\" IS NOT NULL"')
    ids = [x.strip() for x in r.stdout.splitlines() if x.strip()]
    print(f"in LocZ without an address: {len(ids):,}")
    if not ids:
        print("nothing to do")
        return

    tmp = Path(tempfile.gettempdir()) / "locz_addr.csv"
    n = 0
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["source_record_id", "line1", "locality"])
        for sid in ids:
            line1, locality = rows.get(sid, (None, None))
            if not line1 and not locality:
                continue
            w.writerow([sid, line1 or "", locality or ""])
            n += 1
    print(f"shipping {n:,} addresses ({tmp.stat().st_size/1024:.0f} KB)")

    with open(tmp, "rb") as fh:
        t = subprocess.run(["ssh", "-o", "BatchMode=yes", SSH_HOST,
                            "cat > /tmp/addr.csv"], stdin=fh,
                           capture_output=True, timeout=600)
    if t.returncode:
        sys.exit(f"transfer failed: {t.stderr.decode()[:200]}")
    cp = ssh(f"docker cp /tmp/addr.csv {CONTAINER}:/tmp/addr.csv")
    if cp.returncode:
        sys.exit(f"docker cp failed: {cp.stderr[:200]}")

    r = ssh(f"docker exec -i {CONTAINER} psql -U {DB_USER} -d {DB_NAME} -v ON_ERROR_STOP=1",
            stdin=REMOTE_SQL)
    if r.stdout.strip():
        print(r.stdout[-900:])
    if r.stderr.strip():
        print("STDERR:", r.stderr[-900:])
    ssh(f"rm -f /tmp/addr.csv && docker exec {CONTAINER} rm -f /tmp/addr.csv")


if __name__ == "__main__":
    main()
