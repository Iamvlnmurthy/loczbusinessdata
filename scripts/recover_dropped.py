"""Recover Overture records my own filters rejected.

Two populations, handled differently:

  A. ~204k with a category that no rule matched. The round-2 rules in
     load_overture.py now cover most of them. Pure gain: the data is already on
     disk, nothing is re-fetched.

  B. ~139k with NO category at all. These are NOT auto-imported. A business with
     no category cannot be filed under a taxonomy, and guessing one from the name
     is the kind of inference the brief forbids. Instead they are classified by
     name keyword into a review queue with the evidence recorded, so a human
     approves or rejects rather than the pipeline deciding silently.
"""
import json, os, re, unicodedata
from pathlib import Path
import duckdb, psycopg

ROOT = Path(__file__).resolve().parents[1]
PARQUET = (ROOT / "var" / "overture" / "india_places.parquet").as_posix()
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

# reuse the loader's rules so there is one definition, not two
_src = (ROOT / "scripts" / "load_overture.py").read_text(encoding="utf-8").split("def main(")[0]
_src = _src.replace('ROOT = Path(__file__).resolve().parents[1]', 'ROOT = Path(".")')
_ns = {}
exec(_src, _ns)
map_category, norm_phone, clean, canon = (_ns["map_category"], _ns["norm_phone"],
                                          _ns["clean"], _ns["canon"])

# Name-keyword evidence for population B. Deliberately narrow: a word that names
# the trade, not a word that merely co-occurs with it.
NAME_HINTS = [
    (r"\b(medical|medicals|pharmac\w*|chemist|drug ?house|druggist)\b", "medical-stores-and-pharmacies"),
    (r"\b(hospital|clinic|nursing home|diagnostic|scan cent|polyclinic|dental)\b", "hospitals-and-clinics"),
    (r"\b(bakery|bakers|cake ?shop|sweets?|mithai|confection\w*)\b", "bakeries-and-sweets"),
    (r"\b(restaurant|hotel ?&? ?restaurant|dhaba|biryani|tiffin|mess|cafe|caf[eé]|food ?court|canteen)\b", "restaurants-and-food"),
    (r"\b(kirana|general ?stores?|provision|super ?market|supermarket|grocery|grocers)\b", "grocery-and-kirana"),
    (r"\b(hardware|sanitary|plywood|building ?material|cement|paints?)\b", "hardware-stores"),
    (r"\b(electric\w*|electrical\w*)\b", "electrical-stores"),
    (r"\b(electronics|home appliance|appliances)\b", "electronics-stores"),
    (r"\b(mobiles?|mobile ?shop|cell ?point|smart ?phone)\b", "mobile-stores"),
    (r"\b(computers?|laptops?|systems? ?point)\b", "computer-and-laptop-stores"),
    (r"\b(furniture|furnitures|sofa|mattress)\b", "furniture-stores"),
    (r"\b(cloth\w*|garments?|textiles?|sarees?|boutique|fashions?|readymade)\b", "clothing-stores"),
    (r"\b(footwear|shoes?|chappal)\b", "footwear-stores"),
    (r"\b(jewell?ers?|jewell?ery|gold ?smith|silvers?)\b", "jewellery-stores"),
    (r"\b(salon|saloon|beauty ?parlou?r|hair ?(cut|dress)\w*|spa)\b", "salons-and-spas"),
    (r"\b(gym|fitness|yoga)\b", "beauty-and-wellness"),
    (r"\b(petrol ?(pump|bunk)|filling ?station|fuel ?station|hp ?petrol|bharat ?petroleum)\b", "petrol-stations"),
    (r"\b(tyres?|tyre ?works|battery|batteries)\b", "tyre-and-battery-stores"),
    (r"\b(auto ?(mobiles?|works)|motors?|garage|servicing ?cent|denting|painting ?works)\b", "car-repair"),
    (r"\b(school|vidyalaya|vidhyalaya|pathshala|convent)\b", "schools"),
    (r"\b(college|university|institute of|polytechnic|junior ?college)\b", "colleges"),
    (r"\b(coaching|tuitions?|academy|classes|study ?cent)\b", "tuition-and-coaching"),
    (r"\b(tailors?|tailoring|stitching|darzi)\b", "tailoring-and-boutiques"),
    (r"\b(xerox|photo ?copy|printers?|printing|stationery|book ?(shop|stall|store))\b", "printing-and-stationery"),
    (r"\b(courier|cargo|packers|movers|logistics|parcel)\b", "courier-and-parcel-services"),
    (r"\b(travels?|tours ?(and|&) ?travels?|tour ?operator)\b", "travel-services"),
    (r"\b(real ?estate|properties|realtors?|builders?|constructions?|infra\w*)\b", "property-services"),
    (r"\b(agencies|enterprises?|traders?|trading|distributors?|suppliers?|marketing)\b", "wholesale-businesses"),
    (r"\b(industries|engineering ?works|fabrications?|manufacturers?|mills?)\b", "local-manufacturers"),
    (r"\b(pet ?(shop|store|clinic)|aquarium|veterinary)\b", "pet-stores-and-pet-services"),
    (r"\b(nursery|seeds?|fertili[sz]ers?|agro|agri\w*)\b", "agricultural-supplies"),
    (r"\b(studio|photograph\w*|digital ?studio)\b", "event-services"),
    (r"\b(consultan\w*|associates|advocates?|chartered ?account\w*|law ?firm)\b", "professional-services"),
]
COMPILED = [(re.compile(p, re.I), s) for p, s in NAME_HINTS]


