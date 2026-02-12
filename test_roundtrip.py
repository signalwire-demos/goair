#!/usr/bin/env python3
"""Scan Amadeus sandbox routes to find ones that work end-to-end.

Tests: search → price → SQLite round-trip → book
Reports working routes for both round-trip and one-way.
"""

import json
import sys
import time

import config
from amadeus import Client, ResponseError
from state_store import save_call_state, load_call_state, delete_call_state

CALL_ID = "test-route-scan"
DEPARTURE = "2026-08-01"
RETURN_DATE = "2026-08-05"
CABIN = "ECONOMY"

# Major routes likely to have sandbox coverage
ROUTES = [
    ("JFK", "LAX"),
    ("JFK", "LHR"),
    ("JFK", "CDG"),
    ("JFK", "MIA"),
    ("JFK", "ORD"),
    ("JFK", "SFO"),
    ("LAX", "SFO"),
    ("LAX", "ORD"),
    ("LAX", "LHR"),
    ("LAX", "NRT"),
    ("ORD", "MIA"),
    ("ORD", "DFW"),
    ("ORD", "LHR"),
    ("SFO", "NRT"),
    ("MIA", "MAD"),
    ("BOS", "LHR"),
    ("ATL", "JFK"),
    ("DFW", "JFK"),
    ("DEN", "LAX"),
    ("SEA", "LAX"),
    ("EWR", "CDG"),
    ("EWR", "LHR"),
    ("IAD", "FRA"),
    ("PHX", "JFK"),
    ("MCO", "JFK"),
]

TRAVELERS = [{
    "id": "1",
    "dateOfBirth": "1990-01-01",
    "name": {"firstName": "JOHN", "lastName": "SMITH"},
    "gender": "MALE",
    "contact": {
        "emailAddress": "john.smith@example.com",
        "phones": [{"deviceType": "MOBILE", "countryCallingCode": "1", "number": "5551234567"}],
    },
}]


def create_order(client, offer, travelers):
    """Book using the official SDK."""
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
                        {'subType': 'GENERAL_MISCELLANEOUS', 'text': 'VOYAGER TEST'}
                    ]
                },
            }
        })
        return response.data
    except ResponseError:
        return None


def test_route(client, origin, dest, return_date=None):
    """Test search → price → SQLite round-trip → book for a route.

    Returns dict with results for each stage.
    """
    mode = "RT" if return_date else "OW"
    result = {"route": f"{origin}→{dest}", "mode": mode,
              "search": False, "price": False, "sqlite": False, "book": False,
              "pnr": None, "offers": 0, "error": None, "segments": ""}

    # 1. Search
    params = {
        'originLocationCode': origin,
        'destinationLocationCode': dest,
        'departureDate': DEPARTURE,
        'adults': 1,
        'travelClass': CABIN,
        'max': 3,
        'currencyCode': 'USD',
    }
    if return_date:
        params['returnDate'] = return_date

    try:
        response = client.shopping.flight_offers_search.get(**params)
        offers = response.data
    except ResponseError as e:
        result["error"] = f"search: {e}"
        return result

    if not offers:
        result["error"] = "search: no offers"
        return result

    result["search"] = True
    result["offers"] = len(offers)

    # Capture segment info from first offer
    segs = []
    for itin in offers[0].get("itineraries", []):
        for seg in itin.get("segments", []):
            cc = seg.get("carrierCode", "")
            num = seg.get("number", "")
            dep = seg.get("departure", {}).get("iataCode", "")
            arr = seg.get("arrival", {}).get("iataCode", "")
            segs.append(f"{cc}{num} {dep}→{arr}")
    result["segments"] = " | ".join(segs)

    # 2. Price — try each offer until one works
    priced_offer = None
    for i, offer in enumerate(offers):
        if i > 0:
            time.sleep(0.5)
        try:
            priced_data = client.shopping.flight_offers.pricing.post(offer).data
            if priced_data and priced_data.get("flightOffers"):
                priced_offer = priced_data["flightOffers"][0]
                break
        except ResponseError:
            continue

    if not priced_offer:
        result["error"] = "price: all offers failed"
        return result

    result["price"] = True

    # 3. SQLite round-trip
    delete_call_state(CALL_ID)
    state = {"priced_offer": priced_offer}
    save_call_state(CALL_ID, state)
    loaded = load_call_state(CALL_ID)
    loaded_offer = loaded.get("priced_offer")

    orig_json = json.dumps(priced_offer, sort_keys=True)
    loaded_json = json.dumps(loaded_offer, sort_keys=True)
    result["sqlite"] = (orig_json == loaded_json)

    if not result["sqlite"]:
        result["error"] = "sqlite: data changed after round-trip"
        delete_call_state(CALL_ID)
        return result

    # 4. Book with SQLite-loaded offer
    time.sleep(0.5)
    order = create_order(client, loaded_offer, TRAVELERS)
    delete_call_state(CALL_ID)

    if order:
        result["book"] = True
        result["pnr"] = order.get("associatedRecords", [{}])[0].get("reference", "?")
    else:
        result["error"] = "book: order creation failed"

    return result


