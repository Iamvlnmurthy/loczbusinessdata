"""Enrich businesses from their OWN websites.

  python scripts/enrich_websites.py --district Hyderabad --limit 2000

A business publishes its phone and hours on its own site to be found. Reading that
is ordinary use, not extraction from a third party — but it is still someone else's
server, so every fetch obeys:

  * that host's robots.txt (fetched once, cached, honoured including Crawl-delay)
  * one request per second per host, and a global concurrency cap
  * a short timeout, a small read limit, and no retries beyond one
  * an identifying User-Agent with a real contact address

Preference order for what we trust:
  1. schema.org JSON-LD LocalBusiness   - structured, authored by the business
  2. microdata / tel: links             - weaker, still first-party
Nothing is inferred from prose.

Values never overwrite an existing one. They fill blanks, and each write is recorded
in business_field_sources so provenance survives.
"""
import argparse, json, os, re, sys, threading, time, queue
import urllib.request, urllib.robotparser
from urllib.parse import urlparse, urljoin
from html.parser import HTMLParser
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
UA = os.environ.get("LOCZ_USER_AGENT",
                    "LocZ-Pincode-Business-Engine/0.1 (+enrichment; infovivencia2026@gmail.com)")
TIMEOUT = 12
MAX_BYTES = 400_000
PER_HOST_DELAY = 1.0

_robots, _robots_lock = {}, threading.Lock()
_host_last, _host_lock = {}, threading.Lock()


def allowed(url):
    """robots.txt is the host's instruction. A 4xx means no rules; a timeout means
    we do not know, and not knowing is not permission."""
    p = urlparse(url)
    key = f"{p.scheme}://{p.netloc}"
    with _robots_lock:
        rp = _robots.get(key)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(urljoin(key, "/robots.txt"))
        try:
            req = urllib.request.Request(urljoin(key, "/robots.txt"),
                                         headers={"User-Agent": UA})
            body = urllib.request.urlopen(req, timeout=8).read(100_000).decode("utf-8", "replace")
            rp.parse(body.splitlines())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                rp = False                     # explicitly withheld
            else:
                rp.parse([])                   # 404 etc: no rules published
        except Exception:
            rp = False                         # unknown -> do not fetch
        with _robots_lock:
            _robots[key] = rp
    if rp is False:
        return False, 0.0
    try:
        delay = rp.crawl_delay(UA) or 0.0
    except Exception:
        delay = 0.0
    return rp.can_fetch(UA, url), max(float(delay), PER_HOST_DELAY)


def throttle(host, delay):
    while True:
        with _host_lock:
            last = _host_last.get(host, 0.0)
            wait = last + delay - time.time()
            if wait <= 0:
                _host_last[host] = time.time()
                return
        time.sleep(min(wait, 2.0))


