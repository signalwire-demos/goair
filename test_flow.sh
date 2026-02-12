#!/usr/bin/env bash
# =============================================================================
# Voyager SWAIG Flow Test Harness
#
# Tests all functions and the full booking flow using swaig-test with
# persistent call state (--call-id).
#
# Usage:  ./test_flow.sh [--debug]
# =============================================================================

SWAIG="./venv/bin/swaig-test"
AGENT="voyager.py"
PASS=0
FAIL=0
CALL_ID="test-$(date +%s)"
DEBUG=false

# Parse flags
for arg in "$@"; do
    case "$arg" in
        --debug|-d) DEBUG=true ;;
    esac
done

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
DIM='\033[2m'
NC='\033[0m'

# ── helpers ──────────────────────────────────────────────────────────────────

check() {
    local label="$1"
    local pattern="$2"
    # Compensate for multibyte chars (→ is 3 bytes but 1 column)
    local byte_len char_len pad
    byte_len=$(printf '%s' "$label" | wc -c)
    char_len=${#label}
    pad=$((57 + byte_len - char_len))
    printf "${CYAN}  %-${pad}s${NC}" "$label"
    if echo "$OUTPUT" | grep -qi "$pattern"; then
        printf "${GREEN}PASS${NC}\n"
        ((PASS++))
    else
        printf "${RED}FAIL${NC} — expected '%s'\n" "$pattern"
        echo "      Got: $(echo "$OUTPUT" | head -2)"
        ((FAIL++))
    fi
}

run() {
    # Print the command being run in debug mode
    if $DEBUG; then
        printf "\n${DIM}  ▸ swaig-test %s${NC}\n" "$*"
    fi
    # Run swaig-test, store output. Args passed directly.
    OUTPUT=$("$SWAIG" "$AGENT" "$@" 2>/dev/null) || true
    # Print full response in debug mode
    if $DEBUG; then
        echo "$OUTPUT" | while IFS= read -r line; do
            printf "${DIM}    %s${NC}\n" "$line"
        done
    fi
}

section() {
    printf "\n${YELLOW}━━ %s ━━${NC}\n" "$1"
}

# ── Global data payloads ─────────────────────────────────────────────────────

NEW_CALLER='{"global_data":{"passenger_profile":null,"is_new_caller":true,"caller_phone":"+15551234567"}}'

KNOWN_CALLER='{"global_data":{"passenger_profile":{"phone":"+15551234567","first_name":"Test","last_name":"User","date_of_birth":"1990-01-01","gender":"MALE","email":"test@example.com","seat_preference":"AISLE","cabin_preference":"ECONOMY","home_airport_iata":null,"home_airport_name":"Tulsa"},"is_new_caller":false,"caller_phone":"+15551234567"}}'

# =============================================================================
printf "\n${YELLOW}╔══════════════════════════════════════════════════════════╗${NC}\n"
printf "${YELLOW}║           Voyager SWAIG Flow Test Harness                ║${NC}\n"
printf "${YELLOW}╚══════════════════════════════════════════════════════════╝${NC}\n"
printf "  Call ID: ${CYAN}%s${NC}\n" "$CALL_ID"

# =============================================================================
section "1. resolve_location"
# =============================================================================

run --raw --call-id "${CALL_ID}-empty" --exec resolve_location --location_text "" --location_type origin
check "Empty text → error" "No location provided"

run --raw --call-id "$CALL_ID" --exec resolve_location --location_text "Tulsa" --location_type origin
check "Resolve origin (Tulsa) → TUL" "TUL"
check "  → change_step: get_destination" "get_destination"
check "  → saves origin" "origin"

run --raw --call-id "$CALL_ID" --exec resolve_location --location_text "Atlanta" --location_type destination
check "Resolve destination (Atlanta) → ATL" "ATL"
check "  → change_step: collect_trip_type" "collect_trip_type"
check "  → saves destination" "destination"

run --raw --call-id "${CALL_ID}-disambig" --exec resolve_location --location_text "New York" --location_type origin
check "Ambiguous city → multiple airports" "Found multiple\|multiple airports"
check "  → change_step: disambiguate_origin" "disambiguate_origin"

# =============================================================================
section "2. select_airport"
# =============================================================================

run --raw --call-id "${CALL_ID}-no-cand" --exec select_airport --location_type origin --iata_code JFK
check "No candidates → error" "candidates\|resolve_location"

run --raw --call-id "${CALL_ID}-disambig" --exec select_airport --location_type origin --iata_code JFK
check "Select JFK from candidates" "JFK.*selected\|selected.*JFK"
check "  → change_step: get_destination" "get_destination"

# =============================================================================
section "3. save_profile_field (sequential profile steps)"
# =============================================================================

run --raw --call-id "${CALL_ID}-prof" --custom-data "$NEW_CALLER" \
    --exec save_profile_field \
    --field_name first_name --value Test
check "First name saved → profile_last_name" "profile_last_name"

run --raw --call-id "${CALL_ID}-prof" --custom-data "$NEW_CALLER" \
    --exec save_profile_field \
    --field_name last_name --value User
check "Last name saved → profile_dob" "profile_dob"

run --raw --call-id "${CALL_ID}-prof" --custom-data "$NEW_CALLER" \
    --exec save_profile_field \
    --field_name date_of_birth --value 1990-01-01
check "DOB saved → profile_gender" "profile_gender"

run --raw --call-id "${CALL_ID}-prof" --custom-data "$NEW_CALLER" \
    --exec save_profile_field \
    --field_name gender --value MALE
check "Gender saved → profile_email" "profile_email"

run --raw --call-id "${CALL_ID}-prof" --custom-data "$NEW_CALLER" \
    --exec save_profile_field \
    --field_name email --value test@example.com
check "Email saved → profile_seat" "profile_seat"

run --raw --call-id "${CALL_ID}-prof" --custom-data "$NEW_CALLER" \
    --exec save_profile_field \
    --field_name seat_preference --value AISLE
check "Seat saved → profile_cabin" "profile_cabin"

run --raw --call-id "${CALL_ID}-prof" --custom-data "$NEW_CALLER" \
    --exec save_profile_field \
    --field_name cabin_preference --value ECONOMY
check "Cabin saved → profile_airport" "profile_airport"

run --raw --call-id "${CALL_ID}-prof" --custom-data "$NEW_CALLER" \
    --exec save_profile_field \
    --field_name home_airport_name --value Tulsa
check "Profile complete → saved" "Profile saved"
check "  → is_new_caller: false" '"is_new_caller": false'
check "  → disables save_profile_field" '"save_profile_field".*false\|"active": false'
check "  → enables resolve_location" '"resolve_location".*true\|"active": true'
check "  → change_step: get_origin" "get_origin"

# Validation: invalid enum
run --raw --call-id "${CALL_ID}-prof-bad" --custom-data "$NEW_CALLER" \
    --exec save_profile_field \
    --field_name gender --value UNKNOWN
check "Invalid gender → error" "Invalid\|invalid"

# =============================================================================
section "4. save_booking_field (sequential booking steps)"
# =============================================================================

# Note: sleep 2 between sequential saves on same call-id to satisfy cooldown guard.
# In production, real caller interaction (2+ seconds) satisfies this naturally.

# Trip type (one-way) — step transitions like profile fields
run --raw --call-id "$CALL_ID" --exec save_booking_field --field_name trip_type --value one_way
check "Trip type (one-way) saved" "trip_type saved"
check "  → change_step: collect_departure" "collect_departure"

# Departure date (one-way path → collect_passengers, skips return)
sleep 2
run --raw --call-id "$CALL_ID" --exec save_booking_field --field_name departure_date --value 2026-10-01
check "Departure date saved" "departure_date saved"
check "  → change_step: collect_passengers" "collect_passengers"

# Passengers
sleep 2
run --raw --call-id "$CALL_ID" --exec save_booking_field --field_name adults --value 1
check "Adults saved" "adults saved"
check "  → change_step: collect_cabin" "collect_cabin"

# Cabin class → search_flights
sleep 2
run --raw --call-id "$CALL_ID" --exec save_booking_field --field_name cabin_class --value ECONOMY
check "Cabin saved → search_flights" "cabin_class saved"
check "  → change_step: search_flights" "search_flights"

# Round-trip path (needs origin+destination first)
run --raw --call-id "${CALL_ID}-rt" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-rt" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-rt" --exec save_booking_field --field_name trip_type --value round_trip
check "Trip type (round-trip) saved" "trip_type saved"
check "  → change_step: collect_departure" "collect_departure"

sleep 2
run --raw --call-id "${CALL_ID}-rt" --exec save_booking_field --field_name departure_date --value 2026-10-01
check "Departure (round-trip) saved" "departure_date saved"
check "  → change_step: collect_return" "collect_return"

sleep 2
run --raw --call-id "${CALL_ID}-rt" --exec save_booking_field --field_name return_date --value 2026-10-08
check "Return date saved" "return_date saved"
check "  → change_step: collect_passengers" "collect_passengers"

# Gate guard: no origin/destination → rejected
run --raw --call-id "${CALL_ID}-noroute" --exec save_booking_field --field_name trip_type --value one_way
check "No origin/dest → gate error" "Cannot save booking\|origin\|destination\|resolve_location"

# Cooldown guard: rapid-fire second save is rejected
run --raw --call-id "${CALL_ID}-cd" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-cd" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-cd" --exec save_booking_field --field_name trip_type --value one_way
run --raw --call-id "${CALL_ID}-cd" --exec save_booking_field --field_name departure_date --value 2026-10-01
check "Cooldown rejects rapid save" "Too fast"

# Validation: invalid trip_type
run --raw --call-id "${CALL_ID}-v1" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-v1" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-v1" --exec save_booking_field --field_name trip_type --value maybe
check "Invalid trip_type → error" "Invalid trip_type\|invalid"

# Validation: bad date format (set trip_type first, wait for cooldown)
run --raw --call-id "${CALL_ID}-v2" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-v2" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-v2" --exec save_booking_field --field_name trip_type --value one_way
sleep 2
run --raw --call-id "${CALL_ID}-v2" --exec save_booking_field --field_name departure_date --value "next friday"
check "Bad date format → error" "Invalid date\|YYYY-MM-DD"

# Validation: non-positive adults (set prerequisites, wait between saves)
run --raw --call-id "${CALL_ID}-v3" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-v3" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-v3" --exec save_booking_field --field_name trip_type --value one_way
sleep 2
run --raw --call-id "${CALL_ID}-v3" --exec save_booking_field --field_name departure_date --value 2026-10-01
sleep 2
run --raw --call-id "${CALL_ID}-v3" --exec save_booking_field --field_name adults --value 0
check "Non-positive adults → error" "Invalid adults\|positive"

run --raw --call-id "${CALL_ID}-v4" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-v4" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-v4" --exec save_booking_field --field_name trip_type --value one_way
sleep 2
run --raw --call-id "${CALL_ID}-v4" --exec save_booking_field --field_name departure_date --value 2026-10-01
sleep 2
run --raw --call-id "${CALL_ID}-v4" --exec save_booking_field --field_name adults --value abc
check "Non-numeric adults → error" "Invalid adults\|positive"

# Validation: invalid cabin_class (set prerequisites, wait between saves)
run --raw --call-id "${CALL_ID}-v5" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-v5" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-v5" --exec save_booking_field --field_name trip_type --value one_way
sleep 2
run --raw --call-id "${CALL_ID}-v5" --exec save_booking_field --field_name departure_date --value 2026-10-01
sleep 2
run --raw --call-id "${CALL_ID}-v5" --exec save_booking_field --field_name adults --value 1
sleep 2
run --raw --call-id "${CALL_ID}-v5" --exec save_booking_field --field_name cabin_class --value LUXURY
check "Invalid cabin_class → error" "Invalid cabin_class\|invalid"

# Validation: prerequisite guard (skip departure_date, try return_date directly)
run --raw --call-id "${CALL_ID}-v6" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-v6" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-v6" --exec save_booking_field --field_name trip_type --value round_trip
sleep 2
run --raw --call-id "${CALL_ID}-v6" --exec save_booking_field --field_name return_date --value 2026-10-08
check "Skip departure → prerequisite error" "Cannot save\|departure_date first"

# =============================================================================
section "5. search_flights — guard checks"
# =============================================================================

run --raw --call-id "${CALL_ID}-sf-empty" --exec search_flights
check "No origin → get_origin" "No origin"

# Set up origin only
run --raw --call-id "${CALL_ID}-sf-nodest" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-sf-nodest" --exec search_flights
check "No destination → get_destination" "destination"

# Set up origin + destination, no dates
run --raw --call-id "${CALL_ID}-sf-nodate" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-sf-nodate" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-sf-nodate" --exec search_flights
check "No dates → collect_trip_type" "dates\|collect_trip_type"

# =============================================================================
section "6. search_flights — actual search"
# =============================================================================

# Main CALL_ID has: TUL→ATL, 2026-10-01, 1 adult, ECONOMY
run --raw --call-id "$CALL_ID" --exec search_flights
check "Search TUL→ATL returns results" "option\|Option\|found"
check "  → change_step: present_options" "present_options"

# =============================================================================
section "7. select_flight"
# =============================================================================

run --raw --call-id "${CALL_ID}-sf-none" --exec select_flight --option_number 1
check "No offers → search_flights" "No flight options\|search"

run --raw --call-id "$CALL_ID" --exec select_flight --option_number 1
check "Select option 1" "Option 1 selected\|selected"
check "  → change_step: confirm_price" "confirm_price"

# =============================================================================
section "8. get_flight_price"
# =============================================================================

run --raw --call-id "${CALL_ID}-gfp-none" --exec get_flight_price
check "No offer → search_flights" "No flight\|search"

run --raw --call-id "$CALL_ID" --exec get_flight_price
check "Price confirmed" "confirmed price\|price is\|\\\$"
check "  → change_step: create_booking" "create_booking"

# =============================================================================
section "9. book_flight — guard checks"
# =============================================================================

run --raw --call-id "${CALL_ID}-bf-noprofile" --exec book_flight
check "No profile → missing details" "Missing passenger\|missing"

run --raw --call-id "${CALL_ID}-bf-empty" --custom-data "$KNOWN_CALLER" --exec book_flight
check "No origin → get_origin" "No origin\|origin"

# Origin only
run --raw --call-id "${CALL_ID}-bf-nodest" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-bf-nodest" --custom-data "$KNOWN_CALLER" --exec book_flight
check "No destination → get_destination" "destination"

# Origin + destination, no price
run --raw --call-id "${CALL_ID}-bf-noprice" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-bf-noprice" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-bf-noprice" --custom-data "$KNOWN_CALLER" --exec book_flight
check "No priced offer → confirm_price" "No confirmed price\|confirm"

# =============================================================================
section "10. book_flight — full booking"
# =============================================================================

run --raw --call-id "$CALL_ID" --custom-data "$KNOWN_CALLER" --exec book_flight
if echo "$OUTPUT" | grep -qi "Booked\|PNR\|confirmation"; then
    check "Booking succeeded with PNR" "Booked\|PNR\|confirmation"
    check "  → change_step: wrap_up" "wrap_up"
elif echo "$OUTPUT" | grep -qi "failed\|unavailable\|expired"; then
    check "Booking handled failure gracefully" "failed\|unavailable\|expired"
    check "  → change_step: error_recovery" "error_recovery"
else
    printf "${CYAN}  %-52s${NC}${RED}FAIL${NC} — unexpected response\n" "Booking result"
    echo "    Got: $(echo "$OUTPUT" | head -3)"
    ((FAIL++))
fi

# =============================================================================
# Summary
# =============================================================================
printf "\n${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
TOTAL=$((PASS + FAIL))
printf "  Total:  %d\n" "$TOTAL"
printf "  ${GREEN}Passed: %d${NC}\n" "$PASS"
printf "  ${RED}Failed: %d${NC}\n" "$FAIL"
if [ "$FAIL" -eq 0 ]; then
    printf "\n${GREEN}All tests passed!${NC}\n\n"
else
    printf "\n${RED}%d test(s) failed.${NC}\n\n" "$FAIL"
    exit 1
fi
