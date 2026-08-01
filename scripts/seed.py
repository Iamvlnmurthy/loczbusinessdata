"""Phase 1 seed: pincodes (GeoNames), 46-category taxonomy, OSM tag map, source registry.

Idempotent. Re-running updates in place.
"""
import csv, collections, math, os, sys
from pathlib import Path
import psycopg

ROOT = Path(__file__).resolve().parents[1]
GEO = ROOT / "var" / "geonames" / "IN.txt"
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


def hav(a, b, c, d):
    R = 6371.0
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    h = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(h))


# ---------------------------------------------------------------- taxonomy
CATEGORIES = [
    "grocery-and-kirana", "restaurants-and-food", "bakeries-and-sweets",
    "hotels-and-accommodation", "medical-stores-and-pharmacies", "hospitals-and-clinics",
    "hardware-stores", "electrical-stores", "electronics-stores", "mobile-stores",
    "computer-and-laptop-stores", "furniture-stores", "clothing-stores", "footwear-stores",
    "jewellery-stores", "beauty-and-wellness", "salons-and-spas", "automobile-services",
    "bike-repair", "car-repair", "tyre-and-battery-stores", "petrol-stations",
    "education-and-training", "schools", "colleges", "tuition-and-coaching",
    "repair-services", "home-services", "plumbing-services", "electrical-services",
    "cleaning-services", "tailoring-and-boutiques", "home-businesses", "home-food-sellers",
    "pet-stores-and-pet-services", "printing-and-stationery", "gift-stores",
    "travel-services", "courier-and-parcel-services", "property-services",
    "professional-services", "event-services", "agricultural-supplies",
    "wholesale-businesses", "local-manufacturers", "other-local-businesses",
]

RETAIL, FOOD, SERVICE, HOSP, INST, WHOLE = ("RETAIL_STORE", "FOOD_SERVICE",
                                            "SERVICE_PROVIDER", "HOSPITALITY",
                                            "INSTITUTION", "WHOLESALER")
