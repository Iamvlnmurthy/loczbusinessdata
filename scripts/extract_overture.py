"""Pull Overture Maps places for India into a local parquet file.

Licence: CDLA-Permissive-2.0 — redistribution permitted, attribution required.
Per-record source licences are preserved in the `sources` column and must be
carried through to export.

Bbox predicate is pushed down to the remote parquet, so only Indian row-groups
are fetched rather than the whole global dataset.
"""
import time
from pathlib import Path
import duckdb

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var" / "overture"
OUT.mkdir(parents=True, exist_ok=True)
RELEASE = "2026-07-22.0"
SRC = f"s3://overturemaps-us-west-2/release/{RELEASE}/theme=places/type=place/*"

# India, generous bounds (includes A&N, Lakshadweep)
XMIN, XMAX, YMIN, YMAX = 68.0, 97.5, 6.0, 37.6

con = duckdb.connect(str(OUT / "overture.duckdb"))
con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
con.execute("SET s3_region='us-west-2';")
con.execute("SET enable_progress_bar=false;")
con.execute("SET memory_limit='6GB'; SET threads=8;")

t0 = time.time()
print(f"release {RELEASE} — extracting India bbox …", flush=True)

con.execute(f"""
COPY (
  SELECT
    id,
    names.primary                           AS name,
    categories.primary                      AS category,
    categories.alternate                    AS category_alt,
    confidence,
    ST_X(ST_Centroid(geometry))             AS lon,
    ST_Y(ST_Centroid(geometry))             AS lat,
    phones,
    websites,
    emails,
    socials,
    brand.names.primary                     AS brand,
    addresses[1].freeform                   AS addr_freeform,
    addresses[1].locality                   AS addr_locality,
    addresses[1].postcode                   AS addr_postcode,
    addresses[1].region                     AS addr_region,
    operating_status,
    sources
  FROM read_parquet('{SRC}', hive_partitioning=1)
  WHERE bbox.xmin BETWEEN {XMIN} AND {XMAX}
    AND bbox.ymin BETWEEN {YMIN} AND {YMAX}
    AND addresses[1].country = 'IN'
    AND names.primary IS NOT NULL
) TO '{(OUT / "india_places.parquet").as_posix()}'
  (FORMAT PARQUET, COMPRESSION ZSTD)
""")

el = time.time() - t0
f = OUT / "india_places.parquet"
n = con.execute(f"SELECT count(*) FROM read_parquet('{f.as_posix()}')").fetchone()[0]
print(f"\nrows            : {n:,}")
print(f"file            : {f.stat().st_size/1e6:.1f} MB")
print(f"elapsed         : {el/60:.1f} min")

print("\ncoverage of the fields OSM was weakest at:")
for label, expr in [
    ("phone",    "len(phones) > 0"),
    ("website",  "len(websites) > 0"),
    ("email",    "len(emails) > 0"),
    ("postcode", "addr_postcode IS NOT NULL"),
    ("address",  "addr_freeform IS NOT NULL"),
    ("brand",    "brand IS NOT NULL"),
]:
    c = con.execute(f"SELECT count(*) FROM read_parquet('{f.as_posix()}') WHERE {expr}").fetchone()[0]
    print(f"  {label:9s} {c:9,}  {c/n*100:5.1f}%")

print("\ntop categories:")
for cat, c in con.execute(f"""SELECT category, count(*) c FROM read_parquet('{f.as_posix()}')
                              GROUP BY 1 ORDER BY c DESC LIMIT 15""").fetchall():
    print(f"  {str(cat):34s} {c:8,}")
con.close()
