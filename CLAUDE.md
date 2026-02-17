# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GoAir is a voice-driven flight booking AI agent built on SignalWire's telephony platform. Callers dial a phone number, and an AI agent named "Voyager" guides them through flight search, selection, pricing, and booking—with SMS confirmation. The `signalwire-agents` SDK handles SWML routing and SWAIG tool dispatch.

## Setup and Running

```bash
# Activate venv first (always required)
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in credentials

# Run development server (listens on 0.0.0.0:3000)
python voyager.py

# Run in production
gunicorn voyager:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:3000
```

**Required env vars**: `SIGNALWIRE_PROJECT_ID`, `SIGNALWIRE_TOKEN`, `SIGNALWIRE_SPACE`, `SIGNALWIRE_PHONE_NUMBER`, `GOOGLE_MAPS_API_KEY`, `SWML_PROXY_URL_BASE`

**Optional**: `AMADEUS_CLIENT_ID`/`AMADEUS_CLIENT_SECRET` (omit to use mock API), `AI_MODEL` (default: `gpt-oss-120b`), `MOCK_DELAYS=true` (adds 1-9s per API call to simulate GDS latency)

## Testing

```bash
# Integration test: runs all SWAIG functions via swaig-test CLI
./test_flow.sh [--debug]

# Scan Amadeus sandbox for working routes (requires live credentials)
python test_roundtrip.py
```

## Architecture

### Key Files

- **`voyager.py`** — The entire agent: `VoyagerAgent` class, 16-step state machine, 12 SWAIG tool functions, per-call dynamic config
- **`mock_flight_api.py`** — Drop-in Amadeus SDK replacement for development; 150+ airports, 28 airlines, distance-based pricing, timezone-aware schedules
- **`state_store.py`** — SQLite persistence (WAL mode) for call state, bookings, and passenger profiles
- **`config.py`** — Environment variable loading with defaults and validation

### State Machine (16 steps in `voyager.py`)

Each step defines `valid_steps` (allowed transitions) and a small set of available SWAIG tools. The AI cannot skip steps or call tools out of order. Tools call `result.swml_change_step()` to force transitions.

Steps: `greeting` → `collect_profile` → `save_profile_step` → `get_origin` → `disambiguate_origin` → `get_destination` → `disambiguate_destination` → `collect_trip_type` → `collect_booking` → `search_and_present` → `search_flights` → `present_options` → `confirm_price` → `create_booking` → `error_recovery` → `wrap_up`

### Per-Call Dynamic Configuration

`_per_call_config()` runs before every request. It looks up the caller by phone number and:
- **Returning caller**: Pre-fills profile answers in `global_data`, skips `collect_profile` and `save_profile_step`, greets by name
- **New caller**: Starts at `greeting` → `collect_profile` (8-question `gather_info` mode)

The SDK creates an ephemeral agent copy per request so mutations don't leak between calls.

### Data Flow: Heavy JSON vs. Lightweight Backend Context

Flight offer JSON from Amadeus/mock API is stored in SQLite (`call_state` table), keyed by `call_id`. `global_data` is backend-only — SWAIG tools read it via `raw_data["global_data"]`, the AI cannot see it directly. A lightweight summary (~1KB) is kept in `global_data.booking_state` via `build_ai_summary()` in `state_store.py`:

```
Amadeus/mock returns full flight JSON
  → save_call_state(call_id, full_json)
  → build_ai_summary(state) → {has_flight_offers, flight_summaries, ...}
  → update_global_data({booking_state: summary})
  → AI makes decisions on summaries
  → Tool functions reload full JSON from DB when needed
```

### SQLite Tables (in `state_store.py`)

- **`call_state`**: Temporary; deleted when call ends. Holds full flight offer JSON.
- **`bookings`**: Permanent. Route, dates, PNR, price, passenger, status.
- **`passengers`**: Permanent, keyed by phone number. Profile data for returning callers.

### Mock vs. Live Amadeus

`mock_flight_api.py` provides the same response shapes as Amadeus SDK. To switch to live Amadeus, set `AMADEUS_CLIENT_ID` and `AMADEUS_CLIENT_SECRET` in `.env`—the `config.py` / import logic in `voyager.py` handles the switch.

### Voice Design Conventions

- `nato_spell()` — Reads PNR/codes as NATO phonetic alphabet
- `format_duration()` — ISO 8601 → human-readable ("2h 45m")
- `format_time_voice()` — 24h → 12h AM/PM for voice readback
- `summarize_offer()` — Converts flight offer JSON to voice-friendly sentence

### Web Dashboard

Single-page app at `/` showing booking stats and filterable table. Data from `/bookings` JSON endpoint. Auto-refreshes every 30s. Click-to-call button links to SignalWire phone number.

### Deployment

- **Heroku/Dokku**: `git push heroku main` — uses `Procfile` + gunicorn + uvicorn workers
- **Health checks**: Dokku uses `CHECKS` file for zero-downtime deploys
- **CI/CD**: `.github/workflows/deploy.yml` (production) and `preview.yml` (preview)
