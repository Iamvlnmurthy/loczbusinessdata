"""Load Overture Maps India places through the full pipeline.

  filter -> map category -> validate phone -> stage -> dedupe vs existing -> resolve -> score

Licence: CDLA-Permissive-2.0. Redistribution permitted; attribution required.
"""
import os, re, time, unicodedata
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

# Not merchants. Same reasoning that excluded government offices: a temple or a
# monument cannot be claimed, sells nothing, and dilutes the directory.
EXCLUDE = (
    "temple", "church", "mosque", "gurudwara", "synagogue", "shrine", "monastery",
    "religious_organization", "place_of_worship", "cemetery", "funeral_home",
    "landmark", "historical", "monument", "tourist_attraction", "museum",
    "park", "playground", "garden", "beach", "lake", "mountain", "forest",
    "bus_station", "train_station", "railway", "airport", "metro_station",
    "subway", "port", "ferry", "parking", "toll", "bridge",
    "government_office", "public_and_government_association", "embassy",
    "police", "fire_station", "court", "prison", "military",
    "political", "community_services_non_profits", "charity", "nonprofit",
    "neighborhood", "region", "city_hall", "public_plaza", "atms", "atm",
)

# ordered: first hit wins, so specific patterns precede generic ones
RULES = [
    (("pharmac", "drugstore", "chemist", "medical_supply"), "medical-stores-and-pharmacies"),
    (("hospital", "clinic", "doctor", "dentist", "physician", "surgeon", "diagnostic",
      "pathology", "nursing", "physiotherap", "ayurved", "homeopath", "optometr",
      "health_and_medical", "medical_center", "eye_care", "dermatolog", "pediatric",
      "gynecolog", "orthoped", "cardiolog", "psychiatr", "laborator"), "hospitals-and-clinics"),
    (("veterinar", "pet_", "_pet", "animal"), "pet-stores-and-pet-services"),
    (("bakery", "bakeries", "cake", "sweet_shop", "confection", "dessert", "patisserie",
      "ice_cream", "chocolat"), "bakeries-and-sweets"),
    (("restaurant", "cafe", "coffee", "food_court", "dhaba", "canteen", "bar_", "_bar",
      "pub", "brewery", "juice", "tea_", "fast_food", "pizza", "biryani", "catering",
      "food_and_drink", "eatery", "diner", "bistro", "tiffin"), "restaurants-and-food"),
    (("hotel", "resort", "lodge", "guest_house", "hostel", "accommodation", "homestay",
      "motel", "bnb", "serviced_apartment"), "hotels-and-accommodation"),
    (("grocery", "supermarket", "kirana", "convenience_store", "provision", "greengrocer",
      "butcher", "fish_market", "dairy", "beverage_store", "liquor", "wine_shop",
      "food_store", "general_store", "departmental"), "grocery-and-kirana"),
    (("mobile_phone", "cell_phone", "smartphone", "telecom_store"), "mobile-stores"),
    (("computer", "laptop", "it_store", "software_development", "information_technology",
      "web_design", "app_develop", "data_", "cyber"), "computer-and-laptop-stores"),
    (("electronics", "appliance", "television", "audio", "camera_store"), "electronics-stores"),
    (("hardware", "building_material", "cement", "sanitary", "tiles", "paint_store",
      "plywood", "timber", "steel", "glass_", "construction_material"), "hardware-stores"),
    (("electrical_store", "electrical_supply", "lighting"), "electrical-stores"),
    (("furniture", "mattress", "home_decor", "interior_design", "furnishing",
      "carpet", "curtain", "kitchenware", "houseware"), "furniture-stores"),
    (("clothing", "apparel", "boutique", "garment", "saree", "textile", "fabric",
      "fashion", "tailor", "readymade", "uniform"), "clothing-stores"),
    (("shoe_", "footwear", "sandal"), "footwear-stores"),
    (("jewel", "goldsmith", "silversmith", "watch_store", "gems"), "jewellery-stores"),
    (("salon", "spa", "barber", "hair_", "nail_", "massage", "parlour", "parlor"),
     "salons-and-spas"),
    (("beauty", "cosmetic", "gym", "fitness", "yoga", "wellness", "slimming",
      "meditation"), "beauty-and-wellness"),
    (("gas_station", "petrol", "fuel", "cng", "lpg", "charging_station"), "petrol-stations"),
    (("tyre", "tire", "battery_store"), "tyre-and-battery-stores"),
    (("motorcycle_repair", "bike_repair", "two_wheeler_repair", "scooter_repair"), "bike-repair"),
    (("automotive_repair", "car_repair", "car_wash", "auto_repair", "garage",
      "vehicle_repair", "denting"), "car-repair"),
    (("car_dealer", "motorcycle_dealer", "automotive", "auto_parts", "vehicle",
      "driving_school", "car_rental", "truck", "tractor_dealer"), "automobile-services"),
    (("preschool", "kindergarten", "elementary_school", "high_school", "primary_school",
      "secondary_school", "school"), "schools"),
    (("college", "university", "institute_of", "polytechnic"), "colleges"),
    (("tutoring", "coaching", "test_prep", "computer_coaching", "tuition"),
     "tuition-and-coaching"),
    (("education", "training", "library", "learning", "academy", "driving_range"),
     "education-and-training"),
    (("plumb",), "plumbing-services"),
    (("electrician", "electrical_service", "electrical_contractor"), "electrical-services"),
    (("cleaning", "housekeeping", "pest_control", "sanitation"), "cleaning-services"),
    (("laundry", "dry_clean", "home_service", "carpenter", "handyman", "gardening",
      "home_improvement", "appliance_repair"), "home-services"),
    (("repair", "servicing", "maintenance"), "repair-services"),
    (("courier", "post_office", "logistics", "packers", "movers", "delivery_service",
      "freight", "shipping"), "courier-and-parcel-services"),
    (("real_estate", "property", "home_developer", "builder", "apartment_rental",
      "land_", "estate_agent", "construction_services", "architect_"), "property-services"),
    (("travel", "tour_", "_tours", "tourist_information", "visa", "ticket"),
     "travel-services"),
    (("event", "wedding", "banquet", "party", "photograph", "videograph", "dj_",
      "cinema", "theatre", "entertainment", "amusement"), "event-services"),
    (("printing", "stationery", "photocopy", "xerox", "book_store", "bookshop",
      "internet_cafe", "cyber_cafe", "graphic_design", "advertising"),
     "printing-and-stationery"),
    (("gift", "florist", "flower", "toy_", "handicraft", "souvenir", "art_"), "gift-stores"),
    (("agricultur", "farm", "seed", "fertilizer", "pesticide", "nursery_plant",
      "irrigation", "poultry", "dairy_farm"), "agricultural-supplies"),
    (("wholesale", "distributor", "supplier", "trading_company", "importer", "exporter"),
     "wholesale-businesses"),
    (("manufactur", "factory", "industrial", "fabrication", "workshop_", "mill_",
      "processing", "engineering_works"), "local-manufacturers"),
    (("bank", "credit_union", "financial", "insurance", "loan", "lawyer", "legal",
      "accountant", "tax_", "chartered", "consult", "notary", "auditor", "broker",
      "professional_services", "business_services", "recruit", "staffing", "hr_",
      "marketing", "agency", "office_", "corporate", "company", "coworking"),
     "professional-services"),
    # --- round 2: recovered from the unmapped tail (285k records) ---
    (("eyewear", "optician", "optical"), "hospitals-and-clinics"),
    (("physical_therapy", "naturopath", "counseling_and_mental_health", "chiroprac",
      "acupunctur", "rehabilitation", "wellness_program", "nutritionist", "dietitian"),
     "hospitals-and-clinics"),
    (("tattoo", "makeup_artist", "mehendi", "piercing", "waxing", "threading",
      "skin_care", "hair_removal"), "beauty-and-wellness"),
    (("caterer", "catering", "bar", "lounge", "banquet_food"), "restaurants-and-food"),
    (("auto_detailing", "car_detailing", "vehicle_wash"), "car-repair"),
    (("engineering_services", "architectural_designer", "surveying", "drafting",
      "interior_designer", "landscap"), "professional-services"),
    (("telecommunications", "internet_service_provider", "cable_provider",
      "money_transfer", "payment_service", "atm_service", "currency"),
     "professional-services"),
    (("business_to_business", "b2b_", "commercial_equipment", "industrial_equipment",
      "energy_equipment", "machinery"), "wholesale-businesses"),
    (("chemical_plant", "manufacturing_plant", "production_facility", "refinery",
      "packaging", "textile_mill"), "local-manufacturers"),
    (("astrolog", "psychic", "numerolog", "vastu", "palmist", "priest_service"),
     "professional-services"),
    (("music_production", "recording_studio", "arts_and_crafts", "art_gallery",
      "dance_studio", "event_venue", "concert_venue", "party_supplies"),
     "event-services"),
    (("tours", "tour_operator", "tour_agency", "holiday"), "travel-services"),
    (("social_service_organizations", "non_governmental_association", "community_center"),
     None),   # explicitly NOT businesses - keep them out, but stop calling them "unmapped"
    (("home_business", "home_baker", "homemade"), "home-businesses"),
    (("shopping", "store", "shop", "market", "mall", "retail", "outlet", "bazaar"),
     "other-local-businesses"),
]


