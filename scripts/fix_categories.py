"""Repair category assignment. Two bugs, both mine.

BUG 1 — substring matching.
    'pub' matched inside 'public_school', so every record whose Overture
    alternates mentioned a public institution became a pub, and therefore a
    restaurant. 'spa' would have eaten 'spare_parts', 'pet_' eats 'carpet_'.
    Fixed with three match modes chosen by the shape of the keyword:
      multi-word ('gas_station') -> substring, specific enough to be safe
      long stem  ('pharmac')     -> prefix of a token, catches pharmacy/pharmacist
      short word ('pub','spa')   -> whole token only

BUG 2 — alternates outranking the primary.
    Overture gives a primary category and a list of alternates. I joined them
    into one string, so an alternate could win over a more specific primary.
    'Manappuram Finance' is primary=credit_union with alternates that include
    public_school; it became a school. Fixed by resolving the primary alone
    first and only consulting alternates when the primary yields nothing.

Re-maps existing rows rather than reloading: the source data has not changed,
only my reading of it.
"""
import os, re, sys
from pathlib import Path
import duckdb, psycopg

ROOT = Path(__file__).resolve().parents[1]
PARQUET = (ROOT / "var" / "overture" / "india_places.parquet").as_posix()
DSN = os.environ.get("LOCZ_DSN",
                     "host=127.0.0.1 port=5433 dbname=locz_engine user=postgres "
                     "password=LocZEngine_2026!")

_src = (ROOT / "scripts" / "load_overture.py").read_text(encoding="utf-8").split("def main(")[0]
_src = _src.replace('ROOT = Path(__file__).resolve().parents[1]', 'ROOT = Path(".")')
_ns = {}
exec(_src, _ns)
RULES, EXCLUDE = _ns["RULES"], _ns["EXCLUDE"]

# keywords that only appear as a plural or a compound in Overture's vocabulary
EXTRA = {"bookstore": "printing-and-stationery", "spas": "salons-and-spas",
         "gyms": "beauty-and-wellness", "banks": "professional-services"}


def matches(k, hay, toks):
    if "_" in k or " " in k:
        return k in hay
    if len(k) >= 4:
        return any(t.startswith(k) for t in toks)
    return k in toks                       # 'pub' must be a whole token


def resolve(text):
    if not text:
        return None
    hay = text.lower()
    toks = set(re.split(r"[^a-z0-9]+", hay)) - {""}
    for bad in EXCLUDE:
        if matches(bad, hay, toks):
            return "__EXCLUDED__"
    for k, slug in EXTRA.items():
        if k in toks:
            return slug
    for keys, slug in RULES:
        for k in keys:
            if matches(k, hay, toks):
                return slug
    return None


def map_category2(cat, alt):
    """Primary first. Alternates are a fallback, never an override."""
    primary = resolve(cat)
    if primary == "__EXCLUDED__":
        return None
    if primary:
        return primary
    for a in (alt or []):
        r = resolve(a)
        if r == "__EXCLUDED__":
            return None
        if r:
            return r
    return None


def main():
    conn = psycopg.connect(DSN)
    cur = conn.cursor()
    cur.execute("SELECT slug, id FROM categories")
    cat_id = dict(cur.fetchall())

    duck = duckdb.connect()
    rows = duck.execute(f"""SELECT category, category_alt, count(*) n
                            FROM read_parquet('{PARQUET}') GROUP BY 1,2""").fetchall()

    # build the corrected mapping per (category, alt) signature
    fixes, changed = [], 0
    for cat, alt, n in rows:
        new = map_category2(cat, alt)
        if new and new in cat_id:
            fixes.append((cat, list(alt or []), cat_id[new], new, n))
    print(f"distinct category signatures: {len(rows):,}")

    # apply by re-deriving each business's category from its Overture id
    print("building id -> category map ...", flush=True)
    cur.execute("DROP TABLE IF EXISTS cat_fix")
    cur.execute("CREATE TABLE cat_fix (external_id text PRIMARY KEY, category_id int)")

    res = duck.execute(f"SELECT id, category, category_alt FROM read_parquet('{PARQUET}')")
    written = 0
    with cur.copy("COPY cat_fix FROM STDIN") as cp:
        while True:
            batch = res.fetchmany(100_000)
            if not batch:
                break
            for oid, cat, alt in batch:
                new = map_category2(cat, alt)
                if new and new in cat_id:
                    cp.write_row((f"ovt:{oid}", cat_id[new]))
                    written += 1
    conn.commit()
    print(f"corrected mappings staged: {written:,}")

    cur.execute("""SELECT count(*) FROM businesses b JOIN cat_fix f USING (external_id)
                   WHERE b.category_id IS DISTINCT FROM f.category_id""")
    n_diff = cur.fetchone()[0]
    print(f"businesses whose category changes: {n_diff:,}")

    cur.execute("""SELECT co.slug, cn.slug, count(*)
                   FROM businesses b JOIN cat_fix f USING (external_id)
                   LEFT JOIN categories co ON co.id=b.category_id
                   LEFT JOIN categories cn ON cn.id=f.category_id
                   WHERE b.category_id IS DISTINCT FROM f.category_id
                   GROUP BY 1,2 ORDER BY 3 DESC LIMIT 15""")
    print(f"\n{'WAS':30s} {'BECOMES':30s} count")
    for a, b, n in cur.fetchall():
        print(f"  {str(a)[:28]:30s} {str(b)[:28]:30s} {n:8,}")

    cur.execute("""UPDATE businesses b SET category_id = f.category_id, updated_at = now()
                   FROM cat_fix f WHERE b.external_id = f.external_id
                     AND b.category_id IS DISTINCT FROM f.category_id""")
    print(f"\nupdated: {cur.rowcount:,}")
    conn.commit()

    cur.execute("""SELECT c.slug, count(*) FROM businesses b JOIN categories c ON c.id=b.category_id
                   GROUP BY 1 ORDER BY 2 DESC LIMIT 12""")
    print("\ntop categories after repair:")
    for s, n in cur.fetchall():
        print(f"  {s:34s} {n:9,}")

    for probe in ("Manappuram", "Tent House", "vidhya"):
        cur.execute("""SELECT b.display_name, c.slug FROM businesses b
                       JOIN categories c ON c.id=b.category_id
                       WHERE b.display_name ILIKE %s LIMIT 2""", (f"%{probe}%",))
        for nm, sl in cur.fetchall():
            print(f"  check: {nm[:38]:40s} -> {sl}")
    conn.close()


if __name__ == "__main__":
    main()
