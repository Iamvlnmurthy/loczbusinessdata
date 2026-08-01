"""Telangana government registries -> verification evidence, not directory listings.

These datasets prove a business legally exists and what trade it is licensed for.
They carry no coordinates, so they are NOT map pins and NOT inserted into
`businesses`. They land in `registry_entries` and are used to:

  1. upgrade a matched business to source_verified
  2. supply a legally-declared category
  3. show which mandals have registered businesses we have not mapped

`employer_name` is a private individual, not a business contact. It is dropped at
read time and never written.

Licence: Government Open Data Licence - India (GODL). Attribution required.
"""
import argparse, csv, io, json, os, re, sys, time, unicodedata, zipfile
import urllib.request
from pathlib import Path
import psycopg

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "var" / "telangana"
CACHE.mkdir(parents=True, exist_ok=True)
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
UA = os.environ.get("LOCZ_USER_AGENT",
                    "LocZ-Pincode-Business-Engine/0.1 (infovivencia2026@gmail.com)")
CATALOGUE = "https://data.telangana.gov.in/api/1/metastore/schemas/dataset/items"

# Only datasets that name individual businesses. Deliberately excludes:
#   Panchayat trade licences  - aggregate counts per panchayat, no business names
#   Fair Price Shops          - PDS ration outlets, allocated not traded; not commerce
WANTED = {
    "Shops and Establishment": ("shops_establishment", "establishment_name"),
    "GHMC Trade Licence Registrations": ("ghmc_trade_licence", "Title"),
    "GHMC New Trade Licence": ("ghmc_new_trade", "Title"),
    "Telangana Industries MSME": ("msme", "unit_name"),
    "Factories in Telangana": ("factories", None),
    "Regional Transport Authority Dealers": ("rta_dealers", "dealerName"),
}
DROP_COLUMNS = re.compile(r"employer|proprietor|owner_name|applicant|aadhaar|pan|"
                          r"mobile|email|repEmail|contact_person", re.I)

DDL = """
CREATE TABLE IF NOT EXISTS registry_entries (
  id             bigserial PRIMARY KEY,
  registry       text NOT NULL,
  source_file    text NOT NULL,
  name           text NOT NULL,
  canonical_name text NOT NULL,
  nature         text,
  category_hint  text,
  district       text,
  mandal         text,
  village        text,
  city           text,
  pincode_code   varchar(6),
  address        text,
  phone          text,
  registered_on  text,
  status         text,
  extra          jsonb,
  matched_business_id uuid,
  match_method   text,
  match_score    numeric(3,2),
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reg_name_trgm ON registry_entries USING gin (canonical_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS reg_place ON registry_entries (district, mandal);
CREATE INDEX IF NOT EXISTS reg_registry ON registry_entries (registry);
"""


