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
        self.set_param("end_of_speech_timeout", 500)
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
            "Spell confirmation codes using the NATO phonetic alphabet.",
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
                return SwaigFunctionResult("No answer provided. Ask the caller again.")
            # Guard: reject duplicate submission (model batched calls)
            global_data = (raw_data or {}).get("global_data", {})
            existing = global_data.get(_storage_ns, {})
            if existing.get(_key_name):
                return SwaigFunctionResult(
                    f"Already have {_key_name.replace('_', ' ')}. Move on to the next question."
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
        greeting.set_valid_steps(["profile_last_name", "get_destination", "disambiguate_origin"])  # overridden by _per_call_config

        # GET ORIGIN
        get_origin = ctx.add_step("get_origin")
        get_origin.add_section("Task", "Collect the departure city or airport")
        get_origin.add_bullets("Process", [
            "Ask where they're flying from, then call resolve_location with location_text and location_type='origin'",
            "If caller says an IATA code directly ('I'm flying from LAX'), still call resolve_location to validate it",
            "After resolve_location returns, tell the caller which airport was found and ask if that's correct",
            "If they confirm, move to get_destination",
            "If multiple airports are returned, move to disambiguate_origin",
            "If no match, ask the caller to try a different city name",
        ])
        # resolve_location is the only available function; empty text guard is in the tool handler
        get_origin.set_step_criteria("Origin airport resolved and confirmed")
        get_origin.set_functions(["resolve_location"])
        get_origin.set_valid_steps(["get_destination", "disambiguate_origin"])

        # DISAMBIGUATE ORIGIN
        disambiguate_origin = ctx.add_step("disambiguate_origin")
        disambiguate_origin.add_section("Task", "Ask the caller to choose between multiple origin airports")
        disambiguate_origin.add_bullets("Process", [
            "Present the airports by name and city and ask which they prefer",
            "Call select_airport with location_type='origin' and the chosen IATA code",
            "If the caller says 'any', call select_airport with the first candidate",
        ])
        # select_airport is the only available function; valid_steps enforces transitions
        disambiguate_origin.set_step_criteria("Origin airport stored via select_airport")
        disambiguate_origin.set_functions(["select_airport"])
        disambiguate_origin.set_valid_steps(["get_destination"])

        # GET DESTINATION
        get_destination = ctx.add_step("get_destination")
        get_destination.add_section("Task", "Collect the arrival city or airport")
        get_destination.add_bullets("Process", [
            "Ask where they're flying to — or if they already said a destination, call resolve_location right away with location_type='destination'",
            "After resolve_location returns, tell the caller which airport was found and ask if that's correct",
            "If they confirm, move to collect_trip_type",
            "If multiple airports are returned, move to disambiguate_destination",
            "If no match, ask the caller for clarification",
        ])
        # resolve_location is the only available function; valid_steps enforces transitions
        get_destination.set_step_criteria("Destination airport resolved and confirmed")
        get_destination.set_functions(["resolve_location"])
        get_destination.set_valid_steps(["collect_trip_type", "disambiguate_destination"])

        # DISAMBIGUATE DESTINATION
        disambiguate_destination = ctx.add_step("disambiguate_destination")
        disambiguate_destination.add_section("Task", "Ask the caller to choose between multiple destination airports")
        disambiguate_destination.add_bullets("Process", [
            "Present the airports by name and ask which they prefer",
            "Call select_airport with location_type='destination' and the chosen IATA code",
            "If the caller says 'any', call select_airport with the first candidate",
        ])
        # select_airport is the only available function; valid_steps enforces transitions
        disambiguate_destination.set_step_criteria("Destination airport stored via select_airport")
        disambiguate_destination.set_functions(["select_airport"])
        disambiguate_destination.set_valid_steps(["collect_trip_type"])

        # COLLECT TRIP TYPE (simple branch point)
        collect_trip_type = ctx.add_step("collect_trip_type")
        collect_trip_type.add_section("Task", "Ask whether this is a round-trip or one-way flight")
        collect_trip_type.add_bullets("Process", [
            "Ask the caller: 'Is this a round-trip or one-way?'",
            "Read back their answer and ask them to confirm it is correct",
            "Call select_trip_type with trip_type 'round_trip' or 'one_way' and their confirmation",
        ])
        collect_trip_type.set_step_criteria("Trip type confirmed and submitted via select_trip_type")
        collect_trip_type.set_functions(["select_trip_type"])
        collect_trip_type.set_valid_steps(["booking_departure"])

        # SEARCH FLIGHTS
        search_flights_step = ctx.add_step("search_flights")
        search_flights_step.add_section("Task", "Search for flights and prepare up to 3 options for presentation")
        search_flights_step.add_bullets("Process", [
            "Call search_flights to find available flights",
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
            "Read each flight option from booking_state.flight_summaries — include airline, stops, times, duration, and price",
            "Label them 'Option 1' then 'Option 2' then 'Option 3'",
            "Ask which option they'd like or if they want to try different dates or a different route",
            "When the caller picks one, call select_flight with that option_number",
            "If caller wants different dates or a different route, call restart_search",
        ])
        present_options.set_step_criteria("Caller selects an option via select_flight or requests new search via restart_search")
        present_options.set_functions(["select_flight", "restart_search"])
        present_options.set_valid_steps(["confirm_price", "collect_trip_type", "get_origin"])

        # CONFIRM PRICE
        confirm_price = ctx.add_step("confirm_price")
        confirm_price.add_section("Task", "Confirm the live price on the selected flight")
        confirm_price.add_bullets("Process", [
            "Call get_flight_price to confirm the live fare",
            "Read back the confirmed price and baggage allowance to the caller",
            "Ask: 'Shall I go ahead and book this?'",
            "If they say yes, call confirm_booking",
            "If they say no, call decline_booking",
        ])
        confirm_price.set_step_criteria("Caller confirms or declines via confirm_booking or decline_booking")
        confirm_price.set_functions(["get_flight_price", "confirm_booking", "decline_booking"])
        confirm_price.set_valid_steps(["create_booking", "present_options", "error_recovery"])

        # CREATE BOOKING
        create_booking = ctx.add_step("create_booking")
        create_booking.add_section("Task", "Book the flight and wrap up")
        create_booking.add_bullets("Process", [
            "Call book_flight to create the reservation",
            "Read the PNR back to the caller using the NATO phonetic spelling provided",
            "Let them know the confirmation has been texted to their phone",
            "Thank them and say goodbye",
        ])
        # book_flight takes no parameters — profile data is read automatically
        create_booking.set_step_criteria("Booking created, PNR read back, call ending")
        create_booking.set_functions(["book_flight"])
        create_booking.set_valid_steps(["wrap_up", "error_recovery"])

        # ERROR RECOVERY
        error_recovery = ctx.add_step("error_recovery")
        error_recovery.add_section("Task", "Handle booking failures, no results, and mid-flow changes")
        error_recovery.add_bullets("Process", [
            "Offer to search for different options on the same route or try a different route",
            "If different route: ask for the new destination and call resolve_location",
            "If different dates: call restart_booking to start over with new dates",
            "If same route: call search_flights to find new options",
        ])
        error_recovery.set_step_criteria("Recovery action taken")
        error_recovery.set_functions(["resolve_location", "search_flights", "restart_booking"])
        error_recovery.set_valid_steps(["present_options", "collect_trip_type", "get_destination", "disambiguate_origin"])

        # WRAP UP
        wrap_up = ctx.add_step("wrap_up")
        wrap_up.add_section("Task", "End the call")
        wrap_up.add_bullets("Process", [
            "Say a brief, warm goodbye: 'Thanks for flying with Voyager — have an amazing trip!'",
            "End the call",
        ])
        wrap_up.set_functions("none")
        wrap_up.set_valid_steps([])

    def _define_question_steps(self):
        """Create one-step-per-question flows for profile and booking collection."""
        ctx = self._contexts_builder.get_context("default")
        _agent = self  # local ref for closures

        def _call_id(raw_data):
            if not isinstance(raw_data, dict):
                return "unknown"
            return raw_data.get("call_id", "unknown")

        # ── Validators (all take value, raw_data) ──

        def _not_empty(value, raw_data):
            if not value:
                return "No answer provided. Ask the caller again."
            return None

        def _date_format(value, raw_data):
            try:
                date.fromisoformat(value)
            except (ValueError, TypeError):
                return f"'{value}' is not a valid date. Must be YYYY-MM-DD format. Ask the caller again."
            return None

        def _gender_val(value, raw_data):
            if value.upper() not in ("MALE", "FEMALE"):
                return "Must be MALE or FEMALE. Ask the caller again."
            return None

        def _seat_val(value, raw_data):
            if value.upper() not in ("WINDOW", "AISLE"):
                return "Must be WINDOW or AISLE. Ask the caller again."
            return None

        def _cabin_val(value, raw_data):
            if value.upper() not in ("ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"):
                return "Must be ECONOMY, PREMIUM_ECONOMY, BUSINESS, or FIRST. Ask the caller again."
            return None

        def _date_not_past(value, raw_data):
            err = _date_format(value, raw_data)
            if err:
                return err
            if date.fromisoformat(value) < date.today():
                return f"The date {value} is in the past. Ask for a future date."
            return None

        def _return_date_val(value, raw_data):
            err = _date_not_past(value, raw_data)
            if err:
                return err
            global_data = (raw_data or {}).get("global_data", {})
            answers = global_data.get("booking_answers", {})
            dep_str = answers.get("departure_date", "")
            if dep_str:
                try:
                    if date.fromisoformat(value) <= date.fromisoformat(dep_str):
                        return (f"Return date {value} must be after departure date {dep_str}. "
                                "Ask for a later date.")
                except ValueError:
                    pass
            return None

        def _integer_1_8(value, raw_data):
            try:
                n = int(value)
            except (ValueError, TypeError):
                return "Must be a number between 1 and 8. Ask the caller again."
            if n < 1 or n > 8:
                return ("Must be between 1 and 8. For larger parties, "
                        "tell the caller to contact a travel agent.")
            return None

        # ── Profile question steps (8) ──

        self._add_question_step(ctx, "profile_first_name",
            task="Collect the caller's first name",
            question="What is your first name?",
            tool_name="submit_first_name", key_name="first_name",
            storage_ns="profile_answers", next_step="profile_last_name",
            validator=_not_empty).set_valid_steps(["profile_last_name"])

        self._add_question_step(ctx, "profile_last_name",
            task="Collect the caller's last name",
            question="What is your last name?",
            tool_name="submit_last_name", key_name="last_name",
            storage_ns="profile_answers", next_step="profile_dob",
            validator=_not_empty).set_valid_steps(["profile_dob"])

        self._add_question_step(ctx, "profile_dob",
            task="Collect the caller's date of birth",
            question="What is your date of birth including month, day, and year?",
            tool_name="submit_dob", key_name="date_of_birth",
            storage_ns="profile_answers", next_step="profile_gender",
            confirm=True, validator=_date_format,
            extra_instructions=["Accept natural language but submit in YYYY-MM-DD format"]
        ).set_valid_steps(["profile_gender"])

        self._add_question_step(ctx, "profile_gender",
            task="Collect the caller's gender",
            question="Are you male or female?",
            tool_name="submit_gender", key_name="gender",
            storage_ns="profile_answers", next_step="profile_email",
            validator=_gender_val,
            extra_instructions=["Submit exactly MALE or FEMALE"]
        ).set_valid_steps(["profile_email"])

        self._add_question_step(ctx, "profile_email",
            task="Collect the caller's email address",
            question="What email should we send confirmations to?",
            tool_name="submit_email", key_name="email",
            storage_ns="profile_answers", next_step="profile_seat_pref",
            confirm=True, validator=_not_empty
        ).set_valid_steps(["profile_seat_pref"])

        self._add_question_step(ctx, "profile_seat_pref",
            task="Collect the caller's seat preference",
            question="Do you prefer a window or aisle seat?",
            tool_name="submit_seat_pref", key_name="seat_preference",
            storage_ns="profile_answers", next_step="profile_cabin_pref",
            validator=_seat_val,
            extra_instructions=["Submit exactly WINDOW or AISLE"]
        ).set_valid_steps(["profile_cabin_pref"])

        self._add_question_step(ctx, "profile_cabin_pref",
            task="Collect the caller's cabin preference",
            question="What cabin class do you usually fly?",
            tool_name="submit_cabin_pref", key_name="cabin_preference",
            storage_ns="profile_answers", next_step="profile_home_airport",
            validator=_cabin_val,
            extra_instructions=["Submit exactly ECONOMY, PREMIUM_ECONOMY, BUSINESS, or FIRST"]
        ).set_valid_steps(["profile_home_airport"])

        # profile_home_airport — custom handler (creates passenger inline)
        ha_step = ctx.add_step("profile_home_airport")
        ha_step.add_section("Task", "Collect the caller's home airport")
        ha_step.add_bullets("Process", [
            "Ask the caller: 'What airport do you usually fly from?'",
            "Call resolve_location with their answer first to validate it",
            "Submit in 'Airport Name (IATA)' format, e.g. 'San Francisco International (SFO)'",
            "Read back the airport and ask them to confirm it is correct",
            "Call submit_home_airport with the confirmed answer",
        ])
        ha_step.set_step_criteria("Answer submitted via submit_home_airport")
        ha_step.set_functions(["resolve_location", "submit_home_airport"])
        ha_step.set_valid_steps(["get_origin"])

        @self.tool(name="submit_home_airport",
                   description="Submit the caller's home airport",
                   wait_file="/sounds/typing.mp3",
                   parameters={"type": "object", "properties": {
                       "value": {"type": "string",
                                 "description": "Home airport in 'Airport Name (IATA)' format"},
                       "confirmed": {"type": "boolean",
                                     "description": "Set true only after the caller explicitly confirmed"},
                   }, "required": ["value"]})
        def _submit_home_airport(args, raw_data):
            value = (args.get("value") or "").strip()
            confirmed = args.get("confirmed", False)

            # Server-side guard: first call ALWAYS bounces
            call_id = ((raw_data or {}).get("call_id", "unknown")
                       if isinstance(raw_data, dict) else "unknown")
            _state = load_call_state(call_id)
            if not _state.get("_home_airport_asked"):
                _state["_home_airport_asked"] = True
                save_call_state(call_id, _state)
                return SwaigFunctionResult(
                    "Ask the caller for their home airport. "
                    "Then call resolve_location with their answer first, "
                    "then call submit_home_airport with confirmed set to true."
                )
            if not confirmed:
                return SwaigFunctionResult(
                    f"Read '{value}' back to the caller and ask if that's correct. "
                    "Then call submit_home_airport again with confirmed set to true."
                )
            # Clear the asked flag
            _state.pop("_home_airport_asked", None)
            save_call_state(call_id, _state)
            if not value:
                return SwaigFunctionResult("No answer provided. Ask the caller again.")

            global_data = (raw_data or {}).get("global_data", {})
            caller_phone = global_data.get("caller_phone", "")
            answers = dict(global_data.get("profile_answers", {}))
            answers["home_airport_name"] = value

            # Extract IATA code
            home_airport_iata = None
            iata_match = re.search(r"\(([A-Z]{3})\)", value)
            if not iata_match:
                iata_match = re.search(r"\b([A-Za-z]{3})\b", value)
            if iata_match:
                home_airport_iata = iata_match.group(1).upper()

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
                home_airport_name=value,
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
                "home_airport_name": value,
            }

            result = SwaigFunctionResult(
                f"Profile saved for {answers.get('first_name', '')} {answers.get('last_name', '')}. "
                f"Home airport: {value}."
            )
            result.update_global_data({
                "profile_answers": answers,
                "passenger_profile": profile,
                "is_new_caller": False,
                "caller_phone": caller_phone,
            })
            result.swml_change_step("get_origin")
            return result

        # ── Booking question steps (4) ──

        def _departure_next(raw_data):
            cid = _call_id(raw_data)
            st = load_call_state(cid)
            return "booking_return" if st.get("trip_type") == "round_trip" else "booking_adults"

        self._add_question_step(ctx, "booking_departure",
            task="Collect the departure date",
            question="When would you like to depart?",
            tool_name="submit_departure", key_name="departure_date",
            storage_ns="booking_answers", next_step=_departure_next,
            confirm=True, validator=_date_not_past,
            extra_instructions=["Accept natural language but submit in YYYY-MM-DD format"]
        ).set_valid_steps(["booking_return", "booking_adults"])

        self._add_question_step(ctx, "booking_return",
            task="Collect the return date",
            question="And when would you like to return?",
            tool_name="submit_return", key_name="return_date",
            storage_ns="booking_answers", next_step="booking_adults",
            confirm=True, validator=_return_date_val,
            extra_instructions=["Accept natural language but submit in YYYY-MM-DD format",
                                "Must be after the departure date"]
        ).set_valid_steps(["booking_adults"])

        self._add_question_step(ctx, "booking_adults",
            task="Collect the number of passengers",
            question="How many passengers will be traveling?",
            tool_name="submit_adults", key_name="adults",
            storage_ns="booking_answers", next_step="booking_cabin",
            validator=_integer_1_8,
            extra_instructions=["Submit as a positive integer (1-8)",
                                "Maximum 8 — for larger parties, tell the caller to contact a travel agent"]
        ).set_valid_steps(["booking_cabin"])

        # booking_cabin — custom handler (saves to SQLite, transitions to search)
        bc_step = ctx.add_step("booking_cabin")
        bc_step.add_section("Task", "Collect the cabin class for the flight search")
        bc_step.add_bullets("Process", [
            "Ask the caller: 'What cabin class would you like — economy, premium economy, business, or first?'",
            "If the passenger has a stored cabin preference in their profile, suggest it",
            "Call submit_cabin with their answer",
        ])
        bc_step.set_step_criteria("Answer submitted via submit_cabin")
        bc_step.set_functions(["submit_cabin"])
        bc_step.set_valid_steps(["present_options", "error_recovery"])

        @self.tool(name="submit_cabin",
                   description="Submit the caller's cabin class preference",
                   wait_file="/sounds/typing.mp3",
                   parameters={"type": "object", "properties": {
                       "value": {"type": "string",
                                 "description": "Cabin class: ECONOMY, PREMIUM_ECONOMY, BUSINESS, or FIRST"},
                   }, "required": ["value"]})
        def _submit_cabin(args, raw_data):
            value = (args.get("value") or "").strip().upper()
            if value not in ("ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"):
                return SwaigFunctionResult(
                    "Must be ECONOMY, PREMIUM_ECONOMY, BUSINESS, or FIRST. Ask the caller again."
                )

            global_data = (raw_data or {}).get("global_data", {})
            answers = dict(global_data.get("booking_answers", {}))
            answers["cabin_class"] = value

            # Save booking fields to SQLite call state
            call_id = _call_id(raw_data)
            state = load_call_state(call_id)
            state["departure_date"] = answers.get("departure_date", "")
            if answers.get("return_date"):
                state["return_date"] = answers["return_date"]
            try:
                state["adults"] = int(answers.get("adults", "1"))
            except (ValueError, TypeError):
                state["adults"] = 1
            state["cabin_class"] = value
            save_call_state(call_id, state)

            # Run the search inline — skip the search_flights step entirely
            result = _agent._do_search(call_id, state)
            result.update_global_data({
                "booking_answers": answers,
                "booking_state": build_ai_summary(state),
            })
            return result

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

            agent.update_global_data({
                "passenger_profile": profile,
                "is_new_caller": False,
                "caller_phone": caller_phone,
            })

            # RETURNING CALLER — greeting IS the origin collection step
            ctx = agent._contexts_builder.get_context("default")

            greeting_step = ctx.get_step("greeting")
            greeting_step._sections = []  # Clear base shell
            greeting_step.set_functions(["resolve_location"])
            greeting_step.set_valid_steps(["get_destination", "disambiguate_origin"])

            home_airport = passenger.get("home_airport_name")
            if home_airport:
                greeting_step.add_section("Task", "Welcome the caller and confirm departure airport")
                greeting_step.add_bullets("Process", [
                    f"Say: 'Welcome back {passenger['first_name']}! Let me help you book a flight.'",
                    f"The caller's home airport is {home_airport} — offer this first",
                    f"Ask: 'Are you flying from {home_airport} today, or somewhere else?'",
                    f"If they confirm, call resolve_location with '{home_airport}' and location_type='origin'",
                    "If they want a different airport, ask where and call resolve_location with their answer",
                    "After resolve_location returns, tell the caller which airport was found and ask if that's correct",
                    "If they confirm, move to get_destination",
                ])
            else:
                greeting_step.add_section("Task", "Welcome the caller and collect departure airport")
                greeting_step.add_bullets("Process", [
                    f"Say: 'Welcome back {passenger['first_name']}! Let me help you book a flight.'",
                    "Ask where they're flying from, then call resolve_location with location_type='origin'",
                    "After resolve_location returns, tell the caller which airport was found and ask if that's correct",
                    "If they confirm, move to get_destination",
                    "If multiple airports are returned, move to disambiguate_origin",
                ])

            greeting_step.set_step_criteria("Origin airport resolved and confirmed")

            # Disable all profile steps — returning caller skips profile collection
            for step_name in ["profile_first_name", "profile_last_name", "profile_dob",
                              "profile_gender", "profile_email", "profile_seat_pref",
                              "profile_cabin_pref", "profile_home_airport"]:
                ps = ctx.get_step(step_name)
                ps.set_functions("none")
                ps.set_valid_steps([])

            agent.prompt_add_section("Passenger Profile", "${global_data.passenger_profile}")

        else:
            # NEW CALLER
            agent.update_global_data({
                "passenger_profile": None,
                "is_new_caller": True,
                "caller_phone": caller_phone,
            })

            # NEW CALLER — greeting IS the first-name question
            ctx = agent._contexts_builder.get_context("default")

            greeting_step = ctx.get_step("greeting")
            greeting_step._sections = []  # Clear base shell
            greeting_step.add_section("Task", "Welcome the caller and ask for their first name")
            greeting_step.add_bullets("Process", [
                "Say: 'Welcome to Voyager! I'd love to help you book a flight. "
                "Let me get your profile set up.'",
                "Then ask: 'What is your first name?'",
                "Call submit_first_name with their answer",
            ])
            greeting_step.set_functions(["submit_first_name"])
            greeting_step.set_valid_steps(["profile_last_name"])
            greeting_step.set_step_criteria("First name submitted via submit_first_name")

            # Disable profile_first_name — greeting handles it
            pf_step = ctx.get_step("profile_first_name")
            pf_step.set_functions("none")
            pf_step.set_valid_steps([])

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
                return SwaigFunctionResult(
                    "No location provided. Ask the caller for a city or airport name "
                    "and call resolve_location again with their answer."
                )

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

                logger.info(f"resolve_location: auto-selected {top['iata']} for {location_type}")

                if mode == "verify":
                    return SwaigFunctionResult(
                        f"Resolved: {top['name']} ({top['iata']})"
                        f"{' in ' + top['city'] if top['city'] else ''}."
                    )

                state[location_type] = airport_info
                next_step = "get_destination" if location_type == "origin" else "collect_trip_type"
                result = SwaigFunctionResult(
                    f"The closest major airport is {top['name']} ({top['iata']})"
                    f"{' in ' + top['city'] if top['city'] else ''}. "
                    f"Saved as {location_type}. "
                    f"Tell the caller and ask if that's correct before proceeding."
                )
                result.add_dynamic_hints([h for h in [top["name"], top["city"]] if h])
                save_call_state(call_id, state)
                _sync_summary(result, state)
                _change_step(result, next_step)
                return result
            else:
                # Multiple airports — need disambiguation
                top_3 = ranked[:3]
                airport_list = ", ".join(
                    f"{a['name']} ({a['iata']})" for a in top_3
                )

                if mode == "verify":
                    return SwaigFunctionResult(
                        f"Multiple airports: {airport_list}. Ask which one they mean."
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
            description="Record whether this is a round-trip or one-way flight.",
            wait_file="/sounds/typing.mp3",
            parameters={
                "type": "object",
                "properties": {
                    "trip_type": {
                        "type": "string",
                        "description": "round_trip or one_way",
                        "enum": ["round_trip", "one_way"],
                    },
                    "confirmed": {
                        "type": "boolean",
                        "description": "Set true only after the caller explicitly confirmed",
                    },
                },
                "required": ["trip_type"],
            },
        )
        def select_trip_type(args, raw_data):
            trip_type = args["trip_type"]
            confirmed = args.get("confirmed", False)

            call_id = _call_id(raw_data)
            state = load_call_state(call_id)

            # Server-side guard: first call ALWAYS bounces regardless of parameters.
            # The model can't bypass this by sending confirmed=true on the first call.
            if not state.get("_trip_type_asked"):
                state["_trip_type_asked"] = True
                save_call_state(call_id, state)
                return SwaigFunctionResult(
                    "Ask the caller: 'Is this a round-trip or one-way flight?' "
                    "Wait for their answer, then call select_trip_type with their choice "
                    "and confirmed set to true."
                )

            if not confirmed:
                label = trip_type.replace("_", " ")
                return SwaigFunctionResult(
                    f"Read back '{label}' to the caller and ask if that's correct. "
                    f"Then call select_trip_type again with confirmed set to true."
                )

            # Commit — clear the asked flag for potential re-entry
            state.pop("_trip_type_asked", None)
            state["trip_type"] = trip_type
            save_call_state(call_id, state)

            next_step = "booking_departure"
            result = SwaigFunctionResult(f"Got it — {trip_type.replace('_', ' ')}.")
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
                return SwaigFunctionResult("Missing name. Cannot save profile.")

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
                    f"Profile saved for {first_name} {last_name}. "
                    f"Their home airport is {home_airport_name} ({home_airport_iata}). "
                    f"Ask the caller: 'Would you like to fly from {home_airport_name} today, or somewhere else?' "
                    f"If they confirm, call resolve_location with '{home_airport_name}' and location_type='origin'. "
                    "If they want a different airport, ask where and call resolve_location with their answer."
                )
            else:
                result = SwaigFunctionResult(
                    f"Profile saved for {first_name} {last_name}. Now ask where they'd like to fly."
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
                    f"Invalid departure date '{departure_str}'. "
                    "Must be in YYYY-MM-DD format. Ask the caller again."
                )
                _sync_summary(result, state)
                _change_step(result, "booking_departure")
                return result
            if departure_dt < date.today():
                result = SwaigFunctionResult(
                    f"The departure date {departure_str} is in the past. "
                    "Ask the caller for a future departure date."
                )
                _sync_summary(result, state)
                _change_step(result, "booking_departure")
                return result
            state["departure_date"] = departure_str

            # Validate return date for round trips
            if trip_type == "round_trip":
                return_str = fields.get("return_date", "")
                try:
                    return_dt = date.fromisoformat(return_str)
                except (ValueError, TypeError):
                    result = SwaigFunctionResult(
                        f"Invalid return date '{return_str}'. "
                        "Must be in YYYY-MM-DD format. Ask the caller again."
                    )
                    _sync_summary(result, state)
                    _change_step(result, "booking_return")
                    return result
                if return_dt < date.today():
                    result = SwaigFunctionResult(
                        f"The return date {return_str} is in the past. "
                        "Ask the caller for a future return date."
                    )
                    _sync_summary(result, state)
                    _change_step(result, "booking_return")
                    return result
                if return_dt <= departure_dt:
                    result = SwaigFunctionResult(
                        f"The return date {return_str} must be after the departure date {departure_str}. "
                        "Ask the caller for the correct return date."
                    )
                    _sync_summary(result, state)
                    _change_step(result, "booking_return")
                    return result
                state["return_date"] = return_str

            try:
                adults = int(fields.get("adults", "1"))
            except (ValueError, TypeError):
                adults = 1
            if adults > 8:
                result = SwaigFunctionResult(
                    f"We can only book up to 8 passengers at a time. "
                    f"The caller requested {adults}. Let them know they'll need to "
                    "contact a travel agent for parties larger than 8."
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
                result = SwaigFunctionResult(
                    "No origin airport set. Need to resolve the departure city first."
                )
                _change_step(result, "get_origin")
                return result

            if not destination:
                candidates = state.get("destination_candidates")
                if candidates:
                    result = SwaigFunctionResult(
                        "Destination airport not selected yet. The caller needs to pick from the candidates."
                    )
                    _change_step(result, "disambiguate_destination")
                else:
                    result = SwaigFunctionResult(
                        "No destination airport set. Need to resolve the destination city first."
                    )
                    _change_step(result, "get_destination")
                return result

            if not departure_date:
                result = SwaigFunctionResult(
                    "No travel dates set. Need to collect dates from the caller first."
                )
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
                    f"No flights found from {origin_iata} to {dest_iata} on {departure_date}. "
                    "Ask the caller if they'd like to try different dates or a nearby airport."
                )
                _change_step(result, "error_recovery")
                return result

            cabin_note = ""
            if actual_cabin != cabin:
                cabin_note = (
                    f"Note: {cabin.lower().replace('_', ' ')} was not available on this route, "
                    f"showing {actual_cabin.lower().replace('_', ' ')} results instead. "
                    "Let the caller know. "
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
                f"{cabin_note}"
                f"I found {count} option{'s' if count > 1 else ''}. {summary_text}. "
                "Read ALL options to the caller, then ask which one they prefer. "
                "When they choose, call select_flight with the option number (1, 2, or 3)."
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
                result = SwaigFunctionResult(
                    "No flight options available. Need to search for flights first."
                )
                _change_step(result,"search_flights")
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
            if reason == "different_route":
                result = SwaigFunctionResult(
                    "Let the caller know we'll start over with a new route."
                )
                _change_step(result, "get_origin")
            else:
                result = SwaigFunctionResult(
                    "Let the caller know we'll collect new travel dates."
                )
                _change_step(result, "collect_trip_type")
            return result

        # 5c. RESTART BOOKING (caller wants different dates from error_recovery)
        @self.tool(
            name="restart_booking",
            description="Restart the booking with new dates. Call from error recovery.",
            wait_file="/sounds/typing.mp3",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def restart_booking(args, raw_data):
            result = SwaigFunctionResult(
                "Let the caller know we'll collect new travel dates."
            )
            _change_step(result, "collect_trip_type")
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
                result = SwaigFunctionResult(
                    "No flight search results on file. Need to search for flights first."
                )
                _change_step(result,"search_flights")
                return result

            # Price the stored offer (mock always succeeds)
            logger.info(f"get_flight_price: pricing offer id={offer.get('id')}")
            pd = _price_offer(offer)
            po = (pd or {}).get("flightOffers", [])

            if not po:
                result = SwaigFunctionResult(
                    "Could not confirm the price. "
                    "Ask the caller if they'd like to search again or try different dates."
                )
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
                f"The confirmed price is ${total} {currency} per person including taxes. "
                f"{baggage_info}"
                "Tell the caller the price and ask: 'Shall I go ahead and book this?' "
                "If they say yes, call confirm_booking. If they say no, call decline_booking."
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
            result = SwaigFunctionResult("Proceeding to book the flight.")
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
            result = SwaigFunctionResult(
                "No problem. Let the caller know we'll go back to the flight options."
            )
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
                return SwaigFunctionResult(
                    f"Missing passenger details: {' and '.join(missing)}. "
                    "Cannot book without a complete profile."
                )

            call_id = _call_id(raw_data)
            state = load_call_state(call_id)
            priced_offer = state.get("priced_offer")

            logger.info(f"book_flight: state check — "
                        f"origin={state.get('origin')}, "
                        f"destination={state.get('destination')}, "
                        f"priced_offer={'YES' if priced_offer else 'NO'}")

            # Guard: no origin
            if not state.get("origin"):
                result = SwaigFunctionResult(
                    "No origin airport set. Need to resolve the departure city first."
                )
                _change_step(result,"get_origin")
                return result

            # Guard: no destination
            if not state.get("destination"):
                candidates = state.get("destination_candidates")
                if candidates:
                    result = SwaigFunctionResult(
                        "Destination airport not selected yet. The caller needs to pick from the candidates."
                    )
                    _change_step(result,"disambiguate_destination")
                else:
                    result = SwaigFunctionResult(
                        "No destination airport set. Need to collect the destination first."
                    )
                    _change_step(result,"get_destination")
                return result

            # Guard: no confirmed price → back to pricing
            if not priced_offer:
                result = SwaigFunctionResult(
                    "No confirmed price on file. Need to confirm the fare first."
                )
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
                    "The booking failed — this flight isn't available right now. "
                    "All passenger details are still on file — do NOT re-ask for name, email, or phone. "
                    "Ask the caller if they'd like to try a different flight or different dates."
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

            result = SwaigFunctionResult(
                f"Booked! Confirmation code is {pnr} — that's {phonetic}. "
                f"Flight {route_name}, departing {dep_date}, ${total}. "
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
