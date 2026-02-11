"""SQLite state store for Voyager call state.

Moves heavy Amadeus JSON (flight_offer, priced_offer) out of global_data
and into a local SQLite database keyed by call_id. Keeps global_data under
~1KB with only what the AI needs for conversation.
"""

import json
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "voyager_state.db"

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS call_state (
    call_id    TEXT PRIMARY KEY,
    state_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id          TEXT NOT NULL,
    pnr              TEXT,
    passenger_name   TEXT,
    email            TEXT,
    phone            TEXT,
    origin_iata      TEXT,
    origin_name      TEXT,
    destination_iata TEXT,
    destination_name TEXT,
    departure_date   TEXT,
    return_date      TEXT,
    cabin_class      TEXT,
    price            TEXT,
    currency         TEXT DEFAULT 'USD',
    status           TEXT NOT NULL DEFAULT 'confirmed'
                     CHECK(status IN ('confirmed','completed','cancelled')),
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS passengers (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    phone              TEXT UNIQUE NOT NULL,
    first_name         TEXT NOT NULL,
    last_name          TEXT NOT NULL,
    date_of_birth      TEXT,
    gender             TEXT CHECK(gender IN ('MALE', 'FEMALE')),
    email              TEXT,
    seat_preference    TEXT CHECK(seat_preference IN ('WINDOW', 'AISLE')),
    cabin_preference   TEXT CHECK(cabin_preference IN ('ECONOMY', 'PREMIUM_ECONOMY', 'BUSINESS', 'FIRST')),
    home_airport_iata  TEXT,
    home_airport_name  TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_passengers_phone ON passengers(phone);
"""

DEFAULT_STATE = {
    "origin": None,
    "destination": None,
    "departure_date": None,
    "return_date": None,
    "adults": 1,
    "cabin_class": "ECONOMY",
    "flight_offers": None,       # list of up to 3 full Amadeus offer objects
    "flight_summaries": None,    # list of voice-friendly summary strings
    "flight_offer": None,        # the selected offer (after caller picks one)
    "flight_summary": None,      # the selected offer's summary
    "priced_offer": None,
    "confirmed_price": None,
    "booking": None,
}


def _connect():
    """Open a new connection with WAL mode (cheap, thread-safe for ASGI)."""
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_CREATE_TABLES)
    return conn


def load_call_state(call_id):
    """Return the full state dict for a call, or defaults if missing."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT state_json FROM call_state WHERE call_id = ?", (call_id,)
        ).fetchone()
        if row:
            state = json.loads(row[0])
            # Merge with defaults so new keys are always present
            merged = {**DEFAULT_STATE, **state}
            return merged
        return dict(DEFAULT_STATE)
    finally:
        conn.close()


def save_call_state(call_id, state):
    """Upsert the JSON blob for a call."""
    now = time.time()
    blob = json.dumps(state, default=str)
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO call_state (call_id, state_json, created_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(call_id) DO UPDATE SET
                   state_json = excluded.state_json,
                   updated_at = excluded.updated_at""",
            (call_id, blob, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def delete_call_state(call_id):
    """Remove a call's state after the call ends."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM call_state WHERE call_id = ?", (call_id,))
        conn.commit()
        logger.info(f"Deleted state for call_id={call_id}")
    finally:
        conn.close()


def cleanup_stale_states(max_age_hours=24):
    """Prune abandoned calls older than max_age_hours."""
    cutoff = time.time() - (max_age_hours * 3600)
    conn = _connect()
    try:
        cursor = conn.execute(
            "DELETE FROM call_state WHERE updated_at < ?", (cutoff,)
        )
        conn.commit()
        if cursor.rowcount:
            logger.info(f"Cleaned up {cursor.rowcount} stale call states")
    finally:
        conn.close()


def build_ai_summary(state):
    """Extract a lightweight dict for global_data (~1KB).

    Only includes what the AI needs to conduct the conversation.
    Heavy objects (flight_offer, priced_offer) stay in SQLite only.
    """
    summary = {}

    # Origin/destination — AI needs these to talk about the trip
    if state.get("origin"):
        summary["origin"] = state["origin"]
    if state.get("destination"):
        summary["destination"] = state["destination"]

    # Candidates for disambiguation
    if state.get("origin_candidates"):
        summary["origin_candidates"] = state["origin_candidates"]
    if state.get("destination_candidates"):
        summary["destination_candidates"] = state["destination_candidates"]

    # Travel params
    if state.get("departure_date"):
        summary["departure_date"] = state["departure_date"]
    if state.get("return_date"):
        summary["return_date"] = state["return_date"]
    if state.get("adults"):
        summary["adults"] = state["adults"]
    if state.get("cabin_class"):
        summary["cabin_class"] = state["cabin_class"]

    # Status flags — AI knows whether pricing/offer exist without the data
    summary["has_flight_offers"] = bool(state.get("flight_offers"))
    summary["has_flight_offer"] = state.get("flight_offer") is not None
    summary["has_priced_offer"] = state.get("priced_offer") is not None

    # Flight summaries — AI reads these to the caller (text only, not heavy JSON)
    if state.get("flight_summaries"):
        summary["flight_summaries"] = state["flight_summaries"]
    if state.get("flight_summary"):
        summary["flight_summary"] = state["flight_summary"]

    # Confirmed price scalar
    if state.get("confirmed_price"):
        summary["confirmed_price"] = state["confirmed_price"]

    # Booking info — AI reads PNR/details to caller
    if state.get("booking"):
        summary["booking"] = state["booking"]

    return summary


# --- Bookings persistence ---

def save_booking(call_id, pnr, passenger_name, email, phone,
                 origin_iata, origin_name, destination_iata, destination_name,
                 departure_date, return_date, cabin_class, price, currency="USD"):
    """Insert a completed booking record."""
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO bookings
               (call_id, pnr, passenger_name, email, phone,
                origin_iata, origin_name, destination_iata, destination_name,
                departure_date, return_date, cabin_class, price, currency)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (call_id, pnr, passenger_name, email, phone,
             origin_iata, origin_name, destination_iata, destination_name,
             departure_date, return_date, cabin_class, price, currency),
        )
        conn.commit()
        logger.info(f"Saved booking PNR={pnr} for call_id={call_id}")
    finally:
        conn.close()


def get_all_bookings():
    """Return all bookings ordered by most recent first (for dashboard)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM bookings ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Passenger profiles ---

def get_passenger_by_phone(phone):
    """Lookup a passenger by phone number. Returns dict or None."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM passengers WHERE phone = ?", (phone,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_passenger(phone, first_name, last_name, **optional):
    """Upsert a passenger. COALESCE keeps existing values when new ones are None."""
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO passengers
               (phone, first_name, last_name, date_of_birth, gender,
                email, seat_preference, cabin_preference,
                home_airport_iata, home_airport_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(phone) DO UPDATE SET
                   first_name         = COALESCE(excluded.first_name, passengers.first_name),
                   last_name          = COALESCE(excluded.last_name, passengers.last_name),
                   date_of_birth      = COALESCE(excluded.date_of_birth, passengers.date_of_birth),
                   gender             = COALESCE(excluded.gender, passengers.gender),
                   email              = COALESCE(excluded.email, passengers.email),
                   seat_preference    = COALESCE(excluded.seat_preference, passengers.seat_preference),
                   cabin_preference   = COALESCE(excluded.cabin_preference, passengers.cabin_preference),
                   home_airport_iata  = COALESCE(excluded.home_airport_iata, passengers.home_airport_iata),
                   home_airport_name  = COALESCE(excluded.home_airport_name, passengers.home_airport_name),
                   updated_at         = datetime('now')""",
            (
                phone,
                first_name,
                last_name,
                optional.get("date_of_birth"),
                optional.get("gender"),
                optional.get("email"),
                optional.get("seat_preference"),
                optional.get("cabin_preference"),
                optional.get("home_airport_iata"),
                optional.get("home_airport_name"),
            ),
        )
        conn.commit()
        logger.info(f"Upserted passenger phone={phone}")
        return get_passenger_by_phone(phone)
    finally:
        conn.close()


def update_passenger(phone, **fields):
    """Partial update of allowed fields for an existing passenger."""
    allowed = {
        "first_name", "last_name", "date_of_birth", "gender",
        "email", "seat_preference", "cabin_preference",
        "home_airport_iata", "home_airport_name",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_passenger_by_phone(phone)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [phone]
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE passengers SET {set_clause}, updated_at = datetime('now') WHERE phone = ?",
            values,
        )
        conn.commit()
        logger.info(f"Updated passenger phone={phone}, fields={list(updates.keys())}")
        return get_passenger_by_phone(phone)
    finally:
        conn.close()
