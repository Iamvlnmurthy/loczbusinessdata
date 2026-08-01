"""Open Charge Map -> EV charging stations for India.

Licence: CC-BY 4.0 (per-record, reported by the API). Redistribution permitted with
attribution, which travels with every row.

EV charging gets its own canonical category rather than sitting inside
petrol-stations: someone searching "EV charging near me" does not want a diesel
pump, and the two have nothing in common but the word "station".

Coverage note: OCM aggregates operator networks and community submissions. It is
far better than OSM or Overture for this category (~2% coverage there) but is not
a complete national registry - operators publish through their own apps.
"""
import json, os, re, time, urllib.parse, urllib.request
from pathlib import Path
import psycopg

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("LOCZ_DSN",
                     "host=127.0.0.1 port=5433 dbname=locz_engine user=postgres "
                     "password=LocZEngine_2026!")
UA = "LocZ-Pincode-Business-Engine/0.1 (infovivencia2026@gmail.com)"
API = "https://api.openchargemap.io/v3/poi/"
PAGE = 5000


def key():
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("OCM_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("OCM_API_KEY missing from .env")


def fetch_all(api_key):
    out, offset = [], 0
    while True:
        q = urllib.parse.urlencode({
            "countrycode": "IN", "maxresults": PAGE, "offset": offset,
            "compact": "false", "verbose": "false", "key": api_key})
        req = urllib.request.Request(f"{API}?{q}", headers={"User-Agent": UA})
        for attempt in range(3):
            try:
                page = json.load(urllib.request.urlopen(req, timeout=120))
                break
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(4 * (attempt + 1))
        if not page:
            break
        out.extend(page)
        print(f"  fetched {len(out):,}", flush=True)
        if len(page) < PAGE:
            break
        offset += PAGE
        time.sleep(1.0)                      # courtesy to a free API
    return out


def clean(v, n=None):
    if v is None:
        return None
    v = re.sub(r"\s+", " ", "".join(c for c in str(v) if ord(c) >= 32)).strip()
    return (v[:n] if n else v) or None


def norm_phone(raw):
    d = re.sub(r"\D", "", str(raw or ""))
    if d.startswith("91") and len(d) == 12:
        d = d[2:]
    elif d.startswith("0") and len(d) == 11:
        d = d[1:]
    if len(d) != 10 or d[:4] in ("1800", "1860") or re.search(r"(\d)\1{5,}", d):
        return None, None
    if d[0] in "6789":
        return "+91" + d, "mobile"
    if d[0] in "12345":
        return "+91" + d, "landline"
    return None, None


def main():
    conn = psycopg.connect(DSN)
    cur = conn.cursor()

    cur.execute("""INSERT INTO categories (slug, name, sort_order)
                   VALUES ('ev-charging-stations','Ev Charging Stations',46)
                   ON CONFLICT (slug) DO NOTHING RETURNING id""")
    conn.commit()
    cur.execute("SELECT id FROM categories WHERE slug='ev-charging-stations'")
    cat_id = cur.fetchone()[0]

    cur.execute("""INSERT INTO data_sources
        (slug,name,source_type,adapter_key,base_url,provider_name,licence_name,licence_url,
         attribution_text,commercial_use_allowed,storage_allowed,redistribution_allowed,
         automated_access_allowed,rate_limit_per_minute,enabled,status,reviewed_by,
         reviewed_at,notes)
        VALUES ('open-charge-map','Open Charge Map','api','ocm_api',
                'https://api.openchargemap.io/v3/poi/','Open Charge Map',
                'CC-BY-4.0','https://creativecommons.org/licenses/by/4.0/',
                '© Open Charge Map contributors',true,true,true,true,60,
                true,'approved','phase-3',now(),
                'Documented public API, free key. Per-record licence reported as CC BY 4.0.')
        ON CONFLICT (slug) DO UPDATE SET updated_at=now() RETURNING id""")
    src_id = cur.fetchone()[0]
    conn.commit()

    print("fetching Open Charge Map, country=IN ...", flush=True)
    pois = fetch_all(key())
    print(f"total returned: {len(pois):,}")

    cur.execute("DROP TABLE IF EXISTS ocm_stage")
    cur.execute("""CREATE TABLE ocm_stage (external_id text PRIMARY KEY, display_name text,
        canonical_name text, lat double precision, lon double precision, phone text,
        phone_line text, website text, address text, town text, state text,
        pincode varchar(6), operator text, status text, points int, updated timestamptz)""")

    seen, kept, skipped = set(), 0, 0
    with cur.copy("COPY ocm_stage FROM STDIN") as cp:
        for p in pois:
            a = p.get("AddressInfo") or {}
            lat, lon = a.get("Latitude"), a.get("Longitude")
            if lat is None or lon is None or not (6 < lat < 37.6 and 68 < lon < 97.5):
                skipped += 1
                continue
            ext = f"ocm:{p.get('ID')}"
            if ext in seen:
                continue
            seen.add(ext)
            op = clean((p.get("OperatorInfo") or {}).get("Title"), 120)
            if op and op.lower().startswith("(unknown"):
                op = None
            title = clean(a.get("Title"), 180) or (f"{op} charging station" if op else None)
            if not title:
                skipped += 1
                continue
            ph, line = norm_phone(a.get("ContactTelephone1") or
                                  (p.get("OperatorInfo") or {}).get("PhonePrimaryContact"))
            pin = clean(a.get("Postcode"))
            pin = pin if pin and re.fullmatch(r"\d{6}", pin) else None
            canon = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", title.lower())).strip()
            cp.write_row((ext, title, canon[:180], lat, lon, ph, line,
                          clean(a.get("RelatedURL"), 255),
                          clean(a.get("AddressLine1"), 200), clean(a.get("Town"), 120),
                          clean(a.get("StateOrProvince"), 120), pin, op,
                          clean((p.get("StatusType") or {}).get("Title"), 40),
                          p.get("NumberOfPoints"),
                          p.get("DateLastStatusUpdate") or p.get("DateLastVerified")))
            kept += 1
    conn.commit()
    print(f"staged {kept:,}   skipped {skipped:,} (no/out-of-range coordinates or no name)")

    cur.execute("""INSERT INTO businesses
        (external_id, display_name, canonical_name, category_id, subcategory,
         business_type, brand_name, lat, lon, geo, location_accuracy,
         public_phone, phone_line_type, website, address_line_1, city, state,
         pincode_code, pincode_method, pincode_confidence,
         source_id, source_record_id, source_url, source_updated_at,
         attribution_text, licence_name)
      SELECT s.external_id, s.display_name, s.canonical_name, %s, 'ev-charging',
             'SERVICE_PROVIDER', s.operator, s.lat, s.lon,
             ST_SetSRID(ST_MakePoint(s.lon,s.lat),4326)::geography, 'exact_storefront',
             s.phone, s.phone_line, s.website, s.address, s.town, s.state,
             CASE WHEN p.code IS NOT NULL THEN s.pincode END,
             CASE WHEN p.code IS NOT NULL THEN 'exact_source_pincode'::pincode_method END,
             CASE WHEN p.code IS NOT NULL THEN 0.97 END,
             %s, s.external_id,
             'https://openchargemap.org/site/poi/details/'||replace(s.external_id,'ocm:',''),
             s.updated, '© Open Charge Map contributors', 'CC-BY-4.0'
      FROM ocm_stage s
      LEFT JOIN pincodes p ON p.code = s.pincode
      WHERE NOT EXISTS (
        SELECT 1 FROM businesses b
        WHERE ST_DWithin(b.geo, ST_SetSRID(ST_MakePoint(s.lon,s.lat),4326)::geography, 100)
          AND similarity(b.canonical_name, s.canonical_name) > 0.5)
      ON CONFLICT (external_id) DO NOTHING""", (cat_id, src_id))
    print(f"inserted new stations: {cur.rowcount:,}")
    conn.commit()

    # anything without a stated postcode still needs a pincode
    cur.execute("""UPDATE businesses b SET pincode_code=c.code,
                     pincode_method='nearest_named_place',
                     pincode_confidence = CASE WHEN c.d<=3 THEN 0.80
                                               WHEN c.d<=8 THEN 0.70 ELSE 0.55 END
                   FROM (SELECT b2.id, p.code, ST_Distance(b2.geo,p.geo)/1000.0 d
                         FROM businesses b2 CROSS JOIN LATERAL
                           (SELECT code, geo FROM pincodes WHERE targetable
                            ORDER BY pincodes.geo <-> b2.geo LIMIT 1) p
                         WHERE b2.pincode_code IS NULL
                           AND b2.external_id LIKE 'ocm:%%') c
                   WHERE b.id = c.id""")
    conn.commit()

    cur.execute("""UPDATE businesses SET
        completeness_score = 45 + CASE WHEN public_phone IS NOT NULL THEN 20 ELSE 0 END
                                + CASE WHEN address_line_1 IS NOT NULL THEN 15 ELSE 0 END
                                + CASE WHEN website IS NOT NULL THEN 10 ELSE 0 END,
        confidence_score = LEAST(95, 55 + CASE WHEN public_phone IS NOT NULL THEN 15 ELSE 0 END
                                        + CASE WHEN address_line_1 IS NOT NULL THEN 10 ELSE 0 END),
        tier = CASE WHEN pincode_code IS NULL THEN 'HELD'::export_tier
                    WHEN public_phone IS NOT NULL THEN 'CONTACTABLE'::export_tier
                    ELSE 'LOCATABLE'::export_tier END
      WHERE external_id LIKE 'ocm:%'""")
    conn.commit()

    cur.execute("""SELECT count(*), count(public_phone), count(address_line_1),
                          count(pincode_code), count(DISTINCT state)
                   FROM businesses WHERE external_id LIKE 'ocm:%'""")
    n, ph, ad, pin, st = cur.fetchone()
    print(f"\nEV charging in engine: {n:,}")
    print(f"  with phone   {ph:,}\n  with address {ad:,}\n  with pincode {pin:,}\n  states       {st}")
    cur.execute("""SELECT brand_name, count(*) FROM businesses
                   WHERE external_id LIKE 'ocm:%' AND brand_name IS NOT NULL
                   GROUP BY 1 ORDER BY 2 DESC LIMIT 8""")
    print("\ntop networks:")
    for b, c in cur.fetchall():
        print(f"  {b[:38]:40s} {c:6,}")
    conn.close()


if __name__ == "__main__":
    main()
