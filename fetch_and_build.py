"""
fetch_and_build.py
==================
Script lancé chaque nuit par GitHub Actions :
1. Récupère tous les prix depuis l'API gouvernementale
2. Génère le CSV du jour
3. Génère qivia_data.json pour le dashboard
4. Upload le CSV sur Google Drive

Usage local :
    python fetch_and_build.py

Variables d'environnement requises (GitHub Secrets) :
    GDRIVE_CREDENTIALS  → contenu JSON du compte de service Google
    GDRIVE_FOLDER_ID    → ID du dossier Google Drive
"""

import json
import os
import sys
import time
import datetime
import requests
import pandas as pd

# ── CONFIG ──────────────────────────────────────────────────────────────────
API_URL = (
    "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets"
    "/prix-des-carburants-en-france-flux-instantane-v2/records"
)
API_FIELDS = [
    "id", "adresse", "cp", "ville",
    "gazole_prix", "gazole_maj",
    "sp95_prix",   "sp95_maj",
    "e10_prix",    "e10_maj",
    "sp98_prix",   "sp98_maj",
    "e85_prix",    "e85_maj",
    "gplc_prix",   "gplc_maj",
]
FUELS      = ["gazole_prix","sp95_prix","e10_prix","sp98_prix","e85_prix","gplc_prix"]
FUEL_LABELS = {"gazole_prix":"Gazole","sp95_prix":"SP95","e10_prix":"E10",
               "sp98_prix":"SP98","e85_prix":"E85","gplc_prix":"GPLc"}
LIMIT      = 100
MAX_PAGES  = 200          # sécurité : 200 × 100 = 20 000 stations max
TOP_BRANDS = 12
OUTPUT_DIR = "."
# ────────────────────────────────────────────────────────────────────────────


def fetch_api():
    """Récupère toutes les stations depuis l'API gouvernementale."""
    rows, offset = [], 0
    print(f"[API] Récupération des données…")
    while True:
        params = {
            "select": ",".join(API_FIELDS),
            "limit":  LIMIT,
            "offset": offset,
        }
        for attempt in range(3):
            try:
                r = requests.get(API_URL, params=params, timeout=30)
                r.raise_for_status()
                break
            except Exception as e:
                if attempt == 2:
                    raise
                print(f"  Retry {attempt+1}…")
                time.sleep(3)

        batch = r.json().get("results", [])
        if not batch:
            break
        rows.extend(batch)
        offset += LIMIT
        if offset % 1000 == 0:
            print(f"  {offset} stations récupérées…")
        if offset >= MAX_PAGES * LIMIT:
            print(f"  Limite de sécurité atteinte ({MAX_PAGES * LIMIT})")
            break

    print(f"[API] {len(rows)} stations récupérées au total")
    return rows


def build_dataframe(rows):
    """Construit et nettoie le DataFrame."""
    df = pd.DataFrame(rows)

    # Dates
    date_cols = [c for c in df.columns if c.endswith("_maj")]
    for c in date_cols:
        df[c] = pd.to_datetime(df[c], errors="coerce", utc=True)

    # Prix
    for f in FUELS:
        if f in df.columns:
            df[f] = pd.to_numeric(df[f], errors="coerce")

    # Renommer colonnes API → colonnes dashboard
    rename = {
        "id":      "provider_id",
        "adresse": "address",
        "cp":      "postal",
        "ville":   "city",
    }
    df = df.rename(columns=rename)
    df["snapshot_date"] = datetime.date.today().isoformat()
    return df


def save_csv(df, output_dir):
    today = datetime.date.today().strftime("%Y-%m-%d")
    path  = os.path.join(output_dir, f"stations_avec_prix_{today}.csv")
    df.to_csv(path, index=False)
    print(f"[CSV] {path} ({len(df)} lignes)")
    return path


def safe_mean(s):
    v = s.dropna()
    return round(float(v.mean()), 4) if len(v) > 0 else None


