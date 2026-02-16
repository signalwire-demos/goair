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
DEP_DATE=$(date -v+30d +%Y-%m-%d)
RET_DATE=$(date -v+45d +%Y-%m-%d)
PAST_DATE=$(date -v-30d +%Y-%m-%d)
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

# Extract set_global_data JSON from OUTPUT (after Actions: header)
extract_global_data() {
    echo "$OUTPUT" | sed -n '/^Actions:$/,$ p' | sed '1d' | \
        jq -s -c '[.[] | select(has("set_global_data")) | .set_global_data] | add // empty' 2>/dev/null
}

# Wrap GD accumulator as --custom-data JSON
cd_gd() {
    echo "$GD" | jq -c '{global_data: .}'
}

# Run swaig-test with GD as custom-data, then merge any set_global_data into GD
run_and_merge() {
    run --custom-data "$(cd_gd)" "$@"
    local new_data
    new_data=$(extract_global_data)
    if [ -n "$new_data" ]; then
        GD=$(echo "$GD" | jq -c --argjson new "$new_data" '. + $new')
    fi
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
check "  → saved as origin" "origin"

run --raw --call-id "$CALL_ID" --exec resolve_location --location_text "Atlanta" --location_type destination
check "Resolve destination (Atlanta) → ATL" "ATL"
check "  → saved as destination" "destination"

run --raw --call-id "${CALL_ID}-disambig" --exec resolve_location --location_text "New York" --location_type origin
check "Ambiguous city → multiple airports" "Found multiple\|multiple airports"
check "  → response mentions disambiguate" "disambiguate_origin"

# =============================================================================
section "2. select_airport"
# =============================================================================

run --raw --call-id "${CALL_ID}-no-cand" --exec select_airport --location_type origin --iata_code JFK
check "No candidates → error" "candidates\|resolve_location"

run --raw --call-id "${CALL_ID}-disambig" --exec select_airport --location_type origin --iata_code JFK
check "Select JFK from candidates" "JFK.*selected\|selected.*JFK"
check "  → response mentions get_destination" "get_destination"

# =============================================================================
section "3. finalize_profile"
# =============================================================================

# Pre-built global_data with completed profile answers (flat dict format)
PROFILE_ANSWERS='{"global_data":{"is_new_caller":true,"caller_phone":"+15551234567","profile_answers":{"first_name":"Test","last_name":"User","date_of_birth":"1990-01-01","gender":"MALE","email":"test@example.com","seat_preference":"AISLE","cabin_preference":"ECONOMY","home_airport_name":"Tulsa International (TUL)"}}}'

run --raw --call-id "${CALL_ID}-fp" --custom-data "$PROFILE_ANSWERS" --exec finalize_profile
check "Profile saved" "Profile saved"
check "  → offers home airport" "Tulsa"
check "  → sets is_new_caller false" '"is_new_caller": false'
check "  → change_step: get_origin" "get_origin"

# Missing name → error
PROFILE_NO_NAME='{"global_data":{"is_new_caller":true,"caller_phone":"+15551234567","profile_answers":{"first_name":"","last_name":""}}}'
run --raw --call-id "${CALL_ID}-fp-noname" --custom-data "$PROFILE_NO_NAME" --exec finalize_profile
check "Missing name → error" "Missing name"

# IATA extraction from bare code (no parentheses)
PROFILE_BARE_IATA='{"global_data":{"is_new_caller":true,"caller_phone":"+15551234567","profile_answers":{"first_name":"Test","last_name":"User","home_airport_name":"TUL"}}}'
run --raw --call-id "${CALL_ID}-fp-bare" --custom-data "$PROFILE_BARE_IATA" --exec finalize_profile
check "Bare IATA code extracted" "TUL"

# =============================================================================
section "3a. profile question flow (native steps)"
# =============================================================================

# Each submit_* tool is tested independently with global_data accumulation
GD=$(jq -c . <<'EOGD'
{
  "is_new_caller": true,
  "caller_phone": "+15551234567"
}
EOGD
)

# first_name
run_and_merge --raw --call-id "${CALL_ID}-pq" --exec submit_first_name --value "Test"
check "submit_first_name → profile_last_name" "profile_last_name"

# last_name
run_and_merge --raw --call-id "${CALL_ID}-pq" --exec submit_last_name --value "User"
check "submit_last_name → profile_dob" "profile_dob"

# date_of_birth WITHOUT confirmation → rejected
run --raw --call-id "${CALL_ID}-pq" --custom-data "$(cd_gd)" --exec submit_dob --value "1990-01-01"
check "submit_dob unconfirmed → rejected" "confirm"

# date_of_birth WITH confirmation
run_and_merge --raw --call-id "${CALL_ID}-pq" --exec submit_dob --value "1990-01-01" --confirmed true
check "submit_dob confirmed → profile_gender" "profile_gender"

# gender
run_and_merge --raw --call-id "${CALL_ID}-pq" --exec submit_gender --value "MALE"
check "submit_gender → profile_email" "profile_email"

# email WITHOUT confirmation → rejected
run --raw --call-id "${CALL_ID}-pq" --custom-data "$(cd_gd)" --exec submit_email --value "test@example.com"
check "submit_email unconfirmed → rejected" "confirm"

# email WITH confirmation
run_and_merge --raw --call-id "${CALL_ID}-pq" --exec submit_email --value "test@example.com" --confirmed true
check "submit_email confirmed → profile_seat_pref" "profile_seat_pref"

# seat_preference
run_and_merge --raw --call-id "${CALL_ID}-pq" --exec submit_seat_pref --value "AISLE"
check "submit_seat_pref → profile_cabin_pref" "profile_cabin_pref"

# cabin_preference
run_and_merge --raw --call-id "${CALL_ID}-pq" --exec submit_cabin_pref --value "ECONOMY"
check "submit_cabin_pref → profile_home_airport" "profile_home_airport"

# home_airport WITHOUT confirmation → rejected
run --raw --call-id "${CALL_ID}-pq" --custom-data "$(cd_gd)" --exec submit_home_airport --value "Tulsa International (TUL)"
check "submit_home_airport unconfirmed → rejected" "confirm"

# home_airport WITH confirmation → creates passenger, transitions to get_origin
run_and_merge --raw --call-id "${CALL_ID}-pq" --exec submit_home_airport --value "Tulsa International (TUL)" --confirmed true
check "submit_home_airport → Profile saved" "Profile saved"
check "  → change_step: get_origin" "get_origin"
check "  → sets is_new_caller false" '"is_new_caller": false'

# =============================================================================
section "4. select_trip_type + finalize_booking"
# =============================================================================

# select_trip_type — one-way (without confirmed → asks to confirm)
run --raw --call-id "$CALL_ID" --exec select_trip_type --trip_type one_way
check "Trip type (one-way) no confirm → bounce" "confirm\|correct"

# select_trip_type — one-way (with confirmed)
run --raw --call-id "$CALL_ID" --exec select_trip_type --trip_type one_way --confirmed true
check "Trip type (one-way) saved" "one.way\|One.way"
check "  → change_step: booking_departure" "booking_departure"

# select_trip_type — round-trip (phase 1: bounce sets asked flag)
run --raw --call-id "${CALL_ID}-rt2" --exec select_trip_type --trip_type round_trip
check "Trip type (round-trip) phase 1 → bounce" "Ask the caller"

# select_trip_type — round-trip (phase 2: confirmed)
run --raw --call-id "${CALL_ID}-rt2" --exec select_trip_type --trip_type round_trip --confirmed true
check "Trip type (round-trip) saved" "round.trip\|Round.trip"
check "  → change_step: booking_departure" "booking_departure"

# finalize_booking — one-way with pre-populated answers
ONEWAY_ANSWERS='{"global_data":{"booking_answers":{"departure_date":"'"$DEP_DATE"'","adults":"1","cabin_class":"ECONOMY"}}}'
run --raw --call-id "$CALL_ID" --custom-data "$ONEWAY_ANSWERS" --exec finalize_booking
check "Booking details saved" "Booking details saved\|searching"
check "  → change_step: search_flights" "search_flights"

# finalize_booking — round-trip
ROUNDTRIP_ANSWERS='{"global_data":{"booking_answers":{"departure_date":"'"$DEP_DATE"'","return_date":"'"$RET_DATE"'","adults":"2","cabin_class":"BUSINESS"}}}'
run --raw --call-id "${CALL_ID}-rt2" --custom-data "$ROUNDTRIP_ANSWERS" --exec finalize_booking
check "Round-trip booking saved" "Booking details saved\|searching"

# finalize_booking — past departure date rejected
PAST_DEP='{"global_data":{"booking_answers":{"departure_date":"'"$PAST_DATE"'","adults":"1","cabin_class":"ECONOMY"}}}'
run --raw --call-id "${CALL_ID}-pastdep" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-pastdep" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-pastdep" --exec select_trip_type --trip_type one_way
run --raw --call-id "${CALL_ID}-pastdep" --exec select_trip_type --trip_type one_way --confirmed true
run --raw --call-id "${CALL_ID}-pastdep" --custom-data "$PAST_DEP" --exec finalize_booking
check "Past departure date → rejected" "in the past"

# finalize_booking — past return date rejected
PAST_RET='{"global_data":{"booking_answers":{"departure_date":"'"$DEP_DATE"'","return_date":"'"$PAST_DATE"'","adults":"1","cabin_class":"ECONOMY"}}}'
run --raw --call-id "${CALL_ID}-pastret" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-pastret" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-pastret" --exec select_trip_type --trip_type round_trip
run --raw --call-id "${CALL_ID}-pastret" --exec select_trip_type --trip_type round_trip --confirmed true
run --raw --call-id "${CALL_ID}-pastret" --custom-data "$PAST_RET" --exec finalize_booking
check "Past return date → rejected" "in the past"

# finalize_booking — return before departure rejected
BAD_ORDER='{"global_data":{"booking_answers":{"departure_date":"'"$RET_DATE"'","return_date":"'"$DEP_DATE"'","adults":"1","cabin_class":"ECONOMY"}}}'
run --raw --call-id "${CALL_ID}-badord" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-badord" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-badord" --exec select_trip_type --trip_type round_trip
run --raw --call-id "${CALL_ID}-badord" --exec select_trip_type --trip_type round_trip --confirmed true
run --raw --call-id "${CALL_ID}-badord" --custom-data "$BAD_ORDER" --exec finalize_booking
check "Return before departure → rejected" "must be after"

# finalize_booking — >8 adults rejected
OVER8_ANSWERS='{"global_data":{"booking_answers":{"departure_date":"'"$DEP_DATE"'","adults":"10","cabin_class":"ECONOMY"}}}'
run --raw --call-id "${CALL_ID}-over8" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-over8" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-over8" --exec select_trip_type --trip_type one_way
run --raw --call-id "${CALL_ID}-over8" --exec select_trip_type --trip_type one_way --confirmed true
run --raw --call-id "${CALL_ID}-over8" --custom-data "$OVER8_ANSWERS" --exec finalize_booking
check ">8 passengers rejected" "travel agent\|8 passengers"

# finalize_booking — non-numeric adults defaults to 1
BAD_ADULTS='{"global_data":{"booking_answers":{"departure_date":"'"$DEP_DATE"'","adults":"two","cabin_class":"ECONOMY"}}}'
run --raw --call-id "${CALL_ID}-badnum" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-badnum" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-badnum" --exec select_trip_type --trip_type one_way
run --raw --call-id "${CALL_ID}-badnum" --exec select_trip_type --trip_type one_way --confirmed true
run --raw --call-id "${CALL_ID}-badnum" --custom-data "$BAD_ADULTS" --exec finalize_booking
check "Non-numeric adults → no crash" "Booking details saved\|searching"

# =============================================================================
section "4a. oneway booking question flow (native steps)"
# =============================================================================

# Set up origin/destination/trip_type via DB-persisted calls
run --raw --call-id "${CALL_ID}-bq" --exec resolve_location --location_text "Tulsa" --location_type origin
run --raw --call-id "${CALL_ID}-bq" --exec resolve_location --location_text "Atlanta" --location_type destination
run --raw --call-id "${CALL_ID}-bq" --exec select_trip_type --trip_type one_way
run --raw --call-id "${CALL_ID}-bq" --exec select_trip_type --trip_type one_way --confirmed true

# Initialize GD (empty booking_answers)
GD='{"booking_answers":{}}'

# departure_date WITHOUT confirmation → rejected
run --raw --call-id "${CALL_ID}-bq" --custom-data "$(cd_gd)" --exec submit_departure --value "$DEP_DATE"
check "submit_departure unconfirmed → rejected" "confirm"

# departure_date WITH confirmation → booking_adults (one-way skips return)
run_and_merge --raw --call-id "${CALL_ID}-bq" --exec submit_departure --value "$DEP_DATE" --confirmed true
check "submit_departure → booking_adults" "booking_adults"

# adults
run_and_merge --raw --call-id "${CALL_ID}-bq" --exec submit_adults --value "1"
check "submit_adults → booking_cabin" "booking_cabin"

# cabin_class → runs search inline, transitions to present_options
run_and_merge --raw --call-id "${CALL_ID}-bq" --exec submit_cabin --value "ECONOMY"
check "submit_cabin → search ran inline" "option\|Option\|found"
check "  → change_step: present_options" "present_options"

# =============================================================================
section "4b. roundtrip booking question flow (native steps)"
# =============================================================================

# Set up origin/destination/trip_type via DB-persisted calls
run --raw --call-id "${CALL_ID}-br" --exec resolve_location --location_text "Tulsa" --location_type origin
run --raw --call-id "${CALL_ID}-br" --exec resolve_location --location_text "Atlanta" --location_type destination
run --raw --call-id "${CALL_ID}-br" --exec select_trip_type --trip_type round_trip
run --raw --call-id "${CALL_ID}-br" --exec select_trip_type --trip_type round_trip --confirmed true

# Initialize GD (empty booking_answers)
GD='{"booking_answers":{}}'

# departure_date phase 1 (bounce sets asked flag)
run --raw --call-id "${CALL_ID}-br" --custom-data "$(cd_gd)" --exec submit_departure --value "$DEP_DATE"

# departure_date phase 2 WITH confirmation → booking_return (round-trip)
run_and_merge --raw --call-id "${CALL_ID}-br" --exec submit_departure --value "$DEP_DATE" --confirmed true
check "submit_departure → booking_return" "booking_return"

# return_date phase 1 (bounce sets asked flag)
run --raw --call-id "${CALL_ID}-br" --custom-data "$(cd_gd)" --exec submit_return --value "$RET_DATE"

# return_date phase 2 WITH confirmation
run_and_merge --raw --call-id "${CALL_ID}-br" --exec submit_return --value "$RET_DATE" --confirmed true
check "submit_return → booking_adults" "booking_adults"

# adults
run_and_merge --raw --call-id "${CALL_ID}-br" --exec submit_adults --value "2"
check "submit_adults → booking_cabin" "booking_cabin"

# cabin_class → runs search inline, transitions to present_options
run_and_merge --raw --call-id "${CALL_ID}-br" --exec submit_cabin --value "BUSINESS"
check "submit_cabin → search ran inline" "option\|Option\|found"
check "  → change_step: present_options" "present_options"

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

# Main CALL_ID has: TUL→ATL, $DEP_DATE, 1 adult, ECONOMY
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
check "  → asks caller to confirm" "book this\|Shall I"

# =============================================================================
section "8a. forced transition tools"
# =============================================================================

# restart_search — different dates
run --raw --call-id "${CALL_ID}-rs1" --exec restart_search --reason different_dates
check "restart_search (dates) → collect_trip_type" "collect_trip_type"

# restart_search — different route
run --raw --call-id "${CALL_ID}-rs2" --exec restart_search --reason different_route
check "restart_search (route) → get_origin" "get_origin"

# confirm_booking → create_booking
run --raw --call-id "${CALL_ID}-cb" --exec confirm_booking
check "confirm_booking → create_booking" "create_booking"

# decline_booking → present_options
run --raw --call-id "${CALL_ID}-db" --exec decline_booking
check "decline_booking → present_options" "present_options"

# restart_booking → collect_trip_type
run --raw --call-id "${CALL_ID}-rb" --exec restart_booking
check "restart_booking → collect_trip_type" "collect_trip_type"

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
