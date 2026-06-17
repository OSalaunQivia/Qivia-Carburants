"""
build_bornes.py — Génère bornes_lite.geojson (stations de recharge électrique)
à partir du fichier consolidé IRVE de data.gouv.fr (gratuit, sans clé, MAJ quotidienne).

Sortie : bornes_lite.geojson, au MÊME format que stations_lite.geojson
(coordonnées en degrés décimaux — la carte sait déjà les lire).

Usage :
    pip install pandas requests
    python build_bornes.py

Pensé pour tourner dans une GitHub Action (voir .github/workflows/maj-bornes.yml).
"""

import json
import os
import sys

import pandas as pd
import requests

# ── Source officielle IRVE (même dataset que le module stations.py du zip) ──────
DATASET_API = (
    "https://www.data.gouv.fr/api/1/datasets/"
    "fichier-consolide-des-bornes-de-recharge-pour-vehicules-electriques/"
)
HEADERS = {"User-Agent": "Qivia-Carburants/1.0 (data pipeline; GitHub Actions)"}
OUT_PATH = "bornes_lite.geojson"

# Catégories de puissance — référentiel Avere-France / IRVE (repris du zip)
POWER_CATEGORIES = [
    ("Normale", 0.0, 7.4),
    ("Accélérée", 7.4, 22.0),
    ("Rapide", 22.0, 50.0),
    ("HPC", 50.0, 150.0),
    ("Ultra-rapide", 150.0, 1e9),
]


def categorize_power(kw) -> str:
    if kw is None or pd.isna(kw):
        return ""
    for label, lo, hi in POWER_CATEGORIES:
        if lo <= kw < hi:
            return label
    return ""


def first_present(df: pd.DataFrame, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None


# ── Résolution de l'URL du CSV IRVE (logique du stations.py du zip) ─────────────
def resolve_irve_url() -> str:
    r = requests.get(DATASET_API, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    csv_resources = [
        res for res in data.get("resources", [])
        if (res.get("format", "") or "").lower() == "csv"
    ]

    def has_irve_schema(res):
        schema = res.get("schema") or {}
        if isinstance(schema, dict):
            return "irve-statique" in (schema.get("name") or "").lower()
        return False

    BLACKLIST = ("rapport", "report", "validation", "status", "schema-status")

    def looks_like_data(res):
        title = (res.get("title") or "").lower()
        url = (res.get("url") or "").lower()
        if any(b in title for b in BLACKLIST):
            return False
        return "irve-statique" in title or "irve-statique" in url

    candidates = [r for r in csv_resources if has_irve_schema(r)]
    if not candidates:
        candidates = [r for r in csv_resources if looks_like_data(r)]
    if not candidates:
        raise RuntimeError(
            "Aucune ressource CSV IRVE identifiable. Titres : "
            + ", ".join(repr(r.get("title")) for r in csv_resources[:10])
        )
    candidates.sort(key=lambda r: r.get("last_modified", ""), reverse=True)
    return candidates[0]["url"]


def download_irve() -> pd.DataFrame:
    url = resolve_irve_url()
    print(f"[irve] téléchargement : {url}")
    r = requests.get(url, headers=HEADERS, timeout=300)
    r.raise_for_status()
    from io import BytesIO
    df = pd.read_csv(BytesIO(r.content), low_memory=False)
    print(f"[irve] {len(df):,} lignes (points de charge) téléchargées")
    return df


# ── Transformation CSV IRVE -> GeoJSON compact (1 feature = 1 station) ──────────
def build_geojson(df: pd.DataFrame) -> dict:
    lat_col = first_present(df, ["consolidated_latitude", "ylatitude", "latitude"])
    lng_col = first_present(df, ["consolidated_longitude", "xlongitude", "longitude"])
    pow_col = first_present(df, ["puissance_nominale", "puissance_max"])
    if lat_col is None or lng_col is None:
        raise RuntimeError(f"Colonnes lat/lng introuvables. Colonnes : {list(df.columns)[:12]}")

    op_col = first_present(df, ["nom_operateur"])
    ens_col = first_present(df, ["nom_enseigne"])
    nom_col = first_present(df, ["nom_station"])
    adr_col = first_present(df, ["adresse_station"])
    vil_col = first_present(df, ["consolidated_commune", "commune"])
    cp_col = first_present(df, ["consolidated_code_postal", "code_postal"])
    id_col = first_present(df, ["id_station_itinerance", "id_station_local"])

    df = df.rename(columns={lat_col: "lat", lng_col: "lng"})
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lng"] = pd.to_numeric(df["lng"], errors="coerce")
    if pow_col:
        df["_pow"] = pd.to_numeric(df[pow_col], errors="coerce")
    else:
        df["_pow"] = pd.NA
    df = df.dropna(subset=["lat", "lng"])
    # Bornes plausibles (France + DROM), élimine les coordonnées aberrantes
    df = df[df["lat"].between(-25, 52) & df["lng"].between(-65, 56)]

    # Clé d'unicité : station d'itinérance si dispo, sinon coordonnées arrondies
    if id_col and df[id_col].notna().any():
        df["_key"] = df[id_col].astype(str)
        df.loc[df["_key"].isin(["", "nan", "None"]), "_key"] = (
            df["lat"].round(5).astype(str) + "," + df["lng"].round(5).astype(str)
        )
    else:
        df["_key"] = df["lat"].round(5).astype(str) + "," + df["lng"].round(5).astype(str)

    def first_str(s):
        for v in s:
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    agg = {
        "lat": ("lat", "first"),
        "lng": ("lng", "first"),
        "puissance": ("_pow", "max"),
        "nb_pdc": ("_key", "size"),
    }
    named = {}
    for key, col in [("operateur", op_col), ("enseigne", ens_col), ("nom", nom_col),
                     ("adresse", adr_col), ("ville", vil_col), ("cp", cp_col),
                     ("id", id_col)]:
        if col:
            named[key] = col

    grouped = df.groupby("_key", sort=False)
    base = grouped.agg(**agg)
    for out_key, src_col in named.items():
        base[out_key] = grouped[src_col].apply(first_str)
    base = base.reset_index(drop=True)

    features = []
    for row in base.itertuples(index=False):
        d = row._asdict()
        pw = d.get("puissance")
        pw_val = round(float(pw), 1) if pw is not None and not pd.isna(pw) else None
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point",
                         "coordinates": [round(float(d["lng"]), 5), round(float(d["lat"]), 5)]},
            "properties": {
                "id": str(d.get("id", "") or ""),
                "operateur": d.get("operateur", "") or "",
                "enseigne": d.get("enseigne", "") or "",
                "nom": d.get("nom", "") or "",
                "adresse": d.get("adresse", "") or "",
                "ville": d.get("ville", "") or "",
                "cp": str(d.get("cp", "") or "").split(".")[0],
                "puissance": pw_val,
                "categorie": categorize_power(pw_val),
                "nb_pdc": int(d.get("nb_pdc", 1)),
                "type": "elec",
            },
        })

    return {"type": "FeatureCollection", "features": features}


def main():
    df = download_irve()
    gj = build_geojson(df)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(gj, f, ensure_ascii=False, separators=(",", ":"))
    n = len(gj["features"])
    size = os.path.getsize(OUT_PATH) // 1024
    print(f"✅ {OUT_PATH} écrit : {n:,} stations électriques, {size:,} Ko")
    if n < 1000:
        print("⚠️  Très peu de stations — vérifier la source IRVE.", file=sys.stderr)


if __name__ == "__main__":
    main()
