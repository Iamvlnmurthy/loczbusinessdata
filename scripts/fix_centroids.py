"""Pincode centroid correction, bounded so a name match cannot teleport a pincode.

The naive version matched place names nationwide and moved pincodes up to 2,659 km.
Village names repeat across India, so a name match alone is not evidence.

Two anchors constrain every candidate:
  * the district anchor - the median position of OSM places whose name matches the
    district, cross-checked against the district's own pincodes;
  * a hard radius - a correction beyond MAX_MOVE_KM is rejected outright.

Corrections outside the bound are recorded as 'unverified' rather than applied,
so nothing is silently moved to the wrong side of the country.
"""
import os
from pathlib import Path
import re, unicodedata, collections, statistics
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
MAX_MOVE_KM = 60.0          # observed GeoNames error tops out near 100 km; 60 is safe
DISTRICT_RADIUS_KM = 120.0  # a candidate must sit inside its own district's neighbourhood

KIND_RANK = {"city": 6, "town": 5, "municipality": 5, "suburb": 4,
             "village": 3, "neighbourhood": 2, "quarter": 2, "hamlet": 1, "locality": 1}


def canon(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).lower()
    s = re.sub(r"\b(h\.?o\.?|s\.?o\.?|b\.?o\.?)\b", " ", s)
    s = re.sub(r"\((.*?)\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def km(a_lat, a_lon, b_lat, b_lon):
    import math
    R = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = math.radians(b_lat - a_lat), math.radians(b_lon - a_lon)
    h = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(h))


def main():
    conn = psycopg.connect(DSN, autocommit=False)
    cur = conn.cursor()

    cur.execute("SELECT name, lat, lon, COALESCE(population,0), place_kind FROM named_places")
    by_name = collections.defaultdict(list)
    for name, lat, lon, pop, kind in cur.fetchall():
        by_name[canon(name)].append((lat, lon, pop, kind))
    print(f"place index          : {len(by_name):,} distinct names")

    # ---- district anchors, from the pincodes already trusted in that district
    cur.execute("""SELECT district_name, state_name, lat, lon, centroid_src
                   FROM pincodes""")
    rows = cur.fetchall()
    dist_pts = collections.defaultdict(list)
    for d, s, lat, lon, src in rows:
        if src == "osm_postal_boundary":            # verified by a stated postcode
            dist_pts[(d, s)].append((lat, lon))
    anchors = {}
    for key, pts in dist_pts.items():
        if len(pts) >= 2:
            anchors[key] = (statistics.median(p[0] for p in pts),
                            statistics.median(p[1] for p in pts))
    print(f"district anchors     : {len(anchors):,} (from postcode-verified pincodes)")

    cur.execute("""SELECT code, name, district_name, state_name, geonames_lat, geonames_lon
                   FROM pincodes WHERE centroid_src = 'geonames'""")
    todo = cur.fetchall()
    cur.execute("SELECT code, array_agg(office_name) FROM pincode_offices GROUP BY code")
    offices = dict(cur.fetchall())

    applied, rejected, no_match = [], 0, 0
    for code, pname, district, state, glat, glon in todo:
        anchor = anchors.get((district, state))
        names = {canon(pname)} | {canon(o) for o in offices.get(code, [])}
        best = None
        for nm in names:
            if len(nm) < 4:
                continue
            for lat, lon, pop, kind in by_name.get(nm, []):
                d_geo = km(glat, glon, lat, lon)
                if d_geo > MAX_MOVE_KM:
                    continue                                  # too far to be the same place
                if anchor and km(anchor[0], anchor[1], lat, lon) > DISTRICT_RADIUS_KM:
                    continue                                  # outside its own district
                score = (KIND_RANK.get(kind, 0), pop, -d_geo)
                if best is None or score > best[0]:
                    best = (score, lat, lon, d_geo)
        if best is None:
            no_match += 1
            continue
        _, lat, lon, d = best
        applied.append((lat, lon, round(d, 2), code))

    print(f"candidates applied   : {len(applied):,}")
    print(f"no in-bounds match   : {no_match:,}")

    cur.execute("CREATE TEMP TABLE _fix (lat double precision, lon double precision,"
                " off_km numeric, code varchar(6))")
    with cur.copy("COPY _fix (lat,lon,off_km,code) FROM STDIN") as cp:
        for r in applied:
            cp.write_row(r)
    cur.execute("""
        UPDATE pincodes p SET lat=f.lat, lon=f.lon,
          geo = ST_SetSRID(ST_MakePoint(f.lon,f.lat),4326)::geography,
          centroid_src='osm_place_match', centroid_offset_km=f.off_km,
          targetable=true, updated_at=now()
        FROM _fix f WHERE p.code=f.code""")
    print(f"rows updated         : {cur.rowcount:,}")

    # everything still unmatched AND sharing a coordinate is not safe to target
    cur.execute("""UPDATE pincodes SET centroid_src='unverified', targetable=false
                   WHERE centroid_src='geonames' AND shares_coordinate""")
    print(f"marked unverified    : {cur.rowcount:,}")
    conn.commit()

    print("\n--- result ---")
    cur.execute("SELECT centroid_src, count(*) FROM pincodes GROUP BY 1 ORDER BY 2 DESC")
    for src, n in cur.fetchall():
        print(f"  {src:22s} {n:6,}")
    cur.execute("""SELECT round(avg(centroid_offset_km),2), round(max(centroid_offset_km),2),
                          count(*) FILTER (WHERE centroid_offset_km >= 15),
                          count(*) FILTER (WHERE centroid_offset_km < 5)
                   FROM pincodes WHERE centroid_offset_km IS NOT NULL""")
    avg, mx, big, small = cur.fetchone()
    print(f"\n  offset avg {avg} km | max {mx} km | >=15km {big:,} | <5km {small:,}")
    cur.execute("SELECT count(*) FROM pincodes WHERE targetable")
    print(f"  targetable            {cur.fetchone()[0]:,}")
    cur.execute("""SELECT code,name,district_name,centroid_offset_km,centroid_src
                   FROM pincodes WHERE code IN ('506001','500081','560001','700001')""")
    print("\n  spot checks:")
    for r in cur.fetchall():
        print("   ", r)
    conn.close()


if __name__ == "__main__":
    main()