def map_category(cat, alt):
    """Return a canonical slug, or None to drop the record."""
    hay = " ".join(x for x in ([cat] + list(alt or [])) if x).lower()
    if not hay.strip():
        return None
    for bad in EXCLUDE:
        if bad in hay:
            return None
    for keys, slug in RULES:
        for k in keys:
            if k in hay:
                return slug          # slug may be None: recognised but not a merchant
    return None                      # unmapped -> not imported, counted for review


def norm_phone(phones):
    """Strict: exactly 10 national digits, no toll-free, no premium."""
    if not phones:
        return None, None
    for v in phones:
        d = re.sub(r"\D", "", str(v))
        if d.startswith("0091"):
            d = d[4:]
        elif d.startswith("91") and len(d) == 12:
            d = d[2:]
        elif d.startswith("0") and len(d) == 11:
            d = d[1:]
        if len(d) != 10:
            continue
        if d[:4] in ("1800", "1860", "1900") or d[:3] == "140":
            continue                                   # national line, not a shop's own
        if re.search(r"(\d)\1{5,}", d):
            continue                                   # 6+ repeated digits
        if d[0] in "6789":
            return "+91" + d, "mobile"
        if d[0] in "12345":
            return "+91" + d, "landline"
    return None, None


def clean(v, limit=None):
    """Postgres text cannot hold NUL bytes; Overture aggregates sources that do."""
    if v is None:
        return None
    v = "".join(ch for ch in str(v) if ord(ch) >= 32)
    v = re.sub(r"\s+", " ", v).strip()
    if not v:
        return None
    return v[:limit] if limit else v


