"""Round-2 tag mappings: the biggest unmapped categories, plus a deliberate
public-service subset.

Government offices are NOT merchants. Only the facilities a local user actually
searches for are imported, under business_type PUBLIC_SERVICE so LocZ can render
them without a claim button, products, offers or reviews.
"""
import os
import psycopg

DSN = os.environ.get("LOCZ_DSN",
                     "host=127.0.0.1 port=5433 dbname=locz_engine user=postgres "
                     "password=LocZEngine_2026!")

RETAIL, FOOD, SERVICE, HOSP, INST, WHOLE, PUBLIC = (
    "RETAIL_STORE", "FOOD_SERVICE", "SERVICE_PROVIDER", "HOSPITALITY",
    "INSTITUTION", "WHOLESALER", "PUBLIC_SERVICE")

NEW = {
    # --- genuine retail that round 1 missed
    ("shop", "grocery"): ("grocery-and-kirana", "kirana-store", RETAIL),
    ("shop", "beverages"): ("grocery-and-kirana", "beverages", RETAIL),
    ("shop", "tea"): ("grocery-and-kirana", "tea-shop", RETAIL),
    ("shop", "coffee"): ("grocery-and-kirana", "coffee-shop", RETAIL),
    ("shop", "spices"): ("grocery-and-kirana", "spices", RETAIL),
    ("shop", "frozen_food"): ("grocery-and-kirana", "frozen-food", RETAIL),
    ("shop", "health_food"): ("grocery-and-kirana", "health-food", RETAIL),
    ("shop", "water"): ("grocery-and-kirana", "water-supply", RETAIL),
    ("shop", "alcohol"): ("grocery-and-kirana", "liquor-store", RETAIL),
    ("shop", "houseware"): ("furniture-stores", "houseware", RETAIL),
    ("shop", "kitchen"): ("furniture-stores", "kitchen-store", RETAIL),
    ("shop", "curtain"): ("furniture-stores", "furnishings", RETAIL),
    ("shop", "flooring"): ("hardware-stores", "flooring", RETAIL),
    ("shop", "tiles"): ("hardware-stores", "tiles", RETAIL),
    ("shop", "glaziery"): ("hardware-stores", "glass-and-glazing", SERVICE),
    ("shop", "plumber"): ("plumbing-services", "plumbing-supplies", RETAIL),
    ("shop", "sanitary"): ("hardware-stores", "sanitaryware", RETAIL),
    ("shop", "building_materials"): ("hardware-stores", "building-materials", RETAIL),
    ("shop", "mall"): ("other-local-businesses", "shopping-mall", RETAIL),
    ("shop", "ticket"): ("travel-services", "ticket-booking", SERVICE),
    ("shop", "money_lender"): ("professional-services", "money-lender", SERVICE),
    ("shop", "printing"): ("printing-and-stationery", "printing-service", SERVICE),
    ("shop", "funeral_directors"): ("other-local-businesses", "funeral-services", SERVICE),
    ("shop", "fishing"): ("other-local-businesses", "fishing-supplies", RETAIL),
    ("shop", "hairdresser_supply"): ("beauty-and-wellness", "salon-supplies", WHOLE),
    ("shop", "nutrition_supplements"): ("beauty-and-wellness", "supplements", RETAIL),
    ("shop", "second_hand"): ("other-local-businesses", "second-hand-store", RETAIL),
    ("shop", "rental"): ("other-local-businesses", "rental-service", SERVICE),
    ("shop", "storage_rental"): ("property-services", "storage-rental", SERVICE),
    ("shop", "furniture_rental"): ("furniture-stores", "furniture-rental", SERVICE),
    ("shop", "car_service"): ("car-repair", "car-service", SERVICE),
    ("shop", "truck"): ("automobile-services", "commercial-vehicles", RETAIL),
    ("shop", "truck_repair"): ("car-repair", "truck-repair", SERVICE),
    ("shop", "tractor"): ("agricultural-supplies", "tractor-dealer", RETAIL),
    ("shop", "farm"): ("agricultural-supplies", "farm-supplies", RETAIL),
    ("shop", "fertilizer"): ("agricultural-supplies", "fertiliser", RETAIL),
    ("shop", "seed"): ("agricultural-supplies", "seeds", RETAIL),
    ("shop", "gas"): ("other-local-businesses", "lpg-distributor", RETAIL),
    ("shop", "energy"): ("other-local-businesses", "solar-and-energy", RETAIL),
    ("shop", "solar"): ("other-local-businesses", "solar", RETAIL),
    ("shop", "electronics_repair"): ("repair-services", "electronics-repair", SERVICE),
    ("shop", "video_games"): ("gift-stores", "video-games", RETAIL),
    ("shop", "art"): ("gift-stores", "art-store", RETAIL),
    ("shop", "craft"): ("gift-stores", "craft-store", RETAIL),
    ("shop", "musical_instrument"): ("other-local-businesses", "musical-instruments", RETAIL),
    ("shop", "party"): ("event-services", "party-supplies", RETAIL),
    ("shop", "wedding"): ("event-services", "wedding-services", SERVICE),
    ("shop", "catering"): ("event-services", "catering", FOOD),
    ("shop", "deli"): ("restaurants-and-food", "deli", FOOD),
    ("shop", "chocolate"): ("bakeries-and-sweets", "chocolate", FOOD),

    ("craft", "electronics_repair"): ("repair-services", "electronics-repair", SERVICE),
    ("craft", "hvac"): ("repair-services", "ac-repair", SERVICE),
    ("craft", "metal_construction"): ("local-manufacturers", "fabrication", SERVICE),
    ("craft", "builder"): ("property-services", "builder", SERVICE),
    ("craft", "sawmill"): ("local-manufacturers", "sawmill", SERVICE),
    ("craft", "handicraft"): ("home-businesses", "handicraft", SERVICE),
    ("craft", "jeweller"): ("jewellery-stores", "jeweller", SERVICE),
    ("craft", "upholsterer"): ("repair-services", "upholstery", SERVICE),
    ("craft", "key_cutter"): ("repair-services", "key-cutting", SERVICE),
    ("craft", "gardener"): ("home-services", "gardening", SERVICE),

    ("office", "telecommunication"): ("professional-services", "telecom-office", SERVICE),
    ("office", "energy_supplier"): ("professional-services", "energy-supplier", SERVICE),
    ("office", "logistics"): ("courier-and-parcel-services", "logistics", SERVICE),
    ("office", "moving_company"): ("courier-and-parcel-services", "packers-and-movers", SERVICE),
    ("office", "construction_company"): ("property-services", "construction", SERVICE),
    ("office", "architect"): ("professional-services", "architect", SERVICE),
    ("office", "engineer"): ("professional-services", "engineer", SERVICE),
    ("office", "consulting"): ("professional-services", "consultant", SERVICE),
    ("office", "tax_advisor"): ("professional-services", "tax-advisor", SERVICE),
    ("office", "newspaper"): ("professional-services", "media-office", SERVICE),
    ("office", "association"): ("professional-services", "association", SERVICE),
    ("office", "ngo"): ("professional-services", "ngo", SERVICE),
    ("office", "coworking"): ("professional-services", "coworking", SERVICE),
    ("office", "diplomatic"): ("professional-services", "consulate", PUBLIC),

    ("amenity", "health_post"): ("hospitals-and-clinics", "health-post", PUBLIC),
    ("amenity", "social_facility"): ("professional-services", "social-facility", PUBLIC),
    ("amenity", "community_centre"): ("event-services", "community-hall", PUBLIC),
    ("amenity", "conference_centre"): ("event-services", "conference-centre", HOSP),
    ("amenity", "events_venue"): ("event-services", "event-venue", HOSP),
    ("amenity", "banquet_hall"): ("event-services", "banquet-hall", HOSP),
    ("amenity", "vehicle_inspection"): ("automobile-services", "vehicle-inspection", PUBLIC),
    ("amenity", "money_transfer"): ("professional-services", "money-transfer", SERVICE),
    ("amenity", "payment_centre"): ("professional-services", "bill-payment", SERVICE),
    ("amenity", "public_bath"): ("beauty-and-wellness", "public-bath", SERVICE),
    ("amenity", "animal_boarding"): ("pet-stores-and-pet-services", "pet-boarding", SERVICE),
    ("amenity", "animal_shelter"): ("pet-stores-and-pet-services", "animal-shelter", PUBLIC),

    ("tourism", "camp_site"): ("hotels-and-accommodation", "camp-site", HOSP),
    ("tourism", "caravan_site"): ("hotels-and-accommodation", "caravan-site", HOSP),
    ("leisure", "sauna"): ("salons-and-spas", "sauna", SERVICE),
    ("leisure", "amusement_arcade"): ("event-services", "amusement-arcade", HOSP),
    ("leisure", "bowling_alley"): ("event-services", "bowling", HOSP),
    ("leisure", "water_park"): ("event-services", "water-park", HOSP),
    ("leisure", "resort"): ("hotels-and-accommodation", "resort", HOSP),
}

