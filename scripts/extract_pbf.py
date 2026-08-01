"""One pass over india-latest.osm.pbf -> two JSONL files.

  places.jsonl      every named settlement (city/town/village/suburb/...) -> Area Resolver
  businesses.jsonl  every named business POI (node + way centroid)        -> ingestion

Fully offline. No Overpass, no rate limits, no network. ODbL: attribution required.
"""
import json, sys, time
from pathlib import Path
import osmium

ROOT = Path(__file__).resolve().parents[1]
PBF = ROOT / "var" / "osm" / "india-latest.osm.pbf"
OUT = ROOT / "var" / "extract"
OUT.mkdir(parents=True, exist_ok=True)

PLACE_VALUES = {"city", "town", "village", "suburb", "neighbourhood", "hamlet",
                "quarter", "borough", "municipality", "locality"}

# A POI counts as a business if it carries one of these keys (value-filtered later).
BUSINESS_KEYS = ("shop", "amenity", "office", "craft", "healthcare", "tourism",
                 "leisure", "club")

# amenity/tourism/leisure carry a lot of non-business values; keep the business-like ones.
AMENITY_OK = {
    "restaurant","cafe","fast_food","bar","pub","ice_cream","food_court","biergarten",
    "pharmacy","clinic","hospital","doctors","dentist","veterinary","nursing_home",
    "bank","bureau_de_change","atm","fuel","charging_station","car_wash","car_rental",
    "driving_school","school","college","university","kindergarten","language_school",
    "music_school","library","cinema","theatre","nightclub","marketplace","internet_cafe",
    "coworking_space","post_office","courier","money_transfer","payment_centre",
    "vehicle_inspection","training","childcare","studio","photo_booth","printer",
}
TOURISM_OK = {"hotel","guest_house","motel","hostel","apartment","chalet","resort"}
LEISURE_OK = {"fitness_centre","sports_centre","swimming_pool","dance","gym","spa",
              "amusement_arcade","bowling_alley","escape_game","adult_gaming_centre"}


def is_business(t):
    if not t.get("name"):
        return False
    if t.get("shop") or t.get("office") or t.get("craft") or t.get("healthcare"):
        return True
    if t.get("amenity") in AMENITY_OK:
        return True
    if t.get("tourism") in TOURISM_OK:
        return True
    if t.get("leisure") in LEISURE_OK:
        return True
    return False


KEEP_PREFIX = ("addr:", "contact:", "payment:", "service:", "diet:")
KEEP_KEYS = {
    "name","name:en","name:hi","name:te","int_name","alt_name","old_name","brand",
    "brand:wikidata","operator","operator:type","shop","amenity","office","craft",
    "healthcare","healthcare:speciality","tourism","leisure","club","cuisine",
    "phone","mobile","email","website","url","fax","opening_hours","opening_hours:covid19",
    "delivery","takeaway","drive_through","outdoor_seating","wheelchair","internet_access",
    "air_conditioning","smoking","organic","second_hand","self_service","wholesale",
    "building","building:levels","level","description","note","source","check_date",
    "survey:date","start_date","ref","gst:number","website:menu","facebook","instagram",
    "whatsapp","payment:cash","stars","rooms","capacity","denomination",
}


def slim(tags):
    out = {}
    for k, v in tags:
        if k in KEEP_KEYS or k.startswith(KEEP_PREFIX):
            out[k] = v
    return out


class Extractor:
    def __init__(self):
        self.places = open(OUT / "places.jsonl", "w", encoding="utf-8")
        self.biz = open(OUT / "businesses.jsonl", "w", encoding="utf-8")
        self.n_place = self.n_biz = self.n_seen = 0
        self.t0 = time.time()

    def emit_place(self, otype, oid, tags, lat, lon, version, ts):
        self.places.write(json.dumps({
            "osm_type": otype, "osm_id": oid, "place": tags.get("place"),
            "name": tags.get("name"), "name_en": tags.get("name:en"),
            "name_hi": tags.get("name:hi"), "name_te": tags.get("name:te"),
            "postcode": tags.get("addr:postcode") or tags.get("postal_code"),
            "district": tags.get("addr:district"), "state": tags.get("addr:state"),
            "population": tags.get("population"),
            "lat": lat, "lon": lon, "version": version, "timestamp": ts,
        }, ensure_ascii=False) + "\n")
        self.n_place += 1

    def emit_biz(self, otype, oid, tags, lat, lon, version, ts):
        self.biz.write(json.dumps({
            "osm_type": otype, "osm_id": oid, "lat": round(lat, 7), "lon": round(lon, 7),
            "version": version, "timestamp": ts, "tags": slim(tags),
        }, ensure_ascii=False) + "\n")
        self.n_biz += 1

    def close(self):
        self.places.close(); self.biz.close()


def centroid(way):
    """Mean of the way's node locations - good enough for a building footprint."""
    n = 0; slat = slon = 0.0
    for nd in way.nodes:
        try:
            loc = nd.location
            if loc.valid():
                slat += loc.lat; slon += loc.lon; n += 1
        except osmium.InvalidLocationError:
            continue
    return (slat / n, slon / n) if n else (None, None)


def main():
    if not PBF.exists():
        sys.exit(f"missing {PBF}")
    ex = Extractor()
    fp = osmium.FileProcessor(str(PBF)).with_locations("flex_mem")

    for obj in fp:
        ex.n_seen += 1
        if ex.n_seen % 5_000_000 == 0:
            el = time.time() - ex.t0
            print(f"  {ex.n_seen/1e6:5.0f}M objects | places {ex.n_place:,} | "
                  f"businesses {ex.n_biz:,} | {el/60:.1f} min", flush=True)

        tags = {k: v for k, v in obj.tags} if obj.tags else {}
        if not tags:
            continue
        otype = "node" if obj.is_node() else ("way" if obj.is_way() else "relation")
        ver = getattr(obj, "version", None)
        ts = obj.timestamp.isoformat() if obj.timestamp else None

        if obj.is_node():
            lat, lon = obj.location.lat, obj.location.lon
        elif obj.is_way():
            lat, lon = centroid(obj)
            if lat is None:
                continue
        else:
            continue

        if tags.get("place") in PLACE_VALUES and tags.get("name"):
            ex.emit_place(otype, obj.id, tags, lat, lon, ver, ts)
        if is_business(tags):
            ex.emit_biz(otype, obj.id, obj.tags, lat, lon, ver, ts)

    ex.close()
    el = time.time() - ex.t0
    print(f"\nDONE  objects {ex.n_seen:,} | places {ex.n_place:,} | "
          f"businesses {ex.n_biz:,} | {el/60:.1f} min")
    for f in ("places.jsonl", "businesses.jsonl"):
        print(f"  {f}: {(OUT/f).stat().st_size/1e6:.1f} MB")


if __name__ == "__main__":
    main()
