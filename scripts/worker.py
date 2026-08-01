"""Maintenance worker: Postgres-backed queue + scheduled housekeeping.

Runs unattended under a Windows Scheduled Task. Every task is idempotent, so a
kill at any point is safe - the next tick redoes the work rather than corrupting it.

Scheduled work:
  purge_expired_raw     hourly   drop raw payloads past their licence retention
  refresh_coverage      6-hourly recompute pincode x category coverage + gaps
  suppress_shared_phones hourly  a number reused by >3 businesses is not a shop line
  vacuum_analyze        daily    keep the planner honest after bulk loads
"""
import json, os, socket, time, traceback
from datetime import datetime, timezone
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
WORKER = f"{socket.gethostname()}:{os.getpid()}"
POLL_SECONDS = 10

SCHEDULE = [                       # (name, interval_seconds)
    ("purge_expired_raw",      3600),
    ("suppress_shared_phones", 3600),
    ("refresh_coverage",      21600),
    ("vacuum_analyze",        86400),
]


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat()} {msg}", flush=True)


# ---------------------------------------------------------------- tasks
def purge_expired_raw(cur):
    cur.execute("""UPDATE raw_records SET payload='{}'::jsonb, payload_purged_at=now()
                   WHERE purge_after IS NOT NULL AND purge_after < now()
                     AND payload_purged_at IS NULL""")
    return {"purged": cur.rowcount}


def suppress_shared_phones(cur):
    """A number on more than 3 businesses is a call centre, not a shop's own line."""
    cur.execute("""WITH shared AS (
                     SELECT public_phone FROM businesses
                     WHERE public_phone IS NOT NULL
                     GROUP BY 1 HAVING count(*) > 3)
                   UPDATE businesses b
                      SET phone_raw = COALESCE(b.phone_raw, b.public_phone),
                          phone_status = 'shared_number',
                          public_phone = NULL, phone_line_type = NULL
                   FROM shared s WHERE b.public_phone = s.public_phone""")
    n = cur.rowcount
    if n:
        cur.execute("""UPDATE businesses SET tier = CASE
                         WHEN pincode_code IS NULL OR pincode_confidence < 0.55 THEN 'HELD'::export_tier
                         WHEN public_phone IS NOT NULL THEN 'CONTACTABLE'::export_tier
                         ELSE 'LOCATABLE'::export_tier END
                       WHERE phone_status = 'shared_number'""")
    return {"suppressed": n}


def refresh_coverage(cur):
    cur.execute("""
      INSERT INTO pincode_category_coverage
        (pincode_code, category_id, found, contactable, locatable, status)
      SELECT b.pincode_code, b.category_id, count(*),
             count(*) FILTER (WHERE b.tier='CONTACTABLE'),
             count(*) FILTER (WHERE b.tier='LOCATABLE'),
             CASE WHEN count(*) = 0 THEN 'no_source_data'
                  WHEN count(*) = 1 THEN 'insufficient'
                  WHEN count(*) = 2 THEN 'minimum_reached'
                  WHEN count(*) = 3 THEN 'target_reached'
                  ELSE 'over_target' END
      FROM businesses b
      WHERE b.pincode_code IS NOT NULL AND b.category_id IS NOT NULL AND b.tier <> 'HELD'
      GROUP BY 1,2
      ON CONFLICT (pincode_code, category_id) DO UPDATE
        SET found=EXCLUDED.found, contactable=EXCLUDED.contactable,
            locatable=EXCLUDED.locatable, status=EXCLUDED.status, last_run_at=now()""")
    n = cur.rowcount
    cur.execute("""
      INSERT INTO coverage_gaps (pincode_code, category_id, mapped, contactable, gap, priority)
      SELECT pincode_code, category_id, found, contactable,
             GREATEST(2 - contactable, 0),
             GREATEST(2 - contactable, 0) * 10 + LEAST(found, 5)
      FROM pincode_category_coverage
      ON CONFLICT (pincode_code, category_id) DO UPDATE
        SET mapped=EXCLUDED.mapped, contactable=EXCLUDED.contactable,
            gap=EXCLUDED.gap, priority=EXCLUDED.priority, computed_at=now()""")
    return {"cells": n}


def vacuum_analyze(cur):
    return {"skipped": "runs outside a transaction"}


TASKS = {"purge_expired_raw": purge_expired_raw,
         "suppress_shared_phones": suppress_shared_phones,
         "refresh_coverage": refresh_coverage,
         "vacuum_analyze": vacuum_analyze}


def claim_job(cur):
    cur.execute("""UPDATE jobs SET state='running', locked_at=now(), locked_by=%s,
                                   attempts=attempts+1, updated_at=now()
                   WHERE id = (SELECT id FROM jobs
                               WHERE state='queued' AND run_after <= now()
                               ORDER BY priority, run_after
                               FOR UPDATE SKIP LOCKED LIMIT 1)
                   RETURNING id, kind, payload""", (WORKER,))
    return cur.fetchone()


def main():
    last = {name: 0.0 for name, _ in SCHEDULE}
    log(f"worker up as {WORKER}")
    while True:
        try:
            with psycopg.connect(DSN, connect_timeout=10, autocommit=True) as conn:
                while True:
                    cur = conn.cursor()
                    # 1. scheduled housekeeping
                    now = time.time()
                    for name, every in SCHEDULE:
                        if now - last[name] < every:
                            continue
                        if name == "vacuum_analyze":
                            cur.execute("VACUUM (ANALYZE) businesses")
                            log("vacuum_analyze done")
                        else:
                            try:
                                res = TASKS[name](cur)
                                log(f"{name} {json.dumps(res)}")
                            except Exception as exc:
                                log(f"{name} FAILED {exc}")
                        last[name] = now
                    # 2. queued jobs
                    job = claim_job(cur)
                    if job:
                        jid, kind, payload = job
                        try:
                            fn = TASKS.get(kind)
                            res = fn(cur) if fn else {"error": "unknown kind"}
                            cur.execute("UPDATE jobs SET state='done', updated_at=now() WHERE id=%s", (jid,))
                            log(f"job {jid} {kind} {json.dumps(res)}")
                        except Exception as exc:
                            cur.execute("""UPDATE jobs SET state=CASE WHEN attempts>=max_attempts
                                             THEN 'failed' ELSE 'queued' END,
                                           last_error=%s,
                                           run_after=now() + (interval '30 seconds' * attempts),
                                           updated_at=now() WHERE id=%s""",
                                        (str(exc)[:500], jid))
                            log(f"job {jid} {kind} error: {exc}")
                        continue                      # drain the queue before sleeping
                    time.sleep(POLL_SECONDS)
        except Exception:
            log("connection lost, retrying in 20s\n" + traceback.format_exc()[:600])
            time.sleep(20)


if __name__ == "__main__":
    main()