def canon(n):
    n = unicodedata.normalize("NFKD", str(n)).lower()
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(r"\b(the|and|shop|store|stores|centre|center|pvt|ltd|private|limited|"
               r"m s|ms|sri|shri|new)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def get(url, dest):
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        dest.write_bytes(r.read())
    time.sleep(0.4)                      # courtesy: this is a state portal
    return dest


def catalogue():
    p = CACHE / "catalogue.json"
    if not p.exists():
        get(CATALOGUE, p)
    return json.loads(p.read_text(encoding="utf-8"))


def files_for(datasets, limit_per_dataset):
    out = []
    for x in datasets:
        title = x.get("title") or ""
        hit = next((v for k, v in WANTED.items() if k.lower() in title.lower()), None)
        if not hit:
            continue
        registry, _ = hit
        for d in (x.get("distribution") or [])[:limit_per_dataset]:
            dd = d.get("data") or d
            url = dd.get("downloadURL")
            if url and url.lower().endswith(".csv"):
                out.append((registry, url))
    return out


def rows_from(path):
    """Yields dicts. Tolerant: encodings vary by department and year."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return
    # newline='' — some state CSVs carry raw newlines inside unquoted fields
    for row in csv.DictReader(io.StringIO(text, newline="")):
        yield {k.strip(): (v.strip() if isinstance(v, str) else v)
               for k, v in row.items() if k}


def pick(row, *names):
    for n in names:
        for k, v in row.items():
            if k.lower() == n.lower() and v:
                return v
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=24, help="files per dataset")
    a = ap.parse_args()

    conn = psycopg.connect(DSN)
    cur = conn.cursor()
    cur.execute(DDL)
    conn.commit()

    cur.execute("""INSERT INTO data_sources
        (slug,name,source_type,adapter_key,base_url,provider_name,licence_name,
         attribution_text,commercial_use_allowed,storage_allowed,redistribution_allowed,
         automated_access_allowed,enabled,status,reviewed_by,reviewed_at,notes)
        VALUES ('telangana-open-data','Telangana Open Data Portal','government',
                'telangana_csv','https://data.telangana.gov.in','Government of Telangana',
                'GODL-India','Government of Telangana — data.telangana.gov.in',
                true,true,true,true,true,'approved','phase-3',now(),
                'robots.txt permits all but /search. Verification evidence only — '
                'no coordinates, never exported as listings. employer_name dropped at read.')
        ON CONFLICT (slug) DO UPDATE SET updated_at=now()""")
    conn.commit()

    todo = files_for(catalogue(), a.months)
    print(f"files to fetch: {len(todo)}", flush=True)

    cur.execute("SELECT DISTINCT source_file FROM registry_entries")
    done = {r[0] for r in cur.fetchall()}

    total, dropped_pii = 0, set()
    for i, (registry, url) in enumerate(todo, 1):
        fname = url.rsplit("/", 1)[-1]
        if fname in done:
            continue
        try:
            path = get(url, CACHE / fname)
        except Exception as e:
            print(f"  skip {fname}: {type(e).__name__}")
            continue
        batch = []
        try:
            parsed = list(rows_from(path))
        except Exception as e:
            print(f"  unparseable {fname}: {type(e).__name__}")
            continue
        for row in parsed:
            for k in list(row):
                if DROP_COLUMNS.search(k):
                    dropped_pii.add(k)
                    row.pop(k, None)            # never stored, never written
            name = pick(row, "establishment_name", "Title", "unit_name", "dealerName",
                        "name_frim", "name", "factory_name")
            if not name:
                continue
            pin = pick(row, "pincode", "pin_code", "postal_code")
            pin = pin if pin and re.fullmatch(r"\d{6}", str(pin).strip()) else None
            batch.append((
                registry, fname, name[:200], canon(name)[:200],
                pick(row, "nature_of_business", "line_of_activity", "SubCategoryName"),
                pick(row, "category_name", "industry_category", "CategoryName"),
                pick(row, "district", "district_name", "districtName", "DistrictName"),
                pick(row, "mandal", "mandal_name", "mandalName", "CircleName"),
                pick(row, "village", "village_name", "LocalityName", "WardName"),
                pick(row, "city"), pin,
                " ".join(x for x in (pick(row, "address1"), pick(row, "address2"),
                                     pick(row, "address3"), pick(row, "address")) if x) or None,
                pick(row, "contactPhone", "phone"),
                pick(row, "commencement_date", "RegistrationDate", "ondate", "approved_date"),
                pick(row, "status", "presentstatus"),
                json.dumps({k: v for k, v in row.items() if v}, default=str)[:4000],
            ))
        if batch:
            with cur.copy("""COPY registry_entries (registry, source_file, name,
                canonical_name, nature, category_hint, district, mandal, village, city,
                pincode_code, address, phone, registered_on, status, extra) FROM STDIN""") as cp:
                for r in batch:
                    cp.write_row(r)
            conn.commit()
            total += len(batch)
        if i % 15 == 0:
            print(f"  {i}/{len(todo)} files · {total:,} entries", flush=True)

    print(f"\nloaded {total:,} registry entries")
    if dropped_pii:
        print(f"personal-data columns dropped at read: {sorted(dropped_pii)}")

    cur.execute("""SELECT registry, count(*), count(pincode_code), count(phone)
                   FROM registry_entries GROUP BY 1 ORDER BY 2 DESC""")
    print(f"\n{'registry':26s} {'entries':>9s} {'w/pincode':>10s} {'w/phone':>8s}")
    for r, n, p, ph in cur.fetchall():
        print(f"  {r:24s} {n:9,} {p:10,} {ph:8,}")
    conn.close()


if __name__ == "__main__":
    main()