def hint(name):
    for rx, slug in COMPILED:
        if rx.search(name):
            return slug, rx.pattern[:44]
    return None, None


def main():
    conn = psycopg.connect(DSN)
    cur = conn.cursor()
    cur.execute("SELECT slug,id FROM categories")
    cat_id = dict(cur.fetchall())
    cur.execute("SELECT id FROM data_sources WHERE slug='overture-places'")
    src_id = cur.fetchone()[0]

    cur.execute("""CREATE TABLE IF NOT EXISTS review_queue (
        id            bigserial PRIMARY KEY,
        external_id   text UNIQUE NOT NULL,
        reason        text NOT NULL,
        display_name  text NOT NULL,
        suggested_category_id integer REFERENCES categories(id),
        evidence      text,
        payload       jsonb NOT NULL,
        status        text NOT NULL DEFAULT 'pending',
        decided_by    text,
        decided_at    timestamptz,
        created_at    timestamptz NOT NULL DEFAULT now())""")
    conn.commit()

    cur.execute("SELECT external_id FROM businesses WHERE external_id LIKE 'ovt:%'")
    have = {r[0] for r in cur.fetchall()}
    print(f"already loaded: {len(have):,}")

    duck = duckdb.connect()
    res = duck.execute(f"""SELECT id, name, category, category_alt, lat, lon, phones,
                                  websites, emails, brand, addr_freeform, addr_locality,
                                  addr_postcode
                           FROM read_parquet('{PARQUET}')""")

    recovered, queued, skipped = 0, 0, 0
    cur.execute("DROP TABLE IF EXISTS rec_stage")
    cur.execute("""CREATE TABLE rec_stage (external_id text PRIMARY KEY, display_name text,
        canonical_name text, category_id int, business_type text, brand_name text,
        lat double precision, lon double precision, public_phone text,
        phone_line_type text, public_email text, website text, address_line_1 text,
        addr_pin varchar(6))""")

    queue_rows = []
    with cur.copy("COPY rec_stage FROM STDIN") as cp:
        while True:
            rows = res.fetchmany(50_000)
            if not rows:
                break
            for (oid, name, cat, alt, lat, lon, phones, sites, mails, brand,
                 freeform, locality, pcode) in rows:
                ext = f"ovt:{oid}"
                if ext in have or lat is None or lon is None:
                    continue
                nm = clean(name, 180)
                if not nm:
                    continue
                slug = map_category(cat, alt)
                if slug and slug in cat_id:
                    # population A: now mappable thanks to the round-2 rules
                    ph, line = norm_phone(phones)
                    pin = clean(pcode)
                    pin = pin if pin and re.fullmatch(r"\d{6}", pin) else None
                    cp.write_row((ext, nm, canon(nm), cat_id[slug], None,
                                  clean(brand, 120), lat, lon, ph, line,
                                  clean(mails[0], 180) if mails else None,
                                  clean(sites[0], 255) if sites else None,
                                  clean(freeform, 200), pin))
                    recovered += 1
                elif cat is None:
                    # population B: no category at all -> evidence, then a human
                    hslug, ev = hint(nm)
                    if hslug and hslug in cat_id:
                        queue_rows.append((ext, "null_category_name_hint", nm,
                                           cat_id[hslug], ev,
                                           json.dumps({"lat": lat, "lon": lon,
                                                       "phones": list(phones or []),
                                                       "addr": freeform,
                                                       "postcode": pcode},
                                                      default=str)))
                        queued += 1
                    else:
                        skipped += 1
                else:
                    skipped += 1
    conn.commit()
    print(f"population A, newly mappable : {recovered:,}")
    print(f"population B, queued for review: {queued:,}")
    print(f"neither                       : {skipped:,}")

    if queue_rows:
        with cur.copy("""COPY review_queue (external_id, reason, display_name,
                         suggested_category_id, evidence, payload) FROM STDIN""") as cp:
            for r in queue_rows:
                cp.write_row(r)
        conn.commit()

    if recovered:
        cur.execute("""INSERT INTO businesses
            (external_id, display_name, canonical_name, category_id, brand_name,
             lat, lon, geo, location_accuracy, public_phone, phone_line_type,
             public_email, website, address_line_1, source_id, source_record_id,
             attribution_text, licence_name)
          SELECT s.external_id, s.display_name, s.canonical_name, s.category_id,
                 s.brand_name, s.lat, s.lon,
                 ST_SetSRID(ST_MakePoint(s.lon,s.lat),4326)::geography,
                 'exact_storefront', s.public_phone, s.phone_line_type,
                 s.public_email, s.website, s.address_line_1, %s, s.external_id,
                 '© Overture Maps Foundation', 'CDLA-Permissive-2.0'
          FROM rec_stage s
          WHERE NOT EXISTS (SELECT 1 FROM businesses b
                            WHERE ST_DWithin(b.geo,
                                  ST_SetSRID(ST_MakePoint(s.lon,s.lat),4326)::geography, 150)
                              AND similarity(b.canonical_name, s.canonical_name) > 0.55)
          ON CONFLICT (external_id) DO NOTHING""", (src_id,))
        print(f"inserted after dedup          : {cur.rowcount:,}")
        conn.commit()

    cur.execute("SELECT count(*) FROM businesses")
    print(f"\nbusinesses now: {cur.fetchone()[0]:,}")
    cur.execute("SELECT count(*) FROM review_queue WHERE status='pending'")
    print(f"review queue  : {cur.fetchone()[0]:,} pending a human decision")
    conn.close()


if __name__ == "__main__":
    main()