class Extract(HTMLParser):
    """Pull JSON-LD blocks and tel: links. Deliberately shallow — no prose parsing."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.jsonld, self._in_ld, self.tels = [], False, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._in_ld = True
        if tag == "a":
            href = a.get("href") or ""
            if href.lower().startswith("tel:"):
                self.tels.append(href[4:])

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_ld = False

    def handle_data(self, data):
        if self._in_ld and data.strip():
            self.jsonld.append(data.strip())


BIZ_TYPES = ("LocalBusiness", "Store", "Restaurant", "MedicalBusiness", "Dentist",
             "Pharmacy", "HealthAndBeautyBusiness", "AutomotiveBusiness", "Hotel",
             "FoodEstablishment", "ProfessionalService", "HomeAndConstructionBusiness",
             "EducationalOrganization", "Organization")


def walk(node, out):
    if isinstance(node, list):
        for x in node:
            walk(x, out)
        return
    if not isinstance(node, dict):
        return
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    if any(isinstance(x, str) and x in BIZ_TYPES for x in types):
        out.append(node)
    for v in node.values():
        if isinstance(v, (dict, list)):
            walk(v, out)


def norm_in_phone(raw):
    d = re.sub(r"\D", "", str(raw or ""))
    if d.startswith("0091"):
        d = d[4:]
    elif d.startswith("91") and len(d) == 12:
        d = d[2:]
    elif d.startswith("0") and len(d) == 11:
        d = d[1:]
    if len(d) != 10 or d[:4] in ("1800", "1860", "1900") or d[:3] == "140":
        return None, None
    if re.search(r"(\d)\1{5,}", d):
        return None, None
    if d[0] in "6789":
        return "+91" + d, "mobile"
    if d[0] in "12345":
        return "+91" + d, "landline"
    return None, None


def hours_from(node):
    h = node.get("openingHours") or node.get("openingHoursSpecification")
    if isinstance(h, str):
        return h[:120]
    if isinstance(h, list) and h and isinstance(h[0], str):
        return "; ".join(h)[:120]
    if isinstance(h, list) and h and isinstance(h[0], dict):
        parts = []
        for s in h[:7]:
            d = s.get("dayOfWeek")
            d = ",".join(x.split("/")[-1][:2] for x in d) if isinstance(d, list) else str(d).split("/")[-1][:2]
            o, c = s.get("opens"), s.get("closes")
            if d and o and c:
                parts.append(f"{d} {o}-{c}")
        return "; ".join(parts)[:120] or None
    return None


def fetch(url):
    ok, delay = allowed(url)
    if not ok:
        return None, "robots_denied"
    host = urlparse(url).netloc
    throttle(host, delay)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept": "text/html,*/*"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "html" not in ctype:
                return None, "not_html"
            return r.read(MAX_BYTES).decode("utf-8", "replace"), None
    except Exception as e:
        return None, type(e).__name__


def parse(html):
    p = Extract()
    try:
        p.feed(html)
    except Exception:
        pass
    nodes = []
    for blob in p.jsonld:
        try:
            walk(json.loads(blob), nodes)
        except Exception:
            continue
    phone = line = hours = email = None
    for n in nodes:
        if not phone:
            for k in ("telephone", "phone"):
                v = n.get(k)
                if v:
                    phone, line = norm_in_phone(v if isinstance(v, str) else v[0])
                    if phone:
                        break
        if not hours:
            hours = hours_from(n)
        if not email and n.get("email"):
            e = n["email"]
            email = (e if isinstance(e, str) else e[0])[:180]
    if not phone:
        for t in p.tels[:5]:
            phone, line = norm_in_phone(t)
            if phone:
                break
    return {"phone": phone, "line": line, "hours": hours, "email": email}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--district"); ap.add_argument("--state"); ap.add_argument("--pincode", nargs="*")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()

    where, args = ["website IS NOT NULL", "(public_phone IS NULL OR opening_hours_raw IS NULL)"], []
    if a.district:
        where.append("district ILIKE %s"); args.append(a.district)
    if a.state:
        where.append("state ILIKE %s"); args.append(a.state)
    if a.pincode:
        where.append("pincode_code = ANY(%s)"); args.append(a.pincode)

    conn = psycopg.connect(DSN)
    cur = conn.cursor()
    cur.execute(f"""SELECT id, website, public_phone, opening_hours_raw
                    FROM businesses WHERE {' AND '.join(where)}
                    ORDER BY confidence_score DESC LIMIT %s""", args + [a.limit])
    rows = cur.fetchall()
    print(f"candidates: {len(rows):,}   workers: {a.workers}   1 req/s per host")
    if not rows:
        return

    q, results, lock = queue.Queue(), [], threading.Lock()
    for r in rows:
        q.put(r)
    stats = {"ok": 0, "robots": 0, "err": 0, "nothing": 0}

    def work():
        while True:
            try:
                bid, url, cur_phone, cur_hours = q.get_nowait()
            except queue.Empty:
                return
            html, err = fetch(url)
            if err == "robots_denied":
                with lock: stats["robots"] += 1
                q.task_done(); continue
            if not html:
                with lock: stats["err"] += 1
                q.task_done(); continue
            got = parse(html)
            useful = ((got["phone"] and not cur_phone) or
                      (got["hours"] and not cur_hours) or got["email"])
            with lock:
                if useful:
                    results.append((bid, url, got, cur_phone, cur_hours))
                    stats["ok"] += 1
                else:
                    stats["nothing"] += 1
                done = sum(stats.values())
                if done % 100 == 0:
                    print(f"  {done}/{len(rows)}  found {stats['ok']}  "
                          f"robots-denied {stats['robots']}  errors {stats['err']}", flush=True)
            q.task_done()

    t0 = time.time()
    threads = [threading.Thread(target=work, daemon=True) for _ in range(a.workers)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    print(f"\nfetched {len(rows):,} in {(time.time()-t0)/60:.1f} min")
    print(f"  yielded something : {stats['ok']:,}")
    print(f"  robots denied     : {stats['robots']:,}")
    print(f"  errors/timeouts   : {stats['err']:,}")
    print(f"  nothing useful    : {stats['nothing']:,}")

    # write: fill blanks only, and record provenance for every value
    filled_p = filled_h = filled_e = 0
    for bid, url, got, cur_phone, cur_hours in results:
        sets, vals = [], []
        if got["phone"] and not cur_phone:
            sets += ["public_phone=%s", "phone_line_type=%s", "phone_status='valid_'||%s"]
            vals += [got["phone"], got["line"], got["line"]]; filled_p += 1
        if got["hours"] and not cur_hours:
            sets.append("opening_hours_raw=%s"); vals.append(got["hours"]); filled_h += 1
        if got["email"]:
            sets.append("public_email=COALESCE(public_email,%s)"); vals.append(got["email"]); filled_e += 1
        if not sets:
            continue
        cur.execute(f"UPDATE businesses SET {', '.join(sets)}, updated_at=now() WHERE id=%s",
                    vals + [bid])
        for field, value in (("public_phone", got["phone"] if not cur_phone else None),
                             ("opening_hours_raw", got["hours"] if not cur_hours else None),
                             ("public_email", got["email"])):
            if value:
                cur.execute("""INSERT INTO business_field_sources
                    (business_id, field_name, source_value, normalised_value,
                     confidence, licence_name, notes)
                    VALUES (%s,%s,%s,%s,0.85,'first-party-website',%s)""",
                            (bid, field, value, value, f"fetched from {url}"))
    conn.commit()
    print(f"\nfilled  phone {filled_p:,}  hours {filled_h:,}  email {filled_e:,}")

    # a website-sourced number can still be a shared corporate line
    cur.execute("""WITH shared AS (SELECT public_phone FROM businesses
                     WHERE public_phone IS NOT NULL GROUP BY 1 HAVING count(*)>3)
                   UPDATE businesses b SET phone_raw=COALESCE(phone_raw,b.public_phone),
                          phone_status='shared_number', public_phone=NULL, phone_line_type=NULL
                   FROM shared s WHERE b.public_phone=s.public_phone""")
    print(f"re-suppressed shared numbers: {cur.rowcount:,}")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
