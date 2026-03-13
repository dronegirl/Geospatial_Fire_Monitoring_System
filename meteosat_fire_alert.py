import os
import glob
import time
import sqlite3
import zipfile
import tempfile
from datetime import datetime, timedelta, timezone

import pandas as pd
import geopandas as gpd
import xarray as xr
import eumdac
from shapely.geometry import Point
from twilio.rest import Client

from config import (
    EUMETSAT_CONSUMER_KEY,
    EUMETSAT_CONSUMER_SECRET,
    EUM_COLLECTION,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_FROM,
    ALERT_TO,
    AOI_FILE,
    DB_FILE,
    MIN_FRP_MW,
    MIN_CONFIDENCE,
    LOOKBACK_HOURS,
    POLL_INTERVAL_SECONDS,
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
            frp_mw REAL,
            confidence REAL,
            field_name TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def already_sent(alert_id: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sent_alerts WHERE alert_id = ?", (alert_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None

def mark_sent(alert_id, event_time, latitude, longitude, frp_mw, confidence, field_name):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO sent_alerts
        (alert_id, event_time, latitude, longitude, frp_mw, confidence, field_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        alert_id,
        event_time,
        latitude,
        longitude,
        frp_mw,
        confidence,
        field_name,
        datetime.now(timezone.utc).isoformat()
    ))
    conn.commit()
    conn.close()

# =========================================================
# AOI
# =========================================================
def load_aoi():
    aoi = gpd.read_file(AOI_FILE)

    if aoi.crs is None:
        aoi = aoi.set_crs("EPSG:4326")
    else:
        aoi = aoi.to_crs("EPSG:4326")

    if "name" not in aoi.columns:
        aoi["name"] = [f"AOI_{i+1}" for i in range(len(aoi))]

    return aoi

# =========================================================
# EUMDAC
# =========================================================
def get_datastore():
    if not EUMETSAT_CONSUMER_KEY or not EUMETSAT_CONSUMER_SECRET:
        raise ValueError("Missing EUMETSAT credentials in environment variables.")

    token = eumdac.AccessToken((EUMETSAT_CONSUMER_KEY, EUMETSAT_CONSUMER_SECRET))
    datastore = eumdac.DataStore(token)
    return datastore

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
        # Some collections return entry objects, others return plain strings
        entry_name = entry.name if hasattr(entry, "name") else str(entry)
        outfile = os.path.join(out_dir, os.path.basename(entry_name))

        with product.open(entry=entry) as src, open(outfile, "wb") as dst:
            dst.write(src.read())

        files.append(outfile)

    return files

# =========================================================
# FILE HANDLING
# =========================================================
def find_netcdf_files(files):
    nc_files = [f for f in files if f.lower().endswith((".nc", ".nc4"))]
    if nc_files:
        return nc_files

    extracted_nc = []
    for f in files:
        if f.lower().endswith(".zip"):
            extract_dir = tempfile.mkdtemp(prefix="frp_unzip_")
            with zipfile.ZipFile(f, "r") as zf:
                zf.extractall(extract_dir)

            extracted_nc.extend(glob.glob(os.path.join(extract_dir, "**", "*.nc"), recursive=True))
            extracted_nc.extend(glob.glob(os.path.join(extract_dir, "**", "*.nc4"), recursive=True))

    return extracted_nc

def guess_var(ds, candidate_names):
    # exact match first
    for c in candidate_names:
        if c in ds.variables:
            return c

    # partial match second
    for var in ds.variables:
        low = var.lower()
        for c in candidate_names:
            if c.lower() in low:
                return var

    return None

def parse_frp_netcdf(nc_path):
    import numpy as np
    from pyproj import CRS, Transformer

    ds = xr.open_dataset(nc_path)

    # Required variables for MTG Active Fire product
    required = ["x", "y", "fire_result", "fire_probability", "mtg_geos_projection"]
    missing = [v for v in required if v not in ds.variables]
    if missing:
        print("Available variables:", list(ds.variables))
        raise ValueError(f"Missing required variables: {missing}")

    x = ds["x"].values
    y = ds["y"].values
    fire_result = ds["fire_result"].values
    fire_probability = ds["fire_probability"].values

    # Build 2D coordinate grids
    xx, yy = np.meshgrid(x, y)

    # Read geostationary projection metadata from CF grid mapping variable
    proj_var = ds["mtg_geos_projection"]
    attrs = proj_var.attrs

    # Build CRS from CF attributes
    geos_crs = CRS.from_cf(attrs)
    wgs84 = CRS.from_epsg(4326)
    transformer = Transformer.from_crs(geos_crs, wgs84, always_xy=True)

    lon, lat = transformer.transform(xx, yy)

    # Flatten everything
    df = pd.DataFrame({
        "longitude": lon.ravel(),
        "latitude": lat.ravel(),
        "fire_result": fire_result.ravel(),
        "fire_probability": fire_probability.ravel(),
    })

    # Basic cleanup
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.dropna(subset=["longitude", "latitude"])
    df = df[(df["latitude"] >= -90) & (df["latitude"] <= 90)]
    df = df[(df["longitude"] >= -180) & (df["longitude"] <= 180)]

    # Keep only pixels with some fire signal
    # Since class coding may vary, use probability as the main trigger
    df = df[df["fire_probability"].fillna(0) > 0].copy()

    # Use probability as the intensity-like field for downstream logic
    df["frp_mw"] = df["fire_probability"].astype(float)

    # Confidence field for downstream logic
    df["confidence"] = df["fire_probability"].astype(float)

    # Use file timestamp as event time
    df["event_time"] = datetime.now(timezone.utc).isoformat()

    return df

# =========================================================
# SPATIAL FILTER
# =========================================================
def create_alerts(df, aoi):
    if df.empty:
        return []

    # MTG active fire uses probability, not FRP
    if "confidence" in df.columns:
        df = df[(df["confidence"].isna()) | (df["confidence"] >= MIN_CONFIDENCE)].copy()

    if df.empty:
        return []

    gdf = gpd.GeoDataFrame(
        df,
        geometry=[Point(xy) for xy in zip(df["longitude"], df["latitude"])],
        crs="EPSG:4326"
    )

    joined = gpd.sjoin(gdf, aoi[["name", "geometry"]], how="inner", predicate="intersects")

    alerts = []
    for _, row in joined.iterrows():
        lat = float(row["latitude"])
        lon = float(row["longitude"])
        conf = None if pd.isna(row["confidence"]) else float(row["confidence"])
        event_time = str(row["event_time"])
        field_name = str(row["name"])
        fire_result = row.get("fire_result", None)

        alert_id = f"{field_name}_{round(lat,4)}_{round(lon,4)}_{event_time}"

        alerts.append({
            "alert_id": alert_id,
            "field_name": field_name,
            "latitude": lat,
            "longitude": lon,
            "frp_mw": None,
            "confidence": conf,
            "event_time": event_time,
            "fire_result": fire_result,
        })

    return alerts



# =========================================================
# TWILIO
# =========================================================
def send_sms(body: str):
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_FROM or not ALERT_TO:
        raise ValueError("Missing Twilio environment variables.")

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    message = client.messages.create(
        body=body,
        from_=TWILIO_FROM,
        to=ALERT_TO
    )
    return message.sid

# =========================================================
# MAIN RUN
# =========================================================
def run_once():
    print("Loading AOI...")
    aoi = load_aoi()

    print("Connecting to EUMETSAT...")
    datastore = get_datastore()

    print("Searching recent products...")
    products = search_recent_products(datastore, EUM_COLLECTION, LOOKBACK_HOURS)

    if not products:
        print("No recent products found.")
        return

    all_alerts = []

    # only process a few latest products for speed
    for product in products[-3:]:
        print(f"Downloading product: {product}")

        with tempfile.TemporaryDirectory(prefix="frp_") as tmpdir:
            files = download_product(product, tmpdir)
            nc_files = find_netcdf_files(files)

            if not nc_files:
                print("No NetCDF files found in this product.")
                continue

            for nc in nc_files:
                try:
                    df = parse_frp_netcdf(nc)
                    alerts = create_alerts(df, aoi)
                    all_alerts.extend(alerts)
                except Exception as e:
                    print(f"Failed parsing {nc}: {e}")

    # deduplicate
    unique_alerts = {a["alert_id"]: a for a in all_alerts}.values()

    for a in unique_alerts:
        if already_sent(a["alert_id"]):
            continue

        conf_txt = "NA" if a["confidence"] is None else f"{a['confidence']:.2f}"

        sms_text = (
            f"FIRE ALERT\n"
            f"Field: {a['field_name']}\n"
            f"Time: {a['event_time']}\n"
            f"FRP: {a['frp_mw']:.1f} MW\n"
            f"Confidence: {conf_txt}\n"
            f"Lat,Lon: {a['latitude']:.5f}, {a['longitude']:.5f}"
        )

        try:
            sid = send_sms(sms_text)
            print(f"SMS sent: {sid}")
            mark_sent(
                a["alert_id"],
                a["event_time"],
                a["latitude"],
                a["longitude"],
                a["frp_mw"],
                a["confidence"],
                a["field_name"],
            )
        except Exception as e:
            print(f"SMS failed: {e}")

def main():
    init_db()
    while True:
        print(f"\n[{datetime.now().isoformat()}] Checking fire products...")
        run_once()
        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    init_db()
    run_once()