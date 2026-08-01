"""Live monitor for the LocZ Pincode Business Data Engine.

  python scripts/monitor.py      ->  http://127.0.0.1:8420/

Reads the resolved data from PostgreSQL (not the raw extract), plus live
progress of any extraction still writing to disk.
"""
import json, http.server, socketserver, threading, time, os, collections
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import psycopg

ROOT = Path(__file__).resolve().parents[1]
EXTRACT = ROOT / "var" / "extract"
OVERTURE = ROOT / "var" / "overture" / "india_places.parquet"
PORT = 8420
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

_state = {"started": time.time(), "hist": collections.deque(maxlen=90),
          "snap": {}, "last": 0.0}
_lock = threading.Lock()

STATS_SQL = {
    "total":  "SELECT count(*) FROM businesses",
    "tiers":  "SELECT tier::text, count(*) FROM businesses GROUP BY 1",
    "methods": """SELECT COALESCE(pincode_method::text,'unresolved'), count(*),
                         round(avg(pincode_confidence),2)::float
                  FROM businesses GROUP BY 1 ORDER BY 2 DESC""",
    "fields": """SELECT
        count(*) FILTER (WHERE public_phone IS NOT NULL),
        count(*) FILTER (WHERE website IS NOT NULL),
        count(*) FILTER (WHERE public_email IS NOT NULL),
        count(*) FILTER (WHERE opening_hours_raw IS NOT NULL),
        count(*) FILTER (WHERE address_line_1 IS NOT NULL),
        count(*) FILTER (WHERE locality IS NOT NULL),
        count(*) FILTER (WHERE pincode_code IS NOT NULL),
        count(*) FILTER (WHERE brand_name IS NOT NULL),
        count(*) FILTER (WHERE location_accuracy='building'),
        count(*) FROM businesses""",
    "sources": """SELECT d.name, count(b.id)
                  FROM data_sources d LEFT JOIN businesses b ON b.source_id=d.id
                  GROUP BY 1 HAVING count(b.id)>0 ORDER BY 2 DESC""",
    "cats": """SELECT c.slug, count(*) FROM businesses b JOIN categories c ON c.id=b.category_id
               GROUP BY 1 ORDER BY 2 DESC LIMIT 14""",
    "btypes": "SELECT COALESCE(business_type,'—'), count(*) FROM businesses GROUP BY 1 ORDER BY 2 DESC",
    "pins": """SELECT count(DISTINCT pincode_code) FROM businesses WHERE pincode_code IS NOT NULL""",
    "centroids": "SELECT centroid_src::text, count(*) FROM pincodes GROUP BY 1 ORDER BY 2 DESC",
    # mobile vs landline matters: a mobile is usually the owner, a landline the shop
    "lines": """SELECT COALESCE(phone_line_type,'none'), count(*) FROM businesses
                WHERE public_phone IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""",
    "phonestatus": """SELECT COALESCE(phone_status,'no phone'), count(*) FROM businesses
                      GROUP BY 1 ORDER BY 2 DESC""",
}

# Work that runs after the bulk load: identity, recovery, enrichment. Each is a
# separate optional query because the table it reads may not exist yet.
PROGRESS = [
    ("LocZ ids minted",       "SELECT count(locz_id) FROM businesses"),
    ("field provenance rows", "SELECT count(*) FROM business_field_sources"),
    ("enriched from website", "SELECT count(*) FROM business_field_sources WHERE licence_name='first-party-website'"),
    ("review queue pending",  "SELECT count(*) FROM review_queue WHERE status='pending'"),
    ("leads created",         "SELECT count(*) FROM business_leads"),
    ("exports written",       "SELECT count(*) FROM exports WHERE status='complete'"),
    ("with opening hours",    "SELECT count(*) FROM businesses WHERE opening_hours_raw IS NOT NULL"),
]

# Overture ingestion progress. Reported per phase because the dedup join is the
# long pole and a single percentage would hide where the time actually goes.
OVERTURE_TOTAL = 4_489_484
OV_SQL = """
SELECT (SELECT count(*) FROM ov_stage),
       (SELECT count(*) FROM ov_dup),
       (SELECT count(*) FROM businesses b JOIN data_sources d ON d.id=b.source_id
        WHERE d.slug='overture-places')
"""
FIELD_KEYS = ["phone", "website", "email", "hours", "address", "locality",
              "pincode", "brand", "building"]


