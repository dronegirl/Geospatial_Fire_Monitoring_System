import os
import pandas as pd
import geopandas as gpd
import streamlit as st
import folium
from streamlit_folium import st_folium

st.set_page_config(page_title="SAA Fire Monitoring Dashboard", layout="wide")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

AOI_FILE = os.path.join(BASE_DIR, "aoi.geojson")
MTG_FILE = os.path.join(BASE_DIR, "mtg_risk_alerts.geojson")
VIIRS_FILE = os.path.join(BASE_DIR, "viirs_risk_alerts.geojson")
ALERTS_FILE = os.path.join(BASE_DIR, "ranked_fire_alerts.csv")

@st.cache_data
def load_geojson(path):
    if os.path.exists(path):
        return gpd.read_file(path)
    return gpd.GeoDataFrame()

@st.cache_data
def load_csv(path):
    if os.path.exists(path):
        return pd.read_csv(path)
    return pd.DataFrame()

def risk_color(risk):
    risk = str(risk).lower()
    if risk == "high":
        return "red"
    if risk == "medium":
        return "orange"
    if risk == "low":
        return "yellow"
    return "blue"

def add_points_to_map(m, gdf, layer_name):
    if gdf.empty:
        return

    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        lat = geom.y
        lon = geom.x
        risk = row.get("risk_level", "Unknown")
        nearest_cpt = row.get("nearest_cpt", "Unknown")
        distance_km = row.get("distance_km", None)
        source = row.get("source_group", layer_name)

        popup = (
            f"<b>Source:</b> {source}<br>"
            f"<b>Risk:</b> {risk}<br>"
            f"<b>Nearest CPT:</b> {nearest_cpt}<br>"
            f"<b>Distance (km):</b> {distance_km}<br>"
            f"<b>Lat:</b> {lat:.5f}<br>"
            f"<b>Lon:</b> {lon:.5f}"
        )

        folium.CircleMarker(
            location=[lat, lon],
            radius=6,
            color=risk_color(risk),
            fill=True,
            fill_opacity=0.9,
            popup=popup
        ).add_to(m)

aoi = load_geojson(AOI_FILE)
mtg = load_geojson(MTG_FILE)
viirs = load_geojson(VIIRS_FILE)
alerts = load_csv(ALERTS_FILE)

st.title("SAA Fire Monitoring Dashboard")
st.caption("Meteosat early warning + VIIRS hotspot confirmation + compartment risk ranking")

if not alerts.empty and "risk_level" in alerts.columns:
    high_count = (alerts["risk_level"].astype(str).str.lower() == "high").sum()
    medium_count = (alerts["risk_level"].astype(str).str.lower() == "medium").sum()
    low_count = (alerts["risk_level"].astype(str).str.lower() == "low").sum()
else:
    high_count = medium_count = low_count = 0

c1, c2, c3 = st.columns(3)
c1.metric("High Risk Alerts", int(high_count))
c2.metric("Medium Risk Alerts", int(medium_count))
c3.metric("Low Risk Alerts", int(low_count))

st.subheader("Fire Risk Map")

m = folium.Map(location=[-19.0, 29.5], zoom_start=6, tiles="OpenStreetMap")

if not aoi.empty:
    folium.GeoJson(
        aoi,
        name="Compartments",
        style_function=lambda x: {
            "fillColor": "#00000000",
            "color": "yellow",
            "weight": 2
        }
    ).add_to(m)

add_points_to_map(m, mtg, "MTG")
add_points_to_map(m, viirs, "VIIRS")

folium.LayerControl().add_to(m)
st_folium(m, width=1200, height=600)

st.subheader("Ranked Alerts")

if alerts.empty:
    st.info("No alerts available yet.")
else:
    show_cols = [c for c in [
        "source", "nearest_cpt", "distance_km", "risk_level",
        "latitude", "longitude", "alert_text"
    ] if c in alerts.columns]
    st.dataframe(alerts[show_cols], use_container_width=True)