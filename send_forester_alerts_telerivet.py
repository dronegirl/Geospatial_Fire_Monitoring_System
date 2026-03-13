import os
import sqlite3
import pandas as pd
import requests
from datetime import datetime, timezone, timedelta

from config import (
    DB_FILE,
    TELERIVET_API_KEY,
    TELERIVET_PROJECT_ID,
    TELERIVET_GROUP_ID,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ALERTS_FILE = os.path.join(BASE_DIR, "ranked_fire_alerts.csv")

# Send "no fire" heartbeat at most once every 3 hours
NO_FIRE_HEARTBEAT_HOURS = 3


def init_alert_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sent_forester_alerts (
            alert_id TEXT PRIMARY KEY,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS system_messages (
            message_type TEXT PRIMARY KEY,
            last_sent_at TEXT
        )
    """)

    conn.commit()
    conn.close()


def already_sent(alert_id: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sent_forester_alerts WHERE alert_id = ?", (alert_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def mark_sent(alert_id: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO sent_forester_alerts (alert_id, created_at)
        VALUES (?, ?)
    """, (alert_id, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()


def get_last_system_message_time(message_type: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        SELECT last_sent_at
        FROM system_messages
        WHERE message_type = ?
    """, (message_type,))
    row = cur.fetchone()
    conn.close()

    if row and row[0]:
        try:
            return datetime.fromisoformat(row[0])
        except Exception:
            return None
    return None


def update_last_system_message_time(message_type: str):
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO system_messages (message_type, last_sent_at)
        VALUES (?, ?)
        ON CONFLICT(message_type) DO UPDATE SET last_sent_at=excluded.last_sent_at
    """, (message_type, now_iso))
    conn.commit()
    conn.close()


def should_send_no_fire_message() -> bool:
    last_sent = get_last_system_message_time("no_fire")
    if last_sent is None:
        return True

    now = datetime.now(timezone.utc)
    return (now - last_sent) >= timedelta(hours=NO_FIRE_HEARTBEAT_HOURS)


def build_alert_id(row):
    return (
        f"{row.get('source', 'NA')}_"
        f"{row.get('nearest_cpt', 'NA')}_"
        f"{row.get('distance_km', 'NA')}_"
        f"{row.get('latitude', 'NA')}_"
        f"{row.get('longitude', 'NA')}"
    )


def format_fire_message(row):
    confidence_part = ""
    if "confidence" in row and pd.notna(row["confidence"]):
        try:
            confidence_part = f"\nConfidence: {float(row['confidence']):.2f}"
        except Exception:
            confidence_part = ""

    return (
        f"{str(row['risk_level']).upper()} RISK FIRE ALERT\n"
        f"Source: {row.get('source', 'Unknown')}\n"
        f"Nearest CPT: {row.get('nearest_cpt', 'Unknown')}\n"
        f"Distance: {row.get('distance_km', 'NA')} km\n"
        f"Lat,Lon: {row.get('latitude', 'NA')}, {row.get('longitude', 'NA')}"
        f"{confidence_part}\n"
        f"Action: Check immediately"
    )


def format_no_fire_message():
    return (
        "SAA Fire Monitoring Update\n"
        "Status: No fire detected near monitored compartments\n"
        f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        "Source: Meteosat + VIIRS\n"
        "System: Operational"
    )



def send_to_telerivet_group(message_text: str):
    url = f"https://api.telerivet.com/v1/projects/{TELERIVET_PROJECT_ID}/send_broadcast"
    response = requests.post(
        url,
        auth=(TELERIVET_API_KEY, ""),
        json={
            "content": message_text,
            "group_id": TELERIVET_GROUP_ID,
            "message_type": "sms"
        },
        timeout=30
    )

    if not response.ok:
        print("Status code:", response.status_code)
        print("Response text:", response.text)
        response.raise_for_status()

    return response.json()


def send_message(message_text: str):
    result = send_to_telerivet_group(message_text)
    print("Message sent successfully:")
    print(result)
    print("-" * 70)

def main():
    init_alert_db()

    if not TELERIVET_API_KEY or not TELERIVET_PROJECT_ID or not TELERIVET_GROUP_ID:
        raise ValueError("Missing Telerivet group settings in config.py")

    if not os.path.exists(ALERTS_FILE):
        print("ranked_fire_alerts.csv not found.")

        if should_send_no_fire_message():
            no_fire_message = format_no_fire_message()
            try:
                send_message(no_fire_message)
                update_last_system_message_time("no_fire")
            except Exception as e:
                print(f"Failed sending no-fire message: {e}")
        else:
            print("No-fire heartbeat not due yet.")
        return

    alerts = pd.read_csv(ALERTS_FILE)

    if alerts.empty:
        print("ranked_fire_alerts.csv is empty.")

        if should_send_no_fire_message():
            no_fire_message = format_no_fire_message()
            try:
                send_message(no_fire_message)
                update_last_system_message_time("no_fire")
            except Exception as e:
                print(f"Failed sending no-fire message: {e}")
        else:
            print("No-fire heartbeat not due yet.")
        return

    alerts = alerts[
        alerts["risk_level"].astype(str).str.lower().isin(["high", "medium"])
    ].copy()

    if not alerts.empty:
        sent_any = False

        for _, row in alerts.iterrows():
            alert_id = build_alert_id(row)

            if already_sent(alert_id):
                continue

            message_text = format_fire_message(row)

            try:
                send_message(message_text)
                mark_sent(alert_id)
                sent_any = True
            except Exception as e:
                print(f"Failed sending fire alert: {e}")

        if not sent_any:
            print("No new fire alerts to send.")

    else:
        print("No High or Medium fire alerts found.")

        if should_send_no_fire_message():
            no_fire_message = format_no_fire_message()
            try:
                send_message(no_fire_message)
                update_last_system_message_time("no_fire")
            except Exception as e:
                print(f"Failed sending no-fire message: {e}")
        else:
            print("No-fire heartbeat not due yet.")


if __name__ == "__main__":
    main()