def main():
    config.validate()
    client = Client(
        client_id=config.AMADEUS_CLIENT_ID,
        client_secret=config.AMADEUS_CLIENT_SECRET,
        hostname='test' if 'test' in config.AMADEUS_BASE_URL else 'production',
    )

    print("=" * 80)
    print("  Amadeus Sandbox Route Scanner")
    print(f"  Departure: {DEPARTURE}  Return: {RETURN_DATE}  Cabin: {CABIN}")
    print(f"  Routes to test: {len(ROUTES)}")
    print("=" * 80)

    rt_results = []
    ow_results = []

    for i, (origin, dest) in enumerate(ROUTES):
        pct = f"[{i+1}/{len(ROUTES)}]"

        # Round-trip
        print(f"\n{pct} {origin}→{dest} round-trip ... ", end="", flush=True)
        time.sleep(0.6)
        rt = test_route(client, origin, dest, return_date=RETURN_DATE)
        status = "BOOK" if rt["book"] else ("PRICE" if rt["price"] else ("SEARCH" if rt["search"] else "FAIL"))
        pnr_info = f" PNR:{rt['pnr']}" if rt["pnr"] else ""
        print(f"{status}{pnr_info}")
        rt_results.append(rt)

        # One-way
        print(f"{pct} {origin}→{dest} one-way ... ", end="", flush=True)
        time.sleep(0.6)
        ow = test_route(client, origin, dest, return_date=None)
        status = "BOOK" if ow["book"] else ("PRICE" if ow["price"] else ("SEARCH" if ow["search"] else "FAIL"))
        pnr_info = f" PNR:{ow['pnr']}" if ow["pnr"] else ""
        print(f"{status}{pnr_info}")
        ow_results.append(ow)

    # Summary
    print("\n" + "=" * 80)
    print("  ROUND-TRIP RESULTS")
    print("=" * 80)
    print(f"  {'Route':<12} {'Search':>6} {'Price':>6} {'SQLite':>6} {'Book':>6}  {'PNR':<8}  Segments")
    print("  " + "-" * 76)
    for r in rt_results:
        check = lambda v: "YES" if v else " - "
        print(f"  {r['route']:<12} {check(r['search']):>6} {check(r['price']):>6} {check(r['sqlite']):>6} {check(r['book']):>6}  {r['pnr'] or '':.<8}  {r['segments'][:50]}")

    print("\n" + "=" * 80)
    print("  ONE-WAY RESULTS")
    print("=" * 80)
    print(f"  {'Route':<12} {'Search':>6} {'Price':>6} {'SQLite':>6} {'Book':>6}  {'PNR':<8}  Segments")
    print("  " + "-" * 76)
    for r in ow_results:
        check = lambda v: "YES" if v else " - "
        print(f"  {r['route']:<12} {check(r['search']):>6} {check(r['price']):>6} {check(r['sqlite']):>6} {check(r['book']):>6}  {r['pnr'] or '':.<8}  {r['segments'][:50]}")

    # Winners
    rt_bookable = [r for r in rt_results if r["book"]]
    ow_bookable = [r for r in ow_results if r["book"]]
    rt_priceable = [r for r in rt_results if r["price"] and not r["book"]]
    ow_priceable = [r for r in ow_results if r["price"] and not r["book"]]

    print("\n" + "=" * 80)
    print("  RECOMMENDED ROUTES")
    print("=" * 80)

    if rt_bookable:
        best = rt_bookable[0]
        print(f"\n  Round-trip (bookable): {best['route']}")
        print(f"    Segments: {best['segments']}")
        print(f"    PNR: {best['pnr']}")
    elif rt_priceable:
        best = rt_priceable[0]
        print(f"\n  Round-trip (priceable only): {best['route']}")
        print(f"    Segments: {best['segments']}")
    else:
        print("\n  Round-trip: NO WORKING ROUTES FOUND")

    if ow_bookable:
        best = ow_bookable[0]
        print(f"\n  One-way (bookable): {best['route']}")
        print(f"    Segments: {best['segments']}")
        print(f"    PNR: {best['pnr']}")
    elif ow_priceable:
        best = ow_priceable[0]
        print(f"\n  One-way (priceable only): {best['route']}")
        print(f"    Segments: {best['segments']}")
    else:
        print("\n  One-way: NO WORKING ROUTES FOUND")

    print()
    delete_call_state(CALL_ID)


if __name__ == "__main__":
    main()