# (key, value) -> (category slug, subcategory, business_type)
TAGMAP = {
    ("shop","supermarket"):("grocery-and-kirana","supermarket",RETAIL),
    ("shop","convenience"):("grocery-and-kirana","kirana-store",RETAIL),
    ("shop","general"):("grocery-and-kirana","general-store",RETAIL),
    ("shop","greengrocer"):("grocery-and-kirana","greengrocer",RETAIL),
    ("shop","department_store"):("grocery-and-kirana","department-store",RETAIL),
    ("shop","variety_store"):("grocery-and-kirana","variety-store",RETAIL),
    ("shop","butcher"):("grocery-and-kirana","butcher",RETAIL),
    ("shop","dairy"):("grocery-and-kirana","dairy",RETAIL),
    ("shop","seafood"):("grocery-and-kirana","seafood",RETAIL),
    ("shop","bakery"):("bakeries-and-sweets","bakery",FOOD),
    ("shop","confectionery"):("bakeries-and-sweets","sweets",FOOD),
    ("shop","pastry"):("bakeries-and-sweets","pastry",FOOD),
    ("shop","chemist"):("medical-stores-and-pharmacies","chemist",RETAIL),
    ("shop","medical_supply"):("medical-stores-and-pharmacies","medical-supply",RETAIL),
    ("shop","optician"):("hospitals-and-clinics","optician",SERVICE),
    ("shop","hearing_aids"):("hospitals-and-clinics","hearing-aids",SERVICE),
    ("shop","mobile_phone"):("mobile-stores","mobile-phone-store",RETAIL),
    ("shop","electronics"):("electronics-stores","electronics-store",RETAIL),
    ("shop","hifi"):("electronics-stores","audio-store",RETAIL),
    ("shop","appliance"):("electronics-stores","appliance-store",RETAIL),
    ("shop","computer"):("computer-and-laptop-stores","computer-store",RETAIL),
    ("shop","hardware"):("hardware-stores","hardware-store",RETAIL),
    ("shop","doityourself"):("hardware-stores","home-improvement",RETAIL),
    ("shop","paint"):("hardware-stores","paint-store",RETAIL),
    ("shop","trade"):("hardware-stores","building-materials",RETAIL),
    ("shop","electrical"):("electrical-stores","electrical-store",RETAIL),
    ("shop","lighting"):("electrical-stores","lighting-store",RETAIL),
    ("shop","furniture"):("furniture-stores","furniture-store",RETAIL),
    ("shop","interior_decoration"):("furniture-stores","interiors",RETAIL),
    ("shop","bed"):("furniture-stores","mattress-store",RETAIL),
    ("shop","clothes"):("clothing-stores","clothing-store",RETAIL),
    ("shop","fabric"):("clothing-stores","fabric-store",RETAIL),
    ("shop","boutique"):("tailoring-and-boutiques","boutique",RETAIL),
    ("shop","tailor"):("tailoring-and-boutiques","tailor",SERVICE),
    ("shop","shoes"):("footwear-stores","footwear-store",RETAIL),
    ("shop","bag"):("footwear-stores","bags-and-luggage",RETAIL),
    ("shop","jewelry"):("jewellery-stores","jewellery-store",RETAIL),
    ("shop","watches"):("jewellery-stores","watch-store",RETAIL),
    ("shop","beauty"):("beauty-and-wellness","beauty-parlour",SERVICE),
    ("shop","cosmetics"):("beauty-and-wellness","cosmetics-store",RETAIL),
    ("shop","hairdresser"):("salons-and-spas","salon",SERVICE),
    ("shop","massage"):("salons-and-spas","spa",SERVICE),
    ("shop","car"):("automobile-services","car-dealer",RETAIL),
    ("shop","motorcycle"):("automobile-services","two-wheeler-dealer",RETAIL),
    ("shop","bicycle"):("automobile-services","bicycle-store",RETAIL),
    ("shop","car_parts"):("automobile-services","spare-parts",RETAIL),
    ("shop","car_repair"):("car-repair","car-service",SERVICE),
    ("shop","motorcycle_repair"):("bike-repair","two-wheeler-service",SERVICE),
    ("shop","tyres"):("tyre-and-battery-stores","tyre-store",RETAIL),
    ("shop","stationery"):("printing-and-stationery","stationery-store",RETAIL),
    ("shop","copyshop"):("printing-and-stationery","printing-service",SERVICE),
    ("shop","books"):("printing-and-stationery","book-store",RETAIL),
    ("shop","newsagent"):("printing-and-stationery","newsagent",RETAIL),
    ("shop","gift"):("gift-stores","gift-store",RETAIL),
    ("shop","florist"):("gift-stores","florist",RETAIL),
    ("shop","toys"):("gift-stores","toy-store",RETAIL),
    ("shop","pet"):("pet-stores-and-pet-services","pet-store",RETAIL),
    ("shop","pet_grooming"):("pet-stores-and-pet-services","pet-grooming",SERVICE),
    ("shop","laundry"):("home-services","laundry",SERVICE),
    ("shop","dry_cleaning"):("home-services","dry-cleaning",SERVICE),
    ("shop","travel_agency"):("travel-services","travel-agency",SERVICE),
    ("shop","wholesale"):("wholesale-businesses","wholesaler",WHOLE),
    ("shop","agrarian"):("agricultural-supplies","agri-supplies",RETAIL),
    ("shop","garden_centre"):("agricultural-supplies","garden-centre",RETAIL),
    ("shop","hardware_rental"):("repair-services","tool-rental",SERVICE),
    ("shop","sports"):("other-local-businesses","sports-store",RETAIL),
    ("shop","music"):("other-local-businesses","music-store",RETAIL),
    ("shop","photo"):("other-local-businesses","photo-studio",SERVICE),

    ("amenity","restaurant"):("restaurants-and-food","restaurant",FOOD),
    ("amenity","cafe"):("restaurants-and-food","cafe",FOOD),
    ("amenity","fast_food"):("restaurants-and-food","fast-food",FOOD),
    ("amenity","food_court"):("restaurants-and-food","food-court",FOOD),
    ("amenity","ice_cream"):("restaurants-and-food","ice-cream",FOOD),
    ("amenity","bar"):("restaurants-and-food","bar",FOOD),
    ("amenity","pub"):("restaurants-and-food","pub",FOOD),
    ("amenity","pharmacy"):("medical-stores-and-pharmacies","pharmacy",RETAIL),
    ("amenity","clinic"):("hospitals-and-clinics","clinic",SERVICE),
    ("amenity","hospital"):("hospitals-and-clinics","hospital",SERVICE),
    ("amenity","doctors"):("hospitals-and-clinics","doctor",SERVICE),
    ("amenity","dentist"):("hospitals-and-clinics","dentist",SERVICE),
    ("amenity","veterinary"):("pet-stores-and-pet-services","veterinary",SERVICE),
    ("amenity","nursing_home"):("hospitals-and-clinics","nursing-home",SERVICE),
    ("amenity","fuel"):("petrol-stations","petrol-station",RETAIL),
    ("amenity","charging_station"):("petrol-stations","ev-charging",SERVICE),
    ("amenity","car_wash"):("car-repair","car-wash",SERVICE),
    ("amenity","car_rental"):("automobile-services","car-rental",SERVICE),
    ("amenity","driving_school"):("education-and-training","driving-school",INST),
    ("amenity","school"):("schools","school",INST),
    ("amenity","kindergarten"):("schools","kindergarten",INST),
    ("amenity","childcare"):("schools","childcare",INST),
    ("amenity","college"):("colleges","college",INST),
    ("amenity","university"):("colleges","university",INST),
    ("amenity","language_school"):("tuition-and-coaching","language-school",INST),
    ("amenity","music_school"):("tuition-and-coaching","music-school",INST),
    ("amenity","training"):("education-and-training","training-centre",INST),
    ("amenity","library"):("education-and-training","library",INST),
    ("amenity","bank"):("professional-services","bank",SERVICE),
    ("amenity","atm"):("professional-services","atm",SERVICE),
    ("amenity","bureau_de_change"):("professional-services","currency-exchange",SERVICE),
    ("amenity","post_office"):("courier-and-parcel-services","post-office",SERVICE),
    ("amenity","courier"):("courier-and-parcel-services","courier",SERVICE),
    ("amenity","internet_cafe"):("printing-and-stationery","internet-cafe",SERVICE),
    ("amenity","coworking_space"):("professional-services","coworking",SERVICE),
    ("amenity","cinema"):("event-services","cinema",HOSP),
    ("amenity","theatre"):("event-services","theatre",HOSP),
    ("amenity","nightclub"):("event-services","nightclub",HOSP),
    ("amenity","marketplace"):("grocery-and-kirana","marketplace",RETAIL),
    ("amenity","studio"):("other-local-businesses","studio",SERVICE),

    ("office","estate_agent"):("property-services","estate-agent",SERVICE),
    ("office","travel_agent"):("travel-services","travel-agency",SERVICE),
    ("office","lawyer"):("professional-services","lawyer",SERVICE),
    ("office","accountant"):("professional-services","accountant",SERVICE),
    ("office","insurance"):("professional-services","insurance",SERVICE),
    ("office","financial"):("professional-services","financial-services",SERVICE),
    ("office","company"):("professional-services","company-office",SERVICE),
    ("office","it"):("professional-services","it-services",SERVICE),
    ("office","courier"):("courier-and-parcel-services","courier",SERVICE),
    ("office","educational_institution"):("education-and-training","training-institute",INST),
    ("office","employment_agency"):("professional-services","employment-agency",SERVICE),
    ("office","advertising_agency"):("professional-services","advertising",SERVICE),

    ("craft","tailor"):("tailoring-and-boutiques","tailor",SERVICE),
    ("craft","electrician"):("electrical-services","electrician",SERVICE),
    ("craft","plumber"):("plumbing-services","plumber",SERVICE),
    ("craft","carpenter"):("repair-services","carpenter",SERVICE),
    ("craft","painter"):("home-services","painter",SERVICE),
    ("craft","photographer"):("event-services","photographer",SERVICE),
    ("craft","shoemaker"):("repair-services","cobbler",SERVICE),
    ("craft","goldsmith"):("jewellery-stores","goldsmith",SERVICE),
    ("craft","blacksmith"):("local-manufacturers","blacksmith",SERVICE),
    ("craft","welder"):("local-manufacturers","welder",SERVICE),
    ("craft","confectionery"):("home-food-sellers","home-confectioner",FOOD),

    ("healthcare","clinic"):("hospitals-and-clinics","clinic",SERVICE),
    ("healthcare","doctor"):("hospitals-and-clinics","doctor",SERVICE),
    ("healthcare","hospital"):("hospitals-and-clinics","hospital",SERVICE),
    ("healthcare","pharmacy"):("medical-stores-and-pharmacies","pharmacy",RETAIL),
    ("healthcare","dentist"):("hospitals-and-clinics","dentist",SERVICE),
    ("healthcare","laboratory"):("hospitals-and-clinics","diagnostic-lab",SERVICE),
    ("healthcare","physiotherapist"):("hospitals-and-clinics","physiotherapy",SERVICE),
    ("healthcare","alternative"):("hospitals-and-clinics","alternative-medicine",SERVICE),
    ("healthcare","centre"):("hospitals-and-clinics","health-centre",SERVICE),
    ("healthcare","optometrist"):("hospitals-and-clinics","optometrist",SERVICE),

    ("tourism","hotel"):("hotels-and-accommodation","hotel",HOSP),
    ("tourism","guest_house"):("hotels-and-accommodation","guest-house",HOSP),
    ("tourism","motel"):("hotels-and-accommodation","motel",HOSP),
    ("tourism","hostel"):("hotels-and-accommodation","hostel",HOSP),
    ("tourism","apartment"):("hotels-and-accommodation","serviced-apartment",HOSP),
    ("tourism","resort"):("hotels-and-accommodation","resort",HOSP),

    ("leisure","fitness_centre"):("beauty-and-wellness","gym",SERVICE),
    ("leisure","sports_centre"):("other-local-businesses","sports-centre",SERVICE),
    ("leisure","spa"):("salons-and-spas","spa",SERVICE),
    ("leisure","dance"):("tuition-and-coaching","dance-school",INST),
    ("leisure","swimming_pool"):("other-local-businesses","swimming-pool",SERVICE),
}


