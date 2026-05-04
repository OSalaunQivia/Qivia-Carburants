"""
generate_qivia_report.py
========================
Génère le fichier `qivia_data.json` utilisé par le dashboard Qivia Prix Carburants.

Usage :
    python generate_qivia_report.py                         # cherche les CSV dans le dossier courant
    python generate_qivia_report.py --folder /chemin/vers/  # dossier personnalisé
    python generate_qivia_report.py --out ./output/         # dossier de sortie

Le dashboard HTML (qivia_dashboard.html) doit être dans le même dossier que qivia_data.json.
"""

import argparse
import glob
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
FUELS = ["gazole_prix", "sp95_prix", "e10_prix", "sp98_prix", "e85_prix", "gplc_prix"]
FUEL_LABELS = {
    "gazole_prix": "Gazole",
    "sp95_prix": "SP95",
    "e10_prix": "E10",
    "sp98_prix": "SP98",
    "e85_prix": "E85",
    "gplc_prix": "GPLc",
}
TOP_BRANDS = 12


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def safe_mean(series: pd.Series) -> object:
    v = series.dropna()
    return round(float(v.mean()), 4) if len(v) > 0 else None


def safe_min(series: pd.Series) -> object:
    v = series.dropna()
    return round(float(v.min()), 4) if len(v) > 0 else None


def safe_max(series: pd.Series) -> object:
    v = series.dropna()
    return round(float(v.max()), 4) if len(v) > 0 else None


def fuel_row(group: pd.DataFrame) -> dict:
    return {f: safe_mean(group[f]) for f in FUELS}


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def load_data(folder: str) -> pd.DataFrame:
    pattern = os.path.join(folder, "stations_avec_prix_*.csv")
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"[ERROR] Aucun fichier 'stations_avec_prix_*.csv' trouvé dans : {folder}")
        sys.exit(1)

    print(f"[INFO] {len(files)} fichier(s) trouvé(s)")
    dfs = []
    for f in files:
        date_str = Path(f).stem.split("_")[-1]  # YYYYMMDD or YYYY-MM-DD
        try:
            df = pd.read_csv(
                f,
                usecols=["uuid", "name", "city", "department", "region", "brand", "postal",
                         "latitude", "longitude"] + FUELS,
                low_memory=False,
            )
            df["snapshot_date"] = pd.to_datetime(date_str, format="mixed")
            dfs.append(df)
            print(f"  ✓ {Path(f).name} ({len(df)} lignes)")
        except Exception as e:
            print(f"  ✗ {Path(f).name} — ignoré ({e})")

    all_data = pd.concat(dfs, ignore_index=True)
    all_data["year_month"] = all_data["snapshot_date"].dt.strftime("%Y-%m")
    all_data["year"] = all_data["snapshot_date"].dt.year
    all_data["week"] = all_data["snapshot_date"].dt.isocalendar().week.astype(int)
    return all_data


