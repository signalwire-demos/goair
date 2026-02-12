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

# Pre-built global_data with completed profile answers
PROFILE_ANSWERS='{"global_data":{"is_new_caller":true,"caller_phone":"+15551234567","skill:profile":{"answers":[{"key_name":"first_name","answer":"Test"},{"key_name":"last_name","answer":"User"},{"key_name":"date_of_birth","answer":"1990-01-01"},{"key_name":"gender","answer":"MALE"},{"key_name":"email","answer":"test@example.com"},{"key_name":"seat_preference","answer":"AISLE"},{"key_name":"cabin_preference","answer":"ECONOMY"},{"key_name":"home_airport_name","answer":"Tulsa International (TUL)"}]}}}'

run --raw --call-id "${CALL_ID}-fp" --custom-data "$PROFILE_ANSWERS" --exec finalize_profile
check "Profile saved" "Profile saved"
check "  → offers home airport" "Tulsa"
check "  → sets is_new_caller false" '"is_new_caller": false'
check "  → change_step: get_origin" "get_origin"

# Missing name → error
PROFILE_NO_NAME='{"global_data":{"is_new_caller":true,"caller_phone":"+15551234567","skill:profile":{"answers":[{"key_name":"first_name","answer":""},{"key_name":"last_name","answer":""}]}}}'
run --raw --call-id "${CALL_ID}-fp-noname" --custom-data "$PROFILE_NO_NAME" --exec finalize_profile
check "Missing name → error" "Missing name"

# IATA extraction from bare code (no parentheses)
PROFILE_BARE_IATA='{"global_data":{"is_new_caller":true,"caller_phone":"+15551234567","skill:profile":{"answers":[{"key_name":"first_name","answer":"Test"},{"key_name":"last_name","answer":"User"},{"key_name":"home_airport_name","answer":"TUL"}]}}}'
run --raw --call-id "${CALL_ID}-fp-bare" --custom-data "$PROFILE_BARE_IATA" --exec finalize_profile
check "Bare IATA code extracted" "TUL"

# =============================================================================
section "3a. profile info_gatherer flow"
# =============================================================================

# Initialize GD with profile questions initial state
GD=$(jq -c . <<'EOGD'
{
  "is_new_caller": true,
  "caller_phone": "+15551234567",
  "skill:profile": {
    "questions": [
      {"key_name": "first_name", "question_text": "What is your first name?"},
      {"key_name": "last_name", "question_text": "What is your last name?"},
      {"key_name": "date_of_birth", "question_text": "What is your date of birth including month day and year?", "confirm": true, "prompt_add": "Accept natural language but submit in YYYY-MM-DD format. Must have complete date."},
      {"key_name": "gender", "question_text": "Are you male or female?", "prompt_add": "Submit exactly MALE or FEMALE."},
      {"key_name": "email", "question_text": "What email should we send confirmations to?", "confirm": true},
      {"key_name": "seat_preference", "question_text": "Do you prefer a window or aisle seat?", "prompt_add": "Submit exactly WINDOW or AISLE."},
      {"key_name": "cabin_preference", "question_text": "What cabin class do you usually fly?", "prompt_add": "Submit exactly ECONOMY, PREMIUM_ECONOMY, BUSINESS, or FIRST."},
      {"key_name": "home_airport_name", "question_text": "What airport do you usually fly from?", "confirm": true, "prompt_add": "After the caller answers, call resolve_location with their answer to resolve it to an IATA code. Submit the answer as 'Airport Name (IATA)' format, e.g. 'San Francisco International (SFO)'. If multiple airports are returned, ask which one they mean before submitting."}
    ],
    "question_index": 0,
    "answers": []
  }
}
EOGD
)

# start_questions → first question
run_and_merge --raw --call-id "${CALL_ID}-igp" --exec profile_start_questions
check "start_questions → asks first name" "first name"

# first_name
run_and_merge --raw --call-id "${CALL_ID}-igp" --exec profile_submit_answer --answer "Test"
check "first_name → advances to last name" "last name"

# last_name
run_and_merge --raw --call-id "${CALL_ID}-igp" --exec profile_submit_answer --answer "User"
check "last_name → advances to date of birth" "date of birth"

