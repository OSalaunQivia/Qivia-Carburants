"""
fetch_and_build.py — QIVIA
Récupère les prix depuis l'API gouvernementale,
joint avec Prix carburant - Liste brand - Overpass.csv pour les marques,
génère le CSV du jour et qivia_data.json.
"""

import json, os, time, datetime, requests, pandas as pd, glob, re

DKV_IDS_CACHE = "dkv_ids_cache.txt"
DKV_STATIONS_FILE = "DKV_stations.csv"  # fichier CSV avec No ID, Brand, Adresse, Code Postal, Ville

API_URL = (
    "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets"
    "/prix-des-carburants-en-france-flux-instantane-v2/records"
)
API_FIELDS = [
    "id","adresse","cp","ville","region","departement","code_departement",
    "latitude","longitude","services_service","carburants_disponibles",
    "gazole_prix","gazole_maj","sp95_prix","sp95_maj",
    "e10_prix","e10_maj","sp98_prix","sp98_maj",
    "e85_prix","e85_maj","gplc_prix","gplc_maj",
]
FUELS      = ["gazole_prix","sp95_prix","e10_prix","sp98_prix","e85_prix","gplc_prix"]
TOP_BRANDS = 12
CSV_DIR    = "csv"
BRAND_FILE = "Prix carburant - Liste brand - Overpass.csv"

BRAND_MAP = {
    'TotalEnergies': 'TotalEnergies', 'Total': 'TotalEnergies', 'Total Access': 'TotalEnergies', 'Total Express': 'TotalEnergies',
    'E.Leclerc': 'Leclerc', 'Leclerc Express': 'Leclerc',
    'Esso Express': 'Esso',
    'Système U': 'Super U', 'U Express': 'Super U', 'Hyper U': 'Super U',
    'Carrefour Market': 'Carrefour', 'Carrefour Contact': 'Carrefour',
    'Carrefour City': 'Carrefour', 'Carrefour Express': 'Carrefour',
    'Eni': 'Eni/Agip', 'Agip': 'Eni/Agip', 'ENI': 'Eni/Agip',
    'Intermarché Contact': 'Intermarché', 'Intermarché Express': 'Intermarché',
    'Géant Casino': 'Casino', 'Casino': 'Casino',
    'BP': 'BP', 'Shell': 'Shell',
    'Dyneff': 'Dyneff', 'Elan': 'Elan',
}


def load_brands():
    """Charge le fichier Overpass avec les marques."""
    if not os.path.exists(BRAND_FILE):
        print(f"[Brand] Fichier {BRAND_FILE} non trouvé — marques non disponibles")
        return pd.DataFrame(columns=['provider_id','brand','station_name'])

    df = pd.read_csv(BRAND_FILE, low_memory=False)
    df = df[['tags/ref:FR:prix-carburants','tags/brand','tags/name']].copy()
    df = df.rename(columns={
        'tags/ref:FR:prix-carburants': 'provider_id',
        'tags/brand': 'brand',
        'tags/name': 'station_name'
    })
    df['provider_id'] = df['provider_id'].astype(str).str.strip()
    df = df.dropna(subset=['provider_id','brand'])
    df['brand'] = df['brand'].replace(BRAND_MAP)
    print(f"[Brand] {len(df)} stations avec marque chargées")
    print(f"[Brand] Top marques: {df['brand'].value_counts().head(5).to_dict()}")
    return df


def fetch_api():
    rows, offset = [], 0
    print("[API] Récupération des données…")
    while True:
        params = {"select": ",".join(API_FIELDS), "limit": 100, "offset": offset}
        for attempt in range(3):
            try:
                r = requests.get(API_URL, params=params, timeout=30)
                r.raise_for_status()
                break
            except:
                if attempt == 2: raise
                time.sleep(3)
        batch = r.json().get("results", [])
        if not batch: break
        rows.extend(batch)
        offset += 100
        if offset % 1000 == 0: print(f"  {offset} stations…")
    print(f"[API] {len(rows)} stations récupérées")
    return rows


