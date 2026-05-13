"""
fetch_and_build.py — QIVIA
Récupère les prix depuis l'API gouvernementale,
joint avec Prix carburant - Liste brand - Overpass.csv pour les marques,
génère le CSV du jour et qivia_data.json.
"""

import json, os, time, datetime, requests, pandas as pd, glob, re

DKV_PDF_URL = (
    "https://my.dkv-mobility.com/apidnext/geo-static-content-service/v1/"
    "station-network,pdf,FR,en/DKV_FR_sortByAddr_en.pdf"
    "?_gl=1*ad0ek3*_gcl_au*MTc0NzQwODIwMy4xNzc4MTQ1NDE5*FPAU*MTc0NzQwODIwMy4xNzc4MTQ1NDE5"
)
DKV_IDS_CACHE = "dkv_ids_cache.txt"

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


def fetch_dkv_ids():
    """Télécharge le PDF DKV et extrait tous les numéros de stations."""
    import io

    # Use cache if less than 7 days old
    if os.path.exists(DKV_IDS_CACHE):
        age = (datetime.datetime.now().timestamp() - os.path.getmtime(DKV_IDS_CACHE)) / 86400
        if age < 7:
            with open(DKV_IDS_CACHE) as f:
                ids = set(f.read().splitlines())
            ids.discard("")
            print(f"[DKV] {len(ids)} IDs depuis cache ({age:.1f}j)")
            return ids

    print("[DKV] Téléchargement du PDF DKV...")
    try:
        r = requests.get(DKV_PDF_URL, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        print(f"[DKV] PDF téléchargé ({len(r.content)//1024} Ko)")

        # Try with pdfplumber first (best for structured PDFs)
        ids = set()
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(r.content)) as pdf:
                print(f"[DKV] {len(pdf.pages)} pages")
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    found = re.findall(r'DKV No[: ]*([0-9]{6,8})', text)
                    ids.update(found)
            print(f"[DKV] {len(ids)} IDs extraits via pdfplumber")
        except ImportError:
            pass

        # Fallback: raw binary search
        if not ids:
            text = r.content.decode("latin-1", errors="ignore")
            ids = set(re.findall(r'DKV No[: ]*([0-9]{6,8})', text))
            print(f"[DKV] {len(ids)} IDs extraits via raw text")

        if ids:
            with open(DKV_IDS_CACHE, "w") as f:
                f.write("\n".join(sorted(ids)))
            print(f"[DKV] Cache sauvegardé: {len(ids)} IDs")
        return ids

    except Exception as e:
        print(f"[DKV] Erreur: {e}")
        if os.path.exists(DKV_IDS_CACHE):
            with open(DKV_IDS_CACHE) as f:
                ids = set(f.read().splitlines())
            ids.discard("")
            print(f"[DKV] Fallback cache: {len(ids)} IDs")
            return ids
        return set()


