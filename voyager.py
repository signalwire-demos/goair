#!/usr/bin/env python3
"""Voyager - AI Travel Booking Agent powered by SignalWire."""

import os
import sys
import json
import logging
import re
from datetime import date
from pathlib import Path
from dotenv import load_dotenv
from signalwire_agents import AgentBase, AgentServer
from signalwire_agents.core.function_result import SwaigFunctionResult

import config
from mock_flight_api import (
    mock_search_airports,
    mock_nearest_airports,
    mock_get_airport,
    mock_search_flights,
    mock_price_offer,
    mock_create_order,
)
from state_store import (
    load_call_state, save_call_state, delete_call_state,
    cleanup_stale_states, build_ai_summary, save_booking, get_all_bookings,
    get_passenger_by_phone, create_passenger, update_passenger,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, force=True)
logger = logging.getLogger(__name__)

config.validate()


# ── Mock API aliases ─────────────────────────────────────────────────

_search_airports = mock_search_airports
_nearest_airports = mock_nearest_airports
_get_airport = mock_get_airport
_search_flights = mock_search_flights
_price_offer = mock_price_offer
_create_order = mock_create_order


def _extract_segments(offer):
    """Extract carrier+flight-number identifiers for segment matching."""
    segs = []
    for itin in offer.get("itineraries", []):
        for seg in itin.get("segments", []):
            segs.append(f"{seg.get('carrierCode', '')}{seg.get('number', '')}")
    return segs


def _extract_baggage(priced_offer):
    """Extract baggage info from a priced offer's travelerPricings."""
    tp = priced_offer.get("travelerPricings", [])
    if not tp:
        return ""
    segs = tp[0].get("fareDetailsBySegment", [])
    if not segs:
        return ""
    bags = segs[0].get("includedCheckedBags", {})
    qty = bags.get("quantity", 0)
    weight = bags.get("weight")
    if qty:
        return f"{qty} checked bag{'s' if qty > 1 else ''} included. "
    elif weight:
        return f"Checked bags up to {weight}kg included. "
    return "Carry-on only, checked bags extra. "


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