def build_dataframe(rows, brands_df):
    df = pd.DataFrame(rows)

    # Nettoyer les types
    for c in [col for col in df.columns if col.endswith("_maj")]:
        df[c] = pd.to_datetime(df[c], errors="coerce", utc=True)
    for f in FUELS:
        if f in df.columns:
            df[f] = pd.to_numeric(df[f], errors="coerce")

    # Renommer
    df = df.rename(columns={
        "id":             "provider_id",
        "adresse":        "address",
        "cp":             "postal",
        "ville":          "city",
        "departement":    "department",
        "code_departement": "dept_code",
    })

    # Normaliser provider_id pour la jointure
    df["provider_id"] = df["provider_id"].astype(str).str.strip()

    # Jointure avec les marques
    if len(brands_df) > 0:
        df = df.merge(
            brands_df[['provider_id','brand','station_name']],
            on='provider_id',
            how='left'
        )
        df['name'] = df['station_name'].fillna(df['address'])
        df['brand'] = df['brand'].fillna('Autre')
        matched = df['brand'].ne('Autre').sum()
        print(f"[Merge] {matched}/{len(df)} stations avec marque identifiée ({matched/len(df)*100:.1f}%)")
    else:
        df['brand'] = 'Autre'
        df['name']  = df['address']

    df["snapshot_date"] = datetime.date.today().isoformat()
    return df


def save_csv(df):
    os.makedirs(CSV_DIR, exist_ok=True)
    today = datetime.date.today().strftime("%Y-%m-%d")
    path  = os.path.join(CSV_DIR, f"stations_avec_prix_{today}.csv")
    df.to_csv(path, index=False)
    print(f"[CSV] {path} ({len(df)} lignes)")
    return path


def safe_mean(s):
    v = s.dropna()
    return round(float(v.mean()), 4) if len(v) > 0 else None