# date_of_birth WITHOUT confirmation → rejected
run --raw --call-id "${CALL_ID}-igp" --custom-data "$(cd_gd)" --exec profile_submit_answer --answer "1990-01-01"
check "date_of_birth unconfirmed → rejected" "confirm"

# date_of_birth WITH confirmation
run_and_merge --raw --call-id "${CALL_ID}-igp" --exec profile_submit_answer --answer "1990-01-01" --confirmed true
check "date_of_birth confirmed → advances" "male\|female\|gender"

# gender
run_and_merge --raw --call-id "${CALL_ID}-igp" --exec profile_submit_answer --answer "MALE"
check "gender → advances to email" "email"

# email WITH confirmation
run_and_merge --raw --call-id "${CALL_ID}-igp" --exec profile_submit_answer --answer "test@example.com" --confirmed true
check "email confirmed → advances to seat" "seat\|window\|aisle"

# seat_preference
run_and_merge --raw --call-id "${CALL_ID}-igp" --exec profile_submit_answer --answer "AISLE"
check "seat_preference → advances to cabin" "cabin"

# cabin_preference
run_and_merge --raw --call-id "${CALL_ID}-igp" --exec profile_submit_answer --answer "ECONOMY"
check "cabin_preference → advances to airport" "airport\|fly from"

# home_airport_name WITH confirmation → last question → completion
run_and_merge --raw --call-id "${CALL_ID}-igp" --exec profile_submit_answer --answer "Tulsa International (TUL)" --confirmed true
check "home_airport → completion message" "finalize_profile"

# finalize with accumulated answers
run --raw --call-id "${CALL_ID}-igp" --custom-data "$(cd_gd)" --exec finalize_profile
check "finalize_profile → Profile saved" "Profile saved"

# =============================================================================
section "4. select_trip_type + finalize_booking"
# =============================================================================

# select_trip_type — one-way
run --raw --call-id "$CALL_ID" --exec select_trip_type --trip_type one_way
check "Trip type (one-way) saved" "one.way\|One.way"
check "  → change_step: collect_booking_oneway" "collect_booking_oneway"

# select_trip_type — round-trip
run --raw --call-id "${CALL_ID}-rt2" --exec select_trip_type --trip_type round_trip
check "Trip type (round-trip) saved" "round.trip\|Round.trip"
check "  → change_step: collect_booking_roundtrip" "collect_booking_roundtrip"

# finalize_booking — one-way with pre-populated answers
ONEWAY_ANSWERS='{"global_data":{"skill:oneway":{"answers":[{"key_name":"departure_date","answer":"'"$DEP_DATE"'"},{"key_name":"adults","answer":"1"},{"key_name":"cabin_class","answer":"ECONOMY"}]}}}'
run --raw --call-id "$CALL_ID" --custom-data "$ONEWAY_ANSWERS" --exec finalize_booking
check "Booking details saved" "Booking details saved\|searching"
check "  → change_step: search_flights" "search_flights"

# finalize_booking — round-trip
ROUNDTRIP_ANSWERS='{"global_data":{"skill:roundtrip":{"answers":[{"key_name":"departure_date","answer":"'"$DEP_DATE"'"},{"key_name":"return_date","answer":"'"$RET_DATE"'"},{"key_name":"adults","answer":"2"},{"key_name":"cabin_class","answer":"BUSINESS"}]}}}'
run --raw --call-id "${CALL_ID}-rt2" --custom-data "$ROUNDTRIP_ANSWERS" --exec finalize_booking
check "Round-trip booking saved" "Booking details saved\|searching"

# finalize_booking — past departure date rejected
PAST_DEP='{"global_data":{"skill:oneway":{"answers":[{"key_name":"departure_date","answer":"'"$PAST_DATE"'"},{"key_name":"adults","answer":"1"},{"key_name":"cabin_class","answer":"ECONOMY"}]}}}'
run --raw --call-id "${CALL_ID}-pastdep" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-pastdep" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-pastdep" --exec select_trip_type --trip_type one_way
run --raw --call-id "${CALL_ID}-pastdep" --custom-data "$PAST_DEP" --exec finalize_booking
check "Past departure date → rejected" "in the past"