def canon(n):
    n = unicodedata.normalize("NFKD", n).lower()
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(r"\b(the|and|shop|store|stores|centre|center|pvt|ltd|private|limited)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def main():
    t0 = time.time()
    conn = psycopg.connect(DSN)
    cur = conn.cursor()
    cur.execute("SELECT slug,id FROM categories")
    cat_id = dict(cur.fetchall())
    cur.execute("""INSERT INTO data_sources
        (slug,name,source_type,adapter_key,base_url,provider_name,licence_name,licence_url,
         attribution_text,commercial_use_allowed,storage_allowed,redistribution_allowed,
         automated_access_allowed,enabled,status,reviewed_by,reviewed_at,notes)
        VALUES ('overture-places','Overture Maps — Places (India)','overture','overture_parquet',
                's3://overturemaps-us-west-2','Overture Maps Foundation','CDLA-Permissive-2.0',
                'https://cdla.dev/permissive-2-0/','© Overture Maps Foundation',
                true,true,true,true,true,'approved','phase-2',now(),
                'Bulk GeoParquet. Per-record upstream licences preserved in sources column.')
        ON CONFLICT (slug) DO UPDATE SET updated_at=now() RETURNING id""")
    src_id = cur.fetchone()[0]
    conn.commit()

    cur.execute("DROP TABLE IF EXISTS ov_stage")
    cur.execute("""CREATE TABLE ov_stage (
        external_id text PRIMARY KEY, display_name text, canonical_name text,
        category_id int, source_category text, lat double precision, lon double precision,
        public_phone text, phone_line_type text, public_email text, website text,
        brand_name text, address_line_1 text, addr_locality text, addr_pin varchar(6),
        confidence real)""")
    conn.commit()

    duck = duckdb.connect()
    q = f"""SELECT id, name, category, category_alt, confidence, lat, lon, phones,
                   websites, emails, brand, addr_freeform, addr_locality, addr_postcode
            FROM read_parquet('{PARQUET}')"""
    print("streaming parquet ...", flush=True)
    res = duck.execute(q)

    seen, kept, dropped_cat, dropped_geo = set(), 0, 0, 0
    batch, BATCH = [], 50_000
    with cur.copy("COPY ov_stage FROM STDIN") as cp:
        while True:
            rows = res.fetchmany(50_000)
            if not rows:
                break
            for (oid, name, cat, alt, confd, lat, lon, phones, sites, emails,
                 brand, freeform, locality, pcode) in rows:
                if not name or lat is None or lon is None:
                    dropped_geo += 1
                    continue
                slug = map_category(cat, alt)
                if slug is None or slug not in cat_id:
                    dropped_cat += 1
                    continue
                ext = f"ovt:{oid}"
                if ext in seen:
                    continue
                seen.add(ext)
                ph, line = norm_phone(phones)
                site = clean(sites[0], 255) if sites else None
                mail = clean(emails[0], 180) if emails else None
                pin = clean(pcode)
                pin = pin if pin and re.fullmatch(r"\d{6}", pin) else None
                nm = clean(name, 180)
                if not nm:
                    dropped_geo += 1
                    continue
                cp.write_row((ext, nm, canon(nm), cat_id[slug], clean(cat, 120), lat, lon,
                              ph, line, mail, site, clean(brand, 120),
                              clean(freeform, 200), clean(locality, 140), pin, confd))
                kept += 1
    conn.commit()
    print(f"staged            : {kept:,}")
    print(f"dropped, category : {dropped_cat:,} (non-business or unmapped)")
    print(f"dropped, no geom  : {dropped_geo:,}")

    print("deduplicating against existing records ...", flush=True)
    cur.execute("CREATE INDEX ON ov_stage (public_phone)")
    cur.execute("CREATE INDEX ON ov_stage USING gin (canonical_name gin_trgm_ops)")
    cur.execute("""CREATE TABLE ov_dup AS
        SELECT DISTINCT s.external_id FROM ov_stage s
        JOIN businesses b ON b.public_phone = s.public_phone
        WHERE s.public_phone IS NOT NULL""")
    cur.execute("SELECT count(*) FROM ov_dup")
    print(f"  duplicate by phone      : {cur.fetchone()[0]:,}")
    conn.commit()

    cur.execute("""INSERT INTO ov_dup
        SELECT DISTINCT s.external_id FROM ov_stage s
        WHERE s.external_id NOT IN (SELECT external_id FROM ov_dup)
          AND EXISTS (
            SELECT 1 FROM businesses b
            WHERE ST_DWithin(b.geo, ST_SetSRID(ST_MakePoint(s.lon,s.lat),4326)::geography, 150)
              AND similarity(b.canonical_name, s.canonical_name) > 0.55)""")
    print(f"  duplicate by name+150 m : {cur.rowcount:,}")
    conn.commit()

    cur.execute("""INSERT INTO businesses
        (external_id, display_name, canonical_name, category_id, business_type,
         brand_name, lat, lon, geo, location_accuracy, public_phone, phone_line_type,
         public_email, website, address_line_1, source_id, source_record_id, source_url,
         attribution_text, licence_name, phone_status)
      SELECT s.external_id, s.display_name, s.canonical_name, s.category_id,
             m.business_type, s.brand_name, s.lat, s.lon,
             ST_SetSRID(ST_MakePoint(s.lon,s.lat),4326)::geography, 'exact_storefront',
             s.public_phone, s.phone_line_type, s.public_email, s.website,
             s.address_line_1, %s, s.external_id, NULL,
             '© Overture Maps Foundation', 'CDLA-Permissive-2.0',
             CASE WHEN s.public_phone IS NULL THEN NULL ELSE 'valid_'||s.phone_line_type END
      FROM ov_stage s
      LEFT JOIN LATERAL (SELECT business_type FROM source_category_map
                         WHERE category_id = s.category_id LIMIT 1) m ON true
      WHERE s.external_id NOT IN (SELECT external_id FROM ov_dup)
      ON CONFLICT (external_id) DO NOTHING""", (src_id,))
    print(f"inserted new      : {cur.rowcount:,}")
    conn.commit()

    print("resolving pincodes ...", flush=True)
    cur.execute("""UPDATE businesses b SET pincode_code=s.addr_pin,
          pincode_method='exact_source_pincode',
          pincode_confidence = CASE WHEN ST_Distance(b.geo,p.geo)<=25000 THEN 0.97 ELSE 0.55 END
        FROM ov_stage s JOIN pincodes p ON p.code=s.addr_pin
        WHERE b.external_id=s.external_id AND s.addr_pin IS NOT NULL
          AND b.pincode_code IS NULL""")
    print(f"  exact_source_pincode : {cur.rowcount:,}")
    conn.commit()

    cur.execute("""UPDATE businesses b SET pincode_code=c.code,
          pincode_method='nearest_named_place',
          pincode_confidence = CASE WHEN c.d<=3 THEN 0.80 WHEN c.d<=8 THEN 0.70
                                    WHEN c.d<=15 THEN 0.55 ELSE 0.40 END
        FROM (SELECT b2.id, p.code, ST_Distance(b2.geo,p.geo)/1000.0 d
              FROM businesses b2 CROSS JOIN LATERAL
                (SELECT code, geo FROM pincodes WHERE targetable
                 ORDER BY pincodes.geo <-> b2.geo LIMIT 1) p
              WHERE b2.pincode_code IS NULL) c
        WHERE b.id=c.id""")
    print(f"  nearest_named_place  : {cur.rowcount:,}")
    conn.commit()

    print("labelling localities ...", flush=True)
    cur.execute("""UPDATE businesses b SET locality=c.nm, district=p.district_name,
                          state=p.state_name
        FROM (SELECT b2.id, np.name nm FROM businesses b2 CROSS JOIN LATERAL
                (SELECT name, geo FROM named_places ORDER BY named_places.geo <-> b2.geo LIMIT 1) np
              WHERE b2.locality IS NULL) c
        LEFT JOIN businesses bb ON bb.id=c.id
        LEFT JOIN pincodes p ON p.code=bb.pincode_code
        WHERE b.id=c.id""")
    print(f"  localities           : {cur.rowcount:,}")
    conn.commit()

    print("suppressing shared numbers ...", flush=True)
    cur.execute("""WITH shared AS (SELECT public_phone FROM businesses
                     WHERE public_phone IS NOT NULL GROUP BY 1 HAVING count(*) > 3)
                   UPDATE businesses b SET phone_raw=COALESCE(phone_raw,b.public_phone),
                          phone_status='shared_number', public_phone=NULL, phone_line_type=NULL
                   FROM shared s WHERE b.public_phone=s.public_phone""")
    print(f"  suppressed           : {cur.rowcount:,}")
    conn.commit()

    cur.execute("""UPDATE businesses SET
        completeness_score = 45
          + CASE WHEN public_phone IS NOT NULL THEN 20 ELSE 0 END
          + CASE WHEN address_line_1 IS NOT NULL THEN 15 ELSE 0 END
          + CASE WHEN opening_hours_raw IS NOT NULL THEN 10 ELSE 0 END
          + CASE WHEN website IS NOT NULL THEN 10 ELSE 0 END,
        confidence_score = LEAST(95, 40
          + CASE WHEN pincode_confidence>=0.90 THEN 15 WHEN pincode_confidence>=0.70 THEN 8 ELSE 0 END
          + CASE WHEN public_phone IS NOT NULL THEN 15 ELSE 0 END
          + CASE WHEN address_line_1 IS NOT NULL THEN 10 ELSE 0 END
          + CASE WHEN location_accuracy='building' THEN 10 ELSE 5 END
          + CASE WHEN website IS NOT NULL THEN 5 ELSE 0 END),
        tier = CASE
          WHEN pincode_code IS NULL OR pincode_confidence<0.55 THEN 'HELD'::export_tier
          WHEN public_phone IS NOT NULL THEN 'CONTACTABLE'::export_tier
          ELSE 'LOCATABLE'::export_tier END""")
    conn.commit()

    print(f"\n=== TOTAL after Overture ({(time.time()-t0)/60:.1f} min) ===")
    cur.execute("""SELECT tier::text, count(*), round(100.0*count(*)/sum(count(*)) over (),1)
                   FROM businesses GROUP BY 1 ORDER BY 2 DESC""")
    for t, n, p in cur.fetchall():
        print(f"  {t:12s} {n:9,}  {p}%")
    cur.execute("""SELECT d.name, count(b.id) FROM data_sources d
                   JOIN businesses b ON b.source_id=d.id GROUP BY 1 ORDER BY 2 DESC""")
    print()
    for n, c in cur.fetchall():
        print(f"  {n[:42]:44s} {c:9,}")
    cur.execute("SELECT count(*) FROM businesses")
    print(f"\n  grand total          {cur.fetchone()[0]:,}")
    cur.execute("SELECT count(*) FROM businesses WHERE public_phone IS NOT NULL")
    print(f"  publishable phones   {cur.fetchone()[0]:,}")
    cur.execute("SELECT count(DISTINCT pincode_code) FROM businesses WHERE pincode_code IS NOT NULL")
    print(f"  pincodes covered     {cur.fetchone()[0]:,} / 19,238")
    conn.close()


if __name__ == "__main__":
    main()
