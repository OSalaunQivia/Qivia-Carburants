"""
geocode_dkv.py — Ajoute les stations DKV sans ID au GeoJSON via géocodage
Usage : python3 geocode_dkv.py
Fichiers requis dans le même dossier : Stations_essences.xlsx, stations_lite.geojson
"""

import csv, io, json, os, re, time, unicodedata
import pandas as pd
import requests

EXCEL_PATH      = "Stations_essences.xlsx"
GEOJSON_PATH    = "stations_lite.geojson"
BATCH_URL       = "https://api-adresse.data.gouv.fr/search/csv/"
API_URL         = "https://api-adresse.data.gouv.fr/search/"
ADDR_THRESHOLD  = 0.5
BATCH_SIZE      = 50

def normalize(s):
    if not s: return ''
    s = str(s).lower().strip()
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    for pat, repl in {r'\bav\b':'avenue',r'\bbd\b':'boulevard',r'\brt\b':'route',
                      r'\bst\b':'saint',r'\bste\b':'sainte',r'\bimp\b':'impasse'}.items():
        s = re.sub(pat, repl, s)
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9\s]', ' ', s)).strip()

def token_score(a, b):
    ta = set(t for t in a.split() if len(t) > 2)
    tb = set(t for t in b.split() if len(t) > 2)
    if not ta or not tb: return 0
    return len(ta & tb) / max(len(ta), len(tb))

# ── Chargement ────────────────────────────────────────────────────────────────
print("[1/4] Chargement…")
df = pd.read_excel(EXCEL_PATH)
df['ID'] = df['ID'].astype(str).str.strip()
df = df.drop_duplicates('ID', keep='first')

with open(GEOJSON_PATH, encoding='utf-8') as f:
    gj = json.load(f)

existing_ids = {str(feat["properties"].get("id","")) for feat in gj["features"]}

geo_by_cp = {}
for feat in gj["features"]:
    p  = feat["properties"]
    cp = str(p.get("cp","")).split('.')[0]
    geo_by_cp.setdefault(cp, []).append((normalize(p.get("adresse","")), feat))

# ── Matching adresse ──────────────────────────────────────────────────────────
print("[2/4] Matching adresse pour les DKV sans ID…")
dkv_no_id  = df[(df['DKV'] == 1) & (~df['ID'].isin(existing_ids))]
to_geocode = []

for _, row in dkv_no_id.iterrows():
    cp         = str(row.get('Code postale', '')).split('.')[0]
    addr_excel = normalize(str(row.get('Adresse', '')))
    candidates = geo_by_cp.get(cp, [])
    best_score, best_feat = 0, None
    for addr_geo, feat in candidates:
        sc = token_score(addr_excel, addr_geo)
        if sc > best_score:
            best_score, best_feat = sc, feat
    if best_score >= ADDR_THRESHOLD and best_feat:
        for f in gj["features"]:
            if str(f["properties"].get("id","")) == str(best_feat["properties"]["id"]):
                f["properties"]["dkv"] = 1
                break
    else:
        to_geocode.append(row)

print(f"    Matchées par adresse : {len(dkv_no_id) - len(to_geocode)}")
print(f"    À géocoder           : {len(to_geocode)}")

# ── Géocodage ─────────────────────────────────────────────────────────────────
print("[3/4] Géocodage (adresse + ville + CP + département + région)…")
new_features, skipped = [], 0