def build_aggregates(df):
    files = sorted(glob.glob(os.path.join(CSV_DIR, "stations_avec_prix_*.csv")))
    print(f"[Agg] {len(files)} fichier(s) CSV trouvé(s) dans {CSV_DIR}/")
    if files:
        dfs = []
        for f in files:
            date_str = os.path.basename(f).replace("stations_avec_prix_","").replace(".csv","")
            try:
                tmp = pd.read_csv(f, low_memory=False)
                tmp["snapshot_date"] = pd.to_datetime(date_str, format="mixed")
                # Harmoniser les colonnes des anciens CSV (format PostgreSQL)
                if "uuid" in tmp.columns and "provider_id" not in tmp.columns:
                    tmp = tmp.rename(columns={"uuid": "provider_id"})
                if "name" in tmp.columns and "address" not in tmp.columns:
                    tmp["address"] = tmp["name"]
                dfs.append(tmp)
                print(f"  ✓ {os.path.basename(f)} ({len(tmp)} lignes)")
            except Exception as e:
                print(f"  ✗ {f}: {e}")
        all_data = pd.concat(dfs, ignore_index=True) if dfs else df.copy()
        print(f"[Agg] Total : {len(all_data)} lignes, {len(dfs)} snapshots")
    else:
        all_data = df.copy()
        all_data["snapshot_date"] = pd.to_datetime(all_data["snapshot_date"])

    all_data["year_month"] = all_data["snapshot_date"].dt.strftime("%Y-%m")
    all_data["week"]       = all_data["snapshot_date"].dt.isocalendar().week.astype(int)
    all_data["year"]       = all_data["snapshot_date"].dt.year
    all_data["quarter"]    = all_data["snapshot_date"].dt.to_period("Q").astype(str)
    all_data["half"]       = all_data["snapshot_date"].apply(
        lambda d: f"{d.year}-H{'1' if d.month<=6 else '2'}"
    )
    dates_sorted = sorted(all_data["snapshot_date"].unique())

    def fr(g):
        return {f: safe_mean(g[f]) for f in FUELS if f in g.columns}

    daily       = [{"date":str(dt.date()),**fr(g)} for dt,g in all_data.groupby("snapshot_date")]
    weekly      = []
    for (ym,wk),g in all_data.groupby(["year_month","week"]):
        di = sorted(g["snapshot_date"].dt.date.unique())
        weekly.append({"period":f"S{wk}","year_month":ym,"week":int(wk),
                       "date_min":str(di[0]),"date_max":str(di[-1]),**fr(g)})
    monthly     = [{"period":ym,**fr(g)} for ym,g in all_data.groupby("year_month")]
    quarterly   = [{"period":q,**fr(g)}  for q,g  in all_data.groupby("quarter")]
    half_yearly = [{"period":h,**fr(g)}  for h,g  in all_data.groupby("half")]
    yearly      = [{"period":str(y),**fr(g)} for y,g in all_data.groupby("year")]

    reg_col   = "region" if "region" in all_data.columns else None
    brand_col = "brand"  if "brand"  in all_data.columns else None

    regional_daily = []
    if reg_col:
        for (dt,region),g in all_data.groupby(["snapshot_date",reg_col]):
            regional_daily.append({"date":str(dt.date()),"region":str(region),**fr(g)})

    brand_daily, top_brands = [], []
    if brand_col:
        top_brands = all_data[brand_col].value_counts().head(TOP_BRANDS).index.tolist()
        for (dt,brand),g in all_data[all_data[brand_col].isin(top_brands)].groupby(["snapshot_date",brand_col]):
            brand_daily.append({"date":str(dt.date()),"brand":str(brand),**fr(g)})

    # Stations: one entry per unique station, latest prices
    # Sort descending so first occurrence = most recent
    all_sorted = all_data.sort_values("snapshot_date", ascending=False)
    unique_st = all_sorted.drop_duplicates(subset="provider_id", keep="first")
    n_stations_total = len(unique_st)
    # For brand enrichment: also keep a "best brand" lookup from all snapshots
    # (some stations only appear in early large snapshots)
    all_brands = all_data[all_data["brand"].notna() & (all_data["brand"] != "Autre")]        .sort_values("snapshot_date", ascending=False)        .drop_duplicates(subset="provider_id", keep="first")[["provider_id","brand"]]
    brand_lookup = dict(zip(all_brands["provider_id"].astype(str), all_brands["brand"]))
    for s in [None]:  # will be applied in stations loop below
        pass
    stations = []
    for _,r in unique_st.iterrows():
        stations.append({
            "uuid":       str(r.get("provider_id","")),
            "name":       str(r.get("name",r.get("address","")))[:50],
            "address":    str(r.get("address","")),
            "city":       str(r.get("city","")),
            "postal":     str(r.get("postal","")),
            "region":     str(r.get("region","")),
            "department": str(r.get("department","")),
            "brand":      brand_lookup.get(str(r.get("provider_id","")), str(r.get("brand",""))),
            "lat":        float(r["latitude"]) if pd.notna(r.get("latitude")) else None,
            "lng":        float(r["longitude"]) if pd.notna(r.get("longitude")) else None,
            **{f: round(float(r[f]),4) if pd.notna(r.get(f)) else None for f in FUELS},
        })

    # Department daily
    dept_daily = []
    dept_col = "department" if "department" in all_data.columns else None
    if dept_col:
        for (dt, dept), g in all_data.groupby(["snapshot_date", dept_col]):
            dept_daily.append({"date": str(dt.date()), "department": str(dept), **fr(g)})
    print(f"[Agg] dept_daily: {len(dept_daily)} entrées")

    # Brand × Region daily
    brand_region_daily = []
    if brand_col and reg_col:
        for (dt, brand, region), g in all_data[all_data[brand_col].isin(top_brands)].groupby(
            ["snapshot_date", brand_col, reg_col]
        ):
            brand_region_daily.append({
                "date": str(dt.date()),
                "brand": str(brand),
                "region": str(region),
                **fr(g)
            })
    print(f"[Agg] brand_region_daily: {len(brand_region_daily)} entrées")

    return {
        "meta": {
            "generated":     datetime.date.today().isoformat(),
            "n_stations":    n_stations_total,
            "n_snapshots":   len(dates_sorted),
            "latest_date":   str(dates_sorted[-1].date()),
            "earliest_date": str(dates_sorted[0].date()),
            "dates":         [str(d.date()) for d in dates_sorted],
            "months":        sorted(all_data["year_month"].unique().tolist()),
            "quarters":      sorted(all_data["quarter"].unique().tolist()),
            "halves":        sorted(all_data["half"].unique().tolist()),
            "years":         sorted([int(y) for y in all_data["year"].unique().tolist()]),
            "regions":       sorted([str(r) for r in all_data[reg_col].dropna().unique()]) if reg_col else [],
            "departments":   sorted([str(d) for d in all_data["department"].dropna().unique()]) if "department" in all_data.columns else [],
            "brands":        top_brands,
        },
        "daily":daily,"weekly":weekly,"monthly":monthly,
        "quarterly":quarterly,"half_yearly":half_yearly,"yearly":yearly,
        "regional_daily":regional_daily,"brand_daily":brand_daily,
        "brand_region_daily":brand_region_daily,
        "dept_daily":dept_daily,
        "stations":stations,
    }


