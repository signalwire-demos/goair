<p align="center">
  <img src="web/img/logo.png" alt="GoAir" width="280">
</p>

<h1 align="center">GoAir - AI Flight Booking Agent</h1>

<p align="center">
  Voice-powered flight booking built on <a href="https://signalwire.com">SignalWire</a>.<br>
  Callers dial in, speak naturally, and the AI agent — <strong>Voyager</strong> — searches live flights via the Amadeus API, compares options, confirms pricing, books trips, and sends SMS confirmations. All by voice.
</p>

## How It Works

A caller dials a SignalWire phone number. Voyager answers, recognizes returning passengers by caller ID, and walks them through the entire booking flow conversationally. New callers set up a profile first (name, DOB, preferences, home airport), then book. Returning callers skip straight to "Where are you flying?"

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
                    (welcome!)   (by name)
                         │           │
                         v           │
                   Setup Profile     │
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
         │               │       Collect Dates &
         │               │         Passengers
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
         │               │         ┌─────┴─────┐       │
         │               │    (profile)    (no profile)│
         │               │         │           │       │
         │               │         v           v       │
         │               │   Create       Collect Pax  │
         │               │   Booking        Info       │
         │               │         │           │       │
         │               │         │     Create Booking│
         │               │         │           │       │
         │               │         └─────┬─────┘       │
         │               │               v             │
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
                  │  Amadeus   │        │  Google Maps  │   │   SQLite   │
                  │  Python SDK│        │  Geocoding    │   │  State DB  │
                  └────────────┘        └───────────────┘   └────────────┘