def geocode_batch(rows):
    # On construit une requête complète : adresse + ville + CP
    lines = ["adresse,postcode,city"]
    for row in rows:
        adresse = str(row.get('Adresse', '')).replace('"','').replace('\n',' ').strip()
        cp      = str(row.get('Code postale', '')).split('.')[0].strip()
        ville   = str(row.get('Ville', '')).replace('"','').replace('\n',' ').strip()
        lines.append(f'"{adresse}",{cp},"{ville}"')

    resp = requests.post(BATCH_URL,
        files={'data': ('b.csv', '\n'.join(lines).encode('utf-8'), 'text/csv')},
        data={'columns': ['adresse', 'city'], 'postcode': 'postcode'},
        timeout=60)
    resp.raise_for_status()

    reader  = csv.DictReader(io.StringIO(resp.text))
    results = []
    for r in reader:
        try:
            score = float(r.get('result_score') or 0)
            lat   = float(r.get("latitude")  or 0) or None
            lon   = float(r.get("longitude") or 0) or None
        except (ValueError, TypeError):
            score, lat, lon = 0, None, None
        results.append((score, lat, lon))
    return results

def geocode_single(row):
    # Requête enrichie : adresse + ville + CP + département
    adresse = str(row.get('Adresse', ''))
    ville   = str(row.get('Ville', ''))
    cp      = str(row.get('Code postale', '')).split('.')[0]
    q       = f"{adresse}, {ville}"
    try:
        r = requests.get(API_URL, params={'q': q, 'postcode': cp, 'limit': 1}, timeout=10)
        feats = r.json().get('features', [])
        if feats:
            f0 = feats[0]
            return (f0['properties'].get('score', 0),
                    f0['geometry']['coordinates'][1],
                    f0['geometry']['coordinates'][0])
    except Exception:
        pass
    return 0, None, None

nb = (len(to_geocode) - 1) // BATCH_SIZE + 1 if to_geocode else 0
for i in range(0, len(to_geocode), BATCH_SIZE):
    batch = to_geocode[i:i+BATCH_SIZE]
    print(f"    Batch {i//BATCH_SIZE+1}/{nb} ({len(batch)} stations)…", end=' ', flush=True)
    try:
        results = geocode_batch(batch)
        while len(results) < len(batch):
            results.append((0, None, None))
    except Exception as e:
        print(f"⚠ batch échoué ({e}), ligne par ligne…", end=' ', flush=True)
        results = [geocode_single(r) for r in batch]

    ok = 0
    for row, (score, lat, lon) in zip(batch, results):
        # Pas de seuil bloquant — adresse + ville + CP = précis par nature
        if not lat or not lon:
            skipped += 1
            continue
        new_features.append({"type":"Feature",
            "geometry": {"type":"Point","coordinates":[lon, lat]},
            "properties": {
                "id":             str(row.get('ID','')),
                "adresse":        str(row.get('Adresse','')),
                "ville":          str(row.get('Ville','')),
                "cp":             str(row.get('Code postale','')).split('.')[0],
                "departement":    str(row.get('Départements','') or ''),
                "region":         str(row.get('Région','') or ''),
                "brand":          str(row.get('Brand','Autre') or 'Autre'),
                "dkv":            1,
                "total_energies": int(row.get('TotalEnergies',0) or 0),
                "nom":            str(row.get('Nom','') or ''),
                "gazole_prix":None,"sp95_prix":None,"e10_prix":None,
                "sp98_prix":None,"e85_prix":None,"gplc_prix":None,
            }})
        ok += 1
    print(f"{ok} OK")
    time.sleep(0.2)

# ── Sauvegarde ────────────────────────────────────────────────────────────────
print("[4/4] Sauvegarde…")
gj["features"].extend(new_features)
with open(GEOJSON_PATH, 'w', encoding='utf-8') as f:
    json.dump(gj, f, ensure_ascii=False, separators=(',',':'))

total = len(gj["features"])
dkv_t = sum(1 for f in gj["features"] if f["properties"].get("dkv")==1)
print(f"\n✅ stations_lite.geojson mis à jour")
print(f"   Total stations        : {total}")
print(f"   dont DKV              : {dkv_t}")
print(f"   Nouvelles géocodées   : {len(new_features)}")
print(f"   Sans coordonnées      : {skipped}")
print(f"   Taille                : {os.path.getsize(GEOJSON_PATH)//1024} Ko")