OVERPASS_BRAND_FILE = "Prix carburant - Liste brand - Overpass.csv"
TOTAL_WIKIDATA_ID = "Q154037"  # TotalEnergies wikidata ID - covers Total, TotalEnergies, Total Access, Elan, AS24

def fetch_total_ids():
    """Récupère les IDs stations réseau TotalEnergies depuis le fichier Overpass du repo.
    Filtre par tags/brand contenant 'Total', 'Elan', 'AS24' ou 'Access'."""
    import pandas as _pd

    if not os.path.exists(OVERPASS_BRAND_FILE):
        print(f"[Total] Fichier {OVERPASS_BRAND_FILE} introuvable — fallback brand")
        return set()

    try:
        df = _pd.read_csv(OVERPASS_BRAND_FILE, low_memory=False)
        ref_col   = 'tags/ref:FR:prix-carburants'
        brand_col = 'tags/brand'

        if brand_col not in df.columns:
            print(f"[Total] Colonne {brand_col} introuvable")
            return set()

        # Filter: brand contains Total, Elan, AS24 or Access (case insensitive)
        mask = df[brand_col].astype(str).str.contains(
            r'total|elan|as24|access', case=False, na=False, regex=True
        )

        if ref_col in df.columns:
            ids = set(df[mask][ref_col].dropna().astype(str).str.split('.').str[0].tolist())
        else:
            ids = set()

        ids.discard('')
        ids.discard('nan')
        brands = df[mask][brand_col].value_counts().to_dict() if brand_col in df.columns else {}
        print(f"[Total] {mask.sum()} stations Overpass → {len(ids)} IDs")
        print(f"[Total] Marques trouvées: {dict(list(brands.items())[:10])}")
        return ids
    except Exception as e:
        print(f"[Total] Erreur lecture Overpass: {e}")
        return set()


def _normalize(s):
    if not s or str(s).lower() in ('nan','none',''): return ''
    import re as _re
    s = str(s).upper().strip()
    s = _re.sub(r'[^\w\s]', ' ', s)
    s = _re.sub(r'\s+', ' ', s)
    return s.strip()

