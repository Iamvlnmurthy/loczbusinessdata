"""Push every state to LocZ, one at a time, with progress and headroom checks.

State by state rather than one 3.4M transaction: a failure costs one state, not
the whole run, and each chunk commits before the next begins.

Search indexing is deliberately NOT triggered. LocZ's Meilisearch container is
capped at 512 MB and indexing 3.4M documents would OOM it — taking listings search
down with it. Business search needs to move to Postgres FTS (a 124 MB index, no
separate process) before the full set can be searchable.
"""
import os, subprocess, sys, time
from pathlib import Path
import psycopg

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
SSH_HOST = os.environ.get("LOCZ_SSH_HOST", "onrol")
MIN_FREE_GB = 5          # stop if the VPS drops below this


def _dsn():
    v = os.environ.get("LOCZ_DSN")
    if not v:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("LOCZ_DSN="):
                return line.split("=", 1)[1].strip()
        raise SystemExit("LOCZ_DSN is not set")
    return v


def vps(cmd):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", SSH_HOST, cmd],
                       capture_output=True, text=True, timeout=120)
    return r.stdout.strip()


def headroom():
    """Free disk GB on the VPS. Refuse to keep filling a disk that is running out."""
    out = vps("df -BG --output=avail / | tail -1")
    try:
        return int(out.replace("G", "").strip())
    except Exception:
        return 999


def main():
    conn = psycopg.connect(_dsn())
    cur = conn.cursor()
    cur.execute("""SELECT p.state_name, count(*) FROM businesses b
                   JOIN pincodes p ON p.code = b.pincode_code
                   WHERE b.tier <> 'HELD' AND b.pincode_confidence >= 0.70
                     AND p.centroid_src <> 'unverified'
                   GROUP BY 1 ORDER BY 2 DESC""")
    states = cur.fetchall()
    conn.close()
    total = sum(n for _, n in states)
    print(f"{len(states)} states, {total:,} pushable businesses\n")

    done, t0 = 0, time.time()
    for state, n in states:
        free = headroom()
        if free < MIN_FREE_GB:
            print(f"\nSTOPPING: only {free} GB free on the VPS")
            break
        print(f"[{state}]  {n:,} ...", flush=True)
        r = subprocess.run(
            [PY, str(ROOT / "scripts" / "push_to_locz.py"),
             "--state", state, "--limit", str(n + 1000),
             "--per-category", str(max(n, 1000)), "--apply"],
            capture_output=True, text=True, timeout=7200,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        out = (r.stdout or "") + (r.stderr or "")
        ins = [l for l in out.splitlines() if "INSERT" in l or "total_businesses" in l]
        err = [l for l in out.splitlines() if "ERROR" in l or "STDERR" in l]
        done += n
        el = (time.time() - t0) / 60
        print(f"    {' | '.join(ins[-2:]) or 'no rows'}"
              f"   [{done:,}/{total:,}  {el:.0f} min  {free} GB free]")
        if err:
            print(f"    !! {err[0][:160]}")

    print(f"\ndone in {(time.time()-t0)/60:.0f} min")
    print("Search index NOT rebuilt — see the note at the top of this file.")


if __name__ == "__main__":
    main()