# finalize_booking — past return date rejected
PAST_RET='{"global_data":{"skill:roundtrip":{"answers":[{"key_name":"departure_date","answer":"'"$DEP_DATE"'"},{"key_name":"return_date","answer":"'"$PAST_DATE"'"},{"key_name":"adults","answer":"1"},{"key_name":"cabin_class","answer":"ECONOMY"}]}}}'
run --raw --call-id "${CALL_ID}-pastret" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-pastret" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-pastret" --exec select_trip_type --trip_type round_trip
run --raw --call-id "${CALL_ID}-pastret" --custom-data "$PAST_RET" --exec finalize_booking
check "Past return date → rejected" "in the past"

# finalize_booking — return before departure rejected
BAD_ORDER='{"global_data":{"skill:roundtrip":{"answers":[{"key_name":"departure_date","answer":"'"$RET_DATE"'"},{"key_name":"return_date","answer":"'"$DEP_DATE"'"},{"key_name":"adults","answer":"1"},{"key_name":"cabin_class","answer":"ECONOMY"}]}}}'
run --raw --call-id "${CALL_ID}-badord" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-badord" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-badord" --exec select_trip_type --trip_type round_trip
run --raw --call-id "${CALL_ID}-badord" --custom-data "$BAD_ORDER" --exec finalize_booking
check "Return before departure → rejected" "must be after"

# finalize_booking — >8 adults rejected
OVER8_ANSWERS='{"global_data":{"skill:oneway":{"answers":[{"key_name":"departure_date","answer":"'"$DEP_DATE"'"},{"key_name":"adults","answer":"10"},{"key_name":"cabin_class","answer":"ECONOMY"}]}}}'
run --raw --call-id "${CALL_ID}-over8" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-over8" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-over8" --exec select_trip_type --trip_type one_way
run --raw --call-id "${CALL_ID}-over8" --custom-data "$OVER8_ANSWERS" --exec finalize_booking
check ">8 passengers rejected" "travel agent\|8 passengers"

# finalize_booking — non-numeric adults defaults to 1
BAD_ADULTS='{"global_data":{"skill:oneway":{"answers":[{"key_name":"departure_date","answer":"'"$DEP_DATE"'"},{"key_name":"adults","answer":"two"},{"key_name":"cabin_class","answer":"ECONOMY"}]}}}'
run --raw --call-id "${CALL_ID}-badnum" --exec resolve_location --location_text Tulsa --location_type origin
run --raw --call-id "${CALL_ID}-badnum" --exec resolve_location --location_text Atlanta --location_type destination
run --raw --call-id "${CALL_ID}-badnum" --exec select_trip_type --trip_type one_way
run --raw --call-id "${CALL_ID}-badnum" --custom-data "$BAD_ADULTS" --exec finalize_booking
check "Non-numeric adults → no crash" "Booking details saved\|searching"

# =============================================================================
section "4a. oneway booking info_gatherer flow"
# =============================================================================

# Set up origin/destination/trip_type via DB-persisted calls
run --raw --call-id "${CALL_ID}-igb" --exec resolve_location --location_text "Tulsa" --location_type origin
run --raw --call-id "${CALL_ID}-igb" --exec resolve_location --location_text "Atlanta" --location_type destination
run --raw --call-id "${CALL_ID}-igb" --exec select_trip_type --trip_type one_way

# Initialize GD with oneway questions
GD=$(jq -c . <<'EOGD'
{
  "skill:oneway": {
    "questions": [
      {"key_name": "departure_date", "question_text": "When would you like to depart?", "confirm": true, "prompt_add": "Accept natural language but submit in YYYY-MM-DD format."},
      {"key_name": "adults", "question_text": "How many passengers will be traveling?", "prompt_add": "Submit as a positive integer (e.g. 1, 2, 3). Maximum 8 — for larger parties, tell the caller they'll need to contact a travel agent."},
      {"key_name": "cabin_class", "question_text": "What cabin class would you like — economy, premium economy, business, or first?", "prompt_add": "Submit exactly ECONOMY, PREMIUM_ECONOMY, BUSINESS, or FIRST. If the passenger has a stored cabin preference in their profile, suggest it."}
    ],
    "question_index": 0,
    "answers": []
  }
}
EOGD
)