# office=government is deliberately excluded: an administrative office is not a
# merchant, cannot be claimed, and would dilute the directory. The facilities
# people genuinely search for (post office, health post, school) already carry
# their own clean tags and are mapped above.

def main():
    conn = psycopg.connect(DSN)
    cur = conn.cursor()
    cur.execute("SELECT slug,id FROM categories")
    cat = dict(cur.fetchall())
    added, missing = 0, set()
    for (k, v), (slug, sub, bt) in NEW.items():
        if slug not in cat:
            missing.add(slug); continue
        cur.execute("""INSERT INTO source_category_map
              (source_type,source_key,source_value,category_id,subcategory,business_type)
            VALUES ('osm',%s,%s,%s,%s,%s)
            ON CONFLICT (source_type,source_key,source_value) DO UPDATE
              SET category_id=EXCLUDED.category_id, subcategory=EXCLUDED.subcategory,
                  business_type=EXCLUDED.business_type""",
                    (k, v, cat[slug], sub, bt))
        added += 1
    conn.commit()
    if missing:
        print("!! unknown categories:", missing)
    cur.execute("SELECT count(*) FROM source_category_map")
    print(f"mappings added: {added} | total now: {cur.fetchone()[0]}")
    cur.execute("""SELECT business_type, count(*) FROM source_category_map
                   GROUP BY 1 ORDER BY 2 DESC""")
    for bt, n in cur.fetchall():
        print(f"  {bt:18s} {n}")
    conn.close()


if __name__ == "__main__":
    main()
