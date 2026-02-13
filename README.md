<p align="center">
  <img src="web/img/logo.png" alt="GoAir" width="280">
</p>

<h1 align="center">GoAir - AI Flight Booking Agent</h1>

<p align="center">
  Voice-powered flight booking built on <a href="https://signalwire.com">SignalWire</a>.<br>
  Callers dial in, speak naturally, and the AI agent — <strong>Voyager</strong> — searches flights, compares options, confirms pricing, books trips, and sends SMS confirmations. All by voice.
</p>

## How It Works

A caller dials a SignalWire phone number. Voyager answers, recognizes returning passengers by caller ID, and walks them through the entire booking flow conversationally. New callers set up a profile first via the `info_gatherer` skill — it sequences questions automatically without per-field state-machine steps. Returning callers skip straight to "Where are you flying?"

```
                          Caller dials in
                               │
                    _per_call_config (phone lookup)
                               │
                         ┌─────┴─────┐
                     new caller?  returning?
                         │           │
                         v           v
                      Greeting    Greeting
                    (welcome +    (by name)
                  start profile)     │
                         │           │
                         v           │
                   collect_profile   │
                   (info_gatherer    │
                    skill: profile)  │
                         │           │
                  resolve_location   │
                  (mode=verify for   │
                   home airport)     │
                         │           │
                    finalize_profile │
                   (extracts IATA)   │
                         │           │
                         └─────┬─────┘
                               v
         ┌──────────────> Get Origin ──────────┐
         │                     │               v
         │              (single match)   Disambiguate
         │                     │           Origin
         │                     │               │
         │                     └─────┬─────────┘
         │                           v
         │               ┌─── Get Destination ─────┐
         │               │         │               v
         │               │  (single match)   Disambiguate
         │               │         │          Destination
         │               │         │               │
         │               │         └─────┬─────────┘
         │               │               v
         │               │        Collect Trip Type
         │               │          (select_trip_type)
         │               │               │
         │               │          ┌────┴────┐
         │               │     round-trip?  one-way?
         │               │          │         │
         │               │          v         v
         │               │   collect_booking  collect_booking
         │               │     _roundtrip       _oneway
         │               │   (info_gatherer   (info_gatherer
         │               │    skill)           skill)
         │               │          │         │
         │               │          └────┬────┘
         │               │               v
         │               │        finalize_booking
         │               │               │
         │               │               v
         │               │        Search Flights ──────┐
         │               │               │             │
         │               │               v             │
         │  (change      │       Present Options       │
         │   route)      │               │             │
         │               │               v             │
         │               │        Confirm Price        │
         │               │               │             │
         │               │               v             │
         │               │        Create Booking       │
         │               │               │             │
         │               │           Wrap Up           │
         │               │                             │
         │               │      Error Recovery  <──────┘
         │               │        │         │
         └───────────────┘        │         │
              (new dest)          └─────────┘
                              (retry search/dates)
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  SignalWire Cloud                                           │
│                                                             │
│  ┌──────────┐   ┌──────────────┐   ┌─────────────────────┐  │
│  │ Phone #  │──>│  SWML Engine │──>│  Voyager Agent      │  │
│  └──────────┘   └──────────────┘   │  (signalwire-agents)│  │
│                                    └──────────┬──────────┘  │
└───────────────────────────────────────────────┼─────────────┘
                                                │
                        ┌───────────────────────┼─────────────────┐
                        │                       │                 │
                  ┌─────▼──────┐        ┌───────▼───────┐   ┌─────▼──────┐
                  │  Mock      │        │  Google Maps  │   │   SQLite   │
                  │ Flight API │        │  Geocoding    │   │  State DB  │
                  └────────────┘        └───────────────┘   └────────────┘
```

