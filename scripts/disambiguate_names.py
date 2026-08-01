"""Compose a useful display label without inventing anything.

"Durga" is a real source value but useless in a list of results. The fix is not to
guess that it is "Durga Medicals" - that would fabricate a legal name. It is to
compose a label from facts already held:

    display_name  "Durga"              <- source value, never modified
    resolved_name "Durga - Medical Store, Gaganpahad"

Every component is sourced: name from OSM/Overture, category from the tag mapping,
locality from the place index. LocZ can render either field.

Three cases get a label:
  1. ambiguous  - the same name appears more than once in the pincode
  2. generic    - one or two words, no category word present
  3. chain      - brand tag equals the name, so the outlet needs its branch
Everything else keeps its name as-is; a distinctive name needs no help.
"""
import os, re
import psycopg

DSN = os.environ.get("LOCZ_DSN",
                     "host=127.0.0.1 port=5433 dbname=locz_engine user=postgres "
                     "password=LocZEngine_2026!")

# words that already tell the user what the business is
CATEGORY_WORDS = r"""(medical|pharmac|chemist|drug|clinic|hospital|dental|lab|scan|
hotel|restaurant|cafe|bakery|sweet|tiffin|mess|dhaba|biryani|foods?|kitchen|
kirana|super ?market|store|stores|mart|bazaar|traders?|agencies|agency|
hardware|electric|electronic|mobile|computer|furniture|cloth|textile|saree|
jewell?er|gold|silver|salon|beauty|spa|parlou?r|gym|fitness|
motors?|automobile|garage|service|tyres?|petrol|fuel|filling|
school|college|academy|institute|coaching|tuition|library|
bank|finance|insurance|consult|solutions?|enterprises?|industries|
travels?|tours?|courier|cargo|logistics|packers|movers|
studio|photo|press|printers?|stationer|books?|
builders?|constructions?|properties|realtors?|estates?|
pet|vet|nursery|seeds?|fertiliser|fertilizer|agro)"""

SQL_LABELS = {
    "medical-stores-and-pharmacies": "Medical Store",
    "hospitals-and-clinics": "Clinic",
    "restaurants-and-food": "Restaurant",
    "bakeries-and-sweets": "Bakery",
    "hotels-and-accommodation": "Hotel",
    "grocery-and-kirana": "Kirana Store",
    "hardware-stores": "Hardware Store",
    "electrical-stores": "Electrical Store",
    "electronics-stores": "Electronics Store",
    "mobile-stores": "Mobile Store",
    "computer-and-laptop-stores": "Computer Store",
    "furniture-stores": "Furniture Store",
    "clothing-stores": "Clothing Store",
    "footwear-stores": "Footwear Store",
    "jewellery-stores": "Jewellery Store",
    "beauty-and-wellness": "Beauty & Wellness",
    "salons-and-spas": "Salon",
    "automobile-services": "Automobile Services",
    "bike-repair": "Bike Repair",
    "car-repair": "Car Repair",
    "tyre-and-battery-stores": "Tyre & Battery",
    "petrol-stations": "Petrol Station",
    "schools": "School",
    "colleges": "College",
    "tuition-and-coaching": "Coaching Centre",
    "education-and-training": "Training Centre",
    "repair-services": "Repair Service",
    "home-services": "Home Service",
    "plumbing-services": "Plumbing",
    "electrical-services": "Electrician",
    "cleaning-services": "Cleaning Service",
    "tailoring-and-boutiques": "Tailor",
    "home-businesses": "Home Business",
    "home-food-sellers": "Home Food",
    "pet-stores-and-pet-services": "Pet Store",
    "printing-and-stationery": "Printing & Stationery",
    "gift-stores": "Gift Store",
    "travel-services": "Travel Services",
    "courier-and-parcel-services": "Courier",
    "property-services": "Property Services",
    "professional-services": "Professional Services",
    "event-services": "Event Services",
    "agricultural-supplies": "Agri Supplies",
    "wholesale-businesses": "Wholesaler",
    "local-manufacturers": "Manufacturer",
    "other-local-businesses": "Local Business",
}