def poll():
    last_n = None
    while True:
        snap = {}
        try:
            with psycopg.connect(DSN, connect_timeout=5) as c:
                cur = c.cursor()
                cur.execute(STATS_SQL["total"]);   snap["total"] = cur.fetchone()[0]
                cur.execute(STATS_SQL["tiers"]);   snap["tiers"] = dict(cur.fetchall())
                cur.execute(STATS_SQL["methods"]); snap["methods"] = cur.fetchall()
                cur.execute(STATS_SQL["fields"])
                row = cur.fetchone(); tot = max(row[-1], 1)
                snap["fields"] = {k: {"n": row[i], "pct": round(row[i]/tot*100, 1)}
                                  for i, k in enumerate(FIELD_KEYS)}
                cur.execute(STATS_SQL["sources"]); snap["sources"] = cur.fetchall()
                cur.execute(STATS_SQL["cats"]);    snap["cats"] = cur.fetchall()
                cur.execute(STATS_SQL["btypes"]);  snap["btypes"] = cur.fetchall()
                cur.execute(STATS_SQL["pins"]);    snap["pins"] = cur.fetchone()[0]
                cur.execute(STATS_SQL["centroids"]); snap["centroids"] = cur.fetchall()
                cur.execute(STATS_SQL["lines"]);     snap["lines"] = cur.fetchall()
                cur.execute(STATS_SQL["phonestatus"]); snap["phonestatus"] = cur.fetchall()
                prog = []
                for label, sql in PROGRESS:
                    try:
                        with c.transaction():        # savepoint: a missing table
                            cur.execute(sql)         # must not abort the outer work
                            prog.append([label, cur.fetchone()[0]])
                    except Exception:
                        prog.append([label, 0])
                snap["progress"] = prog
                try:
                    cur.execute(OV_SQL)
                    staged, dups, ins = cur.fetchone()
                    phases = [
                        ("extracted from S3", OVERTURE_TOTAL, OVERTURE_TOTAL),
                        ("staged (business categories)", staged, OVERTURE_TOTAL),
                        ("deduplicated vs existing", dups, staged or 1),
                        ("inserted as new businesses", ins, max((staged or 0) - (dups or 0), 1)),
                    ]
                    done = 3 if ins else (2 if dups else 1)
                    snap["overture"] = {
                        "phases": [{"label": l, "n": n, "of": d,
                                    "pct": round(min(n / d * 100, 100), 1) if d else 0}
                                   for l, n, d in phases],
                        "overall": round((done / 4) * 100),
                        "inserted": ins, "staged": staged, "dups": dups}
                except Exception:
                    snap["overture"] = None
            snap["db"] = True
        except Exception as exc:
            snap = {"db": False, "err": str(exc)[:160]}

        # pipelines still writing to disk
        jobs = []
        for label, path, unit in (
            ("OSM extract → businesses.jsonl", EXTRACT / "businesses.jsonl", "MB"),
            ("OSM extract → places.jsonl", EXTRACT / "places.jsonl", "MB"),
            ("Overture → india_places.parquet", OVERTURE, "MB"),
        ):
            if path.exists():
                st = path.stat()
                jobs.append({"label": label, "mb": round(st.st_size/1e6, 1),
                             "hot": (time.time() - st.st_mtime) < 25})
        snap["jobs"] = jobs

        # A monitor that slows the pipeline is worse than no monitor. When a bulk
        # write is running, poll slowly and skip the expensive queries entirely.
        busy = False
        try:
            with psycopg.connect(DSN, connect_timeout=5) as c:
                busy = c.execute("""SELECT count(*) FROM pg_stat_activity
                                    WHERE datname='locz_engine' AND state='active'
                                      AND pid<>pg_backend_pid()
                                      AND query ~* '(update|insert into) businesses'"""
                                 ).fetchone()[0] > 0
        except Exception:
            pass
        snap["busy"] = busy

        now = time.time()
        with _lock:
            n = snap.get("total")
            rate = 0.0
            if n is not None and last_n is not None:
                rate = max(0.0, (n - last_n) / 4.0)
            _state["hist"].append(rate)
            snap["rate"] = round(rate, 1)
            snap["spark"] = [round(x, 1) for x in _state["hist"]][-60:]
            snap["uptime_s"] = round(now - _state["started"])
            _state["snap"] = snap
            _state["last"] = now
            if n is not None:
                last_n = n
        time.sleep(20 if busy else 5)


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            body = (ROOT / "scripts" / "monitor.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # the page changes as panels are added; a cached copy silently hides them
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/stats":
            with _lock:
                self._json(_state["snap"])
            return
        if u.path == "/api/records":
            q = parse_qs(u.query)
            tier = (q.get("tier") or [""])[0]
            term = ((q.get("q") or [""])[0] or "").strip()
            where, args = ["1=1"], []
            if tier in ("CONTACTABLE", "LOCATABLE", "HELD"):
                where.append("b.tier = %s::export_tier"); args.append(tier)
            if term:
                where.append("(b.canonical_name %% %s OR b.locality ILIKE %s "
                             "OR b.city ILIKE %s OR c.slug ILIKE %s OR b.pincode_code = %s)")
                args += [term.lower(), f"%{term}%", f"%{term}%", f"%{term}%", term]
            sql = f"""SELECT b.display_name, c.slug, b.tier::text, b.public_phone,
                             b.opening_hours_raw, b.address_line_1, b.pincode_code,
                             b.locality, b.lat, b.lon, b.completeness_score,
                             b.confidence_score, b.pincode_confidence::float,
                             b.pincode_method::text, b.business_type, b.location_accuracy
                      FROM businesses b LEFT JOIN categories c ON c.id=b.category_id
                      WHERE {' AND '.join(where)}
                      ORDER BY b.updated_at DESC
                      LIMIT 60"""
            try:
                with psycopg.connect(DSN, connect_timeout=5) as conn:
                    cur = conn.cursor()
                    cur.execute("SET pg_trgm.similarity_threshold = 0.35")
                    cur.execute(sql, args)
                    cols = ["name", "cat", "tier", "phone", "hours", "addr", "pin",
                            "locality", "lat", "lon", "score", "conf", "pconf",
                            "pmethod", "btype", "acc"]
                    self._json([dict(zip(cols, r)) for r in cur.fetchall()])
            except Exception as exc:
                self._json({"error": str(exc)[:200]}, 500)
            return
        self.send_error(404)


if __name__ == "__main__":
    threading.Thread(target=poll, daemon=True).start()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler) as srv:
        print(f"LocZ engine monitor -> http://127.0.0.1:{PORT}/")
        srv.serve_forever()