| Component | Purpose |
|-----------|---------|
| **voyager.py** | Main agent — state machine, SWAIG tools, info_gatherer skills, per-call config |
| **mock_flight_api.py** | Mock flight API — Amadeus-compatible response shapes with realistic data generation |
| **state_store.py** | SQLite persistence for call state, bookings, and passenger profiles |
| **config.py** | Environment variable loader and validation |
| **web/** | Static dashboard — booking table, stats, click-to-call |

## Features

### Mock Flight API

The mock API (`mock_flight_api.py`) is a drop-in replacement for the Amadeus Self-Service SDK. It generates realistic flight data on the fly from a built-in database of 150+ airports and 28 airlines.

**Response shapes match the Amadeus JSON API exactly:**
- `mock_search_airports` — matches `GET /v1/reference-data/locations` (keyword search)
- `mock_nearest_airports` — matches `GET /v1/reference-data/locations/airports` (proximity search)
- `mock_search_flights` — matches `GET /v2/shopping/flight-offers` (flight search with up to 3 options)
- `mock_price_offer` — matches `POST /v1/shopping/flight-offers/pricing` (fare lock with 1-3% price bump)
- `mock_create_order` — matches `POST /v1/booking/flight-orders` (PNR creation)

**Realistic behavior:**
- Prices are distance-based with cabin multipliers, time-of-day adjustments, and random variance
- Route-aware airline selection (hub carriers preferred)
- Geographically reasonable connection hubs for 1-stop flights
- Timezone-correct departure/arrival times
- Randomized delays (1-9 seconds per call) simulate real Amadeus/GDS latency when `MOCK_DELAYS=true`

Set `MOCK_DELAYS=true` in your environment to enable the delays. They are **off by default** for fast development.

### Passenger Profiles
- Passengers are identified by caller ID (phone number)
- First-time callers go through a one-time profile setup: name, email, DOB, gender, seat preference, cabin preference, home airport
- Profile questions are handled by the `info_gatherer` skill (prefix: `profile`) — it sequences questions automatically with built-in confirmation support
- Returning callers are greeted by name — profile data pre-fills everything
- Profiles are stored in SQLite and persist across calls
- Home airport is resolved to an IATA code during profile setup via `resolve_location(mode='verify')` — the resolved name and code are stored as `"Airport Name (IATA)"` and the IATA code is extracted by `finalize_profile`

### State Machine
Voyager uses a strict state machine with 15 steps. Each step has:
- **Task** — what the AI does in this step
- **Process** — step-by-step instructions
- **Functions** — which SWAIG tools are available (all others are disabled)
- **Valid steps** — which steps can be transitioned to next

This prevents the AI from jumping ahead, skipping steps, or calling tools out of order.

Profile and booking data collection uses the `info_gatherer` skill (three instances with prefixes `profile`, `oneway`, `roundtrip`). All instances use `skip_prompt: True` to suppress the skill's default POM section — the conversation flow is managed entirely by step-level instructions. Each instance declares its questions as config — the skill handles sequencing, confirmation prompts, and answer storage in `global_data`. Bridge handlers (`finalize_profile`, `finalize_booking`) fire after each skill completes to validate, persist to SQLite, and transition to the next phase.

### Per-Call Dynamic Config
The `_per_call_config` callback runs before each request. The SDK creates an ephemeral copy of the agent — mutations never leak between calls. The callback:
- Looks up the passenger by phone number
- Merges caller data into `global_data` (using `update_global_data` to preserve skill state)
- Modifies the state machine: returning callers skip `collect_profile` and go to `get_origin`; new callers start in `greeting` with `profile_start_questions` available so the profile flow begins immediately
- A server-side guardrail forces `resolve_location` to `mode='verify'` during profile collection (when `is_new_caller=True` and no profile exists yet)

### Booking Flow
1. **Search** — `search_flights` uses the mock API to return up to 3 options with voice-friendly summaries
2. **Price** — `get_flight_price` confirms the live fare (mock adds 1-3% variance)
3. **Book** — `book_flight` creates the PNR using passenger profile from `global_data`
4. **SMS** — booking confirmation is sent via `result.send_sms()` directly from the booking tool

### Data Architecture
- **Flight JSON** (flight offers, priced offers) — stored in SQLite `call_state` table, keyed by `call_id`
- **Lightweight AI context** — `build_ai_summary()` extracts only what the AI needs (booleans, text summaries) into `global_data`, keeping it under ~1KB
- **Passenger profiles** — stored in SQLite `passengers` table, loaded into `global_data` per-call
- **Bookings** — persisted to SQLite `bookings` table for the dashboard

### Dashboard
A single-page web dashboard at `/` shows all bookings with:
- Stats: total, confirmed, completed, cancelled, revenue
- Filterable booking table with PNR, passenger, route, dates, cabin, price, status
- Click-to-call button linked to the SignalWire phone number
- Auto-refreshes every 30 seconds

## State Machine Steps

| Step | Functions | Next Steps | Purpose |
|------|-----------|------------|---------|
| `greeting` | `profile_start_questions` (new) / none (returning) | `collect_profile` (new) / `get_origin` (returning) | Welcome caller, start profile questions for new callers |
| `collect_profile` | `profile_start_questions`, `profile_submit_answer`, `finalize_profile`, `resolve_location` | `get_origin` | Collect new caller profile via info_gatherer, verify home airport |
| `get_origin` | `resolve_location` | `disambiguate_origin`, `get_destination` | Resolve departure airport |
| `disambiguate_origin` | `select_airport` | `get_destination` | Choose between multiple origin airports |
| `get_destination` | `resolve_location` | `disambiguate_destination`, `collect_trip_type` | Resolve arrival airport |
| `disambiguate_destination` | `select_airport` | `collect_trip_type` | Choose between multiple destination airports |
| `collect_trip_type` | `select_trip_type` | `collect_booking_oneway`, `collect_booking_roundtrip` | Branch on round-trip vs one-way |
| `collect_booking_oneway` | `oneway_start_questions`, `oneway_submit_answer`, `finalize_booking` | `search_flights` | Collect one-way booking details via info_gatherer |
| `collect_booking_roundtrip` | `roundtrip_start_questions`, `roundtrip_submit_answer`, `finalize_booking` | `search_flights` | Collect round-trip booking details via info_gatherer |
| `search_flights` | `search_flights` | `present_options`, `error_recovery` | Search for flights |
| `present_options` | `select_flight` | `confirm_price`, `search_flights`, `collect_trip_type`, `error_recovery` | Read options, caller picks one |
| `confirm_price` | `get_flight_price` | `create_booking`, `present_options` | Confirm live price |
| `create_booking` | `book_flight` | `wrap_up`, `error_recovery` | Book, send SMS, read PNR |
| `error_recovery` | `resolve_location`, `search_flights` | `get_origin`, `get_destination`, `collect_trip_type`, `search_flights`, `present_options` | Handle failures without re-collecting passenger info |
| `wrap_up` | none | (end) | Say goodbye |

## SWAIG Tools

| Tool | Parameters | Purpose |
|------|-----------|---------|
| `resolve_location` | `location_text`, `location_type`, `mode` | Google Maps geocoding + mock keyword/proximity search to resolve spoken locations to IATA codes. `mode='verify'` returns the result without changing step (used during profile setup); `mode='normal'` (default) writes to call state and advances the step |
| `select_airport` | `location_type`, `iata_code` | Pick one airport from disambiguation candidates |
| `select_trip_type` | `trip_type` | Record round-trip or one-way, branch to the correct booking flow |
| `finalize_profile` | (none) | Read info_gatherer answers from `global_data`, create passenger record in SQLite, populate `global_data.passenger_profile` |
| `finalize_booking` | (none) | Read info_gatherer answers from `global_data`, validate dates (rejects past dates, return before departure), store booking details in call state |
| `search_flights` | (none) | Search using stored state, returns up to 3 voice-friendly summaries |
| `select_flight` | `option_number` | Lock in the caller's choice (1, 2, or 3) |
| `get_flight_price` | (none) | Confirm live price via mock pricing API |
| `book_flight` | (none) | Book flight + SMS confirmation. Uses passenger profile from `global_data`. Filler: "Booking that for you now" (en-US) |
| `summarize_conversation` | `summary` | Post-call summary (called automatically) |

The `info_gatherer` skill also registers its own tools per prefix: `{prefix}_start_questions` and `{prefix}_submit_answer`. These are managed by the skill and called by the AI during profile/booking collection steps.

All tools use `wait_file="/sounds/typing.mp3"` — the SDK resolves the relative path to a full URL using the agent's base URL.

## Setup

### Prerequisites
- Python 3.10+
- A [SignalWire](https://signalwire.com) account with a phone number
- [Google Maps](https://console.cloud.google.com) Geocoding API key

### Installation

```bash
git clone https://github.com/signalwire-demos/goair.git
cd goair
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Configuration

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

```env
# SignalWire
SIGNALWIRE_PROJECT_ID=your-project-id
SIGNALWIRE_TOKEN=your-token
SIGNALWIRE_SPACE=yourspace.signalwire.com
SIGNALWIRE_PHONE_NUMBER=+15551234567
DISPLAY_PHONE_NUMBER=(555) 123-4567
SWML_BASIC_AUTH_USER=user
SWML_BASIC_AUTH_PASSWORD=pass
SWML_PROXY_URL_BASE=https://your-public-url.ngrok.io

# Google Maps
GOOGLE_MAPS_API_KEY=your-google-key

# AI Model
AI_MODEL=gpt-oss-120b
AI_TOP_P=0.5
AI_TEMPERATURE=0.5

# Mock API — enable randomized delays to simulate Amadeus latency (default: false)
MOCK_DELAYS=false

# Server
HOST=0.0.0.0
PORT=3000
```

### Running

```bash
# Development
python voyager.py

# Production
gunicorn voyager:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:3000
```

The SWML endpoint URL (with auth) is logged on startup. Point your SignalWire phone number's webhook to this URL.

## Database Schema

SQLite database (`voyager_state.db`) with three tables:

### `call_state`
Stores flight JSON per active call. Cleaned up when the call ends.

| Column | Type | Description |
|--------|------|-------------|
| `call_id` | TEXT PK | SignalWire call identifier |
| `state_json` | TEXT | Full booking state (offers, priced offers, etc.) |
| `created_at` | REAL | Unix timestamp |
| `updated_at` | REAL | Unix timestamp |

### `bookings`
Permanent record of all completed bookings (used by the dashboard).

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `call_id` | TEXT | Originating call |
| `pnr` | TEXT | PNR code |
| `passenger_name` | TEXT | Full name |
| `email` | TEXT | Email address |
| `phone` | TEXT | Phone number |
| `origin_iata` / `origin_name` | TEXT | Departure airport |
| `destination_iata` / `destination_name` | TEXT | Arrival airport |
| `departure_date` / `return_date` | TEXT | Travel dates |
| `cabin_class` | TEXT | ECONOMY, BUSINESS, etc. |
| `price` / `currency` | TEXT | Fare amount and currency |
| `status` | TEXT | confirmed, completed, or cancelled |
| `created_at` | TEXT | Datetime |

### `passengers`
Persistent passenger profiles keyed by phone number.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `phone` | TEXT UNIQUE | Caller ID (used for lookup) |
| `first_name` / `last_name` | TEXT | Passenger name |
| `date_of_birth` | TEXT | YYYY-MM-DD |
| `gender` | TEXT | MALE or FEMALE |
| `email` | TEXT | Email address |
| `seat_preference` | TEXT | WINDOW or AISLE |
| `cabin_preference` | TEXT | ECONOMY, PREMIUM_ECONOMY, BUSINESS, or FIRST |
| `home_airport_iata` / `home_airport_name` | TEXT | Preferred departure airport |
| `created_at` / `updated_at` | TEXT | Datetime |

## Project Structure

```
goair/
├── voyager.py            # Main agent (state machine, tools, info_gatherer skills, per-call config)
├── mock_flight_api.py    # Mock flight API (Amadeus-compatible response shapes)
├── state_store.py        # SQLite state store (call state, bookings, passengers)
├── config.py             # Environment variable loader
├── requirements.txt      # Python dependencies
├── .env.example          # Environment template
├── test_flow.sh          # SWAIG function integration tests (swaig-test CLI)
├── test_roundtrip.py     # Roundtrip booking flow unit tests
├── INFO.md               # Detailed technical reference (APIs, state machine, resolution pipeline)
├── Procfile              # Heroku/Dokku process definition
├── app.json              # Heroku app manifest
├── CHECKS                # Dokku zero-downtime deploy health check
├── LICENSE               # MIT License
├── web/
│   ├── index.html        # Dashboard (bookings table, stats)
│   ├── img/
│   │   └── logo.png      # GoAir logo
│   └── sounds/
│       └── typing.mp3    # Wait file played while tools execute
├── .github/
│   └── workflows/
│       ├── deploy.yml    # Production deploy workflow
│       └── preview.yml   # Preview deploy workflow
└── calls/                # Saved call data JSON files (auto-created)
```

## Mock API vs Live Amadeus

The mock API is designed as a drop-in replacement. To switch to live Amadeus, replace the imports in `voyager.py`:

```python
# Current (mock)
from mock_flight_api import mock_search_airports, ...

# Live Amadeus (requires amadeus Python SDK + credentials)
from amadeus_client import search_airports, ...
```

The mock API covers the same endpoints and returns identical response shapes, so no other code changes are needed.

## License

[MIT](LICENSE)