def fetch_dkv_ids():
    """Identifie les provider_ids de nos stations qui correspondent aux stations DKV.
    Utilise DKV_stations.csv (No ID, Adresse, Code Postal) et fait un matching
    par code postal + adresse avec nos CSV de stations."""
    import pandas as _pd

    # Supprimer le cache pour forcer rebuild depuis DKV_stations.csv
    if os.path.exists(DKV_IDS_CACHE):
        os.remove(DKV_IDS_CACHE)
        print("[DKV] Cache supprimé — rebuild depuis DKV_stations.csv")

    if not os.path.exists(DKV_STATIONS_FILE):
        print(f"[DKV] Fichier {DKV_STATIONS_FILE} introuvable — fallback brand")
        return set()

    try:
        # Load DKV stations file
        df_dkv = None
        for sep in [',', ';', '\t']:
            try:
                tmp = _pd.read_csv(DKV_STATIONS_FILE, sep=sep,
                                   encoding='utf-8-sig', low_memory=False)
                if len(tmp.columns) >= 3:
                    df_dkv = tmp
                    break
            except Exception:
                continue

        if df_dkv is None:
            print("[DKV] Impossible de lire le fichier")
            return set()

        print(f"[DKV] {len(df_dkv)} stations dans {DKV_STATIONS_FILE}")
        print(f"[DKV] Colonnes: {list(df_dkv.columns[:6])}")

        # Detect columns
        cols = [c.lower().strip() for c in df_dkv.columns]
        addr_col   = df_dkv.columns[[i for i,c in enumerate(cols) if 'adresse' in c or 'address' in c][0]] if any('adresse' in c or 'address' in c for c in cols) else df_dkv.columns[2]
        postal_col = df_dkv.columns[[i for i,c in enumerate(cols) if 'postal' in c or 'cp' in c or 'code' in c][0]] if any('postal' in c or 'cp' in c or 'code' in c for c in cols) else df_dkv.columns[3]

        # Load all our CSV stations for matching
        import glob as _glob
        csv_files = sorted(_glob.glob(os.path.join(CSV_DIR, "stations_avec_prix_*.csv")))
        dfs = []
        for cf in csv_files:
            try:
                tmp = _pd.read_csv(cf, low_memory=False,
                                   usecols=["provider_id","postal","address"])
                dfs.append(tmp)
            except Exception:
                pass

        if not dfs:
            return set()

        our_df = _pd.concat(dfs).drop_duplicates(subset="provider_id")
        our_df['postal_clean'] = our_df['postal'].astype(str).str.split('.').str[0].str.zfill(5)
        our_df['addr_norm'] = our_df['address'].apply(_normalize)
        our_df['provider_clean'] = our_df['provider_id'].astype(str).str.split('.').str[0]

        # Build lookup: postal -> list of {provider_id, addr_norm}
        lookup = {}
        for _, r in our_df.iterrows():
            p = r['postal_clean']
            if p not in lookup:
                lookup[p] = []
            lookup[p].append({'pid': r['provider_clean'], 'addr': r['addr_norm']})

        NOISE = {'DE','DU','LA','LE','LES','DES','EN','AU','A','N','RN','D','L'}

        matched_ids = set()
        no_match = 0

        score_threshold = 0.2  # seuil réduit pour plus de matches
        no_postal = 0

        for _, row in df_dkv.iterrows():
            postal = str(row[postal_col]).split('.')[0].zfill(5)
            dkv_addr = _normalize(row[addr_col])
            dkv_words = set(dkv_addr.split()) - NOISE

            candidates = lookup.get(postal, [])
            if not candidates:
                no_postal += 1
                continue

            best_score, best_pid = 0.0, None
            for c in candidates:
                our_words = set(c['addr'].split()) - NOISE
                if not dkv_words or not our_words:
                    # Si pas d'adresse DKV mais 1 seul candidat → match
                    if len(candidates) == 1:
                        best_pid = c['pid']
                        best_score = 1.0
                    continue
                score = len(dkv_words & our_words) / max(len(dkv_words), len(our_words))
                if score > best_score:
                    best_score, best_pid = score, c['pid']

            if best_score >= score_threshold and best_pid:
                matched_ids.add(best_pid)
            elif len(candidates) == 1:
                # 1 seul candidat dans ce code postal → on le prend
                matched_ids.add(candidates[0]['pid'])
            else:
                no_match += 1

        print(f"[DKV] {len(matched_ids)} provider_ids matchés")
        print(f"[DKV] Sans code postal: {no_postal}, sans match adresse: {no_match}")
        print(f"[DKV] Colonnes utilisées: postal={postal_col}, adresse={addr_col}")

        with open(DKV_IDS_CACHE, "w") as f:
            f.write("\n".join(sorted(matched_ids)))
        return matched_ids

    except Exception as e:
        print(f"[DKV] Erreur: {e}")
        import traceback
        traceback.print_exc()
        return set()


def fetch_dkv_stations(all_stations_df):
    """Alias pour compatibilité."""
    return fetch_dkv_ids()


GEOCODE_CACHE_FILE = "geocode_cache.json"

def _load_geocode_cache():
    if os.path.exists(GEOCODE_CACHE_FILE):
        try:
            with open(GEOCODE_CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_geocode_cache(cache):
    try:
        with open(GEOCODE_CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass

def _geocode(address, postal, city, cache):
    """Géocode une adresse via Nominatim. Retourne (lat, lon) ou (None, None)."""
    key = f"{postal}|{address}|{city}"
    if key in cache:
        return cache[key]
    
    query = ", ".join(filter(None, [address, postal, city, "France"]))
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "fr"},
            headers={"User-Agent": "QiviaCarburants/1.0"},
            timeout=10
        )
        results = r.json()
        if results:
            lat = float(results[0]["lat"])
            lon = float(results[0]["lon"])
            cache[key] = (lat, lon)
            time.sleep(1)  # respecter la limite Nominatim
            return lat, lon
    except Exception:
        pass
    cache[key] = (None, None)
    return None, None


