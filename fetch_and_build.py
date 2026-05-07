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
    Filtre par wikidata Q154037 (TotalEnergies) pour capturer toutes les sous-marques."""
    import pandas as _pd

    if not os.path.exists(OVERPASS_BRAND_FILE):
        print(f"[Total] Fichier {OVERPASS_BRAND_FILE} introuvable — fallback brand")
        return set()

    try:
        df = _pd.read_csv(OVERPASS_BRAND_FILE, low_memory=False)
        ref_col   = 'tags/ref:FR:prix-carburants'
        wiki_col  = 'tags/brand:wikidata'
        brand_col = 'tags/brand'

        # Filter by wikidata ID Q154037 (all TotalEnergies brands)
        if wiki_col in df.columns:
            mask = df[wiki_col].astype(str).str.strip() == TOTAL_WIKIDATA_ID
        else:
            TOTAL_BRANDS_OSM = {'total', 'totalenergies', 'total access',
                                 'total express', 'elan', 'as24', 'access'}
            mask = df[brand_col].str.lower().str.strip().isin(TOTAL_BRANDS_OSM)

        if ref_col in df.columns:
            ids = set(df[mask][ref_col].dropna().astype(str).str.split('.').str[0].tolist())
        else:
            ids = set()

        ids.discard('')
        ids.discard('nan')
        brands = df[mask][brand_col].value_counts().to_dict() if brand_col in df.columns else {}
        print(f"[Total] {mask.sum()} stations OSM (wikidata {TOTAL_WIKIDATA_ID}) → {len(ids)} IDs")
        print(f"[Total] Marques: {brands}")
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


def generate_carte(stations, template_path="qivia_carte_template.html", output_path="qivia_carte.html"):
    """Regénère qivia_carte.html avec les données stations à jour."""
    import json as _json

    if not os.path.exists(template_path):
        print(f"[Carte] Template {template_path} introuvable — carte non regénérée")
        return

    with open(template_path, encoding="utf-8") as f:
        template = f.read()

    FUELS_FR = {
        "gazole_prix": "gazole", "sp95_prix": "sp95", "e10_prix": "e10",
        "sp98_prix": "sp98",    "e85_prix": "e85",   "gplc_prix": "gplc"
    }
    FUELS = list(FUELS_FR.keys())

    def _clean(v):
        """Nettoie une valeur pour JSON/JS: supprime nan, contrôle les caractères."""
        if v is None: return ""
        s = str(v).strip()
        if s.lower() in ("nan", "none", "nat"): return ""
        # Remove JS/JSON-breaking characters
        s = s.replace("\\", " ")   # backslashes
        s = s.replace('"', "'")      # double quotes -> single quotes
        s = s.replace("</script>", "")
        s = s.replace("\r", " ").replace("\n", " ")
        return s.strip()

    # Build station objects in carte format
    carte_stations = []
    for s in stations:
        if not s.get("lat") or not s.get("lng"):
            continue
        prices = {FUELS_FR[f]: s[f] for f in FUELS if s.get(f) is not None}
        fuels_str = ", ".join(prices.keys())
        carte_stations.append({
            "lat":        s["lat"],
            "lon":        s["lng"],
            "address":    _clean(s.get("address", "")),
            "city":       _clean(s.get("city", "")),
            "postal":     _clean(s.get("postal", "")),
            "region":     _clean(s.get("region", "")),
            "department": _clean(s.get("department", "")),
            "name":       _clean(s.get("name", "")),
            "brand":      _clean(s.get("brand", "")),
            "fuels":      fuels_str,
            "prices":     prices,
            "uuid":       _clean(s.get("uuid", "")),
        })

    # TotalEnergies network via Overpass OSM
    total_ids = fetch_total_ids()
    TOTAL_BRANDS = {'totalenergies', 'total', 'total access', 'total express',
                    'access', 'elan', 'as24'}
    if total_ids and len(total_ids) > 500:
        def _total_match(s):
            uid = str(s.get("uuid","")).split(".")[0].strip()
            return uid in total_ids or (s.get("brand","")).lower().strip() in TOTAL_BRANDS
        total_stations = [s for s in carte_stations if _total_match(s)]
        print(f"[Total] {len(total_stations)} stations réseau TotalEnergies (OSM+brand)")
    else:
        # Fallback: brand filter only
        total_stations = [s for s in carte_stations
                          if (s.get("brand","")).lower().strip() in TOTAL_BRANDS]
        print(f"[Total] {len(total_stations)} stations réseau TotalEnergies (brand fallback)")

    # DKV stations - UNIQUEMENT depuis DKV_stations.csv via matching adresse
    dkv_ids = fetch_dkv_ids()
    if dkv_ids:
        dkv_stations = [s for s in carte_stations
                        if str(s.get("uuid","")).split(".")[0].strip() in dkv_ids]
        print(f"[DKV] {len(dkv_stations)} stations DKV (depuis DKV_stations.csv)")
    else:
        dkv_stations = []
        print("[DKV] DKV_stations.csv introuvable — DKV_DATA vide")

    qivia_js  = _json.dumps(carte_stations, ensure_ascii=True, separators=(",",":"))
    dkv_js    = _json.dumps(dkv_stations,   ensure_ascii=True, separators=(",",":"))
    total_js  = _json.dumps(total_stations, ensure_ascii=True, separators=(",",":"))

    data_block = (
        f"const QIVIA_DATA = {qivia_js};\n"
        f"const DKV_DATA = {dkv_js};\n"
        f"const TOTAL_DATA = {total_js};"
    )

    # Validate JSON before writing
    try:
        _json.loads(qivia_js)
        _json.loads(dkv_js)
        _json.loads(total_js)
        print("[Carte] JSON validé ✓")
    except Exception as e:
        print(f"[Carte] ERREUR JSON: {e} — carte non regénérée")
        return

    html = template.replace(
        "/* ###QIVIA_DATA_START### */\n/* ###QIVIA_DATA_END### */",
        data_block
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[Carte] {output_path} regénéré — {len(carte_stations)} stations ({len(html)//1024} Ko)")

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

