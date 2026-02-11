#!/usr/bin/env python3
"""Voyager - AI Travel Booking Agent powered by SignalWire."""

import os
import sys
import json
import logging
import re
from pathlib import Path
from dotenv import load_dotenv
from signalwire_agents import AgentBase, AgentServer
from signalwire_agents.core.function_result import SwaigFunctionResult

import config
from amadeus import Client as AmadeusSDK, ResponseError, Location
from state_store import (
    load_call_state, save_call_state, delete_call_state,
    cleanup_stale_states, build_ai_summary, save_booking, get_all_bookings,
    get_passenger_by_phone, create_passenger, update_passenger,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, force=True)
logger = logging.getLogger(__name__)

# Initialize Amadeus SDK (handles OAuth2 + content-type automatically)
amadeus = AmadeusSDK(
    client_id=config.AMADEUS_CLIENT_ID,
    client_secret=config.AMADEUS_CLIENT_SECRET,
    hostname='test' if 'test' in config.AMADEUS_BASE_URL else 'production',
)
config.validate()


# ── Amadeus SDK helpers (thin wrappers for error handling) ──────────────

def _log_amadeus_error(context, exc):
    """Extract and log full error details from an SDK ResponseError.

    The SDK's error formatting swallows details when source lacks 'parameter',
    and its JSON parser can fail if Content-Type includes charset. We parse
    the raw body as a fallback.
    """
    import json as _json
    try:
        result = exc.response.result
        if not result and exc.response.body:
            result = _json.loads(exc.response.body)
        if result:
            for err in result.get('errors', []):
                logger.error(
                    f"Amadeus {context} {exc.response.status_code}: "
                    f"[{err.get('code')}] {err.get('title', '')} - "
                    f"{err.get('detail', '')} (source: {err.get('source', {})})"
                )
            return
    except Exception:
        pass
    logger.error(f"Amadeus {context} failed: {exc}")


def _search_airports(keyword):
    """Search airports/cities by keyword. Returns list of location dicts."""
    try:
        return amadeus.reference_data.locations.get(
            keyword=keyword, subType=Location.ANY,
        ).data
    except ResponseError as e:
        logger.error(f"Airport search failed for '{keyword}': {e}")
        return []


def _nearest_airports(lat, lng):
    """Find nearest airports by coordinates. Returns list of location dicts."""
    try:
        return amadeus.reference_data.locations.airports.get(
            latitude=lat, longitude=lng, radius=100, sort='relevance',
        ).data
    except ResponseError as e:
        logger.error(f"Nearest airport search failed: {e}")
        return []


def _search_flights(origin, destination, departure_date, return_date=None,
                    adults=1, cabin_class="ECONOMY", max_results=5):
    """Search flight offers. Returns (offers, dictionaries, actual_cabin)."""
    params = {
        'originLocationCode': origin,
        'destinationLocationCode': destination,
        'departureDate': departure_date,
        'adults': adults,
        'travelClass': cabin_class,
        'max': max_results,
        'currencyCode': 'USD',
    }
    if return_date:
        params['returnDate'] = return_date
    try:
        response = amadeus.shopping.flight_offers_search.get(**params)
        return response.data, response.result.get('dictionaries', {}), cabin_class
    except ResponseError as e:
        logger.error(f"Flight search failed for {cabin_class}: {e}")
        if cabin_class != "ECONOMY":
            logger.info(f"Retrying search with ECONOMY (was {cabin_class})")
            params['travelClass'] = 'ECONOMY'
            try:
                response = amadeus.shopping.flight_offers_search.get(**params)
                return response.data, response.result.get('dictionaries', {}), 'ECONOMY'
            except ResponseError as e2:
                logger.error(f"Flight search ECONOMY retry also failed: {e2}")
        return [], {}, cabin_class


def _price_offer(offer):
    """Confirm live price on a flight offer. Returns pricing data dict or None."""
    try:
        return amadeus.shopping.flight_offers.pricing.post(offer).data
    except ResponseError as e:
        _log_amadeus_error("pricing", e)
        return None