def avg(arr):
    v = [x for x in arr if x is not None]
    return sum(v) / len(v) if v else None


def build_aggregates(df, existing_csvs_dir=None):
    """
    Charge tous les CSV disponibles (historique) et construit
    les agrégats pour le dashboard.
    """
    import glob

    # Charger l'historique complet
    pattern = os.path.join(existing_csvs_dir or OUTPUT_DIR, "stations_avec_prix_*.csv")
    files   = sorted(glob.glob(pattern))
    if not files:
        # Fallback : utiliser uniquement le df courant
        all_data = df.copy()
        all_data["snapshot_date"] = pd.to_datetime(all_data["snapshot_date"])
    else:
        dfs = []
        for f in files:
            date_str = os.path.basename(f).replace("stations_avec_prix_","").replace(".csv","")
            try:
                tmp = pd.read_csv(f, low_memory=False)
                tmp["snapshot_date"] = pd.to_datetime(date_str, format="mixed")
                dfs.append(tmp)
            except Exception as e:
                print(f"  ✗ {f} ignoré : {e}")
        all_data = pd.concat(dfs, ignore_index=True) if dfs else df.copy()

    all_data["year_month"] = all_data["snapshot_date"].dt.strftime("%Y-%m")
    all_data["week"]       = all_data["snapshot_date"].dt.isocalendar().week.astype(int)
    all_data["year"]       = all_data["snapshot_date"].dt.year
    all_data["quarter"]    = all_data["snapshot_date"].dt.to_period("Q").astype(str)
    all_data["half"]       = all_data["snapshot_date"].apply(
        lambda d: f"{d.year}-H{'1' if d.month<=6 else '2'}"
    )

    dates_sorted = sorted(all_data["snapshot_date"].unique())

    def fuel_row(g):
        return {f: safe_mean(g[f]) for f in FUELS if f in g.columns}

    # Daily
    daily = []
    for dt, g in all_data.groupby("snapshot_date"):
        row = {"date": str(dt.date()), **fuel_row(g)}
        daily.append(row)

    # Weekly
    weekly = []
    for (ym, wk), g in all_data.groupby(["year_month","week"]):
        dates_in = sorted(g["snapshot_date"].dt.date.unique())
        row = {"period": f"S{wk}", "year_month": ym, "week": int(wk),
               "date_min": str(dates_in[0]), "date_max": str(dates_in[-1]),
               **fuel_row(g)}
        weekly.append(row)

    # Monthly
    monthly = [{"period": ym, **fuel_row(g)}
               for ym, g in all_data.groupby("year_month")]

    # Quarterly
    quarterly = [{"period": q, **fuel_row(g)}
                 for q, g in all_data.groupby("quarter")]

    # Half-yearly
    half_yearly = [{"period": h, **fuel_row(g)}
                   for h, g in all_data.groupby("half")]

    # Yearly
    yearly = [{"period": str(y), **fuel_row(g)}
              for y, g in all_data.groupby("year")]

    # Regional daily
    regional_daily = []
    reg_col = "region" if "region" in all_data.columns else None
    if reg_col:
        for (dt, region), g in all_data.groupby(["snapshot_date", reg_col]):
            regional_daily.append({"date": str(dt.date()), "region": region, **fuel_row(g)})

    # Brand daily
    brand_daily = []
    brand_col = "brand" if "brand" in all_data.columns else None
    top_brands = []
    if brand_col:
        top_brands = all_data[brand_col].value_counts().head(TOP_BRANDS).index.tolist()
        for (dt, brand), g in all_data[all_data[brand_col].isin(top_brands)].groupby(
            ["snapshot_date", brand_col]
        ):
            brand_daily.append({"date": str(dt.date()), "brand": brand, **fuel_row(g)})

    # Stations (latest snapshot)
    latest_date = dates_sorted[-1]
    latest = all_data[all_data["snapshot_date"] == latest_date]
    stations = []
    for _, r in latest.iterrows():
        if all(pd.isna(r.get(f)) for f in FUELS):
            continue
        stations.append({
            "uuid":   str(r.get("uuid", r.get("provider_id",""))),
            "name":   str(r.get("name", r.get("address","")))[:50],
            "city":   str(r.get("city","")),
            "region": str(r.get("region","")),
            "brand":  str(r.get("brand","")),
            **{f: round(float(r[f]),4) if pd.notna(r.get(f)) else None for f in FUELS},
        })

    return {
        "meta": {
            "generated":     datetime.date.today().isoformat(),
            "n_stations":    int(all_data["provider_id"].nunique()) if "provider_id" in all_data else len(latest),
            "n_snapshots":   len(dates_sorted),
            "latest_date":   str(dates_sorted[-1].date()),
            "earliest_date": str(dates_sorted[0].date()),
            "dates":         [str(d.date()) for d in dates_sorted],
            "months":        sorted(all_data["year_month"].unique().tolist()),
            "quarters":      sorted(all_data["quarter"].unique().tolist()),
            "halves":        sorted(all_data["half"].unique().tolist()),
            "years":         sorted([int(y) for y in all_data["year"].unique().tolist()]),
            "regions":       sorted(all_data[reg_col].dropna().unique().tolist()) if reg_col else [],
            "brands":        top_brands,
        },
        "daily":          daily,
        "weekly":         weekly,
        "monthly":        monthly,
        "quarterly":      quarterly,
        "half_yearly":    half_yearly,
        "yearly":         yearly,
        "regional_daily": regional_daily,
        "brand_daily":    brand_daily,
        "stations":       stations,
    }


