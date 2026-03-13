import os
import glob
import io
import sqlite3
import zipfile
import tempfile
from datetime import datetime, timedelta, timezone

import pandas as pd
import geopandas as gpd
import xarray as xr
import eumdac
import requests

from config import (
    EUMETSAT_CONSUMER_KEY,
    EUMETSAT_CONSUMER_SECRET,
    EUM_COLLECTION,
    AOI_FILE,
    DB_FILE,
    MIN_CONFIDENCE,
    LOOKBACK_HOURS,
    FIRMS_MAP_KEY,
)

# =========================================================
# DATABASE
# =========================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sent_alerts (
            alert_id TEXT PRIMARY KEY,
            event_time TEXT,
            latitude REAL,
            longitude REAL,
            confidence REAL,
            field_name TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


# =========================================================
# AOI / COMPARTMENTS
# =========================================================
def load_aoi():
    aoi = gpd.read_file(AOI_FILE)

    if aoi.crs is None:
        aoi = aoi.set_crs("EPSG:4326")
    else:
        aoi = aoi.to_crs("EPSG:4326")

    if "name" not in aoi.columns:
        aoi["name"] = [f"CPT_{i+1}" for i in range(len(aoi))]

    return aoi


# =========================================================
# EUMETSAT / MTG
# =========================================================
def get_datastore():
    if not EUMETSAT_CONSUMER_KEY or not EUMETSAT_CONSUMER_SECRET:
        raise ValueError("Missing EUMETSAT credentials in config.py")

    token = eumdac.AccessToken((EUMETSAT_CONSUMER_KEY, EUMETSAT_CONSUMER_SECRET))
    return eumdac.DataStore(token)


def search_recent_products(datastore, collection_id, lookback_hours=2):
    collection = datastore.get_collection(collection_id)
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=lookback_hours)

    products = collection.search(
        dtstart=start_time,
        dtend=end_time,
        type="MTIFCI2FIR",
        coverage="FD",
        sat="MTI1",
        sort="publicationDate"
    )
    return list(products)