def generate_carte(stations, template_path="qivia_carte_template.html", output_path="qivia_carte.html"):
    """Regénère qivia_carte.html avec les données stations à jour."""
    import json as _json

    if not os.path.exists(template_path):
        print(f"[Carte] Template {template_path} introuvable — carte non regénérée")
        return

    with open(template_path, encoding="utf-8") as f:
        template = f.read()

    FUELS_FR = {
        "gazole_prix": "Gazole", "sp95_prix": "SP95", "e10_prix": "E10",
        "sp98_prix": "SP98",    "e85_prix": "E85",   "gplc_prix": "GPLc"
    }
    FUELS = list(FUELS_FR.keys())

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
            "address":    s.get("address", ""),
            "city":       s.get("city", ""),
            "postal":     s.get("postal", ""),
            "region":     s.get("region", ""),
            "department": s.get("department", ""),
            "name":       s.get("name", ""),
            "brand":      s.get("brand", ""),
            "fuels":      fuels_str,
            "prices":     prices,
            "uuid":       s.get("uuid", ""),
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

    # DKV stations - match provider_id (uuid field) against DKV IDs
    dkv_ids = fetch_dkv_ids()
    if dkv_ids:
        def _dkv_match(s):
            uid = str(s.get("uuid","")).split(".")[0].strip()
            return uid in dkv_ids
        dkv_stations = [s for s in carte_stations if _dkv_match(s)]
        print(f"[DKV] {len(dkv_stations)} stations DKV identifiées")
        if len(dkv_stations) < 100:
            print("[DKV] Trop peu de stations — fallback toutes stations")
            dkv_stations = carte_stations
    else:
        dkv_stations = carte_stations  # fallback

    qivia_js  = _json.dumps(carte_stations, ensure_ascii=False, separators=(",",":"))
    dkv_js    = _json.dumps(dkv_stations,   ensure_ascii=False, separators=(",",":"))
    total_js  = _json.dumps(total_stations, ensure_ascii=False, separators=(",",":"))

    data_block = (
        f"const QIVIA_DATA = {qivia_js};\n"
        f"const DKV_DATA = {dkv_js};\n"
        f"const TOTAL_DATA = {total_js};"
    )

    html = template.replace(
        "/* ###QIVIA_DATA_START### */\n/* ###QIVIA_DATA_END### */",
        data_block
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[Carte] {output_path} regénéré — {len(carte_stations)} stations ({len(html)//1024} Ko)")

EXCEL_FILE  = "Stations_essences.xlsx"
GEOJSON_OUT = "stations_lite.geojson"
FUELS_GEO   = ["gazole_prix","sp95_prix","e10_prix","sp98_prix","e85_prix","gplc_prix"]

def _normalize(s):
    import unicodedata as _ud
    if not s: return ''
    s = str(s).lower().strip()
    s = _ud.normalize('NFD', s)
    s = ''.join(c for c in s if _ud.category(c) != 'Mn')
    for pat, repl in {r'\bav\b':'avenue',r'\bbd\b':'boulevard',r'\brt\b':'route',
                      r'\bst\b':'saint',r'\bste\b':'sainte'}.items():
        s = re.sub(pat, repl, s)
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9\s]', ' ', s)).strip()

def _token_score(a, b):
    ta = set(t for t in a.split() if len(t) > 2)
    tb = set(t for t in b.split() if len(t) > 2)
    if not ta or not tb: return 0
    return len(ta & tb) / max(len(ta), len(tb))

def generate_geojson(api_df, output_path=GEOJSON_OUT):
    """
    Génère stations_lite.geojson :
      1. Stations API enrichies par Stations_essences.xlsx (ID match + adresse match DKV)
      2. Stations DKV géocodées conservées depuis la version précédente (DKV_SOURCE_)

    Colonnes DataFrame -> GeoJSON :
      provider_id -> id | address -> adresse | city -> ville
      postal -> cp | department -> departement | region -> region
    """
    print("[GeoJSON] Génération de stations_lite.geojson…")

    # ── Chargement Excel ──────────────────────────────────────────────────────
    xl_lookup   = {}
    xl_dkv_rows = []
    if os.path.exists(EXCEL_FILE):
        xl = pd.read_excel(EXCEL_FILE)
        xl['ID'] = xl['ID'].astype(str).str.strip()
        xl = xl.drop_duplicates('ID', keep='first')
        xl_lookup = xl.set_index('ID').to_dict('index')
        # IDs gouvernementaux = provider_id sans décimale
        gov_ids = set(str(int(float(x))) for x in api_df['provider_id'].dropna())
        xl_dkv_rows = xl[(xl['DKV']==1) & (~xl['ID'].isin(gov_ids))].to_dict('records')
    else:
        print(f"  ⚠ {EXCEL_FILE} introuvable — enrichissement désactivé")

    # ── Index API par CP pour matching adresse DKV ────────────────────────────
    geo_by_cp = {}
    for _, row in api_df.iterrows():
        cp = str(row.get('postal','')).split('.')[0]
        geo_by_cp.setdefault(cp, []).append((_normalize(str(row.get('address',''))), row))

    # ── Étape 1 : stations API + enrichissement Excel ──────────────────────────
    features   = []
    marked_dkv = set()

    for _, row in api_df.iterrows():
        try:
            sid = str(int(float(row.get('provider_id', 0))))
        except (TypeError, ValueError):
            sid = str(row.get('provider_id',''))

        xl_r = xl_lookup.get(sid, {})

        # Mapping colonnes DataFrame -> propriétés GeoJSON
        def _val(v):
            return None if (v is None or (isinstance(v, float) and pd.isna(v))) else v

        props = {
            'id':          sid,
            'adresse':     _val(row.get('address')),
            'ville':       _val(row.get('city')),
            'cp':          str(row.get('postal','')).split('.')[0],
            'departement': _val(row.get('department')),
            'region':      _val(row.get('region')),
        }
        for f in FUELS_GEO:
            props[f] = _val(row.get(f))

        props['brand']          = str(xl_r.get('Brand', row.get('brand','Autre')) or 'Autre')
        props['dkv']            = int(xl_r.get('DKV', 0) or 0)
        props['edenred']        = int(xl_r.get('Edenred', xl_r.get('edenred', 0)) or 0)
        props['total_energies'] = int(xl_r.get('TotalEnergies', 0) or 0)
        props['nom']            = str(xl_r.get('Nom','') or '')
        if props['dkv']: marked_dkv.add(sid)

        try:
            lat = float(row.get('latitude') or 0)
            lon = float(row.get('longitude') or 0)
        except (TypeError, ValueError):
            continue
        if not lat or not lon:
            continue

        features.append({"type":"Feature",
            "geometry": {"type":"Point","coordinates":[lon, lat]},
            "properties": props})

    print(f"  Étape 1 — API + Excel : {len(features)} stations")

    # ── Marquer DKV par matching adresse ──────────────────────────────────────
    addr_marked = 0
    for xl_r in xl_dkv_rows:
        cp         = str(xl_r.get('Code postale','')).split('.')[0]
        addr_excel = _normalize(str(xl_r.get('Adresse','')))
        candidates = geo_by_cp.get(cp, [])
        best_score, best_sid = 0, None
        for addr_geo, api_row in candidates:
            sc = _token_score(addr_excel, addr_geo)
            if sc > best_score:
                try:
                    best_sid = str(int(float(api_row.get('provider_id', 0))))
                except (TypeError, ValueError):
                    best_sid = str(api_row.get('provider_id',''))
                best_score = sc
        if best_score >= 0.5 and best_sid and best_sid not in marked_dkv:
            for f in features:
                if str(f['properties'].get('id','')) == best_sid:
                    f['properties']['dkv'] = 1
                    marked_dkv.add(best_sid)
                    addr_marked += 1
                    break

    print(f"  Étape 2 — DKV adresse : {addr_marked} stations mises à jour")

    # ── Étape 3 : conserver les stations DKV géocodées (DKV_SOURCE_) ──────────
    geocoded_kept = 0
    if os.path.exists(output_path):
        with open(output_path, encoding='utf-8') as f:
            old_gj = json.load(f)
        for feat in old_gj.get('features', []):
            old_id = str(feat['properties'].get('id',''))
            if old_id.startswith('DKV_SOURCE_'):
                features.append(feat)
                geocoded_kept += 1

    print(f"  Étape 3 — DKV géocodées conservées : {geocoded_kept} stations")

    # ── Étape 4 : stations Excel avec coordonnées hors API ────────────────────
    api_ids_in_features = {str(f['properties'].get('id','')) for f in features}
    excel_added = 0
    if os.path.exists(EXCEL_FILE):
        xl_full = pd.read_excel(EXCEL_FILE)
        xl_full['ID'] = xl_full['ID'].astype(str).str.strip()
        xl_coords = xl_full[xl_full['Latitude'].notna() & xl_full['Longitude'].notna()]
        xl_hors_api = xl_coords[~xl_coords['ID'].isin(api_ids_in_features)]
        for _, xl_r in xl_hors_api.iterrows():
            try:
                lat = float(xl_r['Latitude']) / 100000
                lon = float(xl_r['Longitude']) / 100000
            except (TypeError, ValueError):
                continue
            if not (-90 < lat < 90) or not (-180 < lon < 180):
                continue
            props = {
                'id':          str(xl_r['ID']),
                'adresse':     str(xl_r.get('Adresse', '') or ''),
                'ville':       str(xl_r.get('Ville', '') or ''),
                'cp':          str(xl_r.get('Code postale', '') or '').split('.')[0],
                'departement': str(xl_r.get('Départements', '') or ''),
                'region':      str(xl_r.get('Région', '') or ''),
                'brand':       str(xl_r.get('Brand', 'Autre') or 'Autre'),
                'dkv':         int(xl_r.get('DKV', 0) or 0),
                'edenred':     int(xl_r.get('Edenred', xl_r.get('edenred', 0)) or 0),
                'total_energies': int(xl_r.get('TotalEnergies', 0) or 0),
                'nom':         str(xl_r.get('Nom', '') or ''),
            }
            for f in FUELS_GEO:
                props[f] = None
            features.append({"type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props})
            excel_added += 1

    print(f"  Étape 4 — Excel coords hors API : {excel_added} stations ajoutées")

    # ── Export ────────────────────────────────────────────────────────────────
    gj = {"type":"FeatureCollection","features":features}
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(gj, f, ensure_ascii=False, separators=(',',':'))

    total = len(features)
    dkv_t = sum(1 for f in features if f['properties'].get('dkv')==1)
    size  = os.path.getsize(output_path) // 1024
    print(f"  ✅ {output_path} — {total} stations (DKV: {dkv_t}) — {size} Ko")
    return output_path


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

    # Générer le GeoJSON enrichi (prix à jour + Excel + DKV géocodées)
    generate_geojson(df)

    print(f"\n[OK] Terminé !")
    print(f"     Régions : {data['meta']['regions'][:3]}…")
    print(f"     Marques : {data['meta']['brands'][:5]}\n")


if __name__ == "__main__":
    main()