# start_questions → first question
run_and_merge --raw --call-id "${CALL_ID}-igb" --exec oneway_start_questions
check "start_questions → asks departure" "depart"

# departure_date WITH confirmation
run_and_merge --raw --call-id "${CALL_ID}-igb" --exec oneway_submit_answer --answer "$DEP_DATE" --confirmed true
check "departure_date → advances to passengers" "passenger\|how many"

# adults
run_and_merge --raw --call-id "${CALL_ID}-igb" --exec oneway_submit_answer --answer "1"
check "adults → advances to cabin" "cabin"

# cabin_class → last question → completion
run_and_merge --raw --call-id "${CALL_ID}-igb" --exec oneway_submit_answer --answer "ECONOMY"
check "cabin_class → completion message" "finalize_booking"

# finalize with accumulated answers
run --raw --call-id "${CALL_ID}-igb" --custom-data "$(cd_gd)" --exec finalize_booking
check "finalize_booking → Booking details saved" "Booking details saved\|searching"

# =============================================================================
section "4b. roundtrip booking info_gatherer flow"
# =============================================================================

# Set up origin/destination/trip_type via DB-persisted calls
run --raw --call-id "${CALL_ID}-igr" --exec resolve_location --location_text "Tulsa" --location_type origin
run --raw --call-id "${CALL_ID}-igr" --exec resolve_location --location_text "Atlanta" --location_type destination
run --raw --call-id "${CALL_ID}-igr" --exec select_trip_type --trip_type round_trip

# Initialize GD with roundtrip questions
GD=$(jq -c . <<'EOGD'
{
  "skill:roundtrip": {
    "questions": [
      {"key_name": "departure_date", "question_text": "When would you like to depart?", "confirm": true, "prompt_add": "Accept natural language but submit in YYYY-MM-DD format."},
      {"key_name": "return_date", "question_text": "And when would you like to return?", "confirm": true, "prompt_add": "Accept natural language but submit in YYYY-MM-DD format. Must be after departure date."},
      {"key_name": "adults", "question_text": "How many passengers will be traveling?", "prompt_add": "Submit as a positive integer (e.g. 1, 2, 3). Maximum 8 — for larger parties, tell the caller they'll need to contact a travel agent."},
      {"key_name": "cabin_class", "question_text": "What cabin class would you like — economy, premium economy, business, or first?", "prompt_add": "Submit exactly ECONOMY, PREMIUM_ECONOMY, BUSINESS, or FIRST. If the passenger has a stored cabin preference in their profile, suggest it."}
    ],
    "question_index": 0,
    "answers": []
  }
}
EOGD
)

# start_questions → first question
run_and_merge --raw --call-id "${CALL_ID}-igr" --exec roundtrip_start_questions
check "start_questions → asks departure" "depart"

# departure_date WITH confirmation
run_and_merge --raw --call-id "${CALL_ID}-igr" --exec roundtrip_submit_answer --answer "$DEP_DATE" --confirmed true
check "departure_date → advances to return" "return"

# return_date WITH confirmation
run_and_merge --raw --call-id "${CALL_ID}-igr" --exec roundtrip_submit_answer --answer "$RET_DATE" --confirmed true
check "return_date → advances to passengers" "passenger\|how many"

# adults
run_and_merge --raw --call-id "${CALL_ID}-igr" --exec roundtrip_submit_answer --answer "2"
check "adults → advances to cabin" "cabin"

# cabin_class → last question → completion
run_and_merge --raw --call-id "${CALL_ID}-igr" --exec roundtrip_submit_answer --answer "BUSINESS"
check "cabin_class → completion message" "finalize_booking"

# finalize with accumulated answers
run --raw --call-id "${CALL_ID}-igr" --custom-data "$(cd_gd)" --exec finalize_booking
check "finalize_booking → Booking details saved" "Booking details saved\|searching"

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