def download_product(product, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    files = []

    for entry in product.entries:
        entry_name = entry.name if hasattr(entry, "name") else str(entry)
        safe_name = os.path.basename(entry_name)
        outfile = os.path.join(out_dir, safe_name)

        try:
            with product.open(entry=entry) as src, open(outfile, "wb") as dst:
                dst.write(src.read())
        except Exception:
            with product.open(entry=entry_name) as src, open(outfile, "wb") as dst:
                dst.write(src.read())

        files.append(outfile)

    return files


def find_netcdf_files(files):
    nc_files = [f for f in files if f.lower().endswith((".nc", ".nc4"))]
    if nc_files:
        return nc_files

    extracted_nc = []
    for f in files:
        if f.lower().endswith(".zip"):
            extract_dir = tempfile.mkdtemp(prefix="mtg_unzip_")
            with zipfile.ZipFile(f, "r") as zf:
                zf.extractall(extract_dir)

            extracted_nc.extend(glob.glob(os.path.join(extract_dir, "**", "*.nc"), recursive=True))
            extracted_nc.extend(glob.glob(os.path.join(extract_dir, "**", "*.nc4"), recursive=True))

    return extracted_nc


def parse_mtg_fire_netcdf(nc_path):
    import numpy as np
    from pyproj import CRS, Transformer

    ds = xr.open_dataset(nc_path)

    required = ["x", "y", "fire_result", "fire_probability", "mtg_geos_projection"]
    missing = [v for v in required if v not in ds.variables]
    if missing:
        print("Available variables:", list(ds.variables))
        raise ValueError(f"Missing required variables: {missing}")

    x = ds["x"].values
    y = ds["y"].values
    fire_result = ds["fire_result"].values
    fire_probability = ds["fire_probability"].values

    attrs = ds["mtg_geos_projection"].attrs
    h = attrs["perspective_point_height"]

    x = x * h
    y = y * h

    xx, yy = np.meshgrid(x, y)

    geos_crs = CRS.from_cf(attrs)
    transformer = Transformer.from_crs(geos_crs, CRS.from_epsg(4326), always_xy=True)
    lon, lat = transformer.transform(xx, yy)

    df = pd.DataFrame({
        "longitude": lon.ravel(),
        "latitude": lat.ravel(),
        "fire_result": fire_result.ravel(),
        "fire_probability": fire_probability.ravel(),
    })

    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.dropna(subset=["longitude", "latitude"])
    df = df[(df["latitude"] >= -90) & (df["latitude"] <= 90)]
    df = df[(df["longitude"] >= -180) & (df["longitude"] <= 180)]

    df = df[
        (df["fire_result"] >= 2) &
        (df["fire_probability"] >= 0.5)
    ].copy()

    df = df[
        (df["longitude"] >= 10) & (df["longitude"] <= 42) &
        (df["latitude"] >= -35) & (df["latitude"] <= -5)
    ].copy()

    df["confidence"] = df["fire_probability"].astype(float)
    df["event_time"] = datetime.now(timezone.utc).isoformat()
    df["source"] = "MTG"

    print(f"\nParsed {len(df)} MTG fire pixels from {os.path.basename(nc_path)}")
    return df


# =========================================================
# VIIRS / FIRMS
# =========================================================
def fetch_firms_viirs():
    if not FIRMS_MAP_KEY:
        raise ValueError("Missing FIRMS_MAP_KEY in config.py")

    datasets = ["VIIRS_NOAA21_NRT", "VIIRS_NOAA20_NRT", "VIIRS_SNPP_NRT"]
    bbox = "10,-35,42,-5"

    all_rows = []

    for dataset in datasets:
        url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{FIRMS_MAP_KEY}/{dataset}/{bbox}/1"
        r = requests.get(url, timeout=60)
        r.raise_for_status()

        text = r.text.strip()
        if not text:
            continue

        df = pd.read_csv(io.StringIO(text))
        if df.empty:
            continue

        df["source"] = dataset
        all_rows.append(df)

    if not all_rows:
        return pd.DataFrame()

    df = pd.concat(all_rows, ignore_index=True)

    df = df.dropna(subset=["latitude", "longitude"])
    df = df[(df["latitude"] >= -90) & (df["latitude"] <= 90)]
    df = df[(df["longitude"] >= -180) & (df["longitude"] <= 180)]

    df = df[
        (df["longitude"] >= 10) & (df["longitude"] <= 42) &
        (df["latitude"] >= -35) & (df["latitude"] <= -5)
    ].copy()

    if "confidence" in df.columns:
        df["confidence_std"] = pd.to_numeric(df["confidence"], errors="coerce")
    else:
        df["confidence_std"] = None

    if "acq_date" in df.columns and "acq_time" in df.columns:
        df["event_time"] = df["acq_date"].astype(str) + " " + df["acq_time"].astype(str)
    else:
        df["event_time"] = datetime.now(timezone.utc).isoformat()

    print(f"Parsed {len(df)} VIIRS fire pixels from FIRMS")
    return df


# =========================================================
# EXPORTS
# =========================================================
def export_geojson(df, filename, lon_col="longitude", lat_col="latitude"):
    if df.empty:
        return False

    gdf = gpd.GeoDataFrame(
        df.copy(),
        geometry=gpd.points_from_xy(df[lon_col], df[lat_col]),
        crs="EPSG:4326"
    )
    gdf.to_file(filename, driver="GeoJSON")
    print(f"Exported {filename}")
    return True


def zimbabwe_subset(df, lon_col="longitude", lat_col="latitude"):
    if df.empty:
        return df.copy()

    return df[
        (df[lon_col] >= 25) & (df[lon_col] <= 34) &
        (df[lat_col] >= -23) & (df[lat_col] <= -15)
    ].copy()


# =========================================================
# RISK CLASSIFICATION
# =========================================================
def classify_fire_risk(fire_df, aoi, source_name, lon_col="longitude", lat_col="latitude", conf_col=None):
    if fire_df.empty:
        return gpd.GeoDataFrame(columns=[
            "source", "latitude", "longitude", "nearest_cpt",
            "distance_km", "risk_level", "geometry"
        ], crs="EPSG:4326")

    fire_gdf = gpd.GeoDataFrame(
        fire_df.copy(),
        geometry=gpd.points_from_xy(fire_df[lon_col], fire_df[lat_col]),
        crs="EPSG:4326"
    )

    aoi_local = aoi.copy()
    if "name" not in aoi_local.columns:
        aoi_local["name"] = [f"CPT_{i+1}" for i in range(len(aoi_local))]

    fire_m = fire_gdf.to_crs("EPSG:3857")
    aoi_m = aoi_local.to_crs("EPSG:3857")[["name", "geometry"]].copy()

    joined = gpd.sjoin_nearest(
        fire_m,
        aoi_m,
        how="left",
        distance_col="distance_m"
    )

    joined["distance_km"] = joined["distance_m"] / 1000.0
    joined["source_group"] = source_name

    def risk_label(d):
        if pd.isna(d):
            return "Unknown"
        if d <= 2:
            return "High"
        elif d <= 5:
            return "Medium"
        elif d <= 10:
            return "Low"
        else:
            return "Outside threshold"

    joined["risk_level"] = joined["distance_km"].apply(risk_label)
    joined = joined.rename(columns={"name": "nearest_cpt"})

    if conf_col and conf_col in joined.columns:
        joined["confidence_out"] = joined[conf_col]
    elif "confidence" in joined.columns:
        joined["confidence_out"] = joined["confidence"]
    else:
        joined["confidence_out"] = None

    return joined.to_crs("EPSG:4326")


def build_ranked_alerts(risk_gdf, source_name):
    if risk_gdf.empty:
        return pd.DataFrame(columns=[
            "source", "nearest_cpt", "distance_km", "risk_level",
            "latitude", "longitude", "confidence", "alert_text"
        ])

    rows = []

    for _, row in risk_gdf.iterrows():
        if row["risk_level"] == "Outside threshold":
            continue

        confidence_value = row.get("confidence_out", None)
        conf_text = ""
        if pd.notna(confidence_value):
            try:
                conf_text = f" | Confidence: {float(confidence_value):.2f}"
            except Exception:
                conf_text = ""

        rows.append({
            "source": source_name,
            "nearest_cpt": row.get("nearest_cpt", "Unknown"),
            "distance_km": round(float(row["distance_km"]), 2) if pd.notna(row["distance_km"]) else None,
            "risk_level": row["risk_level"],
            "latitude": float(row["latitude"]),
            "longitude": float(row["longitude"]),
            "confidence": confidence_value,
            "alert_text": (
                f"{row['risk_level'].upper()} RISK FIRE ALERT | "
                f"Source: {source_name} | "
                f"Nearest compartment: {row.get('nearest_cpt', 'Unknown')} | "
                f"Distance: {float(row['distance_km']):.2f} km | "
                f"Lat,Lon: {float(row['latitude']):.5f}, {float(row['longitude']):.5f}"
                f"{conf_text}"
            )
        })

    alerts_df = pd.DataFrame(rows)
    if alerts_df.empty:
        return alerts_df

    risk_order = {"High": 1, "Medium": 2, "Low": 3, "Unknown": 4}
    alerts_df["risk_sort"] = alerts_df["risk_level"].map(risk_order)
    alerts_df = alerts_df.sort_values(["risk_sort", "distance_km"]).drop(columns="risk_sort")

    return alerts_df


# =========================================================
# MAIN
# =========================================================
def run_once():
    print("Loading compartments/AOI...")
    aoi = load_aoi()

    print("Connecting to EUMETSAT...")
    datastore = get_datastore()

    print("Searching recent MTG products...")
    products = search_recent_products(datastore, EUM_COLLECTION, LOOKBACK_HOURS)
    print(f"Found {len(products)} MTG products")

    mtg_df = pd.DataFrame()

    if products:
        for product in products[-1:]:
            print(f"Downloading MTG product: {product}")

            with tempfile.TemporaryDirectory(prefix="mtg_") as tmpdir:
                files = download_product(product, tmpdir)
                nc_files = find_netcdf_files(files)

                for nc in nc_files:
                    try:
                        df = parse_mtg_fire_netcdf(nc)
                        if not df.empty:
                            mtg_df = pd.concat([mtg_df, df], ignore_index=True)
                    except Exception as e:
                        print(f"Failed parsing MTG {nc}: {e}")

    if not mtg_df.empty:
        mtg_df = mtg_df.drop_duplicates(
            subset=["latitude", "longitude", "fire_result", "fire_probability"]
        ).copy()

    print("Fetching VIIRS from FIRMS...")
    try:
        viirs_df = fetch_firms_viirs()
    except Exception as e:
        print(f"Failed fetching VIIRS: {e}")
        viirs_df = pd.DataFrame()

    if not mtg_df.empty:
        export_geojson(mtg_df, "mtg_southern_africa.geojson")
    else:
        print("No MTG detections after filtering.")

    if not viirs_df.empty:
        export_geojson(viirs_df, "viirs_southern_africa.geojson")
    else:
        print("No VIIRS detections after filtering.")

    mtg_zim = zimbabwe_subset(mtg_df)
    viirs_zim = zimbabwe_subset(viirs_df)

    if not mtg_zim.empty:
        export_geojson(mtg_zim, "mtg_zimbabwe.geojson")
    else:
        print("No MTG detections in Zimbabwe for this timestamp.")

    if not viirs_zim.empty:
        export_geojson(viirs_zim, "viirs_zimbabwe.geojson")
    else:
        print("No VIIRS detections in Zimbabwe for this timestamp.")

    mtg_risk = classify_fire_risk(mtg_df, aoi, "MTG", conf_col="confidence") if not mtg_df.empty else gpd.GeoDataFrame()
    viirs_risk = classify_fire_risk(viirs_df, aoi, "VIIRS", conf_col="confidence_std") if not viirs_df.empty else gpd.GeoDataFrame()

    if not mtg_risk.empty:
        mtg_risk.to_file("mtg_risk_alerts.geojson", driver="GeoJSON")
        print("Exported mtg_risk_alerts.geojson")

    if not viirs_risk.empty:
        viirs_risk.to_file("viirs_risk_alerts.geojson", driver="GeoJSON")
        print("Exported viirs_risk_alerts.geojson")

    mtg_alerts = build_ranked_alerts(mtg_risk, "MTG")
    viirs_alerts = build_ranked_alerts(viirs_risk, "VIIRS")

    combined_alerts = pd.concat([mtg_alerts, viirs_alerts], ignore_index=True)

    if not combined_alerts.empty:
        risk_order = {"High": 1, "Medium": 2, "Low": 3, "Unknown": 4}
        combined_alerts["risk_sort"] = combined_alerts["risk_level"].map(risk_order)
        combined_alerts = combined_alerts.sort_values(["risk_sort", "distance_km"]).drop(columns="risk_sort")
        combined_alerts.to_csv("ranked_fire_alerts.csv", index=False)
        print("Exported ranked_fire_alerts.csv")

        print("\nTop ranked alerts:")
        for msg in combined_alerts["alert_text"].head(10).tolist():
            print(msg)
            print("-" * 80)
    else:
        print("No ranked alerts within 10 km of compartments.")
        print("Presentation message: No active fire detected near monitored compartments at acquisition time.")


if __name__ == "__main__":
    init_db()
    run_once()