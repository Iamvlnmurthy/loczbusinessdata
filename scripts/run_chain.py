"""Wait for finalise to land, then run recovery and the enrichment pilot.

Sequenced rather than parallel: recovery inserts rows, enrichment updates them,
and both want the same table. Running them together would just make each wait.
"""
import subprocess, sys, time, os
from pathlib import Path
import psycopg

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
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


def col_exists(name):
    with psycopg.connect(DSN, connect_timeout=10) as c:
        return c.execute("""SELECT count(*) FROM information_schema.columns
                            WHERE table_name='businesses' AND column_name=%s""",
                         (name,)).fetchone()[0] > 0


def busy():
    with psycopg.connect(DSN, connect_timeout=10) as c:
        return c.execute("""SELECT count(*) FROM pg_stat_activity
                            WHERE datname='locz_engine' AND state='active'
                              AND pid<>pg_backend_pid()
                              AND query ~* '(update|insert into) businesses'""").fetchone()[0] > 0


def run(label, args, timeout=7200):
    print(f"\n{'='*70}\n{label}\n{'='*70}", flush=True)
    r = subprocess.run([PY] + args, capture_output=True, text=True, timeout=timeout)
    out = (r.stdout or "") + (r.stderr or "")
    print("\n".join(out.strip().splitlines()[-40:]), flush=True)
    return r.returncode


def main():
    print("waiting for finalise (locz_id column) ...", flush=True)
    t0 = time.time()
    while not col_exists("locz_id") and time.time() - t0 < 3600:
        time.sleep(20)
    print(f"locz_id present after {int(time.time()-t0)}s" if col_exists("locz_id")
          else "timed out waiting for finalise — continuing anyway", flush=True)

    while busy() and time.time() - t0 < 5400:
        print("  ... table busy", flush=True)
        time.sleep(20)

    run("RECOVERY — records my own filters dropped",
        [str(ROOT / "scripts" / "recover_dropped.py")])

    while busy():
        time.sleep(15)

    run("ENRICHMENT PILOT — Hyderabad, first-party websites",
        [str(ROOT / "scripts" / "enrich_websites.py"),
         "--district", "Hyderabad", "--limit", "1500", "--workers", "12"])

    print("\n" + "="*70)
    with psycopg.connect(DSN) as c:
        for label, sql in [
            ("businesses",        "SELECT count(*) FROM businesses"),
            ("with phone",        "SELECT count(*) FROM businesses WHERE public_phone IS NOT NULL"),
            ("with hours",        "SELECT count(*) FROM businesses WHERE opening_hours_raw IS NOT NULL"),
            ("field provenance",  "SELECT count(*) FROM business_field_sources"),
            ("review queue",      "SELECT count(*) FROM review_queue WHERE status='pending'"),
        ]:
            try:
                print(f"  {label:20s} {c.execute(sql).fetchone()[0]:,}")
            except Exception as e:
                print(f"  {label:20s} -- {str(e)[:50]}")


if __name__ == "__main__":
    main()
