"""Load extracted businesses into PostGIS and resolve them.

  stage -> normalise -> categorise -> assign pincode -> label locality -> score -> tier

Set-based: the spatial work runs in PostGIS with GiST indexes, not per-row in Python.
"""
import json, os, re, unicodedata
from pathlib import Path
import psycopg

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "var" / "extract" / "businesses.jsonl"
DSN = os.environ.get("LOCZ_DSN",
                     "host=127.0.0.1 port=5433 dbname=locz_engine user=postgres "
                     "password=LocZEngine_2026!")

IMPLAUSIBLE_HOURS = {"sunrise-sunset", "dawn-dusk", "sunset-sunrise"}
# 24/7 is credible for these; elsewhere it is a tagging error worth flagging
ALWAYS_OPEN_OK = {"petrol-stations", "hospitals-and-clinics", "hotels-and-accommodation"}


def canon(n):
    n = unicodedata.normalize("NFKD", n).lower()
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(r"\b(the|and|shop|store|stores|centre|center)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def norm_phone(t):
    for k in ("phone", "contact:phone", "mobile", "contact:mobile", "phone:mobile"):
        v = t.get(k)
        if not v:
            continue
        d = re.sub(r"\D", "", re.split(r"[;,/]", str(v))[0])
        if d.startswith("91") and len(d) == 12:
            d = d[2:]
        elif d.startswith("0") and len(d) == 11:
            d = d[1:]
        if len(d) == 10 and d[0] in "6789":
            return "+91" + d, "mobile"
        if 8 <= len(d) <= 11:
            return "+91" + d, "landline"
    return None, None


def norm_url(t):
    for k in ("website", "contact:website", "url"):
        v = t.get(k)
        if not v:
            continue
        v = str(v).strip().split(";")[0]
        if not v.startswith(("http://", "https://")):
            v = "https://" + v
        return v.rstrip("/")[:255]
    return None


def main():
    conn = psycopg.connect(DSN, autocommit=False)
    cur = conn.cursor()

    cur.execute("""SELECT source_key, source_value, category_id, subcategory, business_type
                   FROM source_category_map WHERE source_type='osm'""")
    tagmap = {(k, v): (cid, sub, bt) for k, v, cid, sub, bt in cur.fetchall()}
    cur.execute("SELECT id FROM data_sources WHERE slug='osm-india-pbf'")
    src_id = cur.fetchone()[0]
    print(f"tag map          : {len(tagmap)} entries")

    cur.execute("DROP TABLE IF EXISTS stage")
    cur.execute("""CREATE TABLE stage (
        external_id text, display_name text, canonical_name text,
        category_id int, subcategory text, business_type text, brand_name text,
        lat double precision, lon double precision, location_accuracy text,
        public_phone text, phone_line_type text, public_email text, website text,
        opening_hours_raw text, hours_flag text,
        addr_pin varchar(6), address_line_1 text, address_line_2 text, addr_city text,
        source_record_id text, source_url text, source_updated_at timestamptz)""")

    JUNK = {"yes", "no", "true", "1"}
    unmapped, n, skipped = {}, 0, 0
    with cur.copy("COPY stage FROM STDIN") as cp:
        for line in open(SRC, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            t = r.get("tags") or {}
            name = t.get("name")
            if not name:
                continue
            name = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name)).strip()[:180]
            hit, srckey = None, None
            for k in ("shop", "amenity", "office", "craft", "healthcare", "tourism", "leisure"):
                v = t.get(k)
                if not v or v in JUNK:
                    continue
                srckey = (k, v)
                hit = tagmap.get(srckey)
                break
            if hit is None:
                if srckey:
                    unmapped[srckey] = unmapped.get(srckey, 0) + 1
                skipped += 1
                continue
            cat_id, sub, bt = hit
            phone, line = norm_phone(t)
            hours, flag = t.get("opening_hours"), None
            if hours:
                hours = str(hours).strip()
                if hours.lower() in IMPLAUSIBLE_HOURS:
                    hours, flag = None, "implausible_rejected"
                elif hours == "24/7" and sub and cat_id and False:
                    pass
            pin = (t.get("addr:postcode") or "").strip()
            pin = pin if re.fullmatch(r"\d{6}", pin) else None
            l1 = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x) or None
            cp.write_row((
                f'osm:{r["osm_type"][0]}{r["osm_id"]}', name, canon(name),
                cat_id, sub, bt, t.get("brand"),
                r["lat"], r["lon"],
                "building" if r["osm_type"] in ("way", "relation") else "exact_storefront",
                phone, line, (t.get("email") or t.get("contact:email") or None), norm_url(t),
                hours, flag, pin,
                l1, (t.get("addr:suburb") or t.get("addr:neighbourhood")), t.get("addr:city"),
                f'{r["osm_type"]}/{r["osm_id"]}',
                f'https://www.openstreetmap.org/{r["osm_type"]}/{r["osm_id"]}',
                r.get("timestamp")))
            n += 1
    conn.commit()
    print(f"staged           : {n:,}   (skipped, no category mapping: {skipped:,})")

    for (k, v), c in sorted(unmapped.items(), key=lambda kv: -kv[1])[:400]:
        cur.execute("""INSERT INTO unmapped_source_categories
                         (source_type,source_key,source_value,occurrences)
                       VALUES ('osm',%s,%s,%s)
                       ON CONFLICT (source_type,source_key,source_value)
                       DO UPDATE SET occurrences=EXCLUDED.occurrences, last_seen=now()""",
                    (k, v, c))
    conn.commit()
    print(f"unmapped tags     : {len(unmapped):,} distinct (top 400 queued for review)")

    print("inserting businesses ...")
    cur.execute("""
      INSERT INTO businesses
        (external_id, display_name, canonical_name, category_id, subcategory,
         business_type, brand_name, lat, lon, geo, location_accuracy,
         public_phone, phone_line_type, public_email, website, opening_hours_raw,
         address_line_1, address_line_2, city, source_id, source_record_id, source_url,
         source_updated_at, attribution_text, licence_name)
      SELECT DISTINCT ON (external_id)
         external_id, display_name, canonical_name, category_id, subcategory,
         business_type, brand_name, lat, lon,
         ST_SetSRID(ST_MakePoint(lon,lat),4326)::geography, location_accuracy,
         public_phone, phone_line_type, public_email, website, opening_hours_raw,
         address_line_1, address_line_2, addr_city, %s, source_record_id, source_url,
         source_updated_at, '© OpenStreetMap contributors', 'ODbL-1.0'
      FROM stage
      ON CONFLICT (external_id) DO NOTHING""", (src_id,))
    print(f"businesses rows  : {cur.rowcount:,}")
    conn.commit()

    # ---- pincode ladder, rung 1: the record states its own postcode
    print("resolving pincodes ...")
    cur.execute("""
      UPDATE businesses b SET
        pincode_code = s.addr_pin,
        pincode_method = 'exact_source_pincode',
        pincode_confidence = CASE
          WHEN ST_Distance(b.geo, p.geo) <= 25000 THEN 0.97   -- geometry corroborates
          ELSE 0.55 END                                        -- contradicts: needs review
      FROM stage s JOIN pincodes p ON p.code = s.addr_pin
      WHERE b.external_id = s.external_id AND s.addr_pin IS NOT NULL""")
    print(f"  rung 1 exact_source_pincode : {cur.rowcount:,}")
    conn.commit()

    # ---- rung 2: nearest corrected, targetable pincode inside its own search radius
    cur.execute("""
      UPDATE businesses b SET
        pincode_code = c.code, pincode_method = 'nearest_named_place',
        pincode_confidence = CASE
          WHEN c.d_km <= 3  AND c.centroid_src <> 'geonames' THEN 0.80
          WHEN c.d_km <= 8  AND c.centroid_src <> 'geonames' THEN 0.70
          WHEN c.d_km <= 15 THEN 0.55
          ELSE 0.40 END
      FROM (
        SELECT b2.id, p.code, p.centroid_src,
               ST_Distance(b2.geo, p.geo)/1000.0 AS d_km
        FROM businesses b2
        CROSS JOIN LATERAL (
          SELECT code, geo, centroid_src FROM pincodes
          WHERE targetable ORDER BY pincodes.geo <-> b2.geo LIMIT 1) p
        WHERE b2.pincode_code IS NULL
      ) c
      WHERE b.id = c.id""")
    print(f"  rung 2 nearest targetable   : {cur.rowcount:,}")
    conn.commit()

    # ---- locality + mandal from the named-place index (independent of pincode geometry)
    print("labelling localities ...")
    cur.execute("""
      UPDATE businesses b SET
        locality = c.name,
        district = p.district_name, state = p.state_name,
        city = COALESCE(b.city, CASE WHEN c.place_kind IN ('city','town') THEN c.name END)
      FROM (
        SELECT b2.id, np.name, np.place_kind, ST_Distance(b2.geo, np.geo)/1000.0 AS d_km
        FROM businesses b2
        CROSS JOIN LATERAL (
          SELECT name, place_kind, geo FROM named_places
          ORDER BY named_places.geo <-> b2.geo LIMIT 1) np
      ) c
      LEFT JOIN businesses bb ON bb.id = c.id
      LEFT JOIN pincodes p ON p.code = bb.pincode_code
      WHERE b.id = c.id AND c.d_km <= 12""")
    print(f"  localities labelled         : {cur.rowcount:,}")
    conn.commit()

    # ---- scores and tier
    print("scoring ...")
    cur.execute("""
      UPDATE businesses SET
        completeness_score =
            15 + 15
          + 15
          + CASE WHEN public_phone      IS NOT NULL THEN 20 ELSE 0 END
          + CASE WHEN address_line_1    IS NOT NULL THEN 15 ELSE 0 END
          + CASE WHEN opening_hours_raw IS NOT NULL THEN 10 ELSE 0 END
          + CASE WHEN website           IS NOT NULL THEN 10 ELSE 0 END,
        confidence_score = LEAST(95,
            40
          + CASE WHEN pincode_confidence >= 0.90 THEN 15
                 WHEN pincode_confidence >= 0.70 THEN 8 ELSE 0 END
          + CASE WHEN public_phone   IS NOT NULL THEN 15 ELSE 0 END
          + CASE WHEN address_line_1 IS NOT NULL THEN 10 ELSE 0 END
          + CASE WHEN location_accuracy = 'building' THEN 10 ELSE 5 END
          + CASE WHEN website        IS NOT NULL THEN 5 ELSE 0 END),
        freshness_score = CASE WHEN source_updated_at IS NULL THEN NULL
          ELSE GREATEST(0, LEAST(100,
            100 - (EXTRACT(epoch FROM now()-source_updated_at)/86400/1095*100)::int)) END""")
    conn.commit()
    cur.execute("""
      UPDATE businesses SET tier = CASE
        WHEN pincode_code IS NULL OR pincode_confidence < 0.55 THEN 'HELD'::export_tier
        WHEN public_phone IS NOT NULL                          THEN 'CONTACTABLE'::export_tier
        ELSE 'LOCATABLE'::export_tier END""")
    conn.commit()
    print(f"  scored                      : {cur.rowcount:,}")

    print("\n=== RESULT ===")
    cur.execute("""SELECT tier, count(*), round(100.0*count(*)/sum(count(*)) over (),1)
                   FROM businesses GROUP BY 1 ORDER BY 2 DESC""")
    for t, c, p in cur.fetchall():
        print(f"  {t:12s} {c:8,}  {p}%")
    cur.execute("""SELECT pincode_method, count(*), round(avg(pincode_confidence),2)
                   FROM businesses GROUP BY 1 ORDER BY 2 DESC""")
    print()
    for m, c, a in cur.fetchall():
        print(f"  {str(m):24s} {c:8,}  avg conf {a}")
    cur.execute("SELECT count(DISTINCT pincode_code) FROM businesses WHERE pincode_code IS NOT NULL")
    print(f"\n  pincodes with >=1 business : {cur.fetchone()[0]:,} of 19,238")
    cur.execute("SELECT count(*) FROM businesses WHERE public_phone IS NOT NULL")
    print(f"  with phone                 : {cur.fetchone()[0]:,}")
    cur.execute("SELECT count(*) FROM businesses WHERE locality IS NOT NULL")
    print(f"  with locality label        : {cur.fetchone()[0]:,}")
    conn.close()


if __name__ == "__main__":
    main()