def _create_order(offer, travelers):
    """Create a flight booking. Returns order data dict or None."""
    contacts = []
    if travelers:
        t0 = travelers[0]
        contact = t0.get("contact", {})
        name = t0.get("name", {})
        contact_entry = {
            "addresseeName": {
                "firstName": name.get("firstName", ""),
                "lastName": name.get("lastName", ""),
            },
            "purpose": "STANDARD",
            "address": {
                "lines": ["123 Main St"],
                "postalCode": "00000",
                "cityName": "New York",
                "countryCode": "US",
            },
        }
        if contact.get("emailAddress"):
            contact_entry["emailAddress"] = contact["emailAddress"]
        if contact.get("phones"):
            contact_entry["phones"] = contact["phones"]
        contacts.append(contact_entry)

    try:
        response = amadeus.post('/v1/booking/flight-orders', {
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
        # SDK swallows error details — parse from raw body if needed
        _log_amadeus_error("booking", e)
        return None


def _cheapest_dates(origin, destination, departure_date=None):
    """Find cheapest travel dates. Returns list of date/price entries."""
    params = {'origin': origin, 'destination': destination}
    if departure_date:
        params['departureDate'] = departure_date
    try:
        return amadeus.shopping.flight_dates.get(**params).data
    except ResponseError as e:
        logger.error(f"Cheapest dates failed: {e}")
        return []

# NATO phonetic alphabet for PNR readback
NATO = {
    "A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta",
    "E": "Echo", "F": "Foxtrot", "G": "Golf", "H": "Hotel",
    "I": "India", "J": "Juliet", "K": "Kilo", "L": "Lima",
    "M": "Mike", "N": "November", "O": "Oscar", "P": "Papa",
    "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango",
    "U": "Uniform", "V": "Victor", "W": "Whiskey", "X": "X-ray",
    "Y": "Yankee", "Z": "Zulu",
    "0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four",
    "5": "Five", "6": "Six", "7": "Seven", "8": "Eight", "9": "Nine",
}


def nato_spell(text):
    """Convert a string to NATO phonetic spelling."""
    return " ".join(NATO.get(c.upper(), c) for c in text if c.strip())


def format_duration(iso_duration):
    """Convert ISO 8601 duration (PT2H30M) to human-readable string."""
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", iso_duration or "")
    if not match:
        return iso_duration or "unknown"
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    elif hours:
        return f"{hours}h"
    return f"{minutes}m"


def summarize_offer(offer, index, dictionaries):
    """Summarize a flight offer into a voice-friendly string."""
    try:
        price = offer.get("price", {})
        total = price.get("grandTotal") or price.get("total", "?")
        currency = price.get("currency", "USD")

        itineraries = offer.get("itineraries", [])
        parts = []

        for i, itin in enumerate(itineraries):
            segments = itin.get("segments", [])
            if not segments:
                continue

            leg = "Outbound" if i == 0 else "Return"
            stops = len(segments) - 1
            stop_text = "nonstop" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}"

            first_seg = segments[0]
            last_seg = segments[-1]

            carrier_code = first_seg.get("carrierCode", "")
            carriers = dictionaries.get("carriers", {})
            airline = carriers.get(carrier_code, carrier_code)

            dep_time = first_seg.get("departure", {}).get("at", "")
            arr_time = last_seg.get("arrival", {}).get("at", "")

            # Format times for voice
            dep_display = dep_time[11:16] if len(dep_time) > 15 else dep_time
            arr_display = arr_time[11:16] if len(arr_time) > 15 else arr_time

            duration = format_duration(itin.get("duration", ""))

            parts.append(f"{leg}: {airline}, {stop_text}, departs {dep_display}, arrives {arr_display}, {duration}")

        return f"Option {index}: {', '.join(parts)}. ${total} {currency}"
    except Exception as e:
        logger.error(f"Error summarizing offer {index}: {e}")
        return f"Option {index}: details unavailable"


class VoyagerAgent(AgentBase):
    """Voyager - AI Travel Concierge"""

    def __init__(self):
        super().__init__(name="Voyager", route="/swml",
                         record_call=True, record_format="wav", record_stereo=True)

        # AI model
        self.set_param("ai_model", config.AI_MODEL)
        self.set_param("end_of_speech_timeout", 600)
        self.set_prompt_llm_params(top_p=0.3, temperature=1.0)

        # Personality
        self.prompt_add_section("Personality",
            "You are Voyager, a friendly AI travel concierge who helps callers find and book flights. "
            "Keep it warm and brief — the occasional travel quip is welcome, but efficiency comes first."
        )

        # Hard rules for voice behavior and tool discipline
        self.prompt_add_section("Rules", body="", bullets=[
            "This is a PHONE CALL. Keep every response to 1-2 short sentences.",
            "Ask ONE question at a time. Wait for the answer before continuing.",
            "NEVER make up IATA codes, airport names, flight details, prices, or PNRs. Always use the tools.",
            "NEVER skip a required tool call. Every airport needs resolve_location. Every price needs get_flight_price.",
            "Use airline NAMES not codes ('Delta' not 'DL'). Say times naturally ('seven thirty PM' not '19:30').",
            "Spell confirmation codes using the NATO phonetic alphabet.",
        ])

        # Voice
        self.add_language("English", "en-US", "azure.en-US-AvaNeural")
        self.add_hints(["Voyager", "IATA", "nonstop", "layover", "round trip"])

        # Post-prompt
        self.set_post_prompt("Summarize the conversation.")

        # State machine
        self._define_state_machine()

        # SWAIG tools
        self._define_tools()

        # Per-call dynamic config — SDK creates ephemeral copy per request
        self.set_dynamic_config_callback(self._per_call_config)

    def _define_state_machine(self):
        """Define conversation contexts and steps."""
        contexts = self.define_contexts()
        ctx = contexts.add_context("default")

        # GREETING
        greeting = ctx.add_step("greeting")
        greeting.add_section("Task", "Welcome the caller and determine travel intent")
        greeting.add_bullets("Process", [
            "Check ${global_data.is_new_caller}:",
            "  If RETURNING caller (is_new_caller is false): greet by name using ${global_data.passenger_profile}, ask where they want to fly, move to get_origin",
            "  If NEW caller: welcome them to Voyager, ask for their first and last name, then move to setup_profile",
            "If the caller provides origin and/or destination in their first response, note them for the next step",
        ])
        greeting.set_step_criteria("Travel intent detected or name collected for new caller")
        greeting.set_functions("none")
        greeting.set_valid_steps(["setup_profile", "get_origin"])

        # SETUP PROFILE (new callers only)
        setup_profile = ctx.add_step("setup_profile")
        setup_profile.add_section("Task", "Collect profile details for a new passenger")
        setup_profile.add_bullets("Process", [
            "The caller has already given their first and last name in the greeting step",
            "Ask one question at a time in this order:",
            "  1. Email: 'What is the best email address for booking confirmations?'",
            "  2. Date of birth: 'What is your date of birth?'",
            "  3. Gender: 'And should I have you down as male or female?'",
            "  4. Seat preference: 'Do you prefer a window or aisle seat?'",
            "  5. Cabin preference: 'Do you usually fly economy, premium economy, business, or first class?'",
            "  6. Home airport: 'And which airport do you normally fly from?'",
            "Once you have all answers, call register_passenger with ALL collected details including home_airport_name",
            "After register_passenger confirms, move to get_origin",
        ])
        setup_profile.add_bullets("Do NOT", [
            "Do NOT ask for all details at once — one question at a time",
            "Do NOT skip calling register_passenger — the profile must be saved",
        ])
        setup_profile.set_step_criteria("Profile saved via register_passenger")
        setup_profile.set_functions(["register_passenger"])
        setup_profile.set_valid_steps(["get_origin"])

        # GET ORIGIN
        get_origin = ctx.add_step("get_origin")
        get_origin.add_section("Task", "Collect the departure city or airport")
        get_origin.add_bullets("Process", [
            "Check the passenger profile for a home airport: ${global_data.passenger_profile.home_airport_name}",
            "  If a home airport is on file: offer it — 'Would you like to fly from [home airport name], or a different airport?'",
            "  If they confirm the home airport: call resolve_location with the home airport name and location_type='origin'",
            "  If they want a different airport: ask where they're flying from, then call resolve_location",
            "If no home airport is on file, or the caller already stated an origin city, call resolve_location with location_text and location_type='origin'",
            "If caller says an IATA code directly ('I'm flying from LAX'), still call resolve_location to validate it",
            "After resolve_location returns:",
            "  Single airport: Read back the airport name and city, ask 'Is that right?' If confirmed, move to get_destination",
            "  Multiple airports: Move to disambiguate_origin",
            "  No match: Ask the caller to try a different city name or be more specific",
        ])
        get_origin.add_bullets("Do NOT", [
            "Do NOT skip calling resolve_location — every airport must be resolved to an IATA code, even the home airport",
            "Do NOT call resolve_location until the caller has answered — ask first, WAIT for their spoken response, THEN resolve",
        ])
        get_origin.set_step_criteria("Origin airport resolved and confirmed")
        get_origin.set_functions(["resolve_location"])
        get_origin.set_valid_steps(["disambiguate_origin", "get_destination"])

        # DISAMBIGUATE ORIGIN
        disambiguate_origin = ctx.add_step("disambiguate_origin")
        disambiguate_origin.add_section("Task", "Ask the caller to choose between multiple origin airports")
        disambiguate_origin.add_bullets("Process", [
            "Present the top 2-3 airports by name and city: 'The New York area has JFK, LaGuardia, and Newark. Do you have a preference?'",
            "When the caller picks one, you MUST call select_airport with location_type='origin' and the IATA code from the candidates",
            "If the caller says 'any' or 'whichever is cheapest', call select_airport with the first candidate's IATA code",
            "Do NOT move to the next step until select_airport confirms the selection",
        ])
        disambiguate_origin.add_bullets("Do NOT", [
            "Do NOT ask about dates or destinations here",
            "Do NOT move to get_destination without calling select_airport first",
        ])
        disambiguate_origin.set_step_criteria("Origin airport stored via select_airport")
        disambiguate_origin.set_functions(["select_airport"])
        disambiguate_origin.set_valid_steps(["get_destination"])

        # GET DESTINATION
        get_destination = ctx.add_step("get_destination")
        get_destination.add_section("Task", "Collect the arrival city or airport")
        get_destination.add_bullets("Process", [
            "If the caller already stated a destination, call resolve_location with location_type='destination'",
            "After resolve_location returns:",
            "  Single airport: Confirm with caller, then move to collect_dates",
            "  Multiple airports: Move to disambiguate_destination",
            "  No match: Ask for clarification",
        ])
        get_destination.add_bullets("Do NOT", [
            "Do NOT discuss dates or fares here",
            "Do NOT skip resolve_location — every destination must be validated",
        ])
        get_destination.set_step_criteria("Destination airport resolved and confirmed")
        get_destination.set_functions(["resolve_location"])
        get_destination.set_valid_steps(["disambiguate_destination", "collect_dates"])

        # DISAMBIGUATE DESTINATION
        disambiguate_destination = ctx.add_step("disambiguate_destination")
        disambiguate_destination.add_section("Task", "Ask the caller to choose between multiple destination airports")
        disambiguate_destination.add_bullets("Process", [
            "Present the top 2-3 airports by name: 'London has Heathrow, Gatwick, and Stansted. Any preference?'",
            "When the caller picks one, you MUST call select_airport with location_type='destination' and the IATA code from the candidates",
            "If the caller says 'any', call select_airport with the first candidate's IATA code",
            "Do NOT move to the next step until select_airport confirms the selection",
        ])
        disambiguate_destination.add_bullets("Do NOT", [
            "Do NOT move to collect_dates without calling select_airport first",
        ])
        disambiguate_destination.set_step_criteria("Destination airport stored via select_airport")
        disambiguate_destination.set_functions(["select_airport"])
        disambiguate_destination.set_valid_steps(["collect_dates"])

        # COLLECT DATES & PASSENGERS
        collect_dates = ctx.add_step("collect_dates")
        collect_dates.add_section("Task", "Collect travel dates, passenger count, and cabin class")
        collect_dates.add_bullets("Process", [
            "Ask: 'When would you like to fly?'",
            "Handle relative dates: 'next Friday', 'in two weeks', 'mid-March' — resolve to YYYY-MM-DD",
            "Then ask: 'Is this round-trip or one-way?'",
            "If round-trip, ask: 'And when would you like to come back?'",
            "If the caller says 'whenever it's cheapest' or is flexible, call check_cheapest_dates and present the top 3 options",
            "Once the caller confirms the dates, call set_travel_dates to store them",
            "After set_travel_dates confirms, ask: 'How many passengers will be traveling?'",
            "For cabin class, check ${global_data.passenger_profile.cabin_preference}:",
            "  If set: suggest it — 'Last time you preferred business class. Same again, or would you like something different?'",
            "  If not set: ask — 'And would you prefer economy, business, or first class?'",
            "Once both are confirmed, call set_passenger_info to store them",
            "After set_passenger_info confirms, move to search_flights",
        ])
        collect_dates.add_bullets("Do NOT", [
            "Do NOT move forward until set_travel_dates has stored the dates",
            "Do NOT assume 1 passenger — ask explicitly",
            "Do NOT move to search_flights until set_passenger_info has stored the details",
        ])
        collect_dates.set_step_criteria("Dates and passenger info stored")
        collect_dates.set_functions(["check_cheapest_dates", "set_travel_dates", "set_passenger_info"])
        collect_dates.set_valid_steps(["search_flights"])

        # SEARCH FLIGHTS
        search_flights_step = ctx.add_step("search_flights")
        search_flights_step.add_section("Task", "Search for flights and prepare up to 3 options for presentation")
        search_flights_step.add_bullets("Process", [
            "Call search_flights — it takes no parameters, it reads everything from booking_state automatically",
            "The function returns up to 3 options pre-summarized for voice",
            "Move to present_options to read them to the caller",
            "If no results: move to error_recovery",
        ])
        search_flights_step.set_step_criteria("Flight search completed")
        search_flights_step.set_functions(["search_flights"])
        search_flights_step.set_valid_steps(["present_options", "error_recovery"])

        # PRESENT OPTIONS
        present_options = ctx.add_step("present_options")
        present_options.add_section("Task", "Present up to 3 flight options and let the caller pick one")
        present_options.add_bullets("Process", [
            "Read ALL flight options from flight_summaries: airline, stops, departure/arrival times, duration, and price",
            "For each option, say 'Option 1...', 'Option 2...', 'Option 3...'",
            "Ask: 'Which option would you like? Or should I look at different dates?'",
            "When the caller picks an option, call select_flight with that option_number (1, 2, or 3)",
            "If caller wants different dates: move back to collect_dates",
            "If caller wants to change origin/destination: move to error_recovery",
        ])
        present_options.add_bullets("Do NOT", [
            "Do NOT skip to booking — the caller must pick an option first, then price must be confirmed",
            "Do NOT move to confirm_price without calling select_flight first",
        ])
        present_options.set_step_criteria("Caller selects an option via select_flight")
        present_options.set_functions(["select_flight"])
        present_options.set_valid_steps(["confirm_price", "search_flights", "collect_dates", "error_recovery"])

        # CONFIRM PRICE
        confirm_price = ctx.add_step("confirm_price")
        confirm_price.add_section("Task", "Confirm the live price on the selected flight")
        confirm_price.add_bullets("Process", [
            "Call get_flight_price — no parameters needed, it uses the option chosen via select_flight",
            "Read back: confirmed price, baggage allowance",
            "Ask: 'The confirmed price is $487.20 including taxes. Shall I book this?'",
            "If yes and ${global_data.passenger_profile} exists: go straight to create_booking — you already have all details",
            "If yes and no profile: move to collect_pax to get name and email",
            "If no: move back to present_options to pick a different option, or collect_dates for different dates",
        ])
        confirm_price.add_bullets("Do NOT", [
            "Do NOT use the search price as the final price — it may have changed",
            "Do NOT proceed to booking without explicit 'yes'",
        ])
        confirm_price.set_step_criteria("Price confirmed and caller accepts or declines")
        confirm_price.set_functions(["get_flight_price"])
        confirm_price.set_valid_steps(["create_booking", "collect_pax", "present_options"])

        # COLLECT PASSENGER INFO
        collect_pax = ctx.add_step("collect_pax")
        collect_pax.add_section("Task", "Collect passenger details for the booking")
        collect_pax.add_bullets("Process", [
            "Check ${global_data.passenger_profile} for existing details:",
            "  If profile exists: use it directly — DO NOT re-verify name, email, or phone. Just say something like 'Alright, booking under your name now!' and move straight to create_booking",
            "  If no profile: ask for first name, last name, email address. Use ${global_data.caller_phone} for phone",
            "  Spell back name and email only for NEW callers without a profile",
            "Once details are ready, move to create_booking",
        ])
        collect_pax.add_bullets("Do NOT", [
            "Do NOT proceed without confirmed passenger name",
            "Do NOT ask for passport or ID details in the sandbox demo",
            "Do NOT collect payment information — sandbox bookings are free test PNRs",
        ])
        collect_pax.set_step_criteria("Passenger details confirmed")
        collect_pax.set_functions("none")
        collect_pax.set_valid_steps(["create_booking"])

        # CREATE BOOKING
        create_booking = ctx.add_step("create_booking")
        create_booking.add_section("Task", "Book the flight and wrap up")
        create_booking.add_bullets("Process", [
            "Call book_flight — it reads passenger details from the profile automatically, no parameters needed",
            "book_flight handles everything: booking, SMS confirmation, and returns the PNR",
            "Read the PNR back to the caller using the NATO phonetic spelling provided",
            "Let them know the confirmation has been texted to their phone",
            "Thank them and say goodbye — move to wrap_up to end the call",
        ])
        create_booking.add_bullets("Do NOT", [
            "Do NOT ask for or verify name, email, or phone — book_flight pulls from the profile",
            "Do NOT skip the phonetic readback of the PNR",
        ])
        create_booking.set_step_criteria("Booking created, PNR read back, call ending")
        create_booking.set_functions(["book_flight"])
        create_booking.set_valid_steps(["wrap_up", "error_recovery"])

        # ERROR RECOVERY
        error_recovery = ctx.add_step("error_recovery")
        error_recovery.add_section("Task", "Handle booking failures, no results, and mid-flow changes")
        error_recovery.add_bullets("Process", [
            "IMPORTANT: All passenger details are already on file — do NOT re-ask for name, email, phone, or any profile info",
            "Booking failed / flight unavailable: 'That flight isn't available right now. Want me to search for a different option on the same route, or try a different route?'",
            "  If same route: move to search_flights to find new options",
            "  If different route: ask for the new destination, then move to get_destination",
            "No flights found: 'I didn't find any flights for those dates. Want to try different dates, or maybe a nearby airport?'",
            "Offer expired: 'That fare is no longer available. Let me search again for current options.' Move to search_flights",
            "Caller changed mind: 'No problem! Where would you like to fly instead?'",
        ])
        error_recovery.add_bullets("Do NOT", [
            "Do NOT start the entire flow over — keep what you have and only change what's needed",
        ])
        error_recovery.set_step_criteria("Recovery action taken")
        error_recovery.set_functions(["resolve_location", "search_flights", "check_cheapest_dates"])
        error_recovery.set_valid_steps(["get_origin", "get_destination", "collect_dates", "search_flights", "present_options"])

        # WRAP UP
        wrap_up = ctx.add_step("wrap_up")
        wrap_up.add_section("Task", "End the call")
        wrap_up.add_bullets("Process", [
            "Say a brief, warm goodbye: 'Thanks for flying with Voyager — have an amazing trip!'",
            "End the call",
        ])
        wrap_up.set_functions("none")
        wrap_up.set_valid_steps([])

    def _per_call_config(self, query_params, body_params, headers, agent):
        """Pre-populate passenger data on an ephemeral agent copy.

        The SDK deep-copies the POM, global_data, hints, etc. into `agent`
        before calling this. All mutations here are per-request and never
        leak into the shared instance or other concurrent calls.
        """
        call_data = (body_params or {}).get("call", {})
        caller_phone = call_data.get("from", "")

        passenger = get_passenger_by_phone(caller_phone) if caller_phone else None

        if passenger:
            # RETURNING CALLER — build profile dict
            profile = {
                "phone": passenger["phone"],
                "first_name": passenger["first_name"],
                "last_name": passenger["last_name"],
                "date_of_birth": passenger.get("date_of_birth"),
                "gender": passenger.get("gender"),
                "email": passenger.get("email"),
                "seat_preference": passenger.get("seat_preference"),
                "cabin_preference": passenger.get("cabin_preference"),
                "home_airport_iata": passenger.get("home_airport_iata"),
                "home_airport_name": passenger.get("home_airport_name"),
            }

            agent.set_global_data({
                "passenger_profile": profile,
                "is_new_caller": False,
                "caller_phone": caller_phone,
            })

            # Remove setup_profile / register_passenger — caller is already known
            ctx = agent._contexts_builder.get_context("default")
            greeting_step = ctx.get_step("greeting")
            greeting_step.set_functions("none")
            greeting_step.set_valid_steps(["get_origin"])

            setup_step = ctx.get_step("setup_profile")
            setup_step.set_functions("none")
            setup_step.set_valid_steps([])

            agent.prompt_add_section("Caller",
                f"This is a returning passenger named {passenger['first_name']} {passenger['last_name']} "
                f"calling from {caller_phone}. Greet them by name and ask where they'd like to fly."
            )
            agent.prompt_add_section("Passenger Profile", "${global_data.passenger_profile}")

        else:
            # NEW CALLER
            agent.set_global_data({
                "passenger_profile": None,
                "is_new_caller": True,
                "caller_phone": caller_phone,
            })

            # Force new callers through setup_profile — state machine enforced
            greeting_step = agent._contexts_builder.get_context("default").get_step("greeting")
            greeting_step.set_valid_steps(["setup_profile"])

            agent.prompt_add_section("New Caller",
                f"This is a new caller from {caller_phone}. Welcome them to Voyager "
                "and ask for their first and last name."
            )

    def _define_tools(self):
        """Define all SWAIG tool functions."""

        # Helper: extract call_id from raw_data
        def _call_id(raw_data):
            return raw_data.get("call_id", "unknown")

        def _sync_summary(result, state):
            """Save state to DB and sync lightweight summary to global_data."""
            result.update_global_data({"booking_state": build_ai_summary(state)})
            return result

        # --- Google Maps helpers for geocoding ---
        def geocode_location(location_text):
            """Use Google Geocoding API to get coordinates for a location."""
            try:
                import requests as _requests
                resp = _requests.get(
                    "https://maps.googleapis.com/maps/api/geocode/json",
                    params={
                        "address": location_text,
                        "key": config.GOOGLE_MAPS_API_KEY,
                    }
                )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", [])
                if not results:
                    return None
                loc = results[0]["geometry"]["location"]
                return {
                    "lat": loc["lat"],
                    "lng": loc["lng"],
                    "formatted": results[0].get("formatted_address", location_text),
                }
            except Exception as e:
                logger.error(f"Google Geocoding failed: {e}")
                return None

        # 1. RESOLVE LOCATION
        @self.tool(
            name="resolve_location",
            description="Resolve a spoken city or place name to IATA airport code(s). "
                        "Uses Google Maps for geocoding and Amadeus for airport lookup.",
            wait_file="/sounds/typing.mp3",
            parameters={
                "type": "object",
                "properties": {
                    "location_text": {
                        "type": "string",
                        "description": "The city, airport, or place name spoken by the caller",
                    },
                    "location_type": {
                        "type": "string",
                        "description": "Whether this is an 'origin' or 'destination'",
                        "enum": ["origin", "destination"],
                    },
                },
                "required": ["location_text", "location_type"],
            },
        )
        def resolve_location(args, raw_data):
            location_text = args["location_text"]
            location_type = args["location_type"]
            logger.info(f"resolve_location: text='{location_text}', type='{location_type}'")

            # Guard: profile must be complete before resolving airports
            global_data = (raw_data or {}).get("global_data", {})
            if global_data.get("is_new_caller") and not global_data.get("passenger_profile"):
                logger.warning("resolve_location: blocked — new caller has no profile yet")
                result = SwaigFunctionResult(
                    "The passenger profile is not set up yet. "
                    "Collect the caller's details and call register_passenger first."
                )
                result.swml_change_step("setup_profile")
                return result

            call_id = _call_id(raw_data)
            state = load_call_state(call_id)

            # Step 1: Google Geocoding for coordinates
            geo = geocode_location(location_text)

            # Step 2: Amadeus keyword search
            # Amadeus keyword API rejects long strings like "Miami, Florida" —
            # strip qualifiers after commas and keep just the city/airport name.
            keyword = location_text.split(",")[0].strip()
            keyword_results = _search_airports(keyword)

            # Step 3: Amadeus proximity search (if we have coordinates)
            proximity_results = []
            if geo:
                proximity_results = _nearest_airports(geo["lat"], geo["lng"])

            # Step 4: Cross-reference and rank
            candidates = {}

            for item in keyword_results:
                iata = item.get("iataCode")
                if not iata:
                    continue
                sub_type = item.get("subType", "")
                if sub_type == "CITY":
                    continue
                score = int(item.get("analytics", {}).get("travelers", {}).get("score", 0))
                name = item.get("name", iata).title()
                city = item.get("address", {}).get("cityName", "").title()
                if iata not in candidates or score > candidates[iata]["score"]:
                    candidates[iata] = {
                        "iata": iata,
                        "name": name,
                        "city": city,
                        "score": score,
                        "source": "keyword",
                    }

            for item in proximity_results:
                iata = item.get("iataCode")
                if not iata:
                    continue
                relevance = float(item.get("relevance", 0))
                name = item.get("name", iata).title()
                city = item.get("address", {}).get("cityName", "").title()
                # Merge: boost score if already in candidates
                if iata in candidates:
                    candidates[iata]["score"] += int(relevance)
                else:
                    candidates[iata] = {
                        "iata": iata,
                        "name": name,
                        "city": city,
                        "score": int(relevance),
                        "source": "proximity",
                    }

            if not candidates:
                return SwaigFunctionResult(
                    f"I couldn't find airports near '{location_text}'. "
                    "Ask the caller to try a different city name or be more specific."
                )

            # Sort by score descending
            ranked = sorted(candidates.values(), key=lambda x: x["score"], reverse=True)
            top = ranked[0]
            runner_up_score = ranked[1]["score"] if len(ranked) > 1 else 0

            # Step 5: Auto-select or disambiguate
            if top["score"] > 3 * max(runner_up_score, 1) or len(ranked) == 1:
                # Auto-select — clear winner
                airport_info = {
                    "iata": top["iata"],
                    "name": top["name"],
                    "city": top["city"],
                }
                if geo:
                    airport_info["lat"] = geo["lat"]
                    airport_info["lng"] = geo["lng"]

                state[location_type] = airport_info
                logger.info(f"resolve_location: auto-selected {top['iata']} for {location_type}")

                next_step = "get_destination" if location_type == "origin" else "collect_dates"
                result = SwaigFunctionResult(
                    f"The closest major airport is {top['name']} ({top['iata']})"
                    f"{' in ' + top['city'] if top['city'] else ''}. "
                    "Confirm with the caller that this is correct."
                )
                result.add_dynamic_hints([h for h in [top["name"], top["city"]] if h])
                save_call_state(call_id, state)
                _sync_summary(result, state)
                result.swml_change_step(next_step)
                return result
            else:
                # Multiple airports — need disambiguation
                top_3 = ranked[:3]
                airport_list = ", ".join(
                    f"{a['name']} ({a['iata']})" for a in top_3
                )

                # Store candidates for disambiguation step
                state[f"{location_type}_candidates"] = [
                    {"iata": a["iata"], "name": a["name"], "city": a["city"]}
                    for a in top_3
                ]
                logger.info(f"resolve_location: {len(top_3)} candidates for {location_type}")

                disambig_step = f"disambiguate_{location_type}"
                result = SwaigFunctionResult(
                    f"Found multiple airports: {airport_list}. "
                    "Ask the caller which they prefer."
                )
                hints = []
                for a in top_3:
                    hints.append(a["name"])
                    if a["city"]:
                        hints.append(a["city"])
                result.add_dynamic_hints(hints)
                save_call_state(call_id, state)
                _sync_summary(result, state)
                result.swml_change_step(disambig_step)
                return result

        # 2. SELECT AIRPORT
        @self.tool(
            name="select_airport",
            description="Select an airport from the disambiguation candidates and store it "
                        "in booking_state. Use after the caller picks from multiple airports.",
            wait_file="/sounds/typing.mp3",
            parameters={
                "type": "object",
                "properties": {
                    "location_type": {
                        "type": "string",
                        "description": "Whether this is an 'origin' or 'destination'",
                        "enum": ["origin", "destination"],
                    },
                    "iata_code": {
                        "type": "string",
                        "description": "The IATA code of the selected airport from the candidates list",
                    },
                },
                "required": ["location_type", "iata_code"],
            },
        )
        def select_airport(args, raw_data):
            # Guard: profile must be complete before resolving airports
            global_data = (raw_data or {}).get("global_data", {})
            if global_data.get("is_new_caller") and not global_data.get("passenger_profile"):
                result = SwaigFunctionResult(
                    "The passenger profile is not set up yet. "
                    "Collect the caller's details and call register_passenger first."
                )
                result.swml_change_step("setup_profile")
                return result

            location_type = args["location_type"]
            iata_code = args["iata_code"].upper().strip()
            call_id = _call_id(raw_data)
            state = load_call_state(call_id)

            candidates_key = f"{location_type}_candidates"
            candidates = state.get(candidates_key, [])

            if not candidates:
                return SwaigFunctionResult(
                    f"No {location_type} candidates found. Call resolve_location first."
                )

            # Find the selected airport in candidates
            selected = None
            for c in candidates:
                if c["iata"] == iata_code:
                    selected = c
                    break

            if not selected:
                available = ", ".join(f"{c['name']} ({c['iata']})" for c in candidates)
                return SwaigFunctionResult(
                    f"{iata_code} is not in the candidates. Available: {available}. "
                    "Ask the caller to choose from these."
                )

            # Store selected airport
            state[location_type] = {
                "iata": selected["iata"],
                "name": selected["name"],
                "city": selected["city"],
            }
            logger.info(f"select_airport: set state['{location_type}'] = {selected['iata']}")

            next_step = "get_destination" if location_type == "origin" else "collect_dates"
            result = SwaigFunctionResult(
                f"{selected['name']} ({selected['iata']}) selected as {location_type}. "
                "Confirm with the caller."
            )
            result.add_dynamic_hints([h for h in [selected["name"], selected["city"]] if h])
            save_call_state(call_id, state)
            _sync_summary(result, state)
            result.swml_change_step(next_step)
            return result

        # 3. REGISTER PASSENGER
        @self.tool(
            name="register_passenger",
            description="Save a new passenger's profile. Call after collecting name, email, DOB, gender, "
                        "preferences, and home airport during the setup_profile step.",
            wait_file="/sounds/typing.mp3",
            parameters={
                "type": "object",
                "properties": {
                    "first_name": {
                        "type": "string",
                        "description": "Passenger first name",
                    },
                    "last_name": {
                        "type": "string",
                        "description": "Passenger last name",
                    },
                    "email": {
                        "type": "string",
                        "description": "Passenger email address for booking confirmations",
                    },
                    "date_of_birth": {
                        "type": "string",
                        "description": "Date of birth in YYYY-MM-DD format",
                    },
                    "gender": {
                        "type": "string",
                        "description": "Gender",
                        "enum": ["MALE", "FEMALE"],
                    },
                    "seat_preference": {
                        "type": "string",
                        "description": "Seat preference",
                        "enum": ["WINDOW", "AISLE"],
                    },
                    "cabin_preference": {
                        "type": "string",
                        "description": "Cabin class preference",
                        "enum": ["ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"],
                    },
                    "home_airport_name": {
                        "type": "string",
                        "description": "The name or city of the passenger's preferred home airport (e.g. 'Los Angeles International' or 'LAX')",
                    },
                },
                "required": ["first_name", "last_name", "email", "date_of_birth", "gender"],
            },
        )
        def register_passenger(args, raw_data):
            global_data = (raw_data or {}).get("global_data", {})
            caller_phone = global_data.get("caller_phone", "")

            if not caller_phone:
                return SwaigFunctionResult(
                    "No caller phone number available. Cannot save profile."
                )

            first_name = args["first_name"].strip()
            last_name = args["last_name"].strip()
            email = args.get("email", "").strip() or None
            date_of_birth = args["date_of_birth"].strip()
            gender = args["gender"].strip().upper()
            home_airport_name = (args.get("home_airport_name") or "").strip() or None

            home_airport_iata = None  # Resolved later via resolve_location in get_origin

            logger.info(f"register_passenger: {first_name} {last_name}, phone={caller_phone}, "
                        f"home_airport={home_airport_iata or home_airport_name}")

            passenger = create_passenger(
                phone=caller_phone,
                first_name=first_name,
                last_name=last_name,
                date_of_birth=date_of_birth,
                gender=gender,
                email=email,
                seat_preference=args.get("seat_preference"),
                cabin_preference=args.get("cabin_preference"),
                home_airport_iata=home_airport_iata,
                home_airport_name=home_airport_name,
            )

            # Build profile for global_data
            profile = {
                "phone": caller_phone,
                "first_name": first_name,
                "last_name": last_name,
                "date_of_birth": date_of_birth,
                "gender": gender,
                "email": email,
                "seat_preference": args.get("seat_preference"),
                "cabin_preference": args.get("cabin_preference"),
                "home_airport_iata": home_airport_iata,
                "home_airport_name": home_airport_name,
            }

            result = SwaigFunctionResult(
                f"Profile saved for {first_name} {last_name}. "
                "Ask where they would like to fly today."
            )
            result.update_global_data({
                "passenger_profile": profile,
                "is_new_caller": False,
            })
            result.swml_change_step("get_origin")
            return result

        # 4. SET TRAVEL DATES
        @self.tool(
            name="set_travel_dates",
            description="Store the confirmed departure and return dates into booking_state. "
                        "Call this after the caller confirms their travel dates.",
            wait_file="/sounds/typing.mp3",
            parameters={
                "type": "object",
                "properties": {
                    "departure_date": {
                        "type": "string",
                        "description": "Departure date in YYYY-MM-DD format",
                    },
                    "return_date": {
                        "type": "string",
                        "description": "Return date in YYYY-MM-DD format. Omit for one-way trips.",
                    },
                },
                "required": ["departure_date"],
            },
        )
        def set_travel_dates(args, raw_data):
            call_id = _call_id(raw_data)
            state = load_call_state(call_id)

            departure_date = args.get("departure_date", "")
            return_date = args.get("return_date")

            if not departure_date:
                return SwaigFunctionResult(
                    "I need a departure date. Ask the caller when they want to fly."
                )

            state["departure_date"] = departure_date
            state["return_date"] = return_date
            logger.info(f"set_travel_dates: {departure_date}, return={return_date}")

            trip_type = "round-trip" if return_date else "one-way"
            result = SwaigFunctionResult(
                f"Dates stored: {trip_type}, departing {departure_date}"
                f"{', returning ' + return_date if return_date else ''}. "
                "Now ask how many passengers and what cabin class."
            )
            save_call_state(call_id, state)
            _sync_summary(result, state)
            return result

        # 4. SET PASSENGER INFO
        @self.tool(
            name="set_passenger_info",
            description="Store the number of passengers and cabin class into booking_state. "
                        "Call this after the caller confirms how many are traveling and their cabin preference.",
            wait_file="/sounds/typing.mp3",
            parameters={
                "type": "object",
                "properties": {
                    "adults": {
                        "type": "integer",
                        "description": "Number of adult passengers",
                    },
                    "cabin_class": {
                        "type": "string",
                        "description": "Cabin class preference",
                        "enum": ["ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"],
                    },
                },
                "required": ["adults", "cabin_class"],
            },
        )
        def set_passenger_info(args, raw_data):
            call_id = _call_id(raw_data)
            state = load_call_state(call_id)

            adults = args.get("adults", 1)
            cabin_class = args.get("cabin_class", "ECONOMY")

            state["adults"] = adults
            state["cabin_class"] = cabin_class
            logger.info(f"set_passenger_info: {adults} adults, {cabin_class}")

            pax = f"{adults} adult{'s' if adults > 1 else ''}"
            result = SwaigFunctionResult(
                f"Passenger info stored: {pax}, {cabin_class.lower().replace('_', ' ')}. "
                "Now searching for flights."
            )
            save_call_state(call_id, state)
            _sync_summary(result, state)
            result.swml_change_step("search_flights")
            return result

        # 4. SEARCH FLIGHTS
        @self.tool(
            name="search_flights",
            description="Search for up to 3 flight options using the origin, destination, dates, and passenger info "
                        "already stored in booking_state. No parameters needed — "
                        "everything comes from prior tool calls (resolve_location and set_travel_details).",
            wait_file="/sounds/typing.mp3",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )
        def search_flights(args, raw_data):
            call_id = _call_id(raw_data)
            state = load_call_state(call_id)

            origin = state.get("origin")
            destination = state.get("destination")
            departure_date = state.get("departure_date")

            if not origin:
                result = SwaigFunctionResult(
                    "No origin airport set. Need to resolve the departure city first."
                )
                result.swml_change_step("get_origin")
                return result

            if not destination:
                # Check if we have candidates waiting for selection
                candidates = state.get("destination_candidates")
                if candidates:
                    result = SwaigFunctionResult(
                        "Destination airport not selected yet. The caller needs to pick from the candidates."
                    )
                    result.swml_change_step("disambiguate_destination")
                else:
                    result = SwaigFunctionResult(
                        "No destination airport set. Need to resolve the destination city first."
                    )
                    result.swml_change_step("get_destination")
                return result

            if not departure_date:
                result = SwaigFunctionResult(
                    "No travel dates set. Need to collect dates from the caller first."
                )
                result.swml_change_step("collect_dates")
                return result

            origin_iata = origin["iata"]
            dest_iata = destination["iata"]
            return_date = state.get("return_date")
            adults = state.get("adults", 1)
            cabin = state.get("cabin_class", "ECONOMY")

            logger.info(f"search_flights: {origin_iata}->{dest_iata}, {departure_date}, "
                        f"return={return_date}, cabin={cabin}")

            offers, dictionaries, actual_cabin = _search_flights(
                origin=origin_iata,
                destination=dest_iata,
                departure_date=departure_date,
                return_date=return_date,
                adults=adults,
                cabin_class=cabin,
                max_results=3,
            )

            if not offers:
                result = SwaigFunctionResult(
                    f"No flights found from {origin_iata} to {dest_iata} on {departure_date}. "
                    "Ask the caller if they'd like to try different dates or a nearby airport."
                )
                result.swml_change_step("error_recovery")
                return result

            # Note if we fell back to a different cabin class
            cabin_note = ""
            if actual_cabin != cabin:
                cabin_note = (
                    f"Note: {cabin.lower().replace('_', ' ')} was not available on this route, "
                    f"showing {actual_cabin.lower().replace('_', ' ')} results instead. "
                    "Let the caller know. "
                )
                state["cabin_class"] = actual_cabin

            # Store all offers in DB, generate voice summaries
            summaries = []
            for i, offer in enumerate(offers):
                summaries.append(summarize_offer(offer, i + 1, dictionaries))

            state["flight_offers"] = offers
            state["flight_summaries"] = summaries

            summary_text = " | ".join(summaries)
            count = len(offers)
            result = SwaigFunctionResult(
                f"{cabin_note}"
                f"I found {count} option{'s' if count > 1 else ''}. {summary_text}. "
                "Read ALL options to the caller, then ask which one they prefer. "
                "When they choose, call select_flight with the option number (1, 2, or 3)."
            )
            save_call_state(call_id, state)
            _sync_summary(result, state)
            result.swml_change_step("present_options")
            return result

        # 5. SELECT FLIGHT
        @self.tool(
            name="select_flight",
            description="Select one of the flight options returned by search_flights. "
                        "Call this after the caller picks option 1, 2, or 3.",
            wait_file="/sounds/typing.mp3",
            parameters={
                "type": "object",
                "properties": {
                    "option_number": {
                        "type": "integer",
                        "description": "The option number the caller chose (1, 2, or 3)",
                    },
                },
                "required": ["option_number"],
            },
        )
        def select_flight(args, raw_data):
            option_number = args.get("option_number", 1)
            call_id = _call_id(raw_data)
            state = load_call_state(call_id)

            flight_offers = state.get("flight_offers") or []
            flight_summaries = state.get("flight_summaries") or []

            if not flight_offers:
                result = SwaigFunctionResult(
                    "No flight options available. Need to search for flights first."
                )
                result.swml_change_step("search_flights")
                return result

            idx = option_number - 1
            if idx < 0 or idx >= len(flight_offers):
                available = ", ".join(str(i + 1) for i in range(len(flight_offers)))
                return SwaigFunctionResult(
                    f"Invalid option {option_number}. Available options: {available}. "
                    "Ask the caller which option they prefer."
                )

            state["flight_offer"] = flight_offers[idx]
            state["flight_summary"] = flight_summaries[idx] if idx < len(flight_summaries) else None
            selected = flight_offers[idx]
            logger.info(f"select_flight: caller chose option {option_number}, "
                        f"offer id={selected.get('id') if isinstance(selected, dict) else 'N/A'}, "
                        f"keys={sorted(selected.keys()) if isinstance(selected, dict) else 'N/A'}")

            result = SwaigFunctionResult(
                f"Option {option_number} selected. "
                "Now confirm the price — move to confirm_price step."
            )
            save_call_state(call_id, state)
            _sync_summary(result, state)
            result.swml_change_step("confirm_price")
            return result

        # 6. GET FLIGHT PRICE
        @self.tool(
            name="get_flight_price",
            description="Confirm the exact price for the flight selected via select_flight. "
                        "No parameters needed — reads the stored offer automatically.",
            wait_file="/sounds/typing.mp3",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )
        def get_flight_price(args, raw_data):
            call_id = _call_id(raw_data)
            state = load_call_state(call_id)
            offer = state.get("flight_offer")

            if not offer:
                result = SwaigFunctionResult(
                    "No flight search results on file. Need to search for flights first."
                )
                result.swml_change_step("search_flights")
                return result

            logger.info("get_flight_price: attempt 1 — pricing stored offer directly")

            priced_data = _price_offer(offer)
            priced_offers = (priced_data or {}).get("flightOffers", [])

            # Fallback: if stored offer is rejected (expired/stale), do a
            # fresh search → match by carrier+flight numbers → price that.
            if not priced_offers:
                logger.warning("get_flight_price: stored offer rejected, trying fresh search fallback")
                origin = state.get("origin", {})
                destination = state.get("destination", {})
                dep_date = state.get("departure_date", "")
                return_date = state.get("return_date")
                adults = state.get("adults", 1)
                cabin = state.get("cabin_class", "ECONOMY")

                # Extract carrier+flight identifiers from the stored offer
                original_segments = []
                for itin in offer.get("itineraries", []):
                    for seg in itin.get("segments", []):
                        original_segments.append(
                            f"{seg.get('carrierCode', '')}{seg.get('number', '')}"
                        )
                logger.info(f"get_flight_price: matching segments {original_segments}")

                fresh_offers, fresh_dicts, _ = _search_flights(
                    origin=origin.get("iata", ""),
                    destination=destination.get("iata", ""),
                    departure_date=dep_date,
                    return_date=return_date,
                    adults=adults,
                    cabin_class=cabin,
                    max_results=10,
                )
                logger.info(f"get_flight_price: fresh search returned {len(fresh_offers or [])} offers")

                matched_offer = None
                for fo in (fresh_offers or []):
                    segs = []
                    for itin in fo.get("itineraries", []):
                        for seg in itin.get("segments", []):
                            segs.append(f"{seg.get('carrierCode', '')}{seg.get('number', '')}")
                    if segs == original_segments:
                        matched_offer = fo
                        break

                if matched_offer:
                    logger.info("get_flight_price: attempt 2 — pricing fresh matched offer")
                    priced_data = _price_offer(matched_offer)
                    priced_offers = (priced_data or {}).get("flightOffers", [])
                else:
                    logger.error(f"get_flight_price: no segment match in {len(fresh_offers or [])} fresh offers")

            if not priced_offers:
                return SwaigFunctionResult(
                    "Could not confirm the price — the offer may have expired. "
                    "Ask the caller if they'd like to search again."
                )

            priced_offer = priced_offers[0]
            price = priced_offer.get("price", {})
            total = price.get("grandTotal") or price.get("total", "?")
            currency = price.get("currency", "USD")

            # Extract baggage and fare rules if available
            traveler_pricings = priced_offer.get("travelerPricings", [])
            baggage_info = ""
            if traveler_pricings:
                segments = traveler_pricings[0].get("fareDetailsBySegment", [])
                if segments:
                    cabin_class = segments[0].get("cabin", "ECONOMY")
                    included_bags = segments[0].get("includedCheckedBags", {})
                    bag_qty = included_bags.get("quantity", 0)
                    bag_weight = included_bags.get("weight")
                    if bag_qty:
                        baggage_info = f"{bag_qty} checked bag{'s' if bag_qty > 1 else ''} included. "
                    elif bag_weight:
                        baggage_info = f"Checked bags up to {bag_weight}kg included. "
                    else:
                        baggage_info = "Carry-on only, checked bags extra. "

            state["priced_offer"] = priced_offer
            state["confirmed_price"] = f"${total} {currency}"
            logger.info(f"get_flight_price: confirmed ${total} {currency}")

            result = SwaigFunctionResult(
                f"The confirmed price is ${total} {currency} per person including taxes. "
                f"{baggage_info}"
                "Tell the caller the price and ask: 'Shall I book this?' "
                "If yes, proceed to booking."
            )
            save_call_state(call_id, state)
            _sync_summary(result, state)
            result.swml_change_step("collect_pax")
            return result

        # 6. BOOK FLIGHT
        @self.tool(
            name="book_flight",
            description="Book the confirmed flight. Creates a PNR. "
                        "Uses passenger_profile from global_data automatically. "
                        "Only pass parameters to override the profile (e.g. for new callers without a profile).",
            wait_file="/sounds/typing.mp3",
            parameters={
                "type": "object",
                "properties": {
                    "first_name": {
                        "type": "string",
                        "description": "Passenger first name (optional if profile exists)",
                    },
                    "last_name": {
                        "type": "string",
                        "description": "Passenger last name (optional if profile exists)",
                    },
                    "email": {
                        "type": "string",
                        "description": "Passenger email address (optional if profile exists)",
                    },
                    "phone": {
                        "type": "string",
                        "description": "Passenger phone number (optional if profile exists)",
                    },
                },
                "required": [],
            },
        )
        def book_flight(args, raw_data):
            # Pull from profile first, override with explicit args
            profile = (raw_data or {}).get("global_data", {}).get("passenger_profile") or {}
            caller_phone = (raw_data or {}).get("global_data", {}).get("caller_phone", "")

            first_name = (args.get("first_name") or profile.get("first_name") or "").strip()
            last_name = (args.get("last_name") or profile.get("last_name") or "").strip()
            email = (args.get("email") or profile.get("email") or "").strip()
            phone = (args.get("phone") or caller_phone or profile.get("phone") or "").strip()

            if not first_name or not last_name or not email:
                return SwaigFunctionResult(
                    "Missing passenger details — need at least first name, last name, and email. "
                    "Ask the caller for the missing info."
                )

            call_id = _call_id(raw_data)
            state = load_call_state(call_id)
            priced_offer = state.get("priced_offer")

            logger.info(f"book_flight: state check — "
                        f"origin={state.get('origin')}, "
                        f"destination={state.get('destination')}, "
                        f"priced_offer={'YES' if priced_offer else 'NO'}")

            # Guard: no destination
            if not state.get("destination"):
                candidates = state.get("destination_candidates")
                if candidates:
                    result = SwaigFunctionResult(
                        "Destination airport not selected yet. The caller needs to pick from the candidates."
                    )
                    result.swml_change_step("disambiguate_destination")
                else:
                    result = SwaigFunctionResult(
                        "No destination airport set. Need to collect the destination first."
                    )
                    result.swml_change_step("get_destination")
                return result

            # Guard: no confirmed price → back to pricing
            if not priced_offer:
                result = SwaigFunctionResult(
                    "No confirmed price on file. Need to confirm the fare first."
                )
                result.swml_change_step("confirm_price")
                return result

            logger.info(f"book_flight: {first_name} {last_name}, {email}")

            origin = state.get("origin", {})
            destination = state.get("destination", {})
            dep_date = state.get("departure_date", "")
            return_date = state.get("return_date")
            cabin = state.get("cabin_class", "ECONOMY")

            booking_offer = priced_offer
            price_changed = False
            old_total = priced_offer.get("price", {}).get("grandTotal") or priced_offer.get("price", {}).get("total")
            travelers = [{
                "id": "1",
                "dateOfBirth": profile.get("date_of_birth") or "1990-01-01",
                "name": {
                    "firstName": first_name.upper(),
                    "lastName": last_name.upper(),
                },
                "gender": profile.get("gender") or "MALE",
                "contact": {
                    "emailAddress": email,
                    "phones": [{
                        "deviceType": "MOBILE",
                        "countryCallingCode": "1",
                        "number": re.sub(r"[^\d]", "", phone),
                    }],
                },
            }]

            # Try 1: book directly with the priced offer from get_flight_price
            logger.info("book_flight: attempt 1 — booking with priced offer")
            order = _create_order(booking_offer, travelers)

            # Try 2: if direct booking failed, do a fresh search → match → reprice → book.
            # The pricing API echoes back YOUR segment times without refreshing from
            # the GDS, so stale times from the original search carry through.
            if not order:
                logger.warning("book_flight: attempt 1 FAILED, trying fresh search → reprice → book")
                origin_iata = origin.get("iata", "")
                dest_iata = destination.get("iata", "")
                adults = state.get("adults", 1)

                original_segments = []
                for itin in priced_offer.get("itineraries", []):
                    for seg in itin.get("segments", []):
                        original_segments.append(
                            f"{seg.get('carrierCode', '')}{seg.get('number', '')}"
                        )
                logger.info(f"book_flight: matching segments {original_segments}")

                fresh_offers, _, _ = _search_flights(
                    origin=origin_iata, destination=dest_iata,
                    departure_date=dep_date, return_date=return_date,
                    adults=adults, cabin_class=cabin, max_results=10,
                )
                logger.info(f"book_flight: fresh search returned {len(fresh_offers or [])} offers")

                matched_offer = None
                for fo in (fresh_offers or []):
                    segs = []
                    for itin in fo.get("itineraries", []):
                        for seg in itin.get("segments", []):
                            segs.append(f"{seg.get('carrierCode', '')}{seg.get('number', '')}")
                    if segs == original_segments:
                        matched_offer = fo
                        break

                if matched_offer:
                    fresh_price_data = _price_offer(matched_offer)
                    if fresh_price_data and fresh_price_data.get("flightOffers"):
                        booking_offer = fresh_price_data["flightOffers"][0]
                        fresh_total = booking_offer.get("price", {}).get("grandTotal") or booking_offer.get("price", {}).get("total")
                        price_changed = fresh_total != old_total
                        if price_changed:
                            logger.info(f"book_flight: price changed from ${old_total} to ${fresh_total}")
                        logger.info("book_flight: attempt 2 — booking with fresh priced offer")
                        order = _create_order(booking_offer, travelers)
                        if order:
                            logger.info("book_flight: attempt 2 SUCCEEDED")
                        else:
                            logger.error("book_flight: attempt 2 FAILED")
                    else:
                        logger.error("book_flight: fresh reprice returned no offers")
                else:
                    logger.error(f"book_flight: no segment match in {len(fresh_offers or [])} fresh offers")

            if not order:
                result = SwaigFunctionResult(
                    "The booking failed — this flight isn't available right now. "
                    "All passenger details are still on file — do NOT re-ask for name, email, or phone. "
                    "Ask the caller if they'd like to try a different flight on this route or a different route entirely."
                )
                _sync_summary(result, state)
                result.swml_change_step("error_recovery")
                return result

            # Extract PNR
            associated_records = order.get("associatedRecords", [])
            pnr = associated_records[0].get("reference", "UNKNOWN") if associated_records else "UNKNOWN"
            phonetic = nato_spell(pnr)

            price = booking_offer.get("price", {})
            total = price.get("grandTotal") or price.get("total", "?")

            state["booking"] = {
                "pnr": pnr,
                "phonetic": phonetic,
                "route": f"{origin.get('iata', '?')} to {destination.get('iata', '?')}",
                "departure": dep_date,
                "price": total,
                "passenger": f"{first_name} {last_name}",
                "email": email,
                "phone": phone,
            }

            logger.info(f"book_flight: PNR={pnr}, final price=${total}")

            # Update passenger profile email if missing
            if profile and not profile.get("email") and email:
                profile_phone = profile.get("phone", "")
                if profile_phone:
                    update_passenger(profile_phone, email=email)

            # Persist to bookings table for dashboard
            save_booking(
                call_id=call_id,
                pnr=pnr,
                passenger_name=f"{first_name} {last_name}",
                email=email,
                phone=phone,
                origin_iata=origin.get("iata", "?"),
                origin_name=origin.get("name", ""),
                destination_iata=destination.get("iata", "?"),
                destination_name=destination.get("name", ""),
                departure_date=dep_date,
                return_date=return_date,
                cabin_class=cabin,
                price=total,
                currency=price.get("currency", "USD"),
            )

            price_note = ""
            if price_changed:
                price_note = (
                    f"Note: the price changed slightly from ${old_total} to ${total} "
                    "since it was confirmed. Let the caller know. "
                )

            # Send SMS confirmation
            sms_body = (
                f"Voyager - Flight Confirmed!\n"
                f"PNR: {pnr}\n"
                f"Route: {origin.get('name', origin.get('iata', '?'))} to "
                f"{destination.get('name', destination.get('iata', '?'))}\n"
                f"Departure: {dep_date}\n"
                f"Price: ${total}\n"
                f"Passenger: {first_name} {last_name}\n"
                f"Thank you for using Voyager!"
            )

            result = SwaigFunctionResult(
                f"{price_note}"
                f"Booked! Confirmation code is {pnr} — that's {phonetic}. "
                f"Flight from {origin.get('name', origin.get('iata', '?'))} to "
                f"{destination.get('name', destination.get('iata', '?'))}, "
                f"departing {dep_date}, ${total}. "
                "A confirmation text has been sent to the caller's phone. "
                "Read the confirmation code using the phonetic spelling, "
                "let them know the details have been texted, thank them, and end the call."
            )
            result.send_sms(
                to_number=phone,
                from_number=config.SIGNALWIRE_PHONE_NUMBER,
                body=sms_body,
            )
            save_call_state(call_id, state)
            _sync_summary(result, state)
            result.swml_change_step("wrap_up")
            return result

        # 7. CHECK CHEAPEST DATES
        @self.tool(
            name="check_cheapest_dates",
            description="Find the cheapest travel dates for a route. "
                        "Use when the caller is flexible on dates.",
            wait_file="/sounds/typing.mp3",
            parameters={
                "type": "object",
                "properties": {
                    "month": {
                        "type": "string",
                        "description": "Target month in YYYY-MM format, or empty for next 30 days",
                    },
                },
                "required": [],
            },
        )
        def check_cheapest_dates(args, raw_data):
            call_id = _call_id(raw_data)
            state = load_call_state(call_id)

            origin = state.get("origin")
            destination = state.get("destination")

            if not origin or not destination:
                return SwaigFunctionResult(
                    "I need both origin and destination airports before checking dates."
                )

            month = args.get("month")
            departure_date = f"{month}-01" if month else None

            logger.info(f"check_cheapest_dates: {origin['iata']}->{destination['iata']}, month={month}")

            results = _cheapest_dates(
                origin=origin["iata"],
                destination=destination["iata"],
                departure_date=departure_date,
            )

            if not results:
                return SwaigFunctionResult(
                    "I couldn't find date-price data for that route. "
                    "Ask the caller for specific dates instead."
                )

            # Sort by price, take top 3
            sorted_dates = sorted(results, key=lambda x: float(x.get("price", {}).get("total", "99999")))[:3]

            options = []
            for d in sorted_dates:
                dep = d.get("departureDate", "?")
                ret = d.get("returnDate", "")
                price = d.get("price", {}).get("total", "?")
                if ret:
                    options.append(f"{dep} to {ret} at ${price}")
                else:
                    options.append(f"{dep} at ${price}")

            return SwaigFunctionResult(
                f"The cheapest dates are: {'; '.join(options)}. "
                "Ask the caller which dates work best."
            )

        # 8. SUMMARIZE CONVERSATION
        @self.tool(
            name="summarize_conversation",
            description="Generate a structured call summary. Called automatically when the conversation ends.",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Brief description of what happened during the call",
                    },
                },
                "required": ["summary"],
            },
        )
        def summarize_conversation(args, raw_data):
            call_id = _call_id(raw_data)
            state = load_call_state(call_id)

            origin = state.get("origin") or {}
            destination = state.get("destination") or {}
            booking = state.get("booking")

            summary_data = {
                "summary": args.get("summary", "N/A"),
                "origin": origin.get("iata"),
                "destination": destination.get("iata"),
                "departure_date": state.get("departure_date"),
                "return_date": state.get("return_date"),
                "booking": None,
            }

            if booking:
                summary_data["booking"] = {
                    "pnr": booking.get("pnr"),
                    "passenger": booking.get("passenger"),
                    "price": booking.get("price"),
                }

            return SwaigFunctionResult(json.dumps(summary_data))

    def _render_swml(self, call_id=None, modifications=None):
        """Override to dump the generated SWML to stderr for debugging."""
        swml = super()._render_swml(call_id, modifications)
        try:
            parsed = json.loads(swml) if isinstance(swml, str) else swml
            print(json.dumps(parsed, indent=2, default=str), file=sys.stderr)
        except Exception:
            print(swml, file=sys.stderr)
        return swml

    def on_summary(self, summary=None, raw_data=None):
        """Called when the post-prompt summary is received after the call ends."""
        if summary:
            logger.info(f"Call summary: {summary}")

        if raw_data:
            calls_dir = Path(__file__).parent / "calls"
            calls_dir.mkdir(exist_ok=True)
            call_id = raw_data.get("call_id", "unknown")
            out_path = calls_dir / f"{call_id}.json"
            try:
                out_path.write_text(json.dumps(raw_data, indent=2, default=str))
                logger.info(f"Saved call data to {out_path}")
            except Exception as e:
                logger.error(f"Failed to save call data: {e}")

            # Clean up SQLite state for this call
            delete_call_state(call_id)
            cleanup_stale_states(24)