def generate_carte(stations_with_prices, template_path="qivia_carte_template.html", output_path="qivia_carte.html"):
    """Génère qivia_carte.html avec TOUTES les stations ayant des coordonnées.
    Sources:
    - Prix carburant - Liste brand - Overpass.csv : coordonnées + marques pour toutes les stations
    - stations_with_prices : prix du jour
    - DKV_stations.csv : flag réseau DKV
    """
    import json as _json, re as _re, glob as _glob, math as _math
    import pandas as _pd

    if not os.path.exists(template_path):
        print(f"[Carte] Template {template_path} introuvable")
        return

    with open(template_path, encoding="utf-8") as f:
        template = f.read()

    FUELS_FR = {"gazole_prix":"gazole","sp95_prix":"sp95","e10_prix":"e10",
                "sp98_prix":"sp98","e85_prix":"e85","gplc_prix":"gplc"}
    NOISE = {'DE','DU','LA','LE','LES','DES','EN','AU','A','N','RN','D','L'}

    def _clean(v):
        if v is None: return ""
        s = str(v).strip()
        if s.lower() in ("nan","none","nat",""): return ""
        return s.replace("\\", " ").replace('"', "'").replace("</script>","").strip()

    def _normalize(s):
        if not s: return ''
        s = str(s).upper().strip()
        s = _re.sub(r'[^\w\s]', ' ', s)
        s = _re.sub(r'\s+', ' ', s)
        return s.strip()

    def _addr_score(a1, a2):
        w1 = set(_normalize(a1).split()) - NOISE
        w2 = set(_normalize(a2).split()) - NOISE
        if not w1 or not w2: return 0.0
        return len(w1 & w2) / max(len(w1), len(w2))

    # === BUILD PRICE LOOKUP from qivia_data stations ===
    # key: provider_id -> prices dict
    price_lookup = {}
    for s in stations_with_prices:
        pid = _clean(str(s.get("uuid",""))).split(".")[0]
        prices = {FUELS_FR[f]: s[f] for f in list(FUELS_FR.keys()) if s.get(f) is not None}
        if pid and prices:
            price_lookup[pid] = prices

    print(f"[Carte] {len(price_lookup)} stations avec prix")

    # === LOAD OVERPASS FILE (source principale de coordonnées) ===
    all_stations = []
    ref_to_idx = {}  # ref:FR:prix-carburants -> index in all_stations
    postal_lookup = {}  # postal -> list of indices

    if os.path.exists(OVERPASS_BRAND_FILE):
        try:
            df_ov = _pd.read_csv(OVERPASS_BRAND_FILE, low_memory=False)
            print(f"[Carte] Overpass: {len(df_ov)} lignes")

            ref_col   = 'tags/ref:FR:prix-carburants'
            brand_col = 'tags/brand'
            lat_col   = 'lat'
            lon_col   = 'long'
            city_col  = 'tags/addr:city'
            postal_col_ov = 'tags/addr:postcode'
            addr_col_ov = 'tags/addr:street'
            name_col  = 'tags/name'

            # For way elements, use center/lat and center/lon
            geocode_cache = _load_geocode_cache()
            geocoded_count = 0
            for _, row in df_ov.iterrows():
                # 1. Try direct lat/long (nodes)
                lat = row.get(lat_col)
                lon = row.get(lon_col)
                # 2. Try center coords (ways)
                if _pd.isna(lat) or str(lat).strip() in ('', '0', 'nan'):
                    lat = row.get('center/lat')
                    lon = row.get('center/lon')
                # 3. Try to convert to float
                try:
                    lat = float(lat)
                    lon = float(lon)
                    if lat == 0 or lon == 0:
                        raise ValueError
                except (TypeError, ValueError):
                    lat, lon = None, None

                # 4. Fallback: geocode by address if no coords
                if lat is None or lon is None:
                    addr_g = _clean(str(row.get(addr_col_ov, "")))
                    post_g = _clean(str(row.get(postal_col_ov, ""))).split(".")[0].zfill(5)
                    city_g = _clean(str(row.get(city_col, "")))
                    if addr_g or (post_g and city_g):
                        lat, lon = _geocode(addr_g, post_g, city_g, geocode_cache)
                    if lat is None or lon is None:
                        continue

                ref = _clean(str(row.get(ref_col, "")))
                brand = _clean(str(row.get(brand_col, "")))
                name = _clean(str(row.get(name_col, ""))) or brand
                postal = _clean(str(row.get(postal_col_ov, ""))).split(".")[0].zfill(5)
                city = _clean(str(row.get(city_col, "")))
                address = _clean(str(row.get(addr_col_ov, "")))

                prices = price_lookup.get(ref, {})

                station = {
                    "lat":        round(float(lat), 5),
                    "lon":        round(float(lon), 5),
                    "address":    address,
                    "city":       city,
                    "postal":     postal,
                    "region":     "",
                    "department": "",
                    "name":       name,
                    "brand":      brand,
                    "fuels":      ", ".join(prices.keys()),
                    "prices":     prices,
                    "uuid":       ref,
                    "networks":   ["qivia"],
                }
                idx = len(all_stations)
                all_stations.append(station)
                if ref:
                    ref_to_idx[ref] = idx
                if postal:
                    if postal not in postal_lookup:
                        postal_lookup[postal] = []
                    postal_lookup[postal].append(idx)

            _save_geocode_cache(geocode_cache)
            print(f"[Carte] {len(all_stations)} stations depuis Overpass (avec coords)")
        except Exception as e:
            print(f"[Carte] Erreur Overpass: {e}")
            import traceback
            traceback.print_exc()

    # If no Overpass file, fallback to stations_with_prices
    if not all_stations:
        print("[Carte] Fallback: stations depuis CSV gouvernementaux")
        for s in stations_with_prices:
            if not s.get("lat") or not s.get("lng"): continue
            prices = {FUELS_FR[f]: s[f] for f in list(FUELS_FR.keys()) if s.get(f) is not None}
            postal = _clean(str(s.get("postal",""))).split(".")[0].zfill(5)
            station = {
                "lat": s["lat"], "lon": s["lng"],
                "address": _clean(s.get("address","")),
                "city": _clean(s.get("city","")),
                "postal": postal,
                "region": _clean(s.get("region","")),
                "department": _clean(s.get("department","")),
                "name": _clean(s.get("name","")) or _clean(s.get("brand","")),
                "brand": _clean(s.get("brand","")),
                "fuels": ", ".join(prices.keys()),
                "prices": prices,
                "uuid": _clean(s.get("uuid","")),
                "networks": ["qivia"],
            }
            all_stations.append(station)
            postal = station["postal"]
            if postal not in postal_lookup:
                postal_lookup[postal] = []
            postal_lookup[postal].append(len(all_stations)-1)

    # === RÉSEAU DKV ===
    dkv_matched = 0
    dkv_added = 0
    if os.path.exists(DKV_STATIONS_FILE):
        try:
            df_dkv = None
            for sep in [',', ';', '\t']:
                try:
                    tmp = _pd.read_csv(DKV_STATIONS_FILE, sep=sep, encoding='utf-8-sig', low_memory=False)
                    if len(tmp.columns) >= 3:
                        df_dkv = tmp
                        break
                except Exception:
                    continue

            if df_dkv is not None:
                cols = [c.lower().strip() for c in df_dkv.columns]
                addr_col_dkv = df_dkv.columns[[i for i,c in enumerate(cols) if 'adresse' in c or 'address' in c][0]]
                postal_col_dkv = df_dkv.columns[[i for i,c in enumerate(cols) if 'postal' in c or 'cp' in c][0]]
                brand_col_dkv = df_dkv.columns[[i for i,c in enumerate(cols) if 'brand' in c or 'marque' in c or 'enseigne' in c][0]] if any('brand' in c or 'marque' in c or 'enseigne' in c for c in cols) else None
                city_col_dkv = df_dkv.columns[[i for i,c in enumerate(cols) if 'ville' in c or 'city' in c][0]] if any('ville' in c or 'city' in c for c in cols) else None

                geocode_cache = _load_geocode_cache()
                for _, row in df_dkv.iterrows():
                    postal = str(row[postal_col_dkv]).split('.')[0].zfill(5)
                    addr = str(row[addr_col_dkv])
                    candidates = postal_lookup.get(postal, [])

                    best_score, best_i = 0.0, None
                    for i in candidates:
                        score = _addr_score(addr, all_stations[i]["address"])
                        if score > best_score:
                            best_score, best_i = score, i

                    if best_score >= 0.2 and best_i is not None:
                        if 'dkv' not in all_stations[best_i]["networks"]:
                            all_stations[best_i]["networks"].append("dkv")
                            dkv_matched += 1
                    elif len(candidates) == 1:
                        if 'dkv' not in all_stations[candidates[0]]["networks"]:
                            all_stations[candidates[0]]["networks"].append("dkv")
                            dkv_matched += 1
                    else:
                        # Station DKV not in Overpass - geocode it and add to map
                        addr_dkv = str(row[addr_col_dkv])
                        city_dkv = str(row[city_col_dkv]) if city_col_dkv else ""
                        brand_dkv = str(row[brand_col_dkv]) if brand_col_dkv else ""
                        lat_dkv, lon_dkv = _geocode(addr_dkv, postal, city_dkv, geocode_cache)
                        if lat_dkv and lon_dkv:
                            new_station = {
                                "lat":        round(lat_dkv, 5),
                                "lon":        round(lon_dkv, 5),
                                "address":    _clean(addr_dkv),
                                "city":       _clean(city_dkv),
                                "postal":     postal,
                                "region":     "",
                                "department": "",
                                "name":       _clean(brand_dkv),
                                "brand":      _clean(brand_dkv),
                                "fuels":      "",
                                "prices":     {},
                                "uuid":       "",
                                "networks":   ["qivia", "dkv"],
                            }
                            new_idx = len(all_stations)
                            all_stations.append(new_station)
                            if postal not in postal_lookup:
                                postal_lookup[postal] = []
                            postal_lookup[postal].append(new_idx)
                            dkv_added += 1

                _save_geocode_cache(geocode_cache)
                print(f"[Carte] DKV: {dkv_matched} stations matchées, {dkv_added} géocodées et ajoutées")
        except Exception as e:
            print(f"[Carte] Erreur DKV: {e}")
    else:
        print(f"[Carte] {DKV_STATIONS_FILE} introuvable — réseau DKV vide")

    # === RÉSEAU TOTALENERGIES ===
    total_ids = fetch_total_ids()
    TOTAL_BRANDS_RE = _re.compile(r'total|elan|as24|access', _re.IGNORECASE)
    total_matched = 0
    for s in all_stations:
        uid = str(s.get("uuid","")).split(".")[0].strip()
        brand_val = s.get("brand","")
        if uid in total_ids or (brand_val and TOTAL_BRANDS_RE.search(brand_val)):
            if 'total' not in s["networks"]:
                s["networks"].append("total")
                total_matched += 1
    print(f"[Carte] TotalEnergies: {total_matched} stations identifiées")

    # === ENRICHIR AVEC REGION/DEPARTMENT depuis CSV gouvernementaux ===
    # Build ref -> region/dept lookup
    region_lookup = {}
    for s in stations_with_prices:
        pid = _clean(str(s.get("uuid",""))).split(".")[0]
        if pid:
            region_lookup[pid] = {
                "region": _clean(s.get("region","")),
                "department": _clean(s.get("department","")),
            }
    for s in all_stations:
        uid = s.get("uuid","")
        if uid in region_lookup:
            s["region"] = region_lookup[uid]["region"]
            s["department"] = region_lookup[uid]["department"]

    # === INJECTION ===
    all_js = _json.dumps(all_stations, ensure_ascii=True, separators=(",",":"))

    try:
        _json.loads(all_js)
    except Exception as e:
        print(f"[Carte] ERREUR JSON: {e} — carte non générée")
        return

    data_block = (
        f"const ALL_STATIONS = {all_js};\n"
        f"const QIVIA_DATA = ALL_STATIONS;\n"
        f"const DKV_DATA = ALL_STATIONS.filter(s => s.networks && s.networks.includes('dkv'));\n"
        f"const TOTAL_DATA = ALL_STATIONS.filter(s => s.networks && s.networks.includes('total'));"
    )

    placeholder = "/* ###QIVIA_DATA_START### */\n/* ###QIVIA_DATA_END### */"
    html = template.replace(placeholder, data_block)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    qivia_n = len(all_stations)
    dkv_n = sum(1 for s in all_stations if 'dkv' in s["networks"])
    total_n = sum(1 for s in all_stations if 'total' in s["networks"])
    print(f"[Carte] {output_path} — Qivia={qivia_n}, DKV={dkv_n}, Total={total_n} ({len(html)//1024} Ko)")


def main():
    print(f"\n{'='*55}")
    print("  QIVIA — Fetch & Build nightly")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*55}\n")

    brands_df = load_brands()
    rows      = fetch_api()
    df        = build_dataframe(rows, brands_df)
    save_csv(df)

    print("[JSON] Calcul des agrégats…")
    data = build_aggregates(df)
    with open("qivia_data.json","w",encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, separators=(",",":"))
    print(f"[JSON] qivia_data.json ({os.path.getsize('qivia_data.json')//1024} Ko)")

    # Pré-calculer les stations DKV
    fetch_dkv_ids()
    # Regénérer la carte avec les prix à jour
    generate_carte(data["stations"])

    print(f"\n[OK] Terminé !")
    print(f"     Régions : {data['meta']['regions'][:3]}…")
    print(f"     Marques : {data['meta']['brands'][:5]}\n")


if __name__ == "__main__":
    main()