def main():
    if not GEO.exists():
        sys.exit(f"missing {GEO} — copy GeoNames IN.txt there first")
    conn = psycopg.connect(DSN, autocommit=False)
    cur = conn.cursor()

    # ---------------- categories
    for i, slug in enumerate(CATEGORIES):
        cur.execute("""INSERT INTO categories (slug,name,sort_order) VALUES (%s,%s,%s)
                       ON CONFLICT (slug) DO UPDATE SET sort_order=EXCLUDED.sort_order""",
                    (slug, slug.replace("-", " ").title(), i))
    cur.execute("SELECT slug,id FROM categories")
    cat_id = dict(cur.fetchall())
    print(f"categories       : {len(cat_id)}")

    # ---------------- tag map
    n_map = 0
    for (k, v), (slug, sub, bt) in TAGMAP.items():
        if slug not in cat_id:
            print(f"  !! tagmap points at unknown category {slug}"); continue
        cur.execute("""INSERT INTO source_category_map
                         (source_type,source_key,source_value,category_id,subcategory,business_type)
                       VALUES ('osm',%s,%s,%s,%s,%s)
                       ON CONFLICT (source_type,source_key,source_value) DO UPDATE
                         SET category_id=EXCLUDED.category_id,
                             subcategory=EXCLUDED.subcategory,
                             business_type=EXCLUDED.business_type""",
                    (k, v, cat_id[slug], sub, bt))
        n_map += 1
    print(f"osm tag mappings : {n_map}")

    # ---------------- pincodes
    offices = collections.defaultdict(list)
    for r in csv.reader(open(GEO, encoding="utf-8"), delimiter="\t"):
        if len(r) >= 11 and r[9] and r[10]:
            offices[r[1]].append({"place": r[2], "state": r[3], "district": r[5],
                                  "mandal": r[7], "lat": float(r[9]), "lon": float(r[10])})

    rows, clusters = {}, collections.defaultdict(list)
    for code, offs in offices.items():
        lat = sum(o["lat"] for o in offs) / len(offs)
        lon = sum(o["lon"] for o in offs) / len(offs)
        spread = max((hav(lat, lon, o["lat"], o["lon"]) for o in offs), default=0.0)
        rows[code] = {
            "name": collections.Counter(o["place"] for o in offs).most_common(1)[0][0],
            "district": collections.Counter(o["district"] for o in offs).most_common(1)[0][0],
            "state": offs[0]["state"],
            "mandal": next((o["mandal"] for o in offs if o["mandal"]), None),
            "n": len(offs), "lat": lat, "lon": lon,
            "radius": max(round(spread * 1.25, 2), 2.0), "offs": offs,
        }
        clusters[(round(lat, 4), round(lon, 4))].append(code)

    with cur.copy("""COPY pincodes
        (code,name,district_name,state_name,mandal,office_count,
         geonames_lat,geonames_lon,lat,lon,geo,search_radius_km,
         centroid_src,shares_coordinate,cluster_size,targetable)
        FROM STDIN""") as cp:
        for code, r in rows.items():
            size = len(clusters[(round(r["lat"], 4), round(r["lon"], 4))])
            shared = size > 1
            cp.write_row((code, r["name"], r["district"], r["state"], r["mandal"], r["n"],
                          r["lat"], r["lon"], r["lat"], r["lon"],
                          f'SRID=4326;POINT({r["lon"]} {r["lat"]})', r["radius"],
                          "geonames", shared, size, not shared))
    print(f"pincodes         : {len(rows):,}")

    with cur.copy("COPY pincode_offices (code,office_name,mandal,lat,lon) FROM STDIN") as cp:
        n = 0
        for code, r in rows.items():
            for o in r["offs"]:
                cp.write_row((code, o["place"], o["mandal"] or None, o["lat"], o["lon"]))
                n += 1
    print(f"pincode offices  : {n:,}")

    # ---------------- source registry: OSM, approved (ODbL permits storage + reuse)
    cur.execute("""INSERT INTO data_sources
        (slug,name,source_type,adapter_key,base_url,provider_name,licence_name,licence_url,
         attribution_text,commercial_use_allowed,storage_allowed,redistribution_allowed,
         automated_access_allowed,rate_limit_per_minute,crawl_delay_seconds,
         enabled,status,reviewed_by,reviewed_at,config,notes)
        VALUES ('osm-india-pbf','OpenStreetMap — India extract (Geofabrik)','osm','osm_pbf',
                'https://download.geofabrik.de/asia/india-latest.osm.pbf','OpenStreetMap contributors',
                'ODbL-1.0','https://opendatacommons.org/licenses/odbl/1-0/',
                '© OpenStreetMap contributors',true,true,true,true,NULL,NULL,
                true,'approved','phase-1-seed',now(),
                '{"extract":"india-latest.osm.pbf"}'::jsonb,
                'Bulk extract: no rate limit applies, no live requests made.')
        ON CONFLICT (slug) DO UPDATE SET updated_at=now()""")
    cur.execute("""INSERT INTO data_sources
        (slug,name,source_type,adapter_key,base_url,provider_name,licence_name,
         attribution_text,commercial_use_allowed,storage_allowed,redistribution_allowed,
         automated_access_allowed,rate_limit_per_minute,crawl_delay_seconds,
         enabled,status,reviewed_by,reviewed_at,notes)
        VALUES ('osm-overpass','OpenStreetMap — Overpass API','osm','osm_overpass',
                'https://overpass-api.de/api/interpreter','OpenStreetMap contributors',
                'ODbL-1.0','© OpenStreetMap contributors',true,true,true,true,
                6,2.0,true,'approved_with_restrictions','phase-1-seed',now(),
                'Small bounded queries only. Prefer the PBF extract for anything larger.')
        ON CONFLICT (slug) DO UPDATE SET updated_at=now()""")
    cur.execute("""INSERT INTO data_sources
        (slug,name,source_type,adapter_key,provider_name,licence_name,attribution_text,
         commercial_use_allowed,storage_allowed,redistribution_allowed,automated_access_allowed,
         enabled,status,notes)
        VALUES ('geonames-postal','GeoNames — India postal codes','government','geonames_file',
                'GeoNames','CC-BY-4.0','© GeoNames',true,true,true,true,
                true,'approved','Pincode centroids. Known accuracy issues — see docs/PINCODE-CENTROID-AUDIT.md')
        ON CONFLICT (slug) DO UPDATE SET updated_at=now()""")

    conn.commit()

    cur.execute("SELECT count(*) FROM pincodes WHERE shares_coordinate")
    shared = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM pincodes")
    total = cur.fetchone()[0]
    print(f"\nflagged unusable : {shared:,} of {total:,} ({shared/total*100:.1f}%) share a coordinate")
    cur.execute("SELECT count(*) FROM data_sources WHERE enabled")
    print(f"approved sources : {cur.fetchone()[0]}")
    conn.close()


if __name__ == "__main__":
    main()
