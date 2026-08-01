"""Compose a useful display label without inventing anything.

"Durga" is a real source value but useless in a list of results. The fix is not to
guess it is "Durga Medicals" — that would fabricate a legal name. It is to compose
a label from facts already held:

    display_name  "Durga"
    resolved_name "Durga — Medical Store, Gaganpahad"

Name from the source, category from the tag mapping, locality from the place index.
Nothing invented. LocZ can render either field.

Rewritten after the first version accidentally cross-joined 4M businesses against
the ambiguous-name table (`LEFT JOIN ambiguous a ON true`) and ran for 49 minutes
without finishing. The join was never needed: a scalar EXISTS answers the same
question against an index.
"""
import os
from pathlib import Path
import time
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

LABELS = {
    "medical-stores-and-pharmacies": "Medical Store", "hospitals-and-clinics": "Clinic",
    "restaurants-and-food": "Restaurant", "bakeries-and-sweets": "Bakery",
    "hotels-and-accommodation": "Hotel", "grocery-and-kirana": "Kirana Store",
    "hardware-stores": "Hardware Store", "electrical-stores": "Electrical Store",
    "electronics-stores": "Electronics Store", "mobile-stores": "Mobile Store",
    "computer-and-laptop-stores": "Computer Store", "furniture-stores": "Furniture Store",
    "clothing-stores": "Clothing Store", "footwear-stores": "Footwear Store",
    "jewellery-stores": "Jewellery Store", "beauty-and-wellness": "Beauty & Wellness",
    "salons-and-spas": "Salon", "automobile-services": "Automobile Services",
    "bike-repair": "Bike Repair", "car-repair": "Car Repair",
    "tyre-and-battery-stores": "Tyre & Battery", "petrol-stations": "Petrol Station",
    "ev-charging-stations": "EV Charging", "schools": "School", "colleges": "College",
    "tuition-and-coaching": "Coaching Centre", "education-and-training": "Training Centre",
    "repair-services": "Repair Service", "home-services": "Home Service",
    "plumbing-services": "Plumbing", "electrical-services": "Electrician",
    "cleaning-services": "Cleaning Service", "tailoring-and-boutiques": "Tailor",
    "home-businesses": "Home Business", "home-food-sellers": "Home Food",
    "pet-stores-and-pet-services": "Pet Store",
    "printing-and-stationery": "Printing & Stationery", "gift-stores": "Gift Store",
    "travel-services": "Travel Services", "courier-and-parcel-services": "Courier",
    "property-services": "Property Services", "professional-services": "Professional Services",
    "event-services": "Event Services", "agricultural-supplies": "Agri Supplies",
    "wholesale-businesses": "Wholesaler", "local-manufacturers": "Manufacturer",
    "other-local-businesses": "Local Business",
}

# already says what it is — adding "— Medical Store" to "Durga Medicals" is noise
HAS_CATEGORY_WORD = (
    r"(medical|pharmac|chemist|clinic|hospital|dental|lab|hotel|restaurant|cafe|"
    r"bakery|sweet|tiffin|mess|dhaba|kirana|super ?market|store|stores|mart|bazaar|"
    r"traders?|agencies|hardware|electric|electronic|mobile|computer|furniture|"
    r"cloth|textile|saree|jewell?er|salon|beauty|spa|gym|motors?|automobile|garage|"
    r"tyres?|petrol|school|college|academy|institute|coaching|tuition|bank|finance|"
    r"insurance|consult|solutions?|enterprises?|industries|travels?|tours?|courier|"
    r"cargo|studio|photo|press|printers?|stationer|books?|builders?|properties|"
    r"realtors?|estates?|pet|vet|nursery|seeds?|agro|tailors?|boutique|charging)")


def main():
    t0 = time.time()
    conn = psycopg.connect(DSN, autocommit=True)
    cur = conn.cursor()
    cur.execute("SET statement_timeout='45min'")
    cur.execute("""ALTER TABLE businesses ADD COLUMN IF NOT EXISTS resolved_name text,
                     ADD COLUMN IF NOT EXISTS name_reason text""")

    cur.execute("DROP TABLE IF EXISTS cat_label")
    cur.execute("CREATE TABLE cat_label (category_id int PRIMARY KEY, label text)")
    cur.execute("SELECT slug, id FROM categories")
    ids = dict(cur.fetchall())
    with cur.copy("COPY cat_label (category_id,label) FROM STDIN") as cp:
        for slug, label in LABELS.items():
            if slug in ids:
                cp.write_row((ids[slug], label))

    # names reused by more than one business inside the same pincode
    print("finding ambiguous names …", flush=True)
    cur.execute("""DROP TABLE IF EXISTS ambiguous;
                   CREATE TABLE ambiguous AS
                   SELECT canonical_name, pincode_code
                   FROM businesses
                   WHERE pincode_code IS NOT NULL AND canonical_name <> ''
                   GROUP BY 1,2 HAVING count(*) > 1""")
    cur.execute("SELECT count(*) FROM ambiguous")
    print(f"  ambiguous name+pincode pairs : {cur.fetchone()[0]:,}")
    cur.execute("CREATE UNIQUE INDEX ON ambiguous (canonical_name, pincode_code)")
    cur.execute("ANALYZE ambiguous")

    # One pass, no join to `ambiguous` — a scalar EXISTS hits the unique index.
    print("composing labels …", flush=True)
    cur.execute(f"""
      UPDATE businesses b SET
        name_reason = CASE
          WHEN EXISTS (SELECT 1 FROM ambiguous a
                       WHERE a.canonical_name = b.canonical_name
                         AND a.pincode_code   = b.pincode_code) THEN 'ambiguous_in_pincode'
          WHEN b.brand_name IS NOT NULL
               AND lower(b.brand_name) = lower(b.display_name)   THEN 'chain_branch'
          WHEN b.display_name !~* '{HAS_CATEGORY_WORD}'          THEN 'generic_name'
          ELSE NULL END,
        resolved_name = CASE
          WHEN EXISTS (SELECT 1 FROM ambiguous a
                       WHERE a.canonical_name = b.canonical_name
                         AND a.pincode_code   = b.pincode_code)
            OR (b.brand_name IS NOT NULL AND lower(b.brand_name) = lower(b.display_name))
            OR b.display_name !~* '{HAS_CATEGORY_WORD}'
          THEN b.display_name
               || COALESCE(' — ' || (SELECT label FROM cat_label l
                                     WHERE l.category_id = b.category_id), '')
               || COALESCE(', ' || b.locality, '')
          ELSE b.display_name END,
        updated_at = now()
      WHERE b.resolved_name IS NULL""")
    print(f"  rows written : {cur.rowcount:,}   ({(time.time()-t0)/60:.1f} min)")

    cur.execute("""SELECT COALESCE(name_reason,'kept as-is'), count(*) FROM businesses
                   GROUP BY 1 ORDER BY 2 DESC""")
    print("\nreasons:")
    for r, n in cur.fetchall():
        print(f"  {r:22s} {n:9,}")

    print("\nsamples:")
    cur.execute("""SELECT display_name, resolved_name, name_reason FROM businesses
                   WHERE name_reason IS NOT NULL AND locality IS NOT NULL
                     AND length(display_name) < 24
                   LIMIT 10""")
    for a, b, r in cur.fetchall():
        print(f"  {a[:22]:24s} -> {b[:62]:64s} [{r}]")
    conn.close()


if __name__ == "__main__":
    main()
