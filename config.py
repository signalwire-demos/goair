"""Configuration loader for Voyager flight booking agent."""

import os
from dotenv import load_dotenv

load_dotenv()

# SignalWire
SIGNALWIRE_PROJECT_ID = os.getenv("SIGNALWIRE_PROJECT_ID", "")
SIGNALWIRE_TOKEN = os.getenv("SIGNALWIRE_TOKEN", "")
SIGNALWIRE_SPACE = os.getenv("SIGNALWIRE_SPACE", "")
SIGNALWIRE_PHONE_NUMBER = os.getenv("SIGNALWIRE_PHONE_NUMBER", "")
DISPLAY_PHONE_NUMBER = os.getenv("DISPLAY_PHONE_NUMBER", "")
SWML_BASIC_AUTH_USER = os.getenv("SWML_BASIC_AUTH_USER", "")
SWML_BASIC_AUTH_PASSWORD = os.getenv("SWML_BASIC_AUTH_PASSWORD", "")
SWML_PROXY_URL_BASE = os.getenv("SWML_PROXY_URL_BASE", "")

# Amadeus Self-Service
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID", "")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET", "")
AMADEUS_BASE_URL = os.getenv("AMADEUS_BASE_URL", "https://test.api.amadeus.com")

# Google Maps
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")

# AI Model
AI_MODEL = os.getenv("AI_MODEL", "claude-sonnet-4-20250514")
AI_TOP_P = float(os.getenv("AI_TOP_P", "0.5"))
AI_TEMPERATURE = float(os.getenv("AI_TEMPERATURE", "0.5"))

# Server
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "3000"))


def validate():
    """Validate required configuration is present."""
    missing = []
    if not AMADEUS_CLIENT_ID:
        missing.append("AMADEUS_CLIENT_ID")
    if not AMADEUS_CLIENT_SECRET:
        missing.append("AMADEUS_CLIENT_SECRET")
    if not GOOGLE_MAPS_API_KEY:
        missing.append("GOOGLE_MAPS_API_KEY")
    if not SIGNALWIRE_PHONE_NUMBER:
        missing.append("SIGNALWIRE_PHONE_NUMBER")
    if missing:
        print(f"WARNING: Missing config: {', '.join(missing)}")
        print("Some features may not work. Copy .env.example to .env and fill in values.")