```

| Component | Purpose |
|-----------|---------|
| **voyager.py** | Main agent — state machine, SWAIG tools, per-call config |
| **state_store.py** | SQLite persistence for call state, bookings, and passenger profiles |
| **config.py** | Environment variable loader and validation |
| **web/** | Static dashboard — booking table, stats, click-to-call |

## Features

### Passenger Profiles
- Passengers are identified by caller ID (phone number)
- First-time callers go through a one-time profile setup: name, email, DOB, gender, seat preference, cabin preference, home airport
- Returning callers are greeted by name — profile data pre-fills everything
- Profiles are stored in SQLite and persist across calls
- Home airport is stored as a name during registration; IATA resolution happens via `resolve_location` when booking (with proper disambiguation for ambiguous cities)

### State Machine
Voyager uses a strict state machine with 13 steps. Each step has:
- **Task** — what the AI does in this step
- **Process** — step-by-step instructions
- **Functions** — which SWAIG tools are available (all others are disabled)
- **Valid steps** — which steps can be transitioned to next

This prevents the AI from jumping ahead, skipping steps, or calling tools out of order.

### Per-Call Dynamic Config
The `_per_call_config` callback runs before each request. The SDK creates an ephemeral copy of the agent — mutations never leak between calls. The callback:
- Looks up the passenger by phone number
- Sets `global_data` with the passenger profile (or marks as new caller)
- Modifies the state machine: returning callers skip `setup_profile`, new callers are forced through it

### Booking Flow (Amadeus)
1. **Search** — `GET /v2/shopping/flight-offers` — returns up to 3 options
2. **Price** — `POST /v1/shopping/flight-offers/pricing` — locks the fare
3. **Fresh Search + Match + Price** — at booking time, a fresh search gets current GDS segment times, the original flight is matched by carrier+flight numbers, then re-priced
4. **Book** — `POST /v1/booking/flight-orders` — creates the PNR immediately after fresh pricing
5. **SMS** — booking confirmation is sent via `result.send_sms()` directly from the booking tool

The fresh-search-before-booking pattern prevents `SEGMENT SELL FAILURE` errors caused by stale segment departure/arrival times (the pricing API echoes back input times, it does not refresh them from the GDS schedule).

### Data Architecture
- **Heavy Amadeus JSON** (flight offers, priced offers) — stored in SQLite `call_state` table, keyed by `call_id`
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
| `greeting` | none | `setup_profile`, `get_origin` | Welcome caller, detect new vs returning |
| `setup_profile` | `register_passenger` | `get_origin` | One-time profile collection for new callers |
| `get_origin` | `resolve_location` | `disambiguate_origin`, `get_destination` | Resolve departure airport |
| `disambiguate_origin` | `select_airport` | `get_destination` | Choose between multiple origin airports |
| `get_destination` | `resolve_location` | `disambiguate_destination`, `collect_dates` | Resolve arrival airport |
| `disambiguate_destination` | `select_airport` | `collect_dates` | Choose between multiple destination airports |
| `collect_dates` | `check_cheapest_dates`, `set_travel_dates`, `set_passenger_info` | `search_flights` | Dates, passenger count, and cabin class |
| `search_flights` | `search_flights` | `present_options`, `error_recovery` | Search Amadeus for flights |
| `present_options` | `select_flight` | `confirm_price`, `search_flights`, `collect_dates`, `error_recovery` | Read options, caller picks one |
| `confirm_price` | `get_flight_price` | `create_booking`, `collect_pax`, `present_options` | Confirm live price |
| `collect_pax` | none | `create_booking` | Collect name/email for new callers only |
| `create_booking` | `book_flight` | `wrap_up`, `error_recovery` | Book, send SMS, read PNR |
| `error_recovery` | `resolve_location`, `search_flights`, `check_cheapest_dates` | `get_origin`, `get_destination`, `collect_dates`, `search_flights`, `present_options` | Handle failures without re-collecting passenger info |
| `wrap_up` | none | (end) | Say goodbye |

## SWAIG Tools

| Tool | Parameters | Purpose |
|------|-----------|---------|
| `resolve_location` | `location_text`, `location_type` | Google Maps geocoding + Amadeus keyword/proximity search to resolve spoken locations to IATA codes |
| `select_airport` | `location_type`, `iata_code` | Pick one airport from disambiguation candidates |
| `register_passenger` | `first_name`, `last_name`, `email`, `date_of_birth`, `gender`, `seat_preference`?, `cabin_preference`?, `home_airport_name`? | Save new passenger profile (home airport stored as name, resolved later via `resolve_location`) |
| `set_travel_dates` | `departure_date`, `return_date`? | Store confirmed travel dates |
| `set_passenger_info` | `adults`, `cabin_class` | Store passenger count and cabin preference |
| `search_flights` | (none) | Search Amadeus using stored state, returns up to 3 voice-friendly summaries |
| `select_flight` | `option_number` | Lock in the caller's choice (1, 2, or 3) |
| `get_flight_price` | (none) | Confirm live price via Amadeus pricing API |
| `book_flight` | `first_name`?, `last_name`?, `email`?, `phone`? | Fresh search + match + price + book + SMS confirmation. Falls back to passenger profile for all parameters |
| `check_cheapest_dates` | `month`? | Find cheapest travel dates for flexible callers |
| `summarize_conversation` | `summary` | Post-call summary (called automatically) |

All tools use `wait_file="/sounds/typing.mp3"` — the SDK resolves the relative path to a full URL using the agent's base URL.

## Setup

### Prerequisites
- Python 3.10+
- A [SignalWire](https://signalwire.com) account with a phone number
- [Amadeus Self-Service](https://developers.amadeus.com) API credentials (free sandbox available, production supported)
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

# Amadeus Self-Service
AMADEUS_CLIENT_ID=your-amadeus-key
AMADEUS_CLIENT_SECRET=your-amadeus-secret
AMADEUS_BASE_URL=https://test.api.amadeus.com   # or https://api.amadeus.com for production

# Google Maps
GOOGLE_MAPS_API_KEY=your-google-key

# AI Model
AI_MODEL=gpt-4o-mini

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
Stores heavy Amadeus JSON per active call. Cleaned up when the call ends.

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
| `pnr` | TEXT | Amadeus PNR code |
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
├── voyager.py            # Main agent (state machine, tools, per-call config)
├── state_store.py        # SQLite state store (call state, bookings, passengers)
├── config.py             # Environment variable loader
├── requirements.txt      # Python dependencies
├── .env.example          # Environment template
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

## Amadeus API Notes

GoAir works with both the Amadeus **test** and **production** environments. Set `AMADEUS_BASE_URL` accordingly:

| Environment | Base URL | Notes |
|-------------|----------|-------|
| Test (sandbox) | `https://test.api.amadeus.com` | Free, limited inventory, no real bookings |
| Production | `https://api.amadeus.com` | Live inventory, real PNRs, requires approved account |

### Sandbox-specific considerations
- The sandbox uses a copy of real airline inventory. Heavy test usage can exhaust seats on popular routes, causing `SEGMENT SELL FAILURE` (error 34651).
- Test with dates 3+ months out and less common routes for best results.
- Tokens last 30 minutes in both environments. The client auto-refreshes them.
- The sandbox rate limit is 1 request per 100ms. The client retries on 500 errors with backoff.

## License

[MIT](LICENSE)