def upload_to_gdrive(file_path):
    """Upload un fichier sur Google Drive via un compte de service."""
    creds_json = os.environ.get("GDRIVE_CREDENTIALS")
    folder_id  = os.environ.get("GDRIVE_FOLDER_ID")

    if not creds_json or not folder_id:
        print("[Drive] Variables GDRIVE_CREDENTIALS / GDRIVE_FOLDER_ID manquantes — upload ignoré")
        return

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        creds = service_account.Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/drive.file"]
        )
        service  = build("drive", "v3", credentials=creds)
        filename = os.path.basename(file_path)

        # Vérifier si le fichier existe déjà (pour le mettre à jour)
        existing = service.files().list(
            q=f"name='{filename}' and '{folder_id}' in parents and trashed=false",
            fields="files(id,name)"
        ).execute().get("files", [])

        media = MediaFileUpload(file_path, mimetype="text/csv")
        if existing:
            service.files().update(fileId=existing[0]["id"], media_body=media).execute()
            print(f"[Drive] {filename} mis à jour")
        else:
            service.files().create(
                body={"name": filename, "parents": [folder_id]},
                media_body=media
            ).execute()
            print(f"[Drive] {filename} uploadé")

    except Exception as e:
        print(f"[Drive] Erreur upload : {e}")
        # On ne fait pas planter le script pour ça


def main():
    print(f"\n{'='*55}")
    print("  QIVIA — Fetch & Build nightly")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*55}\n")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 1. Récupérer l'API
    rows = fetch_api()
    df   = build_dataframe(rows)

    # 2. Sauvegarder le CSV
    csv_path = save_csv(df, OUTPUT_DIR)

    # 3. Upload Google Drive
    upload_to_gdrive(csv_path)

    # 4. Générer qivia_data.json
    print("[JSON] Calcul des agrégats…")
    data = build_aggregates(df, existing_csvs_dir=OUTPUT_DIR)
    json_path = os.path.join(OUTPUT_DIR, "qivia_data.json")
    with open(json_path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, separators=(",", ":"))
    print(f"[JSON] {json_path} ({os.path.getsize(json_path)//1024} Ko)")
    print(f"\n[OK] Terminé — {data['meta']['n_snapshots']} snapshots, {data['meta']['n_stations']} stations\n")


if __name__ == "__main__":
    main()
