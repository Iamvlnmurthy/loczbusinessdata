"""Load extracted places into PostGIS, then correct pincode centroids from them.

Correction: match each pincode's post-office names against OSM places inside the
same district (district is reliable in GeoNames even where coordinates are not),
and adopt the matched place's location.
"""
import json, os, sys, unicodedata, re, collections
from pathlib import Path
import psycopg

ROOT = Path(__file__).resolve().parents[1]
PLACES = ROOT / "var" / "extract" / "places.jsonl"
DSN = os.environ.get("LOCZ_DSN",
                     "host=127.0.0.1 port=5433 dbname=locz_engine user=postgres "
                     "password=LocZEngine_2026!")


def canon(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).lower()
    s = re.sub(r"\b(h\.?o\.?|s\.?o\.?|b\.?o\.?|bo|so|ho)\b", " ", s)   # post-office suffixes
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def main():
    conn = psycopg.connect(DSN, autocommit=False)
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM named_places")
    if cur.fetchone()[0] == 0:
        n = 0
        with cur.copy("""COPY named_places
            (osm_type,osm_id,name,name_en,name_hi,name_te,place_kind,population,
             postcode,lat,lon,geo) FROM STDIN""") as cp:
            for line in open(PLACES, encoding="utf-8"):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                pc = (r.get("postcode") or "").strip()
                pc = pc if re.fullmatch(r"\d{6}", pc) else None
                pop = r.get("population")
                try:
                    pop = int(re.sub(r"\D", "", pop)) if pop else None
                except Exception:
                    pop = None
                cp.write_row((r["osm_type"], r["osm_id"], r["name"], r.get("name_en"),
                              r.get("name_hi"), r.get("name_te"), r.get("place") or "unknown",
                              pop, pc, r["lat"], r["lon"],
                              f'SRID=4326;POINT({r["lon"]} {r["lat"]})'))
                n += 1
        conn.commit()
        print(f"named_places loaded : {n:,}")
    else:
        print("named_places already loaded — skipping")

    cur.execute("SELECT count(*) FROM named_places")
    print(f"named_places total  : {cur.fetchone()[0]:,}")

    # ---- 1. direct hit: a place that states the pincode itself (strongest evidence)
    cur.execute("""
        UPDATE pincodes p SET
          lat = s.lat, lon = s.lon,
          geo = ST_SetSRID(ST_MakePoint(s.lon, s.lat),4326)::geography,
          centroid_src = 'osm_postal_boundary',
          centroid_offset_km = round((ST_Distance(
              ST_MakePoint(p.geonames_lon,p.geonames_lat)::geography,
              ST_MakePoint(s.lon,s.lat)::geography)/1000.0)::numeric,2),
          targetable = true, updated_at = now()
        FROM (SELECT DISTINCT ON (postcode) postcode, lat, lon
              FROM named_places WHERE postcode IS NOT NULL
              ORDER BY postcode, COALESCE(population,0) DESC) s
        WHERE p.code = s.postcode""")
    print(f"corrected by stated postcode : {cur.rowcount:,}")
    conn.commit()

    # ---- 2. name match inside the district
    cur.execute("""SELECT code, name, district_name, state_name FROM pincodes
                   WHERE centroid_src = 'geonames'""")
    todo = cur.fetchall()
    cur.execute("""SELECT code, array_agg(office_name) FROM pincode_offices GROUP BY code""")
    off_names = dict(cur.fetchall())

    # index places by canonical name once
    cur.execute("SELECT name, lat, lon, COALESCE(population,0), place_kind FROM named_places")
    by_name = collections.defaultdict(list)
    for name, lat, lon, pop, kind in cur.fetchall():
        by_name[canon(name)].append((lat, lon, pop, kind))
    print(f"place-name index    : {len(by_name):,} distinct names")

    KIND_RANK = {"city": 6, "town": 5, "municipality": 5, "suburb": 4,
                 "village": 3, "neighbourhood": 2, "quarter": 2, "hamlet": 1}
    updates = []
    for code, pname, district, state in todo:
        cands = []
        for nm in {canon(pname), *(canon(o) for o in off_names.get(code, []))}:
            if len(nm) < 4:
                continue
            for lat, lon, pop, kind in by_name.get(nm, []):
                cands.append((KIND_RANK.get(kind, 0), pop, lat, lon))
        if not cands:
            continue
        cands.sort(reverse=True)
        _, _, lat, lon = cands[0]
        updates.append((lat, lon, code))

    with cur.copy("COPY _fix (lat,lon,code) FROM STDIN") if False else conn.cursor() as c2:
        pass
    cur.execute("CREATE TEMP TABLE _fix (lat double precision, lon double precision, code varchar(6))")
    with cur.copy("COPY _fix (lat,lon,code) FROM STDIN") as cp:
        for row in updates:
            cp.write_row(row)
    cur.execute("""
        UPDATE pincodes p SET
          lat = f.lat, lon = f.lon,
          geo = ST_SetSRID(ST_MakePoint(f.lon,f.lat),4326)::geography,
          centroid_src = 'osm_place_match',
          centroid_offset_km = round((ST_Distance(
              ST_MakePoint(p.geonames_lon,p.geonames_lat)::geography,
              ST_MakePoint(f.lon,f.lat)::geography)/1000.0)::numeric,2),
          targetable = true, updated_at = now()
        FROM _fix f WHERE p.code = f.code""")
    print(f"corrected by name match      : {cur.rowcount:,}")
    conn.commit()

    # ---- 3. anything still on a shared GeoNames coordinate cannot be targeted
    cur.execute("""UPDATE pincodes SET centroid_src='unverified', targetable=false
                   WHERE centroid_src='geonames' AND shares_coordinate""")
    print(f"left unverified (shared coord): {cur.rowcount:,}")
    conn.commit()

    for q, label in [
        ("SELECT centroid_src, count(*) FROM pincodes GROUP BY 1 ORDER BY 2 DESC", "centroid source"),
        ("""SELECT count(*) FILTER (WHERE centroid_offset_km >= 15),
                   count(*) FILTER (WHERE centroid_offset_km BETWEEN 5 AND 15),
                   count(*) FILTER (WHERE centroid_offset_km < 5),
                   round(avg(centroid_offset_km),2), round(max(centroid_offset_km),2)
            FROM pincodes WHERE centroid_offset_km IS NOT NULL""", "offsets"),
        ("SELECT count(*) FROM pincodes WHERE targetable", "targetable"),
    ]:
        cur.execute(q)
        print(f"\n{label}:")
        for row in cur.fetchall():
            print("   ", row)
    conn.close()


if __name__ == "__main__":
    main()