def print_startup_url():
    """Print the full SWML URL with auth for easy copy/paste."""
    base = config.SWML_PROXY_URL_BASE
    if base:
        base = base.rstrip("/")
    else:
        host = config.HOST if config.HOST != "0.0.0.0" else "localhost"
        base = f"http://{host}:{config.PORT}"

    user = config.SWML_BASIC_AUTH_USER
    password = config.SWML_BASIC_AUTH_PASSWORD

    if user and password:
        scheme, rest = base.split("://", 1)
        url = f"{scheme}://{user}:{password}@{rest}/swml"
    else:
        url = f"{base}/swml"

    logger.info(f"SWML endpoint: {url}")


def create_server():
    """Create and configure the AgentServer."""
    server = AgentServer(host=config.HOST, port=config.PORT)
    server.register(VoyagerAgent(), "/swml")

    @server.app.get("/api/phone")
    def get_phone():
        """Return the GoAir phone number for the dashboard."""
        return {
            "phone": config.SIGNALWIRE_PHONE_NUMBER,
            "display": config.DISPLAY_PHONE_NUMBER or config.SIGNALWIRE_PHONE_NUMBER,
        }

    @server.app.get("/api/bookings")
    def api_bookings():
        """Return all bookings for the dashboard."""
        return {"bookings": get_all_bookings()}

    # Serve static files from web/ directory
    web_dir = Path(__file__).parent / "web"
    if web_dir.exists():
        server.serve_static_files(str(web_dir))

    print_startup_url()
    return server


server = create_server()
app = server.app

if __name__ == "__main__":
    server.run()
