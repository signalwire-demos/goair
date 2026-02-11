#!/usr/bin/env python3
"""Test that simulates voyager.py's exact data flow through SQLite.

Reproduces: search → store → load → select → store → load → price → store → load → book
Each step round-trips through SQLite JSON serialization, same as the live agent.
"""

import json
import logging
import re
import sys
import time

import config
from amadeus import Client, ResponseError
from state_store import save_call_state, load_call_state, delete_call_state

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

CALL_ID = "test-roundtrip-debug"
ORIGIN = "ATL"
DESTINATION = "DFW"
DEPARTURE = "2026-06-20"
CABIN = "ECONOMY"


def dump_offer(label, offer):
    """Dump key fields of an offer for comparison."""
    print(f"\n  [{label}]")
    print(f"    keys: {sorted(offer.keys())}")
    print(f"    source: {offer.get('source')}")
    print(f"    lastTicketingDate: {offer.get('lastTicketingDate')}")
    print(f"    has travelerPricings: {bool(offer.get('travelerPricings'))}")
    tp = offer.get("travelerPricings", [])
    if tp:
        print(f"    travelerPricings[0] keys: {sorted(tp[0].keys())}")
    for i, itin in enumerate(offer.get("itineraries", [])):
        for seg in itin.get("segments", []):
            cc = seg.get("carrierCode", "")
            num = seg.get("number", "")
            dep = seg.get("departure", {})
            arr = seg.get("arrival", {})
            print(f"    itin[{i}] {cc}{num} "
                  f"{dep.get('iataCode','')}({dep.get('at','')}) → "
                  f"{arr.get('iataCode','')}({arr.get('at','')})")


def create_order(client, offer, travelers):
    """Book using the official SDK with contacts + ticketingAgreement."""
    contacts = [{
        "addresseeName": {
            "firstName": travelers[0]["name"]["firstName"],
            "lastName": travelers[0]["name"]["lastName"],
        },
        "purpose": "STANDARD",
        "address": {
            "lines": ["123 Main St"],
            "postalCode": "00000",
            "cityName": "New York",
            "countryCode": "US",
        },
        "emailAddress": travelers[0]["contact"]["emailAddress"],
        "phones": travelers[0]["contact"]["phones"],
    }]
    try:
        response = client.post('/v1/booking/flight-orders', {
            'data': {
                'type': 'flight-order',
                'flightOffers': [offer],
                'travelers': travelers,
                'contacts': contacts,
                'ticketingAgreement': {
                    'option': 'DELAY_TO_CANCEL',
                    'delay': '6D',
                },
                'remarks': {
                    'general': [
                        {'subType': 'GENERAL_MISCELLANEOUS', 'text': 'VOYAGER AI BOOKING'}
                    ]
                },
            }
        })
        return response.data
    except ResponseError as e:
        logger.error(f"Booking failed: {e}")
        return None