def format_time_voice(hhmm):
    """Convert 24-hour HH:MM to voice-friendly 12-hour format.

    Examples: "06:00" → "6 AM", "17:08" → "5:08 PM", "12:00" → "12 PM",
              "00:30" → "12:30 AM", "13:00" → "1 PM"
    """
    try:
        h, m = int(hhmm[:2]), int(hhmm[3:5])
        period = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        if m == 0:
            return f"{h12} {period}"
        return f"{h12}:{m:02d} {period}"
    except (ValueError, IndexError):
        return hhmm


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

            # Format times for voice — 12-hour with AM/PM
            dep_hhmm = dep_time[11:16] if len(dep_time) > 15 else dep_time
            arr_hhmm = arr_time[11:16] if len(arr_time) > 15 else arr_time
            dep_display = format_time_voice(dep_hhmm)
            arr_display = format_time_voice(arr_hhmm)

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
#        self.set_param("thinking_model", "gpt-oss-120b@groq.ai")
#        self.set_param("enable_thinking", True)

        self.set_prompt_llm_params(top_p=0.9, temperature=0.3)

        # Personality
        self.prompt_add_section("Personality",
            "You are Voyager, a friendly AI travel concierge who helps callers find and book flights. "
            "Keep it warm and brief — the occasional travel quip is welcome."
        )

        # Voice behavior — only things the model needs for natural speech
        self.prompt_add_section("Rules", body="", bullets=[
            "This is a PHONE CALL. Keep every response to 1-2 short sentences.",
            "Use airline names not codes ('Delta' not 'DL'). Say times naturally ('seven thirty PM' not '19:30').",
            "Avoid commas in speech — use 'and' or 'or' instead. Keep sentences short and direct.",
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

        # Native question steps — one step per question with forced transitions
        self._define_question_steps()

        # Per-call dynamic config — SDK creates ephemeral copy per request
        self.set_dynamic_config_callback(self._per_call_config)

    def _add_question_step(self, ctx, step_name, task, question, tool_name,
                           key_name, storage_ns, next_step,
                           confirm=False, validator=None, extra_instructions=None,
                           extra_functions=None):
        """Create a question step with a submit tool that force-transitions."""
        step = ctx.add_step(step_name)
        step.add_section("Task", task)
        bullets = [f"Ask the caller: '{question}'"]
        if confirm:
            bullets.append("Read back their answer and ask them to confirm it is correct")
        bullets.append(f"Call {tool_name} with their response")
        if extra_instructions:
            bullets.extend(extra_instructions)
        step.add_bullets("Process", bullets)
        step.set_step_criteria(f"Answer submitted via {tool_name}")
        functions = [tool_name]
        if extra_functions:
            functions.extend(extra_functions)
        step.set_functions(functions)
        step.set_valid_steps([])  # ALL transitions forced by handler

        # Register the submit tool — closure captures config
        _confirm = confirm
        _validator = validator
        _key_name = key_name
        _storage_ns = storage_ns
        _next_step = next_step
        _tool_name = tool_name

        @self.tool(name=tool_name,
                   description=f"Submit the caller's {key_name.replace('_', ' ')}",
                   wait_file="/sounds/typing.mp3",
                   parameters={"type": "object", "properties": {
                       "value": {"type": "string", "description": "The caller's answer"},
                       "confirmed": {"type": "boolean",
                                     "description": "Set true only after the caller explicitly confirmed"},
                   }, "required": ["value"]})
        def _handler(args, raw_data):
            value = (args.get("value") or "").strip()
            confirmed = args.get("confirmed", False)

            if _confirm:
                # Server-side guard: first call ALWAYS bounces regardless of
                # confirmed flag.  Model can't bypass by sending confirmed=true
                # on the first attempt.
                call_id = ((raw_data or {}).get("call_id", "unknown")
                           if isinstance(raw_data, dict) else "unknown")
                asked_key = f"_{_key_name}_asked"
                _state = load_call_state(call_id)
                if not _state.get(asked_key):
                    _state[asked_key] = True
                    save_call_state(call_id, _state)
                    return SwaigFunctionResult(
                        f"Ask the caller for their {_key_name.replace('_', ' ')}. "
                        f"Then call {_tool_name} with their answer and confirmed set to true."
                    )
                if not confirmed:
                    return SwaigFunctionResult(
                        f"Read '{value}' back to the caller and ask if that's correct. "
                        f"Then call {_tool_name} again with confirmed set to true."
                    )
                # Clear the asked flag on successful confirmation
                _state.pop(asked_key, None)
                save_call_state(call_id, _state)

            if not value:
                return SwaigFunctionResult("No answer provided.")
            # Guard: reject duplicate submission (model batched calls)
            global_data = (raw_data or {}).get("global_data", {})
            existing = global_data.get(_storage_ns, {})
            if existing.get(_key_name):
                return SwaigFunctionResult(
                    f"Already have {_key_name.replace('_', ' ')}."
                )
            if _validator:
                error = _validator(value, raw_data)
                if error:
                    return SwaigFunctionResult(error)
            # Store answer
            answers = dict(existing)
            answers[_key_name] = value
            # Compute next step (can be callable for conditional routing)
            ns = _next_step(raw_data) if callable(_next_step) else _next_step
            result = SwaigFunctionResult("Got it.")
            result.update_global_data({_storage_ns: answers})
            result.swml_change_step(ns)
            return result

        return step

    def _define_state_machine(self):
        """Define conversation contexts and steps."""
        contexts = self.define_contexts()
        ctx = contexts.add_context("default")

        # GREETING — bare shell; _per_call_config sets Process/criteria per caller type
        greeting = ctx.add_step("greeting")
        greeting.add_section("Task", "Welcome the caller")
        greeting.set_functions("none")
        greeting.set_valid_steps(["collect_profile", "get_destination", "disambiguate_origin"])  # overridden by _per_call_config

        # GET ORIGIN
        get_origin = ctx.add_step("get_origin")
        get_origin.add_section("Task", "Collect the departure city or airport")
        get_origin.add_bullets("Process", [
            "Check if ${origin.name} is already set from their home airport",
            "If set, ask: 'Would you like to fly from ${origin.name} or somewhere else?'",
            "If they confirm, proceed to next step",
            "If they want a different airport or origin is not set, ask where they're flying from",
            "Call resolve_location with their answer and location_type='origin'",
            "Once the airport is confirmed, proceed to next step",
        ])
        get_origin.set_step_criteria("Origin airport confirmed and saved")
        get_origin.set_functions(["resolve_location"])
        get_origin.set_valid_steps(["get_destination", "disambiguate_origin",
                                    "collect_trip_type", "search_and_present",
                                    "collect_booking_roundtrip", "collect_booking_oneway"])

        # DISAMBIGUATE ORIGIN
        disambiguate_origin = ctx.add_step("disambiguate_origin")
        disambiguate_origin.add_section("Task", "Ask the caller to choose between multiple origin airports")
        disambiguate_origin.add_bullets("Process", [
            "Say the airport options aloud — list each by name and city before doing anything else",
            "Wait for the caller to respond before doing anything else",
            "Only after the caller answers, call select_airport with location_type='origin' and the IATA code they chose",
            "If the caller says 'any' or 'doesn't matter', call select_airport with the first candidate",
        ])
        disambiguate_origin.set_step_criteria("Caller verbally chose an airport and select_airport was called")
        disambiguate_origin.set_functions(["select_airport"])
        disambiguate_origin.set_valid_steps(["get_destination"])

        # GET DESTINATION
        get_destination = ctx.add_step("get_destination")
        get_destination.add_section("Task", "Collect the arrival city or airport")
        get_destination.add_bullets("Process", [
            "Ask where they're flying to — or if they already said a destination, call resolve_location right away with location_type='destination'",
            "Once the airport is confirmed, proceed to next step",
        ])
        get_destination.set_step_criteria("Destination airport confirmed and saved")
        get_destination.set_functions(["resolve_location"])
        get_destination.set_valid_steps(["collect_trip_type", "disambiguate_destination"])

        # DISAMBIGUATE DESTINATION
        disambiguate_destination = ctx.add_step("disambiguate_destination")
        disambiguate_destination.add_section("Task", "Ask the caller to choose between multiple destination airports")
        disambiguate_destination.add_bullets("Process", [
            "Say the airport options aloud — list each by name and city (e.g. 'I found three airports near Miami: Miami International, Fort Lauderdale-Hollywood, and Palm Beach International — which would you like?')",
            "Wait for the caller to respond before doing anything else",
            "Only after the caller answers, call select_airport with location_type='destination' and the IATA code they chose",
            "If the caller says 'any' or 'doesn't matter', call select_airport with the first candidate",
        ])
        disambiguate_destination.set_step_criteria("Caller verbally chose an airport and select_airport was called")
        disambiguate_destination.set_functions(["select_airport"])
        disambiguate_destination.set_valid_steps(["collect_trip_type"])

        # COLLECT TRIP TYPE — gather_info, single question; no tools available so no spurious calls
        ctx.add_step("collect_trip_type") \
            .set_text("Ask whether this is a round-trip or one-way flight.") \
            .set_functions("none") \
            .set_gather_info(
                output_key="trip_type_answers",
                completion_action="next_step",
                prompt="Ask the caller if this is a round-trip or one-way flight."
            ) \
            .add_gather_question(
                "trip_type",
                "Is this a round trip or one-way?",
                confirm=True,
                prompt="Submit exactly 'round_trip' or 'one_way'."
            ) \
            .set_valid_steps(["apply_trip_type"])

        # APPLY TRIP TYPE — bridge step; AI calls select_trip_type immediately (desired, gather already confirmed)
        apply_trip_type = ctx.add_step("apply_trip_type")
        apply_trip_type.add_section("Task", "Record the trip type and route to booking")
        apply_trip_type.add_bullets("Process", [
            "The caller already answered via gather — call select_trip_type immediately with no arguments",
        ])
        apply_trip_type.set_step_criteria("Trip type recorded via select_trip_type")
        apply_trip_type.set_functions(["select_trip_type"])
        apply_trip_type.set_valid_steps(["collect_booking_roundtrip", "collect_booking_oneway"])

        # SEARCH FLIGHTS
        search_flights_step = ctx.add_step("search_flights")
        search_flights_step.add_section("Task", "Re-search flights (used only for error recovery — happy path searches inline via submit_cabin)")
        search_flights_step.add_bullets("Process", [
            "Call search_flights to find available flights",
        ])
        search_flights_step.set_step_criteria("Flight search completed")
        search_flights_step.set_functions(["search_flights"])
        search_flights_step.set_valid_steps(["present_options", "error_recovery"])

        # PRESENT OPTIONS
        present_options = ctx.add_step("present_options")
        present_options.add_section("Task", "Present up to 3 flight options and let the caller pick one")
        present_options.add_bullets("Process", [
            "Read each flight option from booking_state.flight_summaries — include airline, stops, times, duration, and price",
            "Label them 'Option 1' then 'Option 2' then 'Option 3'",
            "The caller prefers a ${global_data.passenger_profile.seat_preference} seat — mention it naturally when relevant",
            "Ask which option they'd like or if they want to try different dates or a different route",
            "When the caller picks one, call select_flight with that option_number",
            "If caller wants different dates or a different route, call restart_search",
        ])
        present_options.set_step_criteria("Caller selects an option via select_flight or requests new search via restart_search")
        present_options.set_functions(["select_flight", "restart_search"])
        present_options.set_valid_steps(["confirm_price", "collect_booking_roundtrip", "collect_booking_oneway", "get_origin"])

        # CONFIRM PRICE
        confirm_price = ctx.add_step("confirm_price")
        confirm_price.add_section("Task", "Confirm the live price on the selected flight")
        confirm_price.add_bullets("Process", [
            "Call get_flight_price to confirm the live fare",
        ])
        confirm_price.set_step_criteria("Caller confirms or declines via confirm_booking or decline_booking")
        confirm_price.set_functions(["get_flight_price", "confirm_booking", "decline_booking"])
        confirm_price.set_valid_steps(["create_booking", "present_options", "error_recovery"])

        # CREATE BOOKING
        create_booking = ctx.add_step("create_booking")
        create_booking.add_section("Task", "Book the flight and wrap up")
        create_booking.add_bullets("Process", [
            "Call book_flight to create the reservation",
            "Read the PNR back to the caller using the phonetic spelling provided",
            "Thank them and say goodbye",
        ])
        # book_flight takes no parameters — profile data is read automatically
        create_booking.set_step_criteria("Booking created, PNR read back, call ending")
        create_booking.set_functions(["book_flight"])
        create_booking.set_valid_steps(["wrap_up", "error_recovery", "collect_profile"])

        # ERROR RECOVERY
        error_recovery = ctx.add_step("error_recovery")
        error_recovery.add_section("Task", "Handle booking failures, no results, and mid-flow changes")
        error_recovery.add_bullets("Process", [
            "Offer options: try different dates, a different route, or re-search the same route",
            "If different dates: call restart_booking to go back to date collection",
            "If different route: call restart_search with reason='different_route' to start over from origin",
            "If same route: call search_flights to re-search with current settings",
        ])
        error_recovery.set_step_criteria("Recovery action taken")
        error_recovery.set_functions(["search_flights", "restart_booking", "restart_search"])
        error_recovery.set_valid_steps(["present_options", "collect_booking_roundtrip", "collect_booking_oneway", "get_origin"])

        # WRAP UP
        wrap_up = ctx.add_step("wrap_up")
        wrap_up.add_section("Task", "Confirm booking details and end the call")
        wrap_up.add_bullets("Process", [
            "Read back the confirmed flight details: airline, departure time, arrival time, and the PNR (phonetic spelling is provided — read it exactly as given)",
            "Tell the caller an SMS confirmation has been sent to their phone",
            "Say a brief, warm goodbye: 'Thanks for flying with Voyager — have an amazing trip!'",
            "End the call",
        ])
        wrap_up.set_functions("none")
        wrap_up.set_valid_steps([])

    def _define_question_steps(self):
        """Define profile and booking collection using gather_info mode."""
        ctx = self._contexts_builder.get_context("default")

        # ── Profile Collection (gather_info mode) ──

        ctx.add_step("collect_profile") \
            .set_text("Welcome the caller, then collect their profile.") \
            .set_functions("none") \
            .set_gather_info(
                output_key="profile_answers",
                completion_action="next_step",
                prompt="First say: 'Welcome to Voyager! I'd love to help you book a flight.' Then collect the passenger's profile by asking each question. IMPORTANT: Only call gather_submit after the user has explicitly confirmed their answer with 'yes' or equivalent. Never call gather_submit speculatively or with confirmed_by_user set to false."
            ) \
            .add_gather_question("first_name", "What is your first name?") \
            .add_gather_question("last_name", "What is your last name?") \
            .add_gather_question(
                "date_of_birth",
                "What is your date of birth?",
                confirm=True,
                prompt="Accept natural language but submit in YYYY-MM-DD format"
            ) \
            .add_gather_question(
                "gender",
                "Are you male or female?",
                prompt="Submit exactly MALE or FEMALE"
            ) \
            .add_gather_question(
                "email",
                "What email should we send confirmations to?",
                confirm=True
            ) \
            .add_gather_question(
                "seat_preference",
                "Do you prefer a window or aisle seat?",
                prompt="Submit exactly WINDOW or AISLE"
            ) \
            .add_gather_question(
                "cabin_preference",
                "What cabin class do you usually fly?",
                prompt="Options are ECONOMY, PREMIUM_ECONOMY, BUSINESS, or FIRST"
            ) \
            .add_gather_question(
                "home_airport",
                "What airport do you usually fly from?",
                confirm=True,
                prompt="Accept city or airport name. Submit exactly what the caller says."
            ) \
            .set_valid_steps(["save_profile_step"])

        # Save profile after gather completes
        ctx.add_step("save_profile_step") \
            .add_section("Task", "Save the completed profile") \
            .add_bullets("Process", [
                "Call save_profile to create the passenger record",
                "Profile data is in ${profile_answers}"
            ]) \
            .set_functions(["save_profile"]) \
            .set_valid_steps(["get_origin", "collect_profile"])

        @self.tool(name="save_profile",
                   description="Save profile and create passenger",
                   wait_file="/sounds/typing.mp3")
        def _save_profile(args, raw_data):
            global_data = (raw_data or {}).get("global_data", {})
            caller_phone = global_data.get("caller_phone", "")
            call_id = (raw_data or {}).get("call_id", "unknown")
            state = load_call_state(call_id)
            answers = global_data.get("profile_answers", {})

            home_airport_value = answers.get("home_airport", "")

            # Extract or lookup IATA code
            home_airport_iata = None
            home_airport_full_name = home_airport_value

            # First try to extract existing IATA code from answer
            iata_match = re.search(r"\(([A-Z]{3})\)", home_airport_value)
            if not iata_match:
                iata_match = re.search(r"\b([A-Z]{3})\b", home_airport_value)

            if iata_match:
                home_airport_iata = iata_match.group(1).upper()
            else:
                # No IATA code found - search for the airport by name
                # Try multiple search variations
                search_terms = [home_airport_value]

                # If it's "City, State" format, also try just the city name
                if "," in home_airport_value:
                    city_part = home_airport_value.split(",")[0].strip()
                    search_terms.append(city_part)

                airport_results = None
                for term in search_terms:
                    airport_results = _search_airports(term)
                    if airport_results:
                        airport = airport_results[0]
                        home_airport_iata = airport.get("iataCode", "").upper()
                        home_airport_full_name = f"{airport.get('name', home_airport_value).title()} ({home_airport_iata})"
                        logger.info(f"save_profile: looked up '{home_airport_value}' (searched: '{term}') -> {home_airport_iata}")
                        break

            # Validate and set home airport as origin
            if home_airport_iata:
                airport_results = _search_airports(home_airport_iata)
                if airport_results:
                    airport = airport_results[0]  # Take first match
                    state["origin"] = {
                        "iata": airport.get("iataCode", home_airport_iata),
                        "name": airport.get("name", "").title(),
                        "city": airport.get("address", {}).get("cityName", "").title(),
                    }
                    logger.info(f"save_profile: set state['origin'] = {home_airport_iata}")

            # Create passenger
            create_passenger(
                phone=caller_phone,
                first_name=answers.get("first_name", ""),
                last_name=answers.get("last_name", ""),
                date_of_birth=answers.get("date_of_birth"),
                gender=answers.get("gender"),
                email=answers.get("email"),
                seat_preference=answers.get("seat_preference"),
                cabin_preference=answers.get("cabin_preference"),
                home_airport_iata=home_airport_iata,
                home_airport_name=home_airport_full_name,
            )

            profile = {
                "phone": caller_phone,
                "first_name": answers.get("first_name", ""),
                "last_name": answers.get("last_name", ""),
                "date_of_birth": answers.get("date_of_birth"),
                "gender": answers.get("gender"),
                "email": answers.get("email"),
                "seat_preference": answers.get("seat_preference"),
                "cabin_preference": answers.get("cabin_preference"),
                "home_airport_iata": home_airport_iata,
                "home_airport_name": home_airport_full_name,
            }

            save_call_state(call_id, state)

            first_n = answers.get("first_name", "")
            last_n = answers.get("last_name", "")
            home_note = f" Home airport: {home_airport_full_name} ({home_airport_iata})." if home_airport_iata and state.get("origin") else ""
            result = SwaigFunctionResult(f"Profile saved.\nPassenger: {first_n} {last_n}.{home_note}")
            result.update_global_data({
                "passenger_profile": profile,
                "is_new_caller": False,
            })
            result.swml_change_step("get_origin")
            return result

        # ── Booking Collection (gather_info mode, two paths) ──

        # ROUND-TRIP: includes return date question
        ctx.add_step("collect_booking_roundtrip") \
            .set_text("Collect round-trip booking details.") \
            .set_functions("none") \
            .set_gather_info(
                output_key="booking_answers",
                completion_action="next_step",
                prompt="Now let's plan your round trip, ${global_data.passenger_profile.first_name}."
            ) \
            .add_gather_question(
                "departure_date",
                "When would you like to depart?",
                confirm=True,
                prompt="Accept natural language but submit in YYYY-MM-DD format. Must be a future date."
            ) \
            .add_gather_question(
                "return_date",
                "When would you like to return?",
                confirm=True,
                prompt="Accept natural language but submit in YYYY-MM-DD format. Must be after departure date."
            ) \
            .add_gather_question(
                "adults",
                "How many passengers?",
                type="integer",
                prompt="Must be 1-8 passengers"
            ) \
            .add_gather_question(
                "cabin_class",
                "What cabin class would you like — you usually fly ${global_data.passenger_profile.cabin_preference}?",
                prompt="Options: ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST."
            ) \
            .set_valid_steps(["search_and_present"])

        # ONE-WAY: no return date question
        ctx.add_step("collect_booking_oneway") \
            .set_text("Collect one-way booking details.") \
            .set_functions("none") \
            .set_gather_info(
                output_key="booking_answers",
                completion_action="next_step",
                prompt="Now let's plan your trip, ${global_data.passenger_profile.first_name}."
            ) \
            .add_gather_question(
                "departure_date",
                "When would you like to depart?",
                confirm=True,
                prompt="Accept natural language but submit in YYYY-MM-DD format. Must be a future date."
            ) \
            .add_gather_question(
                "adults",
                "How many passengers?",
                type="integer",
                prompt="Must be 1-8 passengers"
            ) \
            .add_gather_question(
                "cabin_class",
                "What cabin class would you like — you usually fly ${global_data.passenger_profile.cabin_preference}?",
                prompt="Options: ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST."
            ) \
            .set_valid_steps(["search_and_present"])

        # Search flights after booking details collected
        ctx.add_step("search_and_present") \
            .add_section("Task", "Search for available flights") \
            .add_bullets("Process", [
                "Booking details are in ${booking_answers}",
                "Call search_flights to find available flights",
            ]) \
            .set_step_criteria("Search completed") \
            .set_functions(["search_flights"]) \
            .set_valid_steps(["present_options", "error_recovery"])

    def _per_call_config(self, query_params, body_params, headers, agent):
        """Pre-populate passenger data for returning callers."""
        call_data = (body_params or {}).get("call", {})
        caller_phone = call_data.get("from", "")

        passenger = get_passenger_by_phone(caller_phone) if caller_phone else None

        if passenger:
            # RETURNING CALLER — skip profile collection
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

            agent.update_global_data({
                "passenger_profile": profile,
                "is_new_caller": False,
                "caller_phone": caller_phone,
            })

            # Modify greeting to skip profile collection
            ctx = agent._contexts_builder.get_context("default")
            greeting_step = ctx.get_step("greeting")
            greeting_step.clear_sections()
            greeting_step.set_functions(["resolve_location"])
            greeting_step.set_valid_steps(["get_destination", "disambiguate_origin"])

            home_airport = passenger.get("home_airport_name")
            if home_airport:
                greeting_step.add_section("Task", "Welcome returning caller and confirm origin")
                greeting_step.add_bullets("Process", [
                    f"Say: 'Welcome back {passenger['first_name']}!'",
                    f"Ask: 'Are you flying from {home_airport} or somewhere else?'",
                    f"If {home_airport}, call resolve_location with '{home_airport}' and location_type='origin'",
                    "If different, ask where and call resolve_location",
                    "After resolve_location, confirm with caller and proceed to get_destination"
                ])
            else:
                greeting_step.add_section("Task", "Welcome returning caller and get origin")
                greeting_step.add_bullets("Process", [
                    f"Say: 'Welcome back {passenger['first_name']}!'",
                    "Ask where they're flying from",
                    "Call resolve_location with location_type='origin'",
                    "Confirm and proceed to get_destination"
                ])

            greeting_step.set_step_criteria("Origin resolved")

            # Disable profile collection steps
            for step_name in ["collect_profile", "save_profile_step"]:
                try:
                    ps = ctx.get_step(step_name)
                    ps.set_functions("none")
                    ps.set_valid_steps([])
                except:
                    pass

            agent.prompt_add_section("Passenger Profile", "${global_data.passenger_profile}")

        else:
            # NEW CALLER — use profile collection
            agent.update_global_data({
                "passenger_profile": None,
                "is_new_caller": True,
                "caller_phone": caller_phone,
            })

            # Skip greeting and start at collect_profile (which includes greeting)
            ctx = agent._contexts_builder.get_context("default")
            ctx.move_step("collect_profile", 0)
            ctx.move_step("save_profile_step", 1)
            ctx.remove_step("greeting")

    def _define_tools(self):
        """Define all SWAIG tool functions."""

        # Helper: extract call_id from raw_data
        def _call_id(raw_data):
            if not isinstance(raw_data, dict):
                return "unknown"
            return raw_data.get("call_id", "unknown")

        def _change_step(result, step):
            """Log and apply a forced step change."""
            logger.info(f"step_change: -> {step}")
            result.swml_change_step(step)

        def _sync_summary(result, state):
            """Save state to DB and sync lightweight summary to global_data."""
            result.update_global_data({"booking_state": build_ai_summary(state)})
            return result

        def _booking_step(state):
            """Return the correct booking gather step name based on trip type."""
            return "collect_booking_roundtrip" if state.get("trip_type") == "round_trip" else "collect_booking_oneway"

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
            description="Resolve a spoken city or place name" ,
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
                    "mode": {
                        "type": "string",
                        "description": "normal = resolve and change step (default). verify = resolve and return result without changing step.",
                        "enum": ["normal", "verify"],
                    },
                },
                "required": ["location_text"],
            },
        )
        def resolve_location(args, raw_data):
            location_text = (args.get("location_text") or "").strip()
            location_type = args.get("location_type", "origin")
            mode = args.get("mode", "normal")

            # Guard: force verify mode during profile collection
            global_data = (raw_data or {}).get("global_data", {})
            if global_data.get("is_new_caller") and not global_data.get("passenger_profile"):
                if mode != "verify":
                    logger.info(f"resolve_location: forcing mode='verify' (profile collection active)")
                    mode = "verify"

            logger.info(f"resolve_location: text='{location_text}', type='{location_type}', mode='{mode}'")

            if not location_text:
                return SwaigFunctionResult("No location text provided.")

            call_id = _call_id(raw_data)
            state = load_call_state(call_id)

            # Guard: destination cannot be resolved before origin is set
            if location_type == "destination" and not state.get("origin") and mode != "verify":
                return SwaigFunctionResult(
                    "Origin airport must be set before destination.\nAsk the caller where they're flying from first."
                )

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
                return SwaigFunctionResult(f"No airports found for '{location_text}'.")

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
                coords = geo or {}
                if not coords:
                    db_entry = _get_airport(top["iata"])
                    if db_entry:
                        coords = {"lat": db_entry["lat"], "lng": db_entry["lng"]}
                if coords:
                    airport_info["lat"] = coords["lat"]
                    airport_info["lng"] = coords["lng"]

                logger.info(f"resolve_location: auto-selected {top['iata']} for {location_type}")

                if mode == "verify":
                    return SwaigFunctionResult(
                        f"Resolved: {top['name']} ({top['iata']})"
                        f"{' in ' + top['city'] if top['city'] else ''}."
                    )

                # Single match: Save to state immediately
                state[location_type] = airport_info
                logger.info(f"resolve_location: set state['{location_type}'] = {top['iata']}")

                result = SwaigFunctionResult(
                    f"Airport resolved.\n"
                    f"{top['name']} ({top['iata']}){', ' + top['city'] if top['city'] else ''}."
                )
                result.add_dynamic_hints([h for h in [top["name"], top["city"]] if h])
                save_call_state(call_id, state)
                _sync_summary(result, state)
                # Mid-flow rejoin: if origin just resolved and destination already set,
                # skip asking destination again and jump to the right point in the flow.
                if location_type == "origin" and state.get("destination"):
                    if state.get("departure_date"):
                        _change_step(result, "search_and_present")
                    elif state.get("trip_type"):
                        _change_step(result, _booking_step(state))
                    else:
                        _change_step(result, "collect_trip_type")
                return result
            else:
                # Multiple airports — need disambiguation
                top_3 = ranked[:3]
                airport_list = ", ".join(
                    f"{a['name']} ({a['iata']})" for a in top_3
                )

                if mode == "verify":
                    return SwaigFunctionResult(f"Multiple airports found.\n{airport_list}")

                # Store candidates for disambiguation step
                state[f"{location_type}_candidates"] = [
                    {"iata": a["iata"], "name": a["name"], "city": a["city"]}
                    for a in top_3
                ]
                if geo:
                    state[f"{location_type}_geo"] = {"lat": geo["lat"], "lng": geo["lng"]}
                logger.info(f"resolve_location: {len(top_3)} candidates for {location_type}")

                disambig_step = f"disambiguate_{location_type}"
                result = SwaigFunctionResult(f"Multiple airports found.\n{airport_list}")
                hints = []
                for a in top_3:
                    hints.append(a["name"])
                    if a["city"]:
                        hints.append(a["city"])
                result.add_dynamic_hints(hints)
                save_call_state(call_id, state)
                _sync_summary(result, state)
                _change_step(result,disambig_step)
                return result

        # 2. SELECT AIRPORT
        @self.tool(
            name="select_airport",
            description="Select an airport from the disambiguation candidates.",
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
                    f"{iata_code} not in candidate list.\nAvailable: {available}"
                )

            # Store selected airport — prefer city geo saved during resolve_location,
            # fall back to the airport's own coordinates from the database.
            airport_info = {
                "iata": selected["iata"],
                "name": selected["name"],
                "city": selected["city"],
            }
            geo = state.get(f"{location_type}_geo")
            if not geo:
                db_entry = _get_airport(selected["iata"])
                if db_entry:
                    geo = {"lat": db_entry["lat"], "lng": db_entry["lng"]}
            if geo:
                airport_info["lat"] = geo["lat"]
                airport_info["lng"] = geo["lng"]
            state[location_type] = airport_info
            logger.info(f"select_airport: set state['{location_type}'] = {selected['iata']} (lat/lng: {bool(geo)})")

            next_step = "get_destination" if location_type == "origin" else "collect_trip_type"
            result = SwaigFunctionResult(
                f"{selected['name']} ({selected['iata']}) selected as {location_type}."
            )
            result.add_dynamic_hints([h for h in [selected["name"], selected["city"]] if h])
            save_call_state(call_id, state)
            _sync_summary(result, state)
            _change_step(result, next_step)
            return result

        # 3. SELECT TRIP TYPE
        @self.tool(
            name="select_trip_type",
            description="Record the trip type from gather and route to booking. Call immediately with no arguments.",
            wait_file="/sounds/typing.mp3",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def select_trip_type(args, raw_data):
            global_data = (raw_data or {}).get("global_data", {})
            raw_trip_type = global_data.get("trip_type_answers", {}).get("trip_type", "").lower().strip()

            # Normalize common variations
            if raw_trip_type in ("round_trip", "roundtrip", "round trip", "round-trip"):
                trip_type = "round_trip"
            elif raw_trip_type in ("one_way", "oneway", "one way", "one-way"):
                trip_type = "one_way"
            else:
                result = SwaigFunctionResult(
                    f"Unrecognized trip type '{raw_trip_type}'.\nExpected round_trip or one_way."
                )
                _change_step(result, "collect_trip_type")
                return result

            call_id = _call_id(raw_data)
            state = load_call_state(call_id)
            state["trip_type"] = trip_type
            save_call_state(call_id, state)

            next_step = "collect_booking_roundtrip" if trip_type == "round_trip" else "collect_booking_oneway"
            result = SwaigFunctionResult(f"Trip type set.\n{trip_type.replace('_', ' ')}.")
            _sync_summary(result, state)
            _change_step(result, next_step)
            return result

        # 4. FINALIZE PROFILE (fallback tool — happy path uses submit_home_airport)
        @self.tool(
            name="finalize_profile",
            description="Save the completed profile. Reads from profile_answers in global_data.",
            wait_file="/sounds/typing.mp3",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def finalize_profile(args, raw_data):
            global_data = (raw_data or {}).get("global_data", {})
            caller_phone = global_data.get("caller_phone", "")

            # Read from profile_answers flat dict (native question steps)
            # Fall back to skill:profile answers list for backwards compat
            fields = global_data.get("profile_answers")
            if not fields:
                skill_data = global_data.get("skill:profile", {})
                answers = skill_data.get("answers", [])
                fields = {
                    a.get("key_name"): a.get("answer")
                    for a in answers
                    if a.get("key_name") and a.get("answer")
                }

            first_name = (fields.get("first_name") or "").strip()
            last_name = (fields.get("last_name") or "").strip()
            if not first_name or not last_name:
                result = SwaigFunctionResult("Missing name. Cannot save profile.")
                _change_step(result, "collect_profile")
                return result

            # Extract home airport IATA — try "(SFO)" format, then bare 3-letter code
            home_airport_name = fields.get("home_airport_name") or ""
            home_airport_iata = None
            iata_match = re.search(r"\(([A-Z]{3})\)", home_airport_name)
            if not iata_match:
                iata_match = re.search(r"\b([A-Za-z]{3})\b", home_airport_name)
            if iata_match:
                home_airport_iata = iata_match.group(1).upper()

            create_passenger(
                phone=caller_phone,
                first_name=first_name, last_name=last_name,
                date_of_birth=fields.get("date_of_birth"),
                gender=fields.get("gender"),
                email=fields.get("email"),
                seat_preference=fields.get("seat_preference"),
                cabin_preference=fields.get("cabin_preference"),
                home_airport_iata=home_airport_iata,
                home_airport_name=home_airport_name,
            )

            profile = {
                "phone": caller_phone, "first_name": first_name, "last_name": last_name,
                "date_of_birth": fields.get("date_of_birth"), "gender": fields.get("gender"),
                "email": fields.get("email"), "seat_preference": fields.get("seat_preference"),
                "cabin_preference": fields.get("cabin_preference"),
                "home_airport_iata": home_airport_iata, "home_airport_name": home_airport_name,
            }

            global_update = {
                "passenger_profile": profile,
                "is_new_caller": False,
                "caller_phone": caller_phone,
            }

            # If home airport resolved, tell AI to offer it — but don't pre-set
            # state["origin"] to avoid stale data if caller declines
            if home_airport_iata and home_airport_name:
                result = SwaigFunctionResult(
                    f"Profile saved.\nPassenger: {first_name} {last_name}. Home airport: {home_airport_name} ({home_airport_iata})."
                )
            else:
                result = SwaigFunctionResult(
                    f"Profile saved.\nPassenger: {first_name} {last_name}. No home airport on file."
                )

            result.update_global_data(global_update)
            _change_step(result, "get_origin")
            return result

        # 5. FINALIZE BOOKING (fallback tool — happy path uses submit_cabin)
        @self.tool(
            name="finalize_booking",
            description="Store the collected booking details. Reads from booking_answers in global_data.",
            wait_file="/sounds/typing.mp3",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def finalize_booking(args, raw_data):
            global_data = (raw_data or {}).get("global_data", {})
            call_id = _call_id(raw_data)
            state = load_call_state(call_id)
            trip_type = state.get("trip_type", "one_way")

            # Read from booking_answers flat dict (native question steps)
            # Fall back to skill:oneway/roundtrip answers list for backwards compat
            fields = global_data.get("booking_answers")
            if not fields:
                skill_key = "skill:roundtrip" if trip_type == "round_trip" else "skill:oneway"
                skill_data = global_data.get(skill_key, {})
                answers = skill_data.get("answers", [])
                fields = {
                    a.get("key_name"): a.get("answer")
                    for a in answers
                    if a.get("key_name") and a.get("answer")
                }

            # Validate departure date
            departure_str = fields.get("departure_date", "")
            try:
                departure_dt = date.fromisoformat(departure_str)
            except (ValueError, TypeError):
                result = SwaigFunctionResult(
                    f"Invalid departure date.\nDate '{departure_str}' is not in YYYY-MM-DD format."
                )
                _sync_summary(result, state)
                _change_step(result, _booking_step(state))
                return result
            if departure_dt < date.today():
                result = SwaigFunctionResult(
                    f"Departure date is in the past.\nDate: {departure_str}."
                )
                _sync_summary(result, state)
                _change_step(result, _booking_step(state))
                return result
            state["departure_date"] = departure_str

            # Validate return date for round trips
            if trip_type == "round_trip":
                return_str = fields.get("return_date", "")
                try:
                    return_dt = date.fromisoformat(return_str)
                except (ValueError, TypeError):
                    result = SwaigFunctionResult(
                        f"Invalid return date.\nDate '{return_str}' is not in YYYY-MM-DD format."
                    )
                    _sync_summary(result, state)
                    _change_step(result, _booking_step(state))
                    return result
                if return_dt < date.today():
                    result = SwaigFunctionResult(
                        f"Return date is in the past.\nDate: {return_str}."
                    )
                    _sync_summary(result, state)
                    _change_step(result, _booking_step(state))
                    return result
                if return_dt <= departure_dt:
                    result = SwaigFunctionResult(
                        f"Return date must be after departure date.\nReturn: {return_str}. Departure: {departure_str}."
                    )
                    _sync_summary(result, state)
                    _change_step(result, _booking_step(state))
                    return result
                state["return_date"] = return_str

            try:
                adults = int(fields.get("adults", "1"))
            except (ValueError, TypeError):
                adults = 1
            if adults > 8:
                result = SwaigFunctionResult(
                    f"Too many passengers.\nRequested: {adults}. Maximum: 8. Parties larger than 8 require a travel agent."
                )
                _sync_summary(result, state)
                _change_step(result, "error_recovery")
                return result
            state["adults"] = adults
            state["cabin_class"] = fields.get("cabin_class", "ECONOMY")

            save_call_state(call_id, state)

            result = SwaigFunctionResult("Booking details saved.")
            _sync_summary(result, state)
            _change_step(result,"search_flights")
            return result

        # ── search helper (shared by submit_cabin and search_flights tool) ──
        def _do_search(call_id, state):
            """Run flight search using call state.  Returns a SwaigFunctionResult."""
            origin = state.get("origin")
            destination = state.get("destination")
            departure_date = state.get("departure_date")

            if not origin:
                result = SwaigFunctionResult("Origin airport not set.")
                _change_step(result, "get_origin")
                return result

            if not destination:
                candidates = state.get("destination_candidates")
                if candidates:
                    result = SwaigFunctionResult(
                        "Destination airport not selected. Multiple candidates available."
                    )
                    _change_step(result, "disambiguate_destination")
                else:
                    result = SwaigFunctionResult("Destination airport not set.")
                    _change_step(result, "get_destination")
                return result

            if not departure_date:
                result = SwaigFunctionResult("Travel dates not set.")
                _change_step(result, "collect_trip_type")
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
                    f"No flights found.\nRoute: {origin_iata} to {dest_iata} on {departure_date}."
                )
                _change_step(result, "error_recovery")
                return result

            cabin_note = ""
            if actual_cabin != cabin:
                cabin_note = (
                    f"Cabin downgrade: {cabin.lower().replace('_', ' ')} unavailable, "
                    f"showing {actual_cabin.lower().replace('_', ' ')}.\n"
                )
                state["cabin_class"] = actual_cabin

            summaries = []
            for i, offer in enumerate(offers):
                summaries.append(summarize_offer(offer, i + 1, dictionaries))

            state["flight_offers"] = offers
            state["flight_summaries"] = summaries

            summary_text = " | ".join(summaries)
            count = len(offers)
            result = SwaigFunctionResult(
                f"{cabin_note}Flights found.\n{count} option{'s' if count > 1 else ''}: {summary_text}."
            )
            save_call_state(call_id, state)
            _sync_summary(result, state)
            _change_step(result, "present_options")
            return result

        self._do_search = _do_search  # expose to _define_question_steps

        # 6. SEARCH FLIGHTS (used by error_recovery for re-searches)
        @self.tool(
            name="search_flights",
            description="Search for available flights and return up to 3 options.",
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

            # If booking data not in state, check booking_answers from gather_info
            global_data = (raw_data or {}).get("global_data", {})
            booking_answers = global_data.get("booking_answers", {})

            if booking_answers and not state.get("departure_date"):
                # Transfer booking_answers to state
                departure_date = booking_answers.get("departure_date", "")
                return_date = booking_answers.get("return_date", "")
                adults = booking_answers.get("adults", 1)
                cabin_class = booking_answers.get("cabin_class", "ECONOMY")

                # Handle one-way trips (return_date might be "ONEWAY")
                if return_date and return_date.upper() == "ONEWAY":
                    return_date = None

                state["departure_date"] = departure_date
                if return_date:
                    state["return_date"] = return_date
                try:
                    state["adults"] = int(adults)
                except (ValueError, TypeError):
                    state["adults"] = 1
                state["cabin_class"] = cabin_class
                save_call_state(call_id, state)

            return _do_search(call_id, state)

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
                        "description": "The option number the caller chose",
                        "enum": [1, 2, 3],
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
                result = SwaigFunctionResult("No flight options on file.")
                _change_step(result,"search_flights")
                return result

            idx = option_number - 1
            if idx < 0 or idx >= len(flight_offers):
                available = ", ".join(str(i + 1) for i in range(len(flight_offers)))
                return SwaigFunctionResult(
                    f"Invalid selection.\nChosen: {option_number}. Valid options: {available}."
                )

            state["flight_offer"] = flight_offers[idx]
            state["flight_summary"] = flight_summaries[idx] if idx < len(flight_summaries) else None
            selected = flight_offers[idx]
            logger.info(f"select_flight: caller chose option {option_number}, "
                        f"offer id={selected.get('id') if isinstance(selected, dict) else 'N/A'}, "
                        f"keys={sorted(selected.keys()) if isinstance(selected, dict) else 'N/A'}")

            result = SwaigFunctionResult(f"Flight selected.\nOption {option_number}.")
            save_call_state(call_id, state)
            _sync_summary(result, state)
            _change_step(result,"confirm_price")
            return result

        # 5b. RESTART SEARCH (caller wants different dates or route)
        @self.tool(
            name="restart_search",
            description="Caller wants to change dates or route. Call this instead of select_flight.",
            wait_file="/sounds/typing.mp3",
            parameters={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why the caller wants to restart: 'different_dates' or 'different_route'",
                        "enum": ["different_dates", "different_route"],
                    },
                },
                "required": ["reason"],
            },
        )
        def restart_search(args, raw_data):
            reason = args.get("reason", "different_dates")
            call_id = _call_id(raw_data)
            # Clear booking asked flags so server-side guards re-fire on re-entry
            state = load_call_state(call_id)
            for flag in ["_departure_date_asked", "_return_date_asked",
                         "_trip_type_asked"]:
                state.pop(flag, None)
            save_call_state(call_id, state)

            if reason == "different_route":
                result = SwaigFunctionResult("Restarting — new route.")
                _change_step(result, "get_origin")
            else:
                result = SwaigFunctionResult("Restarting — new dates. Trip type preserved.")
                _change_step(result, _booking_step(state))
            return result

        # 5c. RESTART BOOKING (caller wants different dates from error_recovery)
        @self.tool(
            name="restart_booking",
            description="Restart the booking with new dates. Call from error recovery.",
            wait_file="/sounds/typing.mp3",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def restart_booking(args, raw_data):
            call_id = _call_id(raw_data)
            state = load_call_state(call_id)
            for flag in ["_departure_date_asked", "_return_date_asked",
                         "_trip_type_asked"]:
                state.pop(flag, None)
            save_call_state(call_id, state)
            result = SwaigFunctionResult("Restarting booking — new dates. Trip type preserved.")
            _change_step(result, _booking_step(state))
            return result

        # 6. GET FLIGHT PRICE
        @self.tool(
            name="get_flight_price",
            description="Confirm the exact price for the flight selected via select_flight.",
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
                result = SwaigFunctionResult("No flight selected.")
                _change_step(result,"search_flights")
                return result

            # Price the stored offer (mock always succeeds)
            logger.info(f"get_flight_price: pricing offer id={offer.get('id')}")
            pd = _price_offer(offer)
            po = (pd or {}).get("flightOffers", [])

            if not po:
                result = SwaigFunctionResult("Price confirmation failed.")
                _change_step(result,"error_recovery")
                return result

            priced_offer = po[0]
            price = priced_offer.get("price", {})
            total = price.get("grandTotal") or price.get("total", "?")
            currency = price.get("currency", "USD")
            baggage_info = _extract_baggage(priced_offer)

            state["priced_offer"] = priced_offer
            state["split_ticketing"] = False
            state["confirmed_price"] = f"${total} {currency}"
            logger.info(f"get_flight_price: confirmed ${total} {currency}")

            result = SwaigFunctionResult(
                f"Price confirmed.\n${total} {currency} per person including taxes. {baggage_info}"
            )
            save_call_state(call_id, state)
            _sync_summary(result, state)
            return result

        # 6a. CONFIRM BOOKING (caller accepts the price)
        @self.tool(
            name="confirm_booking",
            description="Caller accepted the price — proceed to booking.",
            wait_file="/sounds/typing.mp3",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def confirm_booking(args, raw_data):
            result = SwaigFunctionResult("Booking confirmed by caller.")
            _change_step(result, "create_booking")
            return result

        # 6b. DECLINE BOOKING (caller wants to go back)
        @self.tool(
            name="decline_booking",
            description="Caller declined the price — go back to flight options.",
            wait_file="/sounds/typing.mp3",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def decline_booking(args, raw_data):
            result = SwaigFunctionResult("Booking declined.")
            _change_step(result, "present_options")
            return result

        # 6c. BOOK FLIGHT
        @self.tool(
            name="book_flight",
            description="Book the confirmed flight and create a PNR.",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Booking that for you now"]},
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )
        def book_flight(args, raw_data):
            # All passenger details come from global_data — no args needed
            profile = (raw_data or {}).get("global_data", {}).get("passenger_profile") or {}
            caller_phone = (raw_data or {}).get("global_data", {}).get("caller_phone", "")

            first_name = (profile.get("first_name") or "").strip()
            last_name = (profile.get("last_name") or "").strip()
            email = (profile.get("email") or "").strip()
            phone = (caller_phone or profile.get("phone") or "").strip()
            date_of_birth = (profile.get("date_of_birth") or "").strip()
            gender = (profile.get("gender") or "").strip()

            missing = []
            if not first_name or not last_name:
                missing.append("name")
            if not email:
                missing.append("email")
            if not date_of_birth:
                missing.append("date of birth")
            if not gender:
                missing.append("gender")
            if not phone:
                missing.append("phone")
            if missing:
                result = SwaigFunctionResult(
                    f"Cannot book — missing passenger details.\nMissing: {', '.join(missing)}."
                )
                _change_step(result, "collect_profile")
                return result

            call_id = _call_id(raw_data)
            state = load_call_state(call_id)
            priced_offer = state.get("priced_offer")

            logger.info(f"book_flight: state check — "
                        f"origin={state.get('origin')}, "
                        f"destination={state.get('destination')}, "
                        f"priced_offer={'YES' if priced_offer else 'NO'}")

            # Guard: no origin
            if not state.get("origin"):
                result = SwaigFunctionResult("Origin airport not set.")
                _change_step(result,"get_origin")
                return result

            # Guard: no destination
            if not state.get("destination"):
                candidates = state.get("destination_candidates")
                if candidates:
                    result = SwaigFunctionResult(
                        "Destination airport not selected. Multiple candidates available."
                    )
                    _change_step(result,"disambiguate_destination")
                else:
                    result = SwaigFunctionResult("Destination airport not set.")
                    _change_step(result,"get_destination")
                return result

            # Guard: no confirmed price → back to pricing
            if not priced_offer:
                result = SwaigFunctionResult("No confirmed price on file.")
                _change_step(result,"confirm_price")
                return result

            logger.info(f"book_flight: {first_name} {last_name}, {email}")

            origin = state.get("origin", {})
            destination = state.get("destination", {})
            dep_date = state.get("departure_date", "")
            return_date = state.get("return_date")
            cabin = state.get("cabin_class", "ECONOMY")

            travelers = [{
                "id": "1",
                "dateOfBirth": date_of_birth,
                "name": {
                    "firstName": first_name.upper(),
                    "lastName": last_name.upper(),
                },
                "gender": gender,
                "contact": {
                    "emailAddress": email,
                    "phones": [{
                        "deviceType": "MOBILE",
                        "countryCallingCode": "1",
                        "number": re.sub(r"[^\d]", "", phone),
                    }],
                },
            }]

            # Book the priced offer (mock always succeeds)
            logger.info("book_flight: creating order")
            order = _create_order(priced_offer, travelers)

            if not order:
                result = SwaigFunctionResult(
                    "Booking failed — flight unavailable.\nPassenger details retained. Do not re-ask for personal info."
                )
                _sync_summary(result, state)
                _change_step(result,"error_recovery")
                return result

            pnr = order.get("associatedRecords", [{}])[0].get("reference", "UNKNOWN")
            phonetic = nato_spell(pnr)
            price = priced_offer.get("price", {})
            total = price.get("grandTotal") or price.get("total", "?")

            state["booking"] = {
                "pnr": pnr, "phonetic": phonetic,
                "route": f"{origin.get('iata', '?')} to {destination.get('iata', '?')}",
                "departure": dep_date, "price": total,
                "passenger": f"{first_name} {last_name}",
                "email": email, "phone": phone,
            }
            if return_date:
                state["booking"]["return"] = return_date

            logger.info(f"book_flight: PNR={pnr}, price=${total}")

            if profile and not profile.get("email") and email:
                profile_phone = profile.get("phone", "")
                if profile_phone:
                    update_passenger(profile_phone, email=email)

            # Extract per-leg details for dashboard display
            legs = []
            for i, itin in enumerate(priced_offer.get("itineraries", [])):
                direction = "outbound" if i == 0 else "return"
                itin_duration = itin.get("duration", "")
                for seg in itin.get("segments", []):
                    legs.append({
                        "direction": direction,
                        "itin_duration": itin_duration,
                        "carrier": seg.get("carrierCode", ""),
                        "operating_carrier": seg.get("operating", {}).get("carrierCode", ""),
                        "flight": seg.get("carrierCode", "") + seg.get("number", ""),
                        "aircraft": seg.get("aircraft", {}).get("code", ""),
                        "from": seg.get("departure", {}).get("iataCode", ""),
                        "dep_time": seg.get("departure", {}).get("at", ""),
                        "to": seg.get("arrival", {}).get("iataCode", ""),
                        "arr_time": seg.get("arrival", {}).get("at", ""),
                    })

            save_booking(
                call_id=call_id, pnr=pnr,
                passenger_name=f"{first_name} {last_name}",
                email=email, phone=phone,
                origin_iata=origin.get("iata", "?"), origin_name=origin.get("name", ""),
                destination_iata=destination.get("iata", "?"), destination_name=destination.get("name", ""),
                departure_date=dep_date, return_date=return_date,
                cabin_class=cabin, price=total,
                currency=price.get("currency", "USD"),
                legs_json=json.dumps(legs) if legs else None,
            )

            route_name = (f"{origin.get('name', origin.get('iata', '?'))} to "
                          f"{destination.get('name', destination.get('iata', '?'))}")

            sms_body = (
                f"Voyager - Flight Confirmed!\n"
                f"PNR: {pnr}\nRoute: {route_name}\n"
                f"Departure: {dep_date}"
                f"{' | Return: ' + return_date if return_date else ''}\n"
                f"Price: ${total}\nPassenger: {first_name} {last_name}\n"
                f"Thank you for using Voyager!"
            )

            flight_summary = state.get("flight_summary", "")
            result = SwaigFunctionResult(
                f"Booking confirmed.\nPNR: {pnr} ({phonetic}). Route: {route_name}. "
                f"Departure: {dep_date}. Total: ${total}. "
                f"{('Flight: ' + flight_summary + '. ') if flight_summary else ''}"
                f"SMS confirmation sent."
            )
            result.send_sms(
                to_number=phone,
                from_number=config.SIGNALWIRE_PHONE_NUMBER,
                body=sms_body,
            )
            save_call_state(call_id, state)
            _sync_summary(result, state)
            _change_step(result,"wrap_up")
            return result

        # 7. SUMMARIZE CONVERSATION
        @self.tool(
            name="summarize_conversation",
            description="Generate a structured call summary. Called automatically when the conversation ends.",
            wait_file="/sounds/typing.mp3",
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


def _parse_call_flow(call_data):
    """Extract step changes, function calls, and gather events from call log.

    Prefers call_timeline (new enriched format) when present, falls back to
    walking call_log with support for both old and new field names.
    Args for function nodes come from swaig_log (queue per command_name).
    """
    flow = []

    # Build swaig_log args queue by command_name (in order, skip native calls)
    swaig_args_queue = {}
    for entry in call_data.get("swaig_log", []):
        name = entry.get("command_name")
        if name and not entry.get("native"):
            swaig_args_queue.setdefault(name, []).append(entry.get("command_arg", ""))

    def pop_args(func_name):
        queue = swaig_args_queue.get(func_name, [])
        return queue.pop(0) if queue else ""

    # Fast path: use call_timeline if present (new enriched format)
    if "call_timeline" in call_data:
        current_step = None
        for event in call_data["call_timeline"]:
            etype = event.get("type")

            if etype == "step_change":
                from_step = event.get("from_step") or "START"
                to_step = event.get("to_step", "unknown")
                flow.append({"type": "step_change", "from": from_step, "to": to_step})
                current_step = to_step

            elif etype == "function_call" and not event.get("native"):
                func_name = event.get("function", "unknown")
                flow.append({
                    "type": "function_call",
                    "step": current_step or event.get("step", "unknown"),
                    "function": func_name,
                    "args": pop_args(func_name),
                })

            elif etype == "gather_question":
                flow.append({
                    "type": "gather_question",
                    "step": current_step or event.get("step"),
                    "key": event.get("key"),
                    "question_index": event.get("question_index", 0),
                })

            elif etype == "gather_answer":
                flow.append({
                    "type": "gather_answer",
                    "step": current_step or event.get("step"),
                    "key": event.get("key"),
                    "question_index": event.get("question_index", 0),
                    "confirmed": event.get("confirmed", False),
                })

        return flow

    # Fallback: walk call_log (supports old and new field names)
    current_step = None
    pending_functions = []

    for entry in call_data.get("call_log", []):
        role = entry.get("role")

        if role == "system-log":
            action = entry.get("action")
            metadata = entry.get("metadata", {})

            if action in ("change_step", "step_change"):
                # New format: metadata.to_step; old format: entry.name
                step_name = metadata.get("to_step") or entry.get("name", "unknown")

                if current_step is None and pending_functions:
                    flow.append({"type": "step_change", "from": "START", "to": step_name})
                    for func in pending_functions:
                        func["step"] = "START"
                        flow.insert(len(flow) - 1, func)
                    pending_functions = []
                else:
                    flow.append({"type": "step_change", "from": current_step or "START", "to": step_name})

                current_step = step_name

            elif action in ("gather_submit", "call_function", "function_call"):
                # New format: metadata.function; old format: entry.function
                func_name = metadata.get("function") or entry.get("function", action)
                func_obj = {
                    "type": "function_call",
                    "step": current_step,
                    "function": func_name,
                    "args": pop_args(func_name),
                }
                if current_step is None:
                    pending_functions.append(func_obj)
                else:
                    flow.append(func_obj)

    return flow


def _generate_mermaid_flow(flow):
    """Generate Mermaid flowchart from flow data."""
    def sanitize_label(text):
        text = str(text).replace('"', "'").replace('#', '').replace('&', 'and')
        return text[:37] + "..." if len(text) > 40 else text

    lines = ["graph LR"]
    lines.append("    classDef stepNode fill:#2a2e42,stroke:#4a9eff,stroke-width:2px,color:#fff")
    lines.append("    classDef funcNode fill:#1a1d2e,stroke:#ffa500,stroke-width:2px,color:#ffa500")
    lines.append("    classDef gatherQNode fill:#0d1117,stroke:#4a9eff,stroke-width:1px,stroke-dasharray:4,color:#6a9eff")
    lines.append("    classDef gatherANode fill:#0d1117,stroke:#4a9e6a,stroke-width:1px,stroke-dasharray:4,color:#4a9e6a")
    lines.append("")

    node_id = 0
    step_nodes = {}
    last_func_per_step = {}

    # Create step nodes
    for item in flow:
        if item["type"] == "step_change":
            for step in [item["from"], item["to"]]:
                if step not in step_nodes:
                    step_nodes[step] = f"S{node_id}"
                    lines.append(f'    {step_nodes[step]}["{sanitize_label(step)}"]:::stepNode')
                    node_id += 1

    # Create function chains and gather nodes
    for item in flow:
        if item["type"] == "step_change":
            lines.append(f'    {step_nodes[item["from"]]} --> {step_nodes[item["to"]]}')
            last_func_per_step[item["to"]] = None

        elif item["type"] == "function_call":
            func_node = f"F{node_id}"
            node_id += 1
            step = item["step"]
            func_name = item["function"]
            args_str = item.get("args", "")

            label = func_name

            try:
                args_obj = json.loads(args_str) if args_str else {}
                if func_name == "resolve_location":
                    loc = sanitize_label(args_obj.get("location_text", ""))
                    loc_type = args_obj.get("location_type", "")
                    label = f"resolve_location<br/>{loc} ({loc_type})" if loc else func_name
                elif func_name == "select_trip_type":
                    label = "select_trip_type<br/>(from gather)"
                elif func_name == "select_flight":
                    opt = args_obj.get("option_number", "")
                    label = f"select_flight<br/>Option {opt}" if opt else func_name
                elif args_obj:
                    # Generic: show first arg key=value if short enough
                    first_key, first_val = next(iter(args_obj.items()))
                    short = sanitize_label(f"{first_key}={first_val}")
                    label = f"{func_name}<br/>{short}"
            except:
                pass

            lines.append(f'    {func_node}["{label}"]:::funcNode')

            if step in last_func_per_step and last_func_per_step[step]:
                lines.append(f'    {last_func_per_step[step]} --> {func_node}')
            elif step in step_nodes:
                lines.append(f'    {step_nodes[step]} -.-> {func_node}')

            last_func_per_step[step] = func_node

        elif item["type"] == "gather_question":
            q_node = f"Q{node_id}"
            node_id += 1
            step = item["step"]
            key = item.get("key", "?")
            lines.append(f'    {q_node}(["? {sanitize_label(key)}"]):::gatherQNode')

            if step in last_func_per_step and last_func_per_step[step]:
                lines.append(f'    {last_func_per_step[step]} -.-> {q_node}')
            elif step in step_nodes:
                lines.append(f'    {step_nodes[step]} -.-> {q_node}')

            last_func_per_step[step] = q_node

        elif item["type"] == "gather_answer":
            a_node = f"A{node_id}"
            node_id += 1
            step = item["step"]
            key = item.get("key", "?")
            confirmed = item.get("confirmed", False)
            check = " v" if confirmed else ""
            lines.append(f'    {a_node}(["{sanitize_label(key)}{check}"]):::gatherANode')

            if step in last_func_per_step and last_func_per_step[step]:
                lines.append(f'    {last_func_per_step[step]} --> {a_node}')
            elif step in step_nodes:
                lines.append(f'    {step_nodes[step]} -.-> {a_node}')

            last_func_per_step[step] = a_node

    return "\n".join(lines)


def _generate_step_flow_diagram(ctx):
    """Generate Mermaid diagram from context steps."""
    def sanitize_label(text):
        text = str(text).replace('"', "'").replace('#', '').replace('&', 'and')
        return text[:37] + "..." if len(text) > 40 else text

    lines = ["%%{init: {'flowchart': {'rankSpacing': 100, 'nodeSpacing': 50}}}%%"]
    lines.append("flowchart LR")
    lines.append("    classDef stepNode fill:#2a2e42,stroke:#4a9eff,stroke-width:2px,color:#fff")
    lines.append("    classDef funcNode fill:#1a1d2e,stroke:#ffa500,stroke-width:2px,color:#ffa500")
    lines.append("")

    node_id = 0
    step_nodes = {}

    # Create main step flow first (forces horizontal layout)
    step_chain = []
    for step_name in ctx._step_order:
        step_nodes[step_name] = f"S{node_id}"
        step_chain.append(step_nodes[step_name])
        lines.append(f'    {step_nodes[step_name]}["{sanitize_label(step_name)}"]:::stepNode')
        node_id += 1

    # Connect steps in one chain
    chain = " --> ".join(step_chain)
    lines.append(f'    {chain}')

    # Add functions below each step
    for step_name in ctx._step_order:
        step = ctx.get_step(step_name)
        functions = step._functions if hasattr(step, '_functions') else []

        if functions and functions != ["none"]:
            for func in functions:
                func_node = f"F{node_id}"
                node_id += 1
                lines.append(f'    {func_node}["{sanitize_label(func)}"]:::funcNode')
                lines.append(f'    {step_nodes[step_name]} -.-> {func_node}')

    return "\n".join(lines)


def _generate_flow_html(call_data, mermaid_code):
    """Generate HTML page with Mermaid diagram."""
    call_id = call_data.get("call_id", "unknown")
    caller = call_data.get("caller_id_number", "unknown")

    global_data = call_data.get("global_data", {})
    profile = global_data.get("passenger_profile")
    booking = global_data.get("booking_state", {}).get("booking")

    summary = []
    if profile:
        summary.append(f"Profile: {profile.get('first_name')} {profile.get('last_name')}")
    if booking:
        summary.append(f"Booking: {booking.get('pnr')} - {booking.get('route')}")

    summary_html = "<br>".join(summary) if summary else "No booking completed"

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Call Flow: {call_id}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
body{{font-family:sans-serif;background:#0f1117;color:#e0e0e0;margin:0;padding:20px}}
.header{{background:#1a1d2e;padding:20px;border-radius:8px;margin-bottom:20px}}
.header h1{{margin:0 0 10px 0;color:#4a9eff}}
.summary{{background:#1a1d2e;padding:15px;border-radius:8px;margin-bottom:20px;border-left:3px solid #4a9eff}}
.diagram{{background:#1a1d2e;padding:20px;border-radius:8px;overflow-x:auto}}
.mermaid{{display:flex;justify-content:center}}
</style></head><body>
<div class="header"><h1>Call Flow Visualization</h1>
<div>Call ID: <code>{call_id}</code><br>Caller: <code>{caller}</code></div></div>
<div class="summary"><strong>Call Summary:</strong><br>{summary_html}</div>
<div class="diagram"><div class="mermaid">{mermaid_code}</div></div>
<script>mermaid.initialize({{startOnLoad:true,theme:'dark',flowchart:{{curve:'basis',padding:20}}}});</script>
</body></html>"""


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

    @server.app.post("/flow")
    def view_call_flow():
        """Generate interactive flow visualization for uploaded call data."""
        from flask import request

        call_data = request.get_json()
        if not call_data:
            return {"error": "No call data provided"}, 400

        flow = _parse_call_flow(call_data)
        mermaid_code = _generate_mermaid_flow(flow)
        html = _generate_flow_html(call_data, mermaid_code)

        return server.app.response_class(
            response=html,
            status=200,
            mimetype='text/html'
        )

    @server.app.get("/state-flow")
    def state_flow_diagram():
        """Generate state flow diagram from agent configuration."""
        agent = VoyagerAgent()
        ctx = agent._contexts_builder.get_context("default")

        mermaid_code = _generate_step_flow_diagram(ctx)
        html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Voyager State Flow</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
body{{font-family:sans-serif;background:#0f1117;color:#e0e0e0;margin:0;padding:20px}}
.header{{background:#1a1d2e;padding:20px;border-radius:8px;margin-bottom:20px}}
.header h1{{margin:0;color:#4a9eff}}
.diagram{{background:#1a1d2e;padding:20px;border-radius:8px;overflow-x:auto}}
</style></head><body>
<div class="header"><h1>Voyager Agent State Flow</h1></div>
<div class="diagram"><div class="mermaid">{mermaid_code}</div></div>
<script>mermaid.initialize({{startOnLoad:true,theme:'dark'}});</script>
</body></html>"""

        return server.app.response_class(response=html, status=200, mimetype='text/html')

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
