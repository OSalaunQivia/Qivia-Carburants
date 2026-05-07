"""
fetch_and_build.py — QIVIA
Récupère les prix depuis l'API gouvernementale,
joint avec Prix carburant - Liste brand - Overpass.csv pour les marques,
génère le CSV du jour et qivia_data.json.
"""

import json, os, time, datetime, requests, pandas as pd, glob

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

    latest = all_data[all_data["snapshot_date"]==dates_sorted[-1]]
    n_stations_total = int(latest["provider_id"].nunique()) if "provider_id" in latest else len(latest)
    stations = []
    for _,r in latest.iterrows():
        if all(pd.isna(r.get(f)) for f in FUELS): continue
        stations.append({
            "uuid":       str(r.get("provider_id","")),
            "name":       str(r.get("name",r.get("address","")))[:50],
            "city":       str(r.get("city","")),
            "region":     str(r.get("region","")),
            "department": str(r.get("department","")),
            "brand":      str(r.get("brand","")),
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
    print(f"\n[OK] Terminé !")
    print(f"     Régions : {data['meta']['regions'][:3]}…")
    print(f"     Marques : {data['meta']['brands'][:5]}\n")


if __name__ == "__main__":
    main()