def main():
    config.validate()
    client = Client(
        client_id=config.AMADEUS_CLIENT_ID,
        client_secret=config.AMADEUS_CLIENT_SECRET,
        hostname='test' if 'test' in config.AMADEUS_BASE_URL else 'production',
    )

    # Clean up any prior test state
    delete_call_state(CALL_ID)

    # ── Step 1: search_flights ──
    print("\n=== STEP 1: search_flights (search + store to SQLite) ===")
    response = client.shopping.flight_offers_search.get(
        originLocationCode=ORIGIN, destinationLocationCode=DESTINATION,
        departureDate=DEPARTURE, adults=1,
        travelClass=CABIN, max=3, currencyCode='USD',
    )
    offers = response.data
    if not offers:
        print("FAIL: no offers"); sys.exit(1)
    print(f"  Got {len(offers)} offers from search")

    state = {
        "origin": {"iata": ORIGIN},
        "destination": {"iata": DESTINATION},
        "departure_date": DEPARTURE,
        "adults": 1,
        "cabin_class": CABIN,
        "flight_offers": offers,
    }
    save_call_state(CALL_ID, state)
    print("  Saved to SQLite")

    # ── Step 2: select_flight ──
    print("\n=== STEP 2: select_flight (load → pick → store) ===")
    state = load_call_state(CALL_ID)
    loaded_offers = state.get("flight_offers", [])
    print(f"  Loaded {len(loaded_offers)} offers from SQLite")

    # Compare offer before/after round-trip
    original_json = json.dumps(offers[0], sort_keys=True)
    loaded_json = json.dumps(loaded_offers[0], sort_keys=True)
    if original_json == loaded_json:
        print("  ✓ Offer[0] survived SQLite round-trip intact")
    else:
        print("  ✗ OFFER CHANGED AFTER SQLITE ROUND-TRIP!")
        orig_keys = set(json.loads(original_json).keys())
        loaded_keys = set(json.loads(loaded_json).keys())
        if orig_keys != loaded_keys:
            print(f"    Missing keys: {orig_keys - loaded_keys}")
            print(f"    Extra keys: {loaded_keys - orig_keys}")
        else:
            orig_d = json.loads(original_json)
            loaded_d = json.loads(loaded_json)
            for k in sorted(orig_d.keys()):
                if json.dumps(orig_d[k], sort_keys=True) != json.dumps(loaded_d[k], sort_keys=True):
                    print(f"    DIFF in '{k}':")
                    print(f"      orig:   {json.dumps(orig_d[k], sort_keys=True)[:200]}")
                    print(f"      loaded: {json.dumps(loaded_d[k], sort_keys=True)[:200]}")

    state["flight_offer"] = loaded_offers[0]
    save_call_state(CALL_ID, state)

    # ── Step 3: get_flight_price ──
    print("\n=== STEP 3: get_flight_price (load → price API → store) ===")
    state = load_call_state(CALL_ID)
    offer_from_db = state.get("flight_offer")
    dump_offer("offer sent to pricing API (from SQLite)", offer_from_db)

    time.sleep(0.5)
    priced_data = client.shopping.flight_offers.pricing.post(offer_from_db).data
    if not priced_data or not priced_data.get("flightOffers"):
        print("FAIL: pricing returned nothing"); sys.exit(1)

    priced_offer = priced_data["flightOffers"][0]
    dump_offer("priced offer from API (before SQLite store)", priced_offer)

    state["priced_offer"] = priced_offer
    save_call_state(CALL_ID, state)

    # ── Step 4: book_flight (load → book) ──
    print("\n=== STEP 4: book_flight (load priced offer from SQLite → book) ===")
    state = load_call_state(CALL_ID)
    loaded_priced = state.get("priced_offer")
    dump_offer("priced offer LOADED from SQLite", loaded_priced)

    # Compare priced offer before/after SQLite round-trip
    priced_orig_json = json.dumps(priced_offer, sort_keys=True)
    priced_loaded_json = json.dumps(loaded_priced, sort_keys=True)
    if priced_orig_json == priced_loaded_json:
        print("\n  ✓ Priced offer survived SQLite round-trip intact")
    else:
        print("\n  ✗ PRICED OFFER CHANGED AFTER SQLITE ROUND-TRIP!")
        orig_d = json.loads(priced_orig_json)
        loaded_d = json.loads(priced_loaded_json)
        for k in sorted(set(list(orig_d.keys()) + list(loaded_d.keys()))):
            ov = json.dumps(orig_d.get(k), sort_keys=True)
            lv = json.dumps(loaded_d.get(k), sort_keys=True)
            if ov != lv:
                print(f"    DIFF in '{k}':")
                print(f"      before: {ov[:300]}")
                print(f"      after:  {lv[:300]}")

    # Now try booking directly with the SQLite-loaded priced offer
    travelers = [{
        "id": "1",
        "dateOfBirth": "1990-01-01",
        "name": {"firstName": "JOHN", "lastName": "SMITH"},
        "gender": "MALE",
        "contact": {
            "emailAddress": "john.smith@example.com",
            "phones": [{"deviceType": "MOBILE", "countryCallingCode": "1", "number": "9185551234"}],
        },
    }]

    print("\n  Attempting booking with SQLite-loaded priced offer...")
    time.sleep(0.5)
    order = create_order(client, loaded_priced, travelers)
    if order:
        pnr = order.get("associatedRecords", [{}])[0].get("reference", "?")
        print(f"  ✓ BOOKING SUCCEEDED — PNR: {pnr}")
    else:
        print("  ✗ BOOKING FAILED with SQLite-loaded priced offer")

        # Now try the test_api_flow way: fresh search → match → reprice → book
        print("\n  Trying fresh search → reprice → book (like test_api_flow)...")
        time.sleep(0.5)
        fresh_response = client.shopping.flight_offers_search.get(
            originLocationCode=ORIGIN, destinationLocationCode=DESTINATION,
            departureDate=DEPARTURE, adults=1,
            travelClass=CABIN, max=5, currencyCode='USD',
        )
        fresh_offers = fresh_response.data

        original_segments = []
        for itin in loaded_priced.get("itineraries", []):
            for seg in itin.get("segments", []):
                original_segments.append(f"{seg.get('carrierCode','')}{seg.get('number','')}")

        matched = None
        for fo in (fresh_offers or []):
            segs = []
            for itin in fo.get("itineraries", []):
                for seg in itin.get("segments", []):
                    segs.append(f"{seg.get('carrierCode','')}{seg.get('number','')}")
            if segs == original_segments:
                matched = fo
                break

        if matched:
            dump_offer("fresh matched offer", matched)
            time.sleep(0.5)
            fresh_priced = client.shopping.flight_offers.pricing.post(matched).data
            if fresh_priced and fresh_priced.get("flightOffers"):
                bookable = fresh_priced["flightOffers"][0]
                dump_offer("fresh priced offer", bookable)
                time.sleep(0.5)
                order = create_order(client, bookable, travelers)
                if order:
                    pnr = order.get("associatedRecords", [{}])[0].get("reference", "?")
                    print(f"\n  ✓ FRESH SEARCH BOOKING SUCCEEDED — PNR: {pnr}")
                else:
                    print("\n  ✗ FRESH SEARCH BOOKING ALSO FAILED")
            else:
                print("  ✗ Fresh repricing failed")
        else:
            print(f"  ✗ No segment match in {len(fresh_offers or [])} fresh offers")
            if fresh_offers:
                dump_offer("fresh_offers[0] (no match)", fresh_offers[0])

    # Cleanup
    delete_call_state(CALL_ID)
    print("\n=== DONE ===\n")


if __name__ == "__main__":
    main()
