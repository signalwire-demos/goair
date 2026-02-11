#!/usr/bin/env python3
"""End-to-end Amadeus API test: ATL → DFW, Jun 20, Economy, 1 pax."""

import sys
import json
import logging
import re
import time

import config
from amadeus_client import AmadeusClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

ORIGIN = "ATL"
DESTINATION = "DFW"
DEPARTURE = "2026-06-20"
RETURN = None
CABIN = "ECONOMY"
ADULTS = 1

# Test passenger
FIRST_NAME = "JOHN"
LAST_NAME = "SMITH"
EMAIL = "john.smith@example.com"
PHONE = "9185551234"


def divider(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def main():
    config.validate()

    client = AmadeusClient(
        config.AMADEUS_CLIENT_ID,
        config.AMADEUS_CLIENT_SECRET,
        config.AMADEUS_BASE_URL,
    )

    # ── Step 1: Airport Search ──────────────────────────────
    divider("STEP 1: Airport Search")

    print(f"\nSearching for origin: '{ORIGIN}'")
    origin_results = client.airport_city_search(ORIGIN)
    if not origin_results:
        print("FAIL: No results for origin")
        sys.exit(1)
    top_origin = next((r for r in origin_results if r.get("iataCode") == ORIGIN), origin_results[0])
    print(f"  Found: {top_origin.get('name')} ({top_origin.get('iataCode')})")

    time.sleep(0.5)
    print(f"\nSearching for destination: '{DESTINATION}'")
    dest_results = client.airport_city_search(DESTINATION)
    if not dest_results:
        print("FAIL: No results for destination")
        sys.exit(1)
    top_dest = next((r for r in dest_results if r.get("iataCode") == DESTINATION), dest_results[0])
    print(f"  Found: {top_dest.get('name')} ({top_dest.get('iataCode')})")

    # ── Step 2: Flight Offers Search (1 result) ─────────────
    divider("STEP 2: Flight Offers Search")
    time.sleep(1)
    trip_desc = f"{DEPARTURE} to {RETURN}" if RETURN else f"{DEPARTURE} (one-way)"
    print(f"\n{ORIGIN} -> {DESTINATION}, {trip_desc}, {CABIN}, {ADULTS} adult")

    offers, dictionaries, actual_cabin = client.flight_offers_search(
        origin=ORIGIN,
        destination=DESTINATION,
        departure_date=DEPARTURE,
        return_date=RETURN,
        adults=ADULTS,
        cabin_class=CABIN,
        max_results=1,
    )

    if not offers:
        print("FAIL: No flight offers returned")
        sys.exit(1)

    offer = offers[0]
    price = offer.get("price", {})
    total = price.get("grandTotal") or price.get("total", "?")
    currency = price.get("currency", "USD")
    carriers = dictionaries.get("carriers", {})
    itins = offer.get("itineraries", [])
    carrier = itins[0]["segments"][0]["carrierCode"] if itins else "?"
    airline = carriers.get(carrier, carrier)
    stops = len(itins[0].get("segments", [])) - 1 if itins else "?"
    print(f"  Best: {airline}, {stops} stop(s), ${total} {currency} (cabin: {actual_cabin})")

    # ── Step 3: Price Confirmation ──────────────────────────
    divider("STEP 3: Flight Offers Price")
    time.sleep(1)

    priced_data = client.flight_offers_price(offer)
    if not priced_data:
        print("\nFAIL: Pricing returned None (check error logs above)")
        sys.exit(1)

    priced_offers = priced_data.get("flightOffers", [])
    if not priced_offers:
        print("FAIL: No priced offers in response")
        sys.exit(1)

    priced_offer = priced_offers[0]
    confirmed_price = priced_offer.get("price", {})
    confirmed_total = confirmed_price.get("grandTotal") or confirmed_price.get("total", "?")
    confirmed_currency = confirmed_price.get("currency", "USD")
    print(f"  Confirmed price: ${confirmed_total} {confirmed_currency}")

    tp = priced_offer.get("travelerPricings", [])
    if tp:
        segs = tp[0].get("fareDetailsBySegment", [])
        if segs:
            print(f"  Cabin: {segs[0].get('cabin', '?')}")
            print(f"  Checked bags: {segs[0].get('includedCheckedBags', {})}")

    # ── Step 4: Create Booking (fresh search → match → price → book) ──
    divider("STEP 4: Fresh Search → Match → Price → Book")

    # The pricing API echoes back YOUR segment times — it doesn't refresh
    # them from the GDS schedule.  A fresh search gets current times, then
    # we match the same carrier+flight numbers, price, and book immediately.

    time.sleep(1)
    print(f"\nFresh search to get current segment schedule...")
    fresh_offers, fresh_dicts, _ = client.flight_offers_search(
        origin=ORIGIN, destination=DESTINATION,
        departure_date=DEPARTURE, return_date=RETURN,
        adults=ADULTS, cabin_class=CABIN, max_results=5,
    )
    if not fresh_offers:
        print("FAIL: Fresh search returned no offers")
        sys.exit(1)

    # Extract carrier+flight number pairs from original priced offer
    original_segments = []
    for itin in priced_offer.get("itineraries", []):
        for seg in itin.get("segments", []):
            original_segments.append(f"{seg.get('carrierCode','')}{seg.get('number','')}")
    print(f"  Original segments: {original_segments}")

    # Match in fresh results
    matched = None
    for fo in fresh_offers:
        fo_segs = []
        for itin in fo.get("itineraries", []):
            for seg in itin.get("segments", []):
                fo_segs.append(f"{seg.get('carrierCode','')}{seg.get('number','')}")
        if fo_segs == original_segments:
            matched = fo
            break

    if not matched:
        print(f"WARN: Exact match not found in {len(fresh_offers)} fresh offers, using first")
        matched = fresh_offers[0]
    else:
        print("  Matched original flight in fresh search results")

    # Price the fresh offer
    time.sleep(0.5)
    print("  Re-pricing fresh offer...")
    fresh_priced = client.flight_offers_price(matched)
    if not fresh_priced or not fresh_priced.get("flightOffers"):
        print("FAIL: Fresh pricing returned no offers")
        sys.exit(1)
    bookable_offer = fresh_priced["flightOffers"][0]
    bp = bookable_offer.get("price", {})
    print(f"  Fresh confirmed price: ${bp.get('grandTotal', '?')} {bp.get('currency', 'USD')}")

    # Book immediately with fresh priced offer
    travelers = [{
        "id": "1",
        "dateOfBirth": "1990-01-01",
        "name": {
            "firstName": FIRST_NAME,
            "lastName": LAST_NAME,
        },
        "gender": "MALE",
        "contact": {
            "emailAddress": EMAIL,
            "phones": [{
                "deviceType": "MOBILE",
                "countryCallingCode": "1",
                "number": re.sub(r"[^\d]", "", PHONE),
            }],
        },
    }]

    print(f"\nBooking for: {FIRST_NAME} {LAST_NAME} ({EMAIL})")
    time.sleep(0.5)

    order = client.flight_create_order(bookable_offer, travelers)
    if not order:
        print("\nFAIL: Booking returned None (sandbox may be flaky, try again)")
        print("NOTE: Steps 1-3 (search, price, confirm) all passed.")
        sys.exit(1)

    # Extract PNR
    associated = order.get("associatedRecords", [])
    pnr = associated[0].get("reference", "UNKNOWN") if associated else "UNKNOWN"
    order_id = order.get("id", "?")

    print(f"\n  BOOKED!")
    print(f"  PNR: {pnr}")
    print(f"  Order ID: {order_id}")

    booked_offers = order.get("flightOffers", [])
    if booked_offers:
        bp = booked_offers[0].get("price", {})
        print(f"  Final price: ${bp.get('grandTotal', '?')} {bp.get('currency', 'USD')}")

    traveler_list = order.get("travelers", [])
    if traveler_list:
        t = traveler_list[0]
        print(f"  Passenger: {t.get('name', {}).get('firstName')} {t.get('name', {}).get('lastName')}")

    # ── Summary ─────────────────────────────────────────────
    divider("TEST COMPLETE")
    print(f"""
  Route:      {ORIGIN} -> {DESTINATION}
  Dates:      {DEPARTURE}{' to ' + RETURN if RETURN else ' (one-way)'}
  Cabin:      {actual_cabin}
  Search:     ${total} {currency}
  Confirmed:  ${confirmed_total} {confirmed_currency}
  PNR:        {pnr}
  Order ID:   {order_id}
  Status:     ALL STEPS PASSED
""")


if __name__ == "__main__":
    main()
