"""Run the post-load steps in order, once the ingestion pipeline is idle.

  identity -> leads schema -> disambiguation -> compliance tests -> pilot export

Each step is idempotent and reports what it changed.
"""
import subprocess, sys, time, os
from pathlib import Path
import psycopg

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
PSQL = r"C:\Program Files\PostgreSQL\18\bin\psql.exe"
DSN = os.environ.get("LOCZ_DSN",
                     "host=127.0.0.1 port=5433 dbname=locz_engine user=postgres "
                     "password=LocZEngine_2026!")
ENV = {**os.environ, "PGPASSWORD": "LocZEngine_2026!"}


def wait_idle(timeout=1800):
    """DDL needs an exclusive lock; taking it while a bulk UPDATE runs just queues
    behind it and blocks every reader in the meantime."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        with psycopg.connect(DSN, connect_timeout=10) as c:
            n = c.execute("""SELECT count(*) FROM pg_stat_activity
                             WHERE datname='locz_engine' AND state='active'
                               AND pid <> pg_backend_pid()
                               AND query ~* '(update|insert into) businesses'""").fetchone()[0]
        if n == 0:
            return True
        print(f"  ... pipeline busy ({int(time.time()-t0)}s)", flush=True)
        time.sleep(15)
    return False


def psql(path):
    r = subprocess.run([PSQL, "-U", "postgres", "-h", "127.0.0.1", "-p", "5433",
                        "-d", "locz_engine", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(path)],
                       env=ENV, capture_output=True, text=True)
    if r.returncode:
        print(r.stderr[-600:])
    return r.returncode == 0


def sql(stmt):
    with psycopg.connect(DSN, autocommit=True) as c:
        cur = c.cursor()
        cur.execute(stmt)
        try:
            return cur.fetchall()
        except psycopg.ProgrammingError:
            return cur.rowcount


def main():
    print("waiting for the ingestion pipeline to go idle ...", flush=True)
    if not wait_idle():
        sys.exit("pipeline still busy after 30 min — not forcing a lock")
    print("pipeline idle\n")

    # ---------------------------------------------------------------- 1. identity
    print("[1/5] LocZ business identity")
    if psql(ROOT / "sql" / "004_locz_id.sql"):
        n = sql("SELECT count(*) FROM businesses WHERE locz_id IS NULL")[0][0]
        if n:
            print(f"  backfilling {n:,} ids ...", flush=True)
            sql("""UPDATE businesses SET locz_id = locz_mint(nextval('locz_id_seq'))
                   WHERE locz_id IS NULL""")
        sql("CREATE UNIQUE INDEX IF NOT EXISTS businesses_locz_id_uq ON businesses (locz_id)")
        got = sql("SELECT count(*), count(DISTINCT locz_id) FROM businesses")[0]
        print(f"  ids: {got[0]:,} rows / {got[1]:,} distinct")
        for r in sql("SELECT locz_id, display_name, pincode_code FROM businesses LIMIT 4"):
            print(f"    {r[0]}  {str(r[1])[:38]:40s} {r[2]}")

    # ---------------------------------------------------------------- 2. leads
    print("\n[2/5] leads schema")
    print("  ok" if psql(ROOT / "sql" / "003_leads.sql") else "  FAILED")

    # ---------------------------------------------------------------- 3. names
    print("\n[3/5] name disambiguation")
    r = subprocess.run([PY, str(ROOT / "scripts" / "disambiguate_names.py")],
                       capture_output=True, text=True)
    print("  " + "\n  ".join((r.stdout or r.stderr).strip().splitlines()[-14:]))

    # ---------------------------------------------------------------- 4. tests
    print("\n[4/5] compliance tests")
    r = subprocess.run([PY, str(ROOT / "tests" / "test_compliance.py")],
                       capture_output=True, text=True)
    print("  " + "\n  ".join((r.stdout or r.stderr).strip().splitlines()))

    # ---------------------------------------------------------------- 5. export
    print("\n[5/5] pilot export — Hyderabad + Warangal")
    r = subprocess.run([PY, str(ROOT / "scripts" / "export.py"),
                        "--district", "Hyderabad", "--tier", "CONTACTABLE",
                        "--format", "csv", "--limit", "5000"],
                       capture_output=True, text=True)
    print("  " + "\n  ".join((r.stdout or r.stderr).strip().splitlines()[-12:]))


if __name__ == "__main__":
    main()