def build_json(all_data: pd.DataFrame) -> dict:
    dates_sorted = sorted(all_data["snapshot_date"].unique())
    latest_date = dates_sorted[-1]

    # ── 1. Daily national ────────────────────────────────────────────────
    daily = []
    for dt, g in all_data.groupby("snapshot_date"):
        row = {"date": str(dt.date()), **fuel_row(g)}
        daily.append(row)

    # ── 2. Weekly national ───────────────────────────────────────────────
    weekly = []
    for (ym, wk), g in all_data.groupby(["year_month", "week"]):
        dates_in_week = sorted(g["snapshot_date"].dt.date.unique())
        row = {
            "period": f"S{wk}",
            "year_month": ym,
            "week": int(wk),
            "date_min": str(dates_in_week[0]),
            "date_max": str(dates_in_week[-1]),
            **fuel_row(g),
        }
        weekly.append(row)

    # ── 3. Monthly national ──────────────────────────────────────────────
    monthly = []
    for ym, g in all_data.groupby("year_month"):
        row = {"period": ym, **fuel_row(g)}
        monthly.append(row)

    # ── 4. Quarterly / Semi-annual / Annual ──────────────────────────────
    all_data["quarter"] = all_data["snapshot_date"].dt.to_period("Q").astype(str)
    all_data["half"] = all_data["snapshot_date"].apply(
        lambda d: f"{d.year}-H{'1' if d.month <= 6 else '2'}"
    )

    quarterly = []
    for q, g in all_data.groupby("quarter"):
        row = {"period": q, **fuel_row(g)}
        quarterly.append(row)

    half_yearly = []
    for h, g in all_data.groupby("half"):
        row = {"period": h, **fuel_row(g)}
        half_yearly.append(row)

    yearly = []
    for y, g in all_data.groupby("year"):
        row = {"period": str(y), **fuel_row(g)}
        yearly.append(row)

    # ── 5. Regional (per date) ───────────────────────────────────────────
    regional_daily = []
    for (dt, region), g in all_data.groupby(["snapshot_date", "region"]):
        row = {"date": str(dt.date()), "region": region, **fuel_row(g)}
        regional_daily.append(row)

    # ── 6. Brand (per date) ──────────────────────────────────────────────
    top_brands = all_data["brand"].value_counts().head(TOP_BRANDS).index.tolist()
    brand_daily = []
    for (dt, brand), g in all_data[all_data["brand"].isin(top_brands)].groupby(
        ["snapshot_date", "brand"]
    ):
        row = {"date": str(dt.date()), "brand": brand, **fuel_row(g)}
        brand_daily.append(row)

    # ── 7. Per-station (latest snapshot) ────────────────────────────────
    latest = all_data[all_data["snapshot_date"] == latest_date].copy()
    stations = []
    for _, r in latest.iterrows():
        if all(pd.isna(r[f]) for f in FUELS):
            continue
        stations.append({
            "uuid": str(r["uuid"]),
            "name": str(r.get("name", ""))[:50],
            "city": str(r.get("city", "")),
            "region": str(r.get("region", "")),
            "brand": str(r.get("brand", "")),
            "lat": float(r["latitude"]) if pd.notna(r.get("latitude")) else None,
            "lng": float(r["longitude"]) if pd.notna(r.get("longitude")) else None,
            **{f: round(float(r[f]), 4) if pd.notna(r[f]) else None for f in FUELS},
        })

    # ── 8. Department aggregates (latest) ────────────────────────────────
    dept_latest = []
    for dept, g in latest.groupby("department"):
        row = {"department": dept, "region": g["region"].mode().iloc[0] if len(g) > 0 else "", **fuel_row(g)}
        dept_latest.append(row)

    # ── 9. Min/Max/Std stats (latest) ────────────────────────────────────
    stats = {}
    for f in FUELS:
        stats[f] = {
            "mean": safe_mean(all_data[f]),
            "min":  safe_min(all_data[f]),
            "max":  safe_max(all_data[f]),
            "latest_mean": safe_mean(latest[f]),
            "first_mean":  safe_mean(all_data[all_data["snapshot_date"] == dates_sorted[0]][f]),
        }

    return {
        "meta": {
            "generated": str(pd.Timestamp.now().date()),
            "generated_ts": pd.Timestamp.now().isoformat(),
            "n_stations": int(all_data["uuid"].nunique()),
            "n_snapshots": len(dates_sorted),
            "latest_date": str(latest_date.date()),
            "earliest_date": str(dates_sorted[0].date()),
            "dates": [str(d.date()) for d in dates_sorted],
            "months": sorted(all_data["year_month"].unique().tolist()),
            "quarters": sorted(all_data["quarter"].unique().tolist()),
            "halves": sorted(all_data["half"].unique().tolist()),
            "years": sorted(all_data["year"].unique().tolist()),
            "regions": sorted(all_data["region"].dropna().unique().tolist()),
            "brands": top_brands,
        },
        "stats": stats,
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
        "quarterly": quarterly,
        "half_yearly": half_yearly,
        "yearly": yearly,
        "regional_daily": regional_daily,
        "brand_daily": brand_daily,
        "stations": stations,
        "dept_latest": dept_latest,
    }


def main():
    parser = argparse.ArgumentParser(description="Génère qivia_data.json depuis les CSV stations.")
    parser.add_argument("--folder", default=".", help="Dossier contenant les CSV (défaut: .)")
    parser.add_argument("--out", default=".", help="Dossier de sortie pour qivia_data.json (défaut: .)")
    args = parser.parse_args()

    print(f"\n{'='*55}")
    print("  QIVIA — Générateur de rapport prix carburants")
    print(f"{'='*55}")
    print(f"  Source : {os.path.abspath(args.folder)}")
    print(f"  Sortie : {os.path.abspath(args.out)}\n")

    all_data = load_data(args.folder)

    print(f"\n[INFO] {all_data['uuid'].nunique():,} stations uniques | {len(all_data):,} lignes totales")
    print(f"[INFO] Période : {all_data['snapshot_date'].min().date()} → {all_data['snapshot_date'].max().date()}\n")

    print("[INFO] Calcul des agrégats...")
    data = build_json(all_data)

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, "qivia_data.json")
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, separators=(",", ":"))

    size_kb = os.path.getsize(out_path) / 1024
    print(f"[OK] qivia_data.json généré ({size_kb:.1f} Ko)")
    print(f"[OK] {len(data['daily'])} snapshots journaliers")
    print(f"[OK] {len(data['weekly'])} semaines")
    print(f"[OK] {len(data['monthly'])} mois")
    print(f"[OK] {len(data['regional_daily'])} entrées régionales")
    print(f"[OK] {len(data['stations'])} stations (dernier snapshot)")
    print(f"\n→ Ouvrez qivia_dashboard.html dans votre navigateur.\n")


if __name__ == "__main__":
    main()