def main():
    conn = psycopg.connect(DSN)
    cur = conn.cursor()
    cur.execute("""ALTER TABLE businesses ADD COLUMN IF NOT EXISTS resolved_name text;
                   ALTER TABLE businesses ADD COLUMN IF NOT EXISTS name_reason text;""")
    conn.commit()

    # category label lookup, as a temp table so the work stays set-based
    cur.execute("DROP TABLE IF EXISTS cat_label")
    cur.execute("CREATE TEMP TABLE cat_label (slug text primary key, label text)")
    with cur.copy("COPY cat_label (slug,label) FROM STDIN") as cp:
        for k, v in SQL_LABELS.items():
            cp.write_row((k, v))

    cw = re.sub(r"\s+", "", CATEGORY_WORDS)

    # 1. ambiguous: the same canonical name used by 2+ businesses in one pincode
    cur.execute("""
      CREATE TEMP TABLE ambiguous AS
      SELECT canonical_name, pincode_code
      FROM businesses
      WHERE pincode_code IS NOT NULL AND canonical_name <> ''
      GROUP BY 1,2 HAVING count(*) > 1""")
    cur.execute("CREATE INDEX ON ambiguous (canonical_name, pincode_code)")
    cur.execute("SELECT count(*) FROM ambiguous")
    print(f"ambiguous name+pincode pairs : {cur.fetchone()[0]:,}")

    # 2. apply labels
    cur.execute(f"""
      UPDATE businesses b SET
        resolved_name = b.display_name
          || CASE WHEN cl.label IS NOT NULL THEN ' — ' || cl.label ELSE '' END
          || CASE WHEN b.locality IS NOT NULL THEN ', ' || b.locality ELSE '' END,
        name_reason = CASE
          WHEN a.canonical_name IS NOT NULL THEN 'ambiguous_in_pincode'
          WHEN b.brand_name IS NOT NULL
               AND lower(b.brand_name) = lower(b.display_name) THEN 'chain_branch'
          ELSE 'generic_name' END
      FROM categories c
      LEFT JOIN cat_label cl ON cl.slug = c.slug
      LEFT JOIN ambiguous a ON true
      WHERE b.category_id = c.id
        AND a.canonical_name IS NOT DISTINCT FROM
            (CASE WHEN EXISTS (SELECT 1 FROM ambiguous x
                               WHERE x.canonical_name = b.canonical_name
                                 AND x.pincode_code = b.pincode_code)
                  THEN b.canonical_name ELSE NULL END)
        AND (
          -- ambiguous within its pincode
          EXISTS (SELECT 1 FROM ambiguous x
                  WHERE x.canonical_name = b.canonical_name
                    AND x.pincode_code = b.pincode_code)
          -- or a chain outlet that needs its branch
          OR (b.brand_name IS NOT NULL AND lower(b.brand_name) = lower(b.display_name))
          -- or short and carrying no category word of its own
          OR (array_length(regexp_split_to_array(trim(b.display_name), '\\s+'),1) <= 2
              AND b.display_name !~* '{cw}')
        )""")
    print(f"labels applied               : {cur.rowcount:,}")
    conn.commit()

    # everything else keeps its own name
    cur.execute("UPDATE businesses SET resolved_name = display_name WHERE resolved_name IS NULL")
    print(f"kept as-is                   : {cur.rowcount:,}")
    conn.commit()

    cur.execute("""SELECT name_reason, count(*) FROM businesses
                   WHERE name_reason IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""")
    print("\nreasons:")
    for r, n in cur.fetchall():
        print(f"  {r:22s} {n:9,}")

    print("\nsamples:")
    cur.execute("""SELECT display_name, resolved_name, name_reason FROM businesses
                   WHERE name_reason IS NOT NULL AND locality IS NOT NULL
                   ORDER BY random() LIMIT 12""")
    for a, b, r in cur.fetchall():
        print(f"  {a[:26]:28s} -> {b[:66]:68s} [{r}]")
    conn.close()


if __name__ == "__main__":
    main()
