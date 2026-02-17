"""Mock Flight API — drop-in replacement for Amadeus SDK helpers.

Generates realistic flight data on the fly so development and testing
never hit rate limits, pricing errors, or carrier restrictions.

Response shapes match the Amadeus Self-Service JSON API (v2 flight-offers,
v1 pricing, v1 flight-orders).  When MOCK_DELAYS=true, each call sleeps
1-9 s to simulate real-world Amadeus/GDS latency.
"""

import math
import random
import string
import time
import zoneinfo
from datetime import datetime, timedelta
from uuid import uuid4

import config

def _maybe_delay(lo=1, hi=5):
    """Sleep for a random interval when MOCK_DELAYS is enabled."""
    if config.MOCK_DELAYS:
        time.sleep(random.uniform(lo, hi))

# ── Airport database ──────────────────────────────────────────────────

AIRPORTS = {
    # ── US — Top 50 + notable secondary ──────────────────────────────
    "ATL": {"iata": "ATL", "name": "Hartsfield-Jackson Atlanta International", "city": "Atlanta", "lat": 33.6407, "lng": -84.4277, "tz": "America/New_York"},
    "LAX": {"iata": "LAX", "name": "Los Angeles International", "city": "Los Angeles", "lat": 33.9425, "lng": -118.4081, "tz": "America/Los_Angeles"},
    "ORD": {"iata": "ORD", "name": "O'Hare International", "city": "Chicago", "lat": 41.9742, "lng": -87.9073, "tz": "America/Chicago"},
    "DFW": {"iata": "DFW", "name": "Dallas/Fort Worth International", "city": "Dallas", "lat": 32.8998, "lng": -97.0403, "tz": "America/Chicago"},
    "DEN": {"iata": "DEN", "name": "Denver International", "city": "Denver", "lat": 39.8561, "lng": -104.6737, "tz": "America/Denver"},
    "JFK": {"iata": "JFK", "name": "John F Kennedy International", "city": "New York", "lat": 40.6413, "lng": -73.7781, "tz": "America/New_York"},
    "SFO": {"iata": "SFO", "name": "San Francisco International", "city": "San Francisco", "lat": 37.6213, "lng": -122.3790, "tz": "America/Los_Angeles"},
    "SEA": {"iata": "SEA", "name": "Seattle-Tacoma International", "city": "Seattle", "lat": 47.4502, "lng": -122.3088, "tz": "America/Los_Angeles"},
    "LAS": {"iata": "LAS", "name": "Harry Reid International", "city": "Las Vegas", "lat": 36.0840, "lng": -115.1537, "tz": "America/Los_Angeles"},
    "MIA": {"iata": "MIA", "name": "Miami International", "city": "Miami", "lat": 25.7959, "lng": -80.2870, "tz": "America/New_York"},
    "MCO": {"iata": "MCO", "name": "Orlando International", "city": "Orlando", "lat": 28.4312, "lng": -81.3081, "tz": "America/New_York"},
    "EWR": {"iata": "EWR", "name": "Newark Liberty International", "city": "Newark", "lat": 40.6895, "lng": -74.1745, "tz": "America/New_York"},
    "CLT": {"iata": "CLT", "name": "Charlotte Douglas International", "city": "Charlotte", "lat": 35.2140, "lng": -80.9431, "tz": "America/New_York"},
    "PHX": {"iata": "PHX", "name": "Phoenix Sky Harbor International", "city": "Phoenix", "lat": 33.4373, "lng": -112.0078, "tz": "America/Phoenix"},
    "IAH": {"iata": "IAH", "name": "George Bush Intercontinental", "city": "Houston", "lat": 29.9902, "lng": -95.3368, "tz": "America/Chicago"},
    "BOS": {"iata": "BOS", "name": "Boston Logan International", "city": "Boston", "lat": 42.3656, "lng": -71.0096, "tz": "America/New_York"},
    "MSP": {"iata": "MSP", "name": "Minneapolis-Saint Paul International", "city": "Minneapolis", "lat": 44.8848, "lng": -93.2223, "tz": "America/Chicago"},
    "FLL": {"iata": "FLL", "name": "Fort Lauderdale-Hollywood International", "city": "Fort Lauderdale", "lat": 26.0726, "lng": -80.1527, "tz": "America/New_York"},
    "DTW": {"iata": "DTW", "name": "Detroit Metropolitan", "city": "Detroit", "lat": 42.2124, "lng": -83.3534, "tz": "America/Detroit"},
    "PHL": {"iata": "PHL", "name": "Philadelphia International", "city": "Philadelphia", "lat": 39.8744, "lng": -75.2424, "tz": "America/New_York"},
    "LGA": {"iata": "LGA", "name": "LaGuardia", "city": "New York", "lat": 40.7772, "lng": -73.8726, "tz": "America/New_York"},
    "BWI": {"iata": "BWI", "name": "Baltimore/Washington International", "city": "Baltimore", "lat": 39.1754, "lng": -76.6684, "tz": "America/New_York"},
    "SLC": {"iata": "SLC", "name": "Salt Lake City International", "city": "Salt Lake City", "lat": 40.7899, "lng": -111.9791, "tz": "America/Denver"},
    "IAD": {"iata": "IAD", "name": "Washington Dulles International", "city": "Washington", "lat": 38.9531, "lng": -77.4565, "tz": "America/New_York"},
    "DCA": {"iata": "DCA", "name": "Ronald Reagan Washington National", "city": "Washington", "lat": 38.8512, "lng": -77.0402, "tz": "America/New_York"},
    "SAN": {"iata": "SAN", "name": "San Diego International", "city": "San Diego", "lat": 32.7338, "lng": -117.1933, "tz": "America/Los_Angeles"},
    "TPA": {"iata": "TPA", "name": "Tampa International", "city": "Tampa", "lat": 27.9755, "lng": -82.5332, "tz": "America/New_York"},
    "AUS": {"iata": "AUS", "name": "Austin-Bergstrom International", "city": "Austin", "lat": 30.1975, "lng": -97.6664, "tz": "America/Chicago"},
    "BNA": {"iata": "BNA", "name": "Nashville International", "city": "Nashville", "lat": 36.1263, "lng": -86.6774, "tz": "America/Chicago"},
    "PDX": {"iata": "PDX", "name": "Portland International", "city": "Portland", "lat": 45.5898, "lng": -122.5951, "tz": "America/Los_Angeles"},
    "HNL": {"iata": "HNL", "name": "Daniel K. Inouye International", "city": "Honolulu", "lat": 21.3187, "lng": -157.9224, "tz": "Pacific/Honolulu"},
    "MDW": {"iata": "MDW", "name": "Midway International", "city": "Chicago", "lat": 41.7868, "lng": -87.7522, "tz": "America/Chicago"},
    "DAL": {"iata": "DAL", "name": "Dallas Love Field", "city": "Dallas", "lat": 32.8471, "lng": -96.8518, "tz": "America/Chicago"},
    "HOU": {"iata": "HOU", "name": "William P Hobby", "city": "Houston", "lat": 29.6454, "lng": -95.2789, "tz": "America/Chicago"},
    "STL": {"iata": "STL", "name": "St. Louis Lambert International", "city": "St. Louis", "lat": 38.7487, "lng": -90.3700, "tz": "America/Chicago"},
    "RDU": {"iata": "RDU", "name": "Raleigh-Durham International", "city": "Raleigh", "lat": 35.8776, "lng": -78.7875, "tz": "America/New_York"},
    "SJC": {"iata": "SJC", "name": "San Jose International", "city": "San Jose", "lat": 37.3626, "lng": -121.9290, "tz": "America/Los_Angeles"},
    "MSY": {"iata": "MSY", "name": "Louis Armstrong New Orleans International", "city": "New Orleans", "lat": 29.9934, "lng": -90.2580, "tz": "America/Chicago"},
    "SMF": {"iata": "SMF", "name": "Sacramento International", "city": "Sacramento", "lat": 38.6954, "lng": -121.5908, "tz": "America/Los_Angeles"},
    "SNA": {"iata": "SNA", "name": "John Wayne Orange County", "city": "Santa Ana", "lat": 33.6757, "lng": -117.8682, "tz": "America/Los_Angeles"},
    "RSW": {"iata": "RSW", "name": "Southwest Florida International", "city": "Fort Myers", "lat": 26.5362, "lng": -81.7552, "tz": "America/New_York"},
    "SAT": {"iata": "SAT", "name": "San Antonio International", "city": "San Antonio", "lat": 29.5337, "lng": -98.4698, "tz": "America/Chicago"},
    "PIT": {"iata": "PIT", "name": "Pittsburgh International", "city": "Pittsburgh", "lat": 40.4957, "lng": -80.2413, "tz": "America/New_York"},
    "IND": {"iata": "IND", "name": "Indianapolis International", "city": "Indianapolis", "lat": 39.7173, "lng": -86.2944, "tz": "America/Indiana/Indianapolis"},
    "CLE": {"iata": "CLE", "name": "Cleveland Hopkins International", "city": "Cleveland", "lat": 41.4117, "lng": -81.8498, "tz": "America/New_York"},
    "CMH": {"iata": "CMH", "name": "John Glenn Columbus International", "city": "Columbus", "lat": 39.9980, "lng": -82.8919, "tz": "America/New_York"},
    "JAX": {"iata": "JAX", "name": "Jacksonville International", "city": "Jacksonville", "lat": 30.4941, "lng": -81.6879, "tz": "America/New_York"},
    "MCI": {"iata": "MCI", "name": "Kansas City International", "city": "Kansas City", "lat": 39.2976, "lng": -94.7139, "tz": "America/Chicago"},
    "OAK": {"iata": "OAK", "name": "Oakland International", "city": "Oakland", "lat": 37.7213, "lng": -122.2208, "tz": "America/Los_Angeles"},
    "BUR": {"iata": "BUR", "name": "Hollywood Burbank", "city": "Burbank", "lat": 34.2005, "lng": -118.3586, "tz": "America/Los_Angeles"},
    "CVG": {"iata": "CVG", "name": "Cincinnati/Northern Kentucky International", "city": "Cincinnati", "lat": 39.0488, "lng": -84.6678, "tz": "America/New_York"},
    "MKE": {"iata": "MKE", "name": "Milwaukee Mitchell International", "city": "Milwaukee", "lat": 42.9472, "lng": -87.8966, "tz": "America/Chicago"},
    "OKC": {"iata": "OKC", "name": "Will Rogers World", "city": "Oklahoma City", "lat": 35.3931, "lng": -97.6007, "tz": "America/Chicago"},
    "TUL": {"iata": "TUL", "name": "Tulsa International", "city": "Tulsa", "lat": 36.1984, "lng": -95.8881, "tz": "America/Chicago"},
    "ABQ": {"iata": "ABQ", "name": "Albuquerque International Sunport", "city": "Albuquerque", "lat": 35.0402, "lng": -106.6090, "tz": "America/Denver"},
    "OMA": {"iata": "OMA", "name": "Eppley Airfield", "city": "Omaha", "lat": 41.3032, "lng": -95.8941, "tz": "America/Chicago"},
    "ANC": {"iata": "ANC", "name": "Ted Stevens Anchorage International", "city": "Anchorage", "lat": 61.1743, "lng": -149.9962, "tz": "America/Anchorage"},
    "RNO": {"iata": "RNO", "name": "Reno-Tahoe International", "city": "Reno", "lat": 39.4991, "lng": -119.7681, "tz": "America/Los_Angeles"},
    # US — secondary / leisure / regional
    "MEM": {"iata": "MEM", "name": "Memphis International", "city": "Memphis", "lat": 35.0424, "lng": -89.9767, "tz": "America/Chicago"},
    "PBI": {"iata": "PBI", "name": "Palm Beach International", "city": "West Palm Beach", "lat": 26.6832, "lng": -80.0956, "tz": "America/New_York"},
    "BDL": {"iata": "BDL", "name": "Bradley International", "city": "Hartford", "lat": 41.9389, "lng": -72.6832, "tz": "America/New_York"},
    "BUF": {"iata": "BUF", "name": "Buffalo Niagara International", "city": "Buffalo", "lat": 42.9405, "lng": -78.7322, "tz": "America/New_York"},
    "ORF": {"iata": "ORF", "name": "Norfolk International", "city": "Norfolk", "lat": 36.8946, "lng": -76.2012, "tz": "America/New_York"},
    "RIC": {"iata": "RIC", "name": "Richmond International", "city": "Richmond", "lat": 37.5052, "lng": -77.3197, "tz": "America/New_York"},
    "CHS": {"iata": "CHS", "name": "Charleston International", "city": "Charleston", "lat": 32.8986, "lng": -80.0405, "tz": "America/New_York"},
    "SAV": {"iata": "SAV", "name": "Savannah/Hilton Head International", "city": "Savannah", "lat": 32.1276, "lng": -81.2021, "tz": "America/New_York"},
    "DSM": {"iata": "DSM", "name": "Des Moines International", "city": "Des Moines", "lat": 41.5341, "lng": -93.6631, "tz": "America/Chicago"},
    "ICT": {"iata": "ICT", "name": "Wichita Dwight D Eisenhower National", "city": "Wichita", "lat": 37.6499, "lng": -97.4331, "tz": "America/Chicago"},
    "LIT": {"iata": "LIT", "name": "Bill and Hillary Clinton National", "city": "Little Rock", "lat": 34.7294, "lng": -92.2243, "tz": "America/Chicago"},
    "TUS": {"iata": "TUS", "name": "Tucson International", "city": "Tucson", "lat": 32.1161, "lng": -110.9410, "tz": "America/Phoenix"},
    "ELP": {"iata": "ELP", "name": "El Paso International", "city": "El Paso", "lat": 31.8072, "lng": -106.3778, "tz": "America/Denver"},
    "BOI": {"iata": "BOI", "name": "Boise Airport", "city": "Boise", "lat": 43.5644, "lng": -116.2228, "tz": "America/Boise"},
    "SDF": {"iata": "SDF", "name": "Louisville Muhammad Ali International", "city": "Louisville", "lat": 38.1744, "lng": -85.7360, "tz": "America/Kentucky/Louisville"},
    "OGG": {"iata": "OGG", "name": "Kahului", "city": "Maui", "lat": 20.8986, "lng": -156.4305, "tz": "Pacific/Honolulu"},
    "SYR": {"iata": "SYR", "name": "Syracuse Hancock International", "city": "Syracuse", "lat": 43.1112, "lng": -76.1063, "tz": "America/New_York"},
    "ROC": {"iata": "ROC", "name": "Frederick Douglass Greater Rochester International", "city": "Rochester", "lat": 43.1189, "lng": -77.6724, "tz": "America/New_York"},
    "GRR": {"iata": "GRR", "name": "Gerald R Ford International", "city": "Grand Rapids", "lat": 42.8808, "lng": -85.5228, "tz": "America/Detroit"},
    "GSP": {"iata": "GSP", "name": "Greenville-Spartanburg International", "city": "Greenville", "lat": 34.8957, "lng": -82.2189, "tz": "America/New_York"},
    "HSV": {"iata": "HSV", "name": "Huntsville International", "city": "Huntsville", "lat": 34.6372, "lng": -86.7751, "tz": "America/Chicago"},
    "SRQ": {"iata": "SRQ", "name": "Sarasota Bradenton International", "city": "Sarasota", "lat": 27.3954, "lng": -82.5544, "tz": "America/New_York"},
    "PNS": {"iata": "PNS", "name": "Pensacola International", "city": "Pensacola", "lat": 30.4734, "lng": -87.1866, "tz": "America/Chicago"},
    "XNA": {"iata": "XNA", "name": "Northwest Arkansas National", "city": "Fayetteville", "lat": 36.2819, "lng": -94.3068, "tz": "America/Chicago"},
    "ONT": {"iata": "ONT", "name": "Ontario International", "city": "Ontario", "lat": 34.0560, "lng": -117.6012, "tz": "America/Los_Angeles"},
    "PSP": {"iata": "PSP", "name": "Palm Springs International", "city": "Palm Springs", "lat": 33.8297, "lng": -116.5067, "tz": "America/Los_Angeles"},
    "OGG": {"iata": "OGG", "name": "Kahului", "city": "Maui", "lat": 20.8986, "lng": -156.4305, "tz": "Pacific/Honolulu"},
    "KOA": {"iata": "KOA", "name": "Ellison Onizuka Kona International", "city": "Kona", "lat": 19.7388, "lng": -156.0456, "tz": "Pacific/Honolulu"},
    "LIH": {"iata": "LIH", "name": "Lihue", "city": "Kauai", "lat": 21.9760, "lng": -159.3390, "tz": "Pacific/Honolulu"},

    # ── European hubs ────────────────────────────────────────────────
    "LHR": {"iata": "LHR", "name": "Heathrow", "city": "London", "lat": 51.4700, "lng": -0.4543, "tz": "Europe/London"},
    "LGW": {"iata": "LGW", "name": "Gatwick", "city": "London", "lat": 51.1537, "lng": -0.1821, "tz": "Europe/London"},
    "CDG": {"iata": "CDG", "name": "Charles de Gaulle", "city": "Paris", "lat": 49.0097, "lng": 2.5479, "tz": "Europe/Paris"},
    "ORY": {"iata": "ORY", "name": "Orly", "city": "Paris", "lat": 48.7233, "lng": 2.3794, "tz": "Europe/Paris"},
    "FRA": {"iata": "FRA", "name": "Frankfurt", "city": "Frankfurt", "lat": 50.0379, "lng": 8.5622, "tz": "Europe/Berlin"},
    "MUC": {"iata": "MUC", "name": "Munich", "city": "Munich", "lat": 48.3537, "lng": 11.7750, "tz": "Europe/Berlin"},
    "AMS": {"iata": "AMS", "name": "Schiphol", "city": "Amsterdam", "lat": 52.3105, "lng": 4.7683, "tz": "Europe/Amsterdam"},
    "MAD": {"iata": "MAD", "name": "Adolfo Suarez Madrid-Barajas", "city": "Madrid", "lat": 40.4983, "lng": -3.5676, "tz": "Europe/Madrid"},
    "BCN": {"iata": "BCN", "name": "Barcelona-El Prat", "city": "Barcelona", "lat": 41.2971, "lng": 2.0785, "tz": "Europe/Madrid"},
    "FCO": {"iata": "FCO", "name": "Leonardo da Vinci-Fiumicino", "city": "Rome", "lat": 41.8003, "lng": 12.2389, "tz": "Europe/Rome"},
    "ZRH": {"iata": "ZRH", "name": "Zurich", "city": "Zurich", "lat": 47.4647, "lng": 8.5492, "tz": "Europe/Zurich"},
    "IST": {"iata": "IST", "name": "Istanbul", "city": "Istanbul", "lat": 41.2753, "lng": 28.7519, "tz": "Europe/Istanbul"},
    "DUB": {"iata": "DUB", "name": "Dublin", "city": "Dublin", "lat": 53.4264, "lng": -6.2499, "tz": "Europe/Dublin"},
    "CPH": {"iata": "CPH", "name": "Copenhagen", "city": "Copenhagen", "lat": 55.6180, "lng": 12.6561, "tz": "Europe/Copenhagen"},
    "OSL": {"iata": "OSL", "name": "Oslo Gardermoen", "city": "Oslo", "lat": 60.1976, "lng": 11.1004, "tz": "Europe/Oslo"},
    "ARN": {"iata": "ARN", "name": "Stockholm Arlanda", "city": "Stockholm", "lat": 59.6519, "lng": 17.9186, "tz": "Europe/Stockholm"},
    "HEL": {"iata": "HEL", "name": "Helsinki-Vantaa", "city": "Helsinki", "lat": 60.3172, "lng": 24.9633, "tz": "Europe/Helsinki"},
    "LIS": {"iata": "LIS", "name": "Lisbon Humberto Delgado", "city": "Lisbon", "lat": 38.7756, "lng": -9.1354, "tz": "Europe/Lisbon"},
    "VIE": {"iata": "VIE", "name": "Vienna International", "city": "Vienna", "lat": 48.1103, "lng": 16.5697, "tz": "Europe/Vienna"},
    "BRU": {"iata": "BRU", "name": "Brussels", "city": "Brussels", "lat": 50.9014, "lng": 4.4844, "tz": "Europe/Brussels"},
    "WAW": {"iata": "WAW", "name": "Warsaw Chopin", "city": "Warsaw", "lat": 52.1657, "lng": 20.9671, "tz": "Europe/Warsaw"},
    "PRG": {"iata": "PRG", "name": "Vaclav Havel Prague", "city": "Prague", "lat": 50.1008, "lng": 14.2600, "tz": "Europe/Prague"},
    "BUD": {"iata": "BUD", "name": "Budapest Ferenc Liszt", "city": "Budapest", "lat": 47.4369, "lng": 19.2556, "tz": "Europe/Budapest"},
    "ATH": {"iata": "ATH", "name": "Athens Eleftherios Venizelos", "city": "Athens", "lat": 37.9364, "lng": 23.9445, "tz": "Europe/Athens"},
    "EDI": {"iata": "EDI", "name": "Edinburgh", "city": "Edinburgh", "lat": 55.9500, "lng": -3.3725, "tz": "Europe/London"},
    "MAN": {"iata": "MAN", "name": "Manchester", "city": "Manchester", "lat": 53.3537, "lng": -2.2750, "tz": "Europe/London"},
    "MXP": {"iata": "MXP", "name": "Milan Malpensa", "city": "Milan", "lat": 45.6306, "lng": 8.7281, "tz": "Europe/Rome"},
    "GVA": {"iata": "GVA", "name": "Geneva", "city": "Geneva", "lat": 46.2381, "lng": 6.1090, "tz": "Europe/Zurich"},
    "KEF": {"iata": "KEF", "name": "Keflavik International", "city": "Reykjavik", "lat": 63.9850, "lng": -22.6056, "tz": "Atlantic/Reykjavik"},

    # ── Asian hubs ───────────────────────────────────────────────────
    "NRT": {"iata": "NRT", "name": "Narita International", "city": "Tokyo", "lat": 35.7647, "lng": 140.3864, "tz": "Asia/Tokyo"},
    "HND": {"iata": "HND", "name": "Haneda", "city": "Tokyo", "lat": 35.5494, "lng": 139.7798, "tz": "Asia/Tokyo"},
    "ICN": {"iata": "ICN", "name": "Incheon International", "city": "Seoul", "lat": 37.4602, "lng": 126.4407, "tz": "Asia/Seoul"},
    "PEK": {"iata": "PEK", "name": "Beijing Capital International", "city": "Beijing", "lat": 40.0799, "lng": 116.6031, "tz": "Asia/Shanghai"},
    "PVG": {"iata": "PVG", "name": "Shanghai Pudong International", "city": "Shanghai", "lat": 31.1443, "lng": 121.8083, "tz": "Asia/Shanghai"},
    "HKG": {"iata": "HKG", "name": "Hong Kong International", "city": "Hong Kong", "lat": 22.3080, "lng": 113.9185, "tz": "Asia/Hong_Kong"},
    "SIN": {"iata": "SIN", "name": "Singapore Changi", "city": "Singapore", "lat": 1.3644, "lng": 103.9915, "tz": "Asia/Singapore"},
    "BKK": {"iata": "BKK", "name": "Suvarnabhumi", "city": "Bangkok", "lat": 13.6900, "lng": 100.7501, "tz": "Asia/Bangkok"},
    "DEL": {"iata": "DEL", "name": "Indira Gandhi International", "city": "Delhi", "lat": 28.5562, "lng": 77.1000, "tz": "Asia/Kolkata"},
    "BOM": {"iata": "BOM", "name": "Chhatrapati Shivaji Maharaj International", "city": "Mumbai", "lat": 19.0896, "lng": 72.8656, "tz": "Asia/Kolkata"},
    "DXB": {"iata": "DXB", "name": "Dubai International", "city": "Dubai", "lat": 25.2532, "lng": 55.3657, "tz": "Asia/Dubai"},
    "DOH": {"iata": "DOH", "name": "Hamad International", "city": "Doha", "lat": 25.2731, "lng": 51.6081, "tz": "Asia/Qatar"},
    "MNL": {"iata": "MNL", "name": "Ninoy Aquino International", "city": "Manila", "lat": 14.5086, "lng": 121.0198, "tz": "Asia/Manila"},
    "KUL": {"iata": "KUL", "name": "Kuala Lumpur International", "city": "Kuala Lumpur", "lat": 2.7456, "lng": 101.7099, "tz": "Asia/Kuala_Lumpur"},
    "TPE": {"iata": "TPE", "name": "Taiwan Taoyuan International", "city": "Taipei", "lat": 25.0797, "lng": 121.2342, "tz": "Asia/Taipei"},
    "AUH": {"iata": "AUH", "name": "Abu Dhabi International", "city": "Abu Dhabi", "lat": 24.4331, "lng": 54.6511, "tz": "Asia/Dubai"},
    "TLV": {"iata": "TLV", "name": "Ben Gurion International", "city": "Tel Aviv", "lat": 32.0114, "lng": 34.8867, "tz": "Asia/Jerusalem"},

    # ── Canada ────────────────────────────────────────────────────────
    "YYZ": {"iata": "YYZ", "name": "Toronto Pearson International", "city": "Toronto", "lat": 43.6777, "lng": -79.6248, "tz": "America/Toronto"},
    "YVR": {"iata": "YVR", "name": "Vancouver International", "city": "Vancouver", "lat": 49.1967, "lng": -123.1815, "tz": "America/Vancouver"},
    "YUL": {"iata": "YUL", "name": "Montreal-Trudeau International", "city": "Montreal", "lat": 45.4706, "lng": -73.7408, "tz": "America/Toronto"},
    "YYC": {"iata": "YYC", "name": "Calgary International", "city": "Calgary", "lat": 51.1215, "lng": -114.0076, "tz": "America/Edmonton"},

    # ── Mexico ───────────────────────────────────────────────────────
    "MEX": {"iata": "MEX", "name": "Mexico City International", "city": "Mexico City", "lat": 19.4363, "lng": -99.0721, "tz": "America/Mexico_City"},
    "CUN": {"iata": "CUN", "name": "Cancun International", "city": "Cancun", "lat": 21.0365, "lng": -86.8771, "tz": "America/Cancun"},
    "GDL": {"iata": "GDL", "name": "Guadalajara International", "city": "Guadalajara", "lat": 20.5218, "lng": -103.3113, "tz": "America/Mexico_City"},
    "MTY": {"iata": "MTY", "name": "Monterrey International", "city": "Monterrey", "lat": 25.7785, "lng": -100.1069, "tz": "America/Monterrey"},
    "SJD": {"iata": "SJD", "name": "Los Cabos International", "city": "San Jose del Cabo", "lat": 23.1518, "lng": -109.7215, "tz": "America/Mazatlan"},
    "PVR": {"iata": "PVR", "name": "Gustavo Diaz Ordaz International", "city": "Puerto Vallarta", "lat": 20.6801, "lng": -105.2544, "tz": "America/Mexico_City"},

    # ── Central America & Caribbean ──────────────────────────────────
    "SJO": {"iata": "SJO", "name": "Juan Santamaria International", "city": "San Jose", "lat": 9.9939, "lng": -84.2088, "tz": "America/Costa_Rica"},
    "PTY": {"iata": "PTY", "name": "Tocumen International", "city": "Panama City", "lat": 9.0714, "lng": -79.3835, "tz": "America/Panama"},
    "GUA": {"iata": "GUA", "name": "La Aurora International", "city": "Guatemala City", "lat": 14.5833, "lng": -90.5275, "tz": "America/Guatemala"},
    "SAL": {"iata": "SAL", "name": "Oscar Arnulfo Romero International", "city": "San Salvador", "lat": 13.4409, "lng": -89.0557, "tz": "America/El_Salvador"},
    "BZE": {"iata": "BZE", "name": "Philip S W Goldson International", "city": "Belize City", "lat": 17.5391, "lng": -88.3082, "tz": "America/Belize"},
    "SJU": {"iata": "SJU", "name": "Luis Munoz Marin International", "city": "San Juan", "lat": 18.4394, "lng": -66.0018, "tz": "America/Puerto_Rico"},
    "MBJ": {"iata": "MBJ", "name": "Sangster International", "city": "Montego Bay", "lat": 18.5037, "lng": -77.9134, "tz": "America/Jamaica"},
    "KIN": {"iata": "KIN", "name": "Norman Manley International", "city": "Kingston", "lat": 17.9357, "lng": -76.7875, "tz": "America/Jamaica"},
    "NAS": {"iata": "NAS", "name": "Lynden Pindling International", "city": "Nassau", "lat": 25.0390, "lng": -77.4662, "tz": "America/Nassau"},
    "PUJ": {"iata": "PUJ", "name": "Punta Cana International", "city": "Punta Cana", "lat": 18.5674, "lng": -68.3634, "tz": "America/Santo_Domingo"},
    "SDQ": {"iata": "SDQ", "name": "Las Americas International", "city": "Santo Domingo", "lat": 18.4297, "lng": -69.6689, "tz": "America/Santo_Domingo"},
    "HAV": {"iata": "HAV", "name": "Jose Marti International", "city": "Havana", "lat": 22.9892, "lng": -82.4091, "tz": "America/Havana"},
    "AUA": {"iata": "AUA", "name": "Queen Beatrix International", "city": "Aruba", "lat": 12.5014, "lng": -70.0152, "tz": "America/Aruba"},
    "SXM": {"iata": "SXM", "name": "Princess Juliana International", "city": "St Maarten", "lat": 18.0410, "lng": -63.1089, "tz": "America/Lower_Princes"},
    "STT": {"iata": "STT", "name": "Cyril E King", "city": "St Thomas", "lat": 18.3373, "lng": -64.9734, "tz": "America/Virgin"},
    "GCM": {"iata": "GCM", "name": "Owen Roberts International", "city": "Grand Cayman", "lat": 19.2928, "lng": -81.3577, "tz": "America/Cayman"},
    "POS": {"iata": "POS", "name": "Piarco International", "city": "Port of Spain", "lat": 10.5954, "lng": -61.3372, "tz": "America/Port_of_Spain"},

    # ── South America ────────────────────────────────────────────────
    "GRU": {"iata": "GRU", "name": "Sao Paulo-Guarulhos International", "city": "Sao Paulo", "lat": -23.4356, "lng": -46.4731, "tz": "America/Sao_Paulo"},
    "GIG": {"iata": "GIG", "name": "Rio de Janeiro-Galeao International", "city": "Rio de Janeiro", "lat": -22.8100, "lng": -43.2506, "tz": "America/Sao_Paulo"},
    "EZE": {"iata": "EZE", "name": "Ministro Pistarini International", "city": "Buenos Aires", "lat": -34.8222, "lng": -58.5358, "tz": "America/Argentina/Buenos_Aires"},
    "BOG": {"iata": "BOG", "name": "El Dorado International", "city": "Bogota", "lat": 4.7016, "lng": -74.1469, "tz": "America/Bogota"},
    "CTG": {"iata": "CTG", "name": "Rafael Nunez International", "city": "Cartagena", "lat": 10.4424, "lng": -75.5130, "tz": "America/Bogota"},
    "MDE": {"iata": "MDE", "name": "Jose Maria Cordova International", "city": "Medellin", "lat": 6.1645, "lng": -75.4231, "tz": "America/Bogota"},
    "LIM": {"iata": "LIM", "name": "Jorge Chavez International", "city": "Lima", "lat": -12.0219, "lng": -77.1143, "tz": "America/Lima"},
    "CUZ": {"iata": "CUZ", "name": "Alejandro Velasco Astete International", "city": "Cusco", "lat": -13.5357, "lng": -71.9388, "tz": "America/Lima"},
    "SCL": {"iata": "SCL", "name": "Arturo Merino Benitez International", "city": "Santiago", "lat": -33.3930, "lng": -70.7858, "tz": "America/Santiago"},
    "UIO": {"iata": "UIO", "name": "Mariscal Sucre International", "city": "Quito", "lat": -0.1292, "lng": -78.3575, "tz": "America/Guayaquil"},
    "GYE": {"iata": "GYE", "name": "Jose Joaquin de Olmedo International", "city": "Guayaquil", "lat": -2.1574, "lng": -79.8837, "tz": "America/Guayaquil"},
    "CCS": {"iata": "CCS", "name": "Simon Bolivar International", "city": "Caracas", "lat": 10.6012, "lng": -66.9912, "tz": "America/Caracas"},
    "MVD": {"iata": "MVD", "name": "Carrasco International", "city": "Montevideo", "lat": -34.8384, "lng": -56.0308, "tz": "America/Montevideo"},
    "ASU": {"iata": "ASU", "name": "Silvio Pettirossi International", "city": "Asuncion", "lat": -25.2400, "lng": -57.5190, "tz": "America/Asuncion"},
    "VVI": {"iata": "VVI", "name": "Viru Viru International", "city": "Santa Cruz", "lat": -17.6448, "lng": -63.1354, "tz": "America/La_Paz"},
    "LPB": {"iata": "LPB", "name": "El Alto International", "city": "La Paz", "lat": -16.5133, "lng": -68.1923, "tz": "America/La_Paz"},

    # ── Oceania ──────────────────────────────────────────────────────
    "SYD": {"iata": "SYD", "name": "Sydney Kingsford Smith", "city": "Sydney", "lat": -33.9461, "lng": 151.1772, "tz": "Australia/Sydney"},
    "MEL": {"iata": "MEL", "name": "Melbourne Tullamarine", "city": "Melbourne", "lat": -37.6690, "lng": 144.8410, "tz": "Australia/Melbourne"},
    "BNE": {"iata": "BNE", "name": "Brisbane", "city": "Brisbane", "lat": -27.3842, "lng": 153.1175, "tz": "Australia/Brisbane"},
    "AKL": {"iata": "AKL", "name": "Auckland", "city": "Auckland", "lat": -37.0082, "lng": 174.7850, "tz": "Pacific/Auckland"},
    "PPT": {"iata": "PPT", "name": "Faaa International", "city": "Tahiti", "lat": -17.5537, "lng": -149.6073, "tz": "Pacific/Tahiti"},
    "NAN": {"iata": "NAN", "name": "Nadi International", "city": "Nadi", "lat": -17.7554, "lng": 177.4431, "tz": "Pacific/Fiji"},

    # ── Africa ───────────────────────────────────────────────────────
    "JNB": {"iata": "JNB", "name": "O R Tambo International", "city": "Johannesburg", "lat": -26.1392, "lng": 28.2460, "tz": "Africa/Johannesburg"},
    "CAI": {"iata": "CAI", "name": "Cairo International", "city": "Cairo", "lat": 30.1219, "lng": 31.4056, "tz": "Africa/Cairo"},
    "CMN": {"iata": "CMN", "name": "Mohammed V International", "city": "Casablanca", "lat": 33.3675, "lng": -7.5900, "tz": "Africa/Casablanca"},
    "ADD": {"iata": "ADD", "name": "Bole International", "city": "Addis Ababa", "lat": 8.9779, "lng": 38.7993, "tz": "Africa/Addis_Ababa"},
    "NBO": {"iata": "NBO", "name": "Jomo Kenyatta International", "city": "Nairobi", "lat": -1.3192, "lng": 36.9278, "tz": "Africa/Nairobi"},
    "LOS": {"iata": "LOS", "name": "Murtala Muhammed International", "city": "Lagos", "lat": 6.5774, "lng": 3.3211, "tz": "Africa/Lagos"},
    "CPT": {"iata": "CPT", "name": "Cape Town International", "city": "Cape Town", "lat": -33.9649, "lng": 18.6017, "tz": "Africa/Johannesburg"},
}

# Size tier for relevance scoring (higher = busier)
_AIRPORT_TIERS = {
    # US top 50
    "ATL": 40, "LAX": 38, "ORD": 37, "DFW": 36, "DEN": 35, "JFK": 35,
    "SFO": 33, "SEA": 32, "LAS": 31, "MIA": 31, "MCO": 30, "EWR": 30,
    "CLT": 29, "PHX": 28, "IAH": 28, "BOS": 27, "MSP": 27, "FLL": 26,
    "DTW": 26, "PHL": 26, "LGA": 25, "BWI": 25, "SLC": 24, "IAD": 24,
    "DCA": 24, "SAN": 23, "TPA": 23, "AUS": 22, "BNA": 22, "PDX": 22,
    "HNL": 21, "MDW": 21, "DAL": 20, "HOU": 20, "STL": 20, "RDU": 19,
    "SJC": 19, "MSY": 19, "SMF": 18, "SNA": 18, "RSW": 18, "SAT": 17,
    "PIT": 17, "IND": 17, "CLE": 16, "CMH": 16, "JAX": 16, "MCI": 16,
    "OAK": 16, "BUR": 15, "CVG": 15, "MKE": 15,
    # US secondary
    "OKC": 14, "TUL": 13, "ABQ": 13, "OMA": 12, "ANC": 12, "RNO": 12,
    "MEM": 14, "PBI": 13, "BDL": 12, "BUF": 12, "ORF": 11, "RIC": 11,
    "CHS": 12, "SAV": 11, "DSM": 10, "ICT": 10, "LIT": 10, "TUS": 11,
    "ELP": 10, "BOI": 11, "SDF": 11, "OGG": 13, "SYR": 10, "ROC": 10,
    "GRR": 10, "GSP": 10, "HSV": 10, "SRQ": 11, "PNS": 10, "XNA": 10,
    "ONT": 13, "PSP": 11, "KOA": 10, "LIH": 10,
    # International — Europe
    "LHR": 39, "CDG": 35, "FRA": 34, "AMS": 33, "IST": 33, "DXB": 37,
    "FCO": 28, "MAD": 28, "BCN": 27, "MUC": 26, "ZRH": 25, "LGW": 25,
    "ORY": 22, "DUB": 23, "CPH": 23, "OSL": 21, "ARN": 21, "HEL": 20,
    "LIS": 22, "VIE": 22, "BRU": 22, "WAW": 20, "PRG": 20, "BUD": 19,
    "ATH": 20, "EDI": 19, "MAN": 20, "MXP": 23, "GVA": 19, "KEF": 17,
    # International — Asia/Middle East
    "HKG": 34, "SIN": 33, "NRT": 32, "ICN": 32, "PEK": 34, "PVG": 33,
    "BKK": 30, "HND": 31, "DEL": 30, "BOM": 28, "DOH": 27,
    "MNL": 25, "KUL": 25, "TPE": 26, "AUH": 24, "TLV": 22,
    # International — Americas
    "YYZ": 29, "YVR": 24, "YUL": 22, "YYC": 20,
    "MEX": 27, "CUN": 24, "GDL": 18, "SJO": 16, "PTY": 18,
    "GRU": 28, "EZE": 23, "BOG": 22, "LIM": 21, "SCL": 20,
    "MTY": 17, "SJD": 16, "PVR": 15,
    "GUA": 14, "SAL": 13, "BZE": 10,
    "MBJ": 15, "KIN": 13, "NAS": 14, "SJU": 19, "PUJ": 16,
    "SDQ": 14, "HAV": 15, "AUA": 12, "SXM": 11, "STT": 12,
    "GCM": 11, "POS": 12,
    "GIG": 22, "CTG": 14, "MDE": 16, "CUZ": 13,
    "UIO": 16, "GYE": 14, "CCS": 17, "MVD": 15, "ASU": 12,
    "VVI": 11, "LPB": 11,
    # International — Oceania
    "SYD": 29, "MEL": 25, "BNE": 22, "AKL": 21, "PPT": 12, "NAN": 13,
    # International — Africa
    "JNB": 24, "CAI": 22, "CMN": 18, "ADD": 20, "NBO": 18, "LOS": 17,
    "CPT": 19,
}

# ── Airline database ─────────────────────────────────────────────────

AIRLINES = {
    "UA": "United Airlines",
    "DL": "Delta Air Lines",
    "AA": "American Airlines",
    "WN": "Southwest Airlines",
    "BA": "British Airways",
    "LH": "Lufthansa",
    "AF": "Air France",
    "NH": "All Nippon Airways",
    "JL": "Japan Airlines",
    "KE": "Korean Air",
    "SQ": "Singapore Airlines",
    "EK": "Emirates",
    "QR": "Qatar Airways",
    "AC": "Air Canada",
    "AS": "Alaska Airlines",
    "B6": "JetBlue Airways",
    "NK": "Spirit Airlines",
    "F9": "Frontier Airlines",
    "KL": "KLM Royal Dutch Airlines",
    "IB": "Iberia",
    "QF": "Qantas",
    "TK": "Turkish Airlines",
    "AM": "Aeromexico",
    "AV": "Avianca",
    "LA": "LATAM Airlines",
    "CM": "Copa Airlines",
    "AR": "Aerolineas Argentinas",
    "G3": "Gol Linhas Aereas",
    "Y4": "Volaris",
}

AIRCRAFT = ["738", "739", "320", "321", "77W", "789", "359", "388"]

# Hub airports for generating connections
HUBS = ["ORD", "DFW", "ATL", "DEN", "IAH", "CLT", "PHX", "MSP", "DTW", "EWR",
        "LHR", "FRA", "AMS", "IST", "DXB", "SIN", "NRT", "ICN",
        "MEX", "PTY", "BOG", "GRU", "SCL", "LIM", "EZE"]

# Cabin class multipliers
CABIN_MULTIPLIERS = {
    "ECONOMY": 1.0,
    "PREMIUM_ECONOMY": 1.8,
    "BUSINESS": 3.5,
    "FIRST": 6.0,
}

CABIN_BAGS = {
    "ECONOMY": 0,
    "PREMIUM_ECONOMY": 1,
    "BUSINESS": 2,
    "FIRST": 3,
}

CABIN_BOOKING_CLASS = {
    "ECONOMY": "Y",
    "PREMIUM_ECONOMY": "W",
    "BUSINESS": "J",
    "FIRST": "F",
}


# ── Utility functions ────────────────────────────────────────────────

def _haversine_miles(lat1, lng1, lat2, lng2):
    """Great-circle distance in miles."""
    R = 3959
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _flight_duration_minutes(distance_miles):
    """Estimate flight time: ~500 mph cruise + 30 min taxi/climb/descent."""
    return int(distance_miles / 500 * 60) + 30


def _format_iso_duration(minutes):
    """Convert minutes to ISO 8601 duration string."""
    h, m = divmod(minutes, 60)
    if m:
        return f"PT{h}H{m}M"
    return f"PT{h}H"


def _make_times(origin_tz, dest_tz, departure_date, depart_hour, flight_minutes):
    """Generate departure and arrival ISO strings in local time."""
    dep_tz = zoneinfo.ZoneInfo(origin_tz)
    arr_tz = zoneinfo.ZoneInfo(dest_tz)

    dep_local = datetime.strptime(departure_date, "%Y-%m-%d").replace(
        hour=depart_hour, minute=0, tzinfo=dep_tz
    )
    arr_utc = dep_local + timedelta(minutes=flight_minutes)
    arr_local = arr_utc.astimezone(arr_tz)

    return (
        dep_local.strftime("%Y-%m-%dT%H:%M:%S"),
        arr_local.strftime("%Y-%m-%dT%H:%M:%S"),
    )


def _random_flight_number():
    """Generate a random 3-4 digit flight number."""
    return str(random.randint(100, 9999))


def _random_pnr():
    """Generate a 6-character alphanumeric PNR code."""
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


_AIRLINE_HUBS = {
    # US majors
    "DL": {"ATL", "DTW", "MSP", "SLC", "JFK", "LAX", "SEA", "BOS"},
    "UA": {"ORD", "IAH", "EWR", "DEN", "SFO", "LAX"},
    "AA": {"DFW", "CLT", "MIA", "PHX", "PHL", "ORD", "LAX", "JFK"},
    "WN": {"MDW", "DAL", "HOU", "LAS", "BWI", "DEN", "OAK", "PHX"},
    # US others
    "B6": {"JFK", "BOS", "FLL", "MCO", "SJU"},
    "AS": {"SEA", "PDX", "SFO", "LAX", "ANC"},
    "NK": {"FLL", "LAS", "DTW", "MCO", "DFW"},
    "F9": {"DEN", "LAS", "MCO", "PHX"},
    # European
    "BA": {"LHR", "LGW"},
    "LH": {"FRA", "MUC"},
    "AF": {"CDG", "ORY"},
    "KL": {"AMS"},
    "IB": {"MAD", "BCN"},
    "TK": {"IST"},
    # Middle East
    "EK": {"DXB"},
    "QR": {"DOH"},
    # Asian
    "NH": {"NRT", "HND"},
    "JL": {"NRT", "HND"},
    "KE": {"ICN"},
    "SQ": {"SIN"},
    # Oceania
    "QF": {"SYD", "MEL", "BNE"},
    # Canada
    "AC": {"YYZ", "YVR", "YUL"},
    # Latin America
    "AM": {"MEX", "GDL", "MTY", "CUN"},
    "Y4": {"MEX", "GDL", "CUN", "MTY"},
    "AV": {"BOG", "MDE", "CTG"},
    "LA": {"SCL", "LIM", "GRU", "EZE"},
    "CM": {"PTY"},
    "AR": {"EZE"},
    "G3": {"GRU", "GIG"},
}

_US_ZONES = {
    "America/New_York", "America/Chicago", "America/Denver",
    "America/Los_Angeles", "America/Phoenix", "America/Detroit",
    "America/Indiana/Indianapolis", "America/Anchorage", "Pacific/Honolulu",
    "America/Boise", "America/Kentucky/Louisville",
    "America/Puerto_Rico", "America/Virgin",
}

_LATAM_ZONES = {
    "America/Mexico_City", "America/Cancun", "America/Monterrey",
    "America/Mazatlan", "America/Costa_Rica", "America/Panama",
    "America/Guatemala", "America/El_Salvador", "America/Belize",
    "America/Jamaica", "America/Nassau", "America/Santo_Domingo",
    "America/Havana", "America/Aruba", "America/Lower_Princes",
    "America/Cayman", "America/Port_of_Spain",
    "America/Sao_Paulo", "America/Argentina/Buenos_Aires",
    "America/Bogota", "America/Lima", "America/Santiago",
    "America/Guayaquil", "America/Caracas", "America/Montevideo",
    "America/Asuncion", "America/La_Paz",
}

_CANADA_ZONES = {"America/Toronto", "America/Vancouver", "America/Edmonton"}


def _pick_airlines_for_route(origin, dest, count=3):
    """Pick plausible airlines for a route, preferring hub carriers."""
    # Find airlines that hub at origin or destination
    hub_carriers = []
    for code, hubs in _AIRLINE_HUBS.items():
        if origin in hubs or dest in hubs:
            hub_carriers.append(code)
    random.shuffle(hub_carriers)

    # Determine regional pool as fallback
    o = AIRPORTS.get(origin, {})
    d = AIRPORTS.get(dest, {})
    o_tz = o.get("tz", "")
    d_tz = d.get("tz", "")

    o_us = o_tz in _US_ZONES
    d_us = d_tz in _US_ZONES
    o_latam = o_tz in _LATAM_ZONES
    d_latam = d_tz in _LATAM_ZONES
    o_canada = d_tz in _CANADA_ZONES
    d_canada = d_tz in _CANADA_ZONES
    o_europe = "Europe" in o_tz or "Atlantic" in o_tz
    d_europe = "Europe" in d_tz or "Atlantic" in d_tz
    o_asia = "Asia" in o_tz
    d_asia = "Asia" in d_tz
    o_oceania = "Australia" in o_tz or "Pacific" in o_tz
    d_oceania = "Australia" in d_tz or "Pacific" in d_tz

    if o_us and d_us:
        pool = ["UA", "DL", "AA", "WN", "AS", "B6", "NK", "F9"]
    elif (o_us or d_us) and (o_latam or d_latam):
        pool = ["AA", "UA", "DL", "AM", "AV", "CM", "LA", "B6", "WN", "Y4"]
    elif o_latam and d_latam:
        pool = ["AM", "AV", "LA", "CM", "AR", "G3", "Y4", "AA", "UA"]
    elif (o_us or d_us) and (o_europe or d_europe):
        pool = ["UA", "DL", "AA", "BA", "LH", "AF", "KL", "IB", "AC", "TK"]
    elif (o_us or d_us) and (o_asia or d_asia):
        pool = ["UA", "DL", "AA", "NH", "JL", "KE", "SQ", "AS", "EK", "QR"]
    elif (o_us or d_us) and (o_canada or d_canada):
        pool = ["AC", "UA", "DL", "AA", "WN", "AS"]
    elif o_europe and d_europe:
        pool = ["BA", "LH", "AF", "KL", "IB", "TK"]
    elif o_asia and d_asia:
        pool = ["NH", "JL", "KE", "SQ", "EK", "QR", "TK"]
    elif "Asia/Dubai" in (o_tz, d_tz) or "Asia/Qatar" in (o_tz, d_tz):
        pool = ["EK", "QR", "TK", "BA", "LH"]
    elif o_oceania or d_oceania:
        pool = ["QF", "UA", "DL", "NH", "SQ"]
    else:
        pool = list(AIRLINES.keys())

    # Build result: hub carriers first, then fill from pool
    result = []
    for c in hub_carriers:
        if len(result) >= count:
            break
        result.append(c)

    remaining = [c for c in pool if c not in result]
    random.shuffle(remaining)
    for c in remaining:
        if len(result) >= count:
            break
        result.append(c)

    # Fallback if we still don't have enough
    if len(result) < count:
        extras = [c for c in AIRLINES if c not in result]
        random.shuffle(extras)
        result.extend(extras[:count - len(result)])

    return result[:count]


def _pick_connection_hub(origin, dest):
    """Pick a geographically reasonable hub between origin and dest."""
    o = AIRPORTS.get(origin)
    d = AIRPORTS.get(dest)
    if not o or not d:
        return random.choice(HUBS)

    # Bounding box between origin and dest (with margin)
    lat_min = min(o["lat"], d["lat"]) - 10
    lat_max = max(o["lat"], d["lat"]) + 10
    lng_min = min(o["lng"], d["lng"]) - 15
    lng_max = max(o["lng"], d["lng"]) + 15

    candidates = []
    for hub in HUBS:
        if hub in (origin, dest):
            continue
        h = AIRPORTS.get(hub)
        if not h:
            continue
        if lat_min <= h["lat"] <= lat_max and lng_min <= h["lng"] <= lng_max:
            candidates.append(hub)

    if not candidates:
        # Fallback: pick hub closest to midpoint
        mid_lat = (o["lat"] + d["lat"]) / 2
        mid_lng = (o["lng"] + d["lng"]) / 2
        best = None
        best_dist = float("inf")
        for hub in HUBS:
            if hub in (origin, dest):
                continue
            h = AIRPORTS.get(hub)
            if not h:
                continue
            dist = _haversine_miles(mid_lat, mid_lng, h["lat"], h["lng"])
            if dist < best_dist:
                best_dist = dist
                best = hub
        return best or "ORD"

    return random.choice(candidates)


# ── Mock API functions ───────────────────────────────────────────────

def mock_search_airports(keyword):
    """Fuzzy match on IATA code, airport name, and city name.

    Returns location dicts matching Amadeus format.
    """
    _maybe_delay(1, 5)
    if not keyword:
        return []

    keyword_lower = keyword.lower()
    results = []

    for iata, info in AIRPORTS.items():
        # Check IATA code, name, and city
        if (keyword_lower in iata.lower() or
                keyword_lower in info["name"].lower() or
                keyword_lower in info["city"].lower()):
            score = _AIRPORT_TIERS.get(iata, 10)
            # Boost exact IATA match
            relevance = 100.0 if keyword_lower == iata.lower() else 50.0 + score
            results.append({
                "iataCode": iata,
                "name": info["name"].upper(),
                "subType": "AIRPORT",
                "address": {"cityName": info["city"].upper()},
                "analytics": {"travelers": {"score": score}},
                "relevance": relevance,
            })

    # Sort by relevance descending
    results.sort(key=lambda x: x["relevance"], reverse=True)
    return results[:5]


def mock_get_airport(iata):
    """Return the raw airport record for a given IATA code, or None if not found."""
    return AIRPORTS.get(iata.upper())


def mock_nearest_airports(lat, lng):
    """Find nearest airports by coordinates using haversine distance.

    Returns top 5 sorted by distance, formatted like Amadeus.
    """
    _maybe_delay(1, 5)
    distances = []
    for iata, info in AIRPORTS.items():
        dist = _haversine_miles(lat, lng, info["lat"], info["lng"])
        distances.append((iata, info, dist))

    distances.sort(key=lambda x: x[2])
    results = []
    for iata, info, dist in distances[:5]:
        # Cap at 75 miles — matches Amadeus radius=100km behavior.
        # Beyond this, airports are in a different city/metro area.
        if dist > 75:
            continue
        relevance = max(1, int(100 * math.exp(-dist / 50)))
        results.append({
            "iataCode": iata,
            "name": info["name"].upper(),
            "subType": "AIRPORT",
            "address": {"cityName": info["city"].upper()},
            "analytics": {"travelers": {"score": _AIRPORT_TIERS.get(iata, 10)}},
            "relevance": relevance,
            "distance": {"value": round(dist, 1), "unit": "MI"},
        })

    return results


def mock_search_flights(origin, destination, departure_date, return_date=None,
                        adults=1, cabin_class="ECONOMY", max_results=5):
    """Generate realistic flight offers on the fly.

    Returns (offers_list, dictionaries, cabin_class) matching Amadeus format.
    """
    _maybe_delay(1, 5)
    o = AIRPORTS.get(origin)
    d = AIRPORTS.get(destination)
    if not o or not d:
        return [], {}, cabin_class

    distance = _haversine_miles(o["lat"], o["lng"], d["lat"], d["lng"])
    base_minutes = _flight_duration_minutes(distance)

    # Determine nonstop vs 1-stop mix
    is_short_route = distance < 1200
    num_offers = min(random.randint(3, 5), max_results)

    airlines = _pick_airlines_for_route(origin, destination, count=3)
    used_carriers = {}
    for code in airlines:
        used_carriers[code] = AIRLINES.get(code, code)

    # Departure time slots (hours in local time)
    time_slots = [6, 8, 10, 13, 16, 19, 21]
    random.shuffle(time_slots)

    offers = []
    for i in range(num_offers):
        airline = airlines[i % len(airlines)]
        depart_hour = time_slots[i % len(time_slots)]

        # Nonstop vs 1-stop
        if is_short_route:
            is_nonstop = random.random() < 0.8
        else:
            is_nonstop = random.random() < 0.4

        itineraries = []

        # ── Build outbound itinerary ──
        outbound_segments = _build_segments(
            origin, destination, departure_date, depart_hour,
            base_minutes, airline, is_nonstop
        )
        outbound_total_min = sum(
            s["_duration_min"] for s in outbound_segments
        )
        itineraries.append({
            "duration": _format_iso_duration(outbound_total_min),
            "segments": [{k: v for k, v in s.items() if k != "_duration_min"}
                         for s in outbound_segments],
        })

        # ── Build return itinerary if round-trip ──
        if return_date:
            ret_hour = random.choice([h for h in time_slots if h != depart_hour] or [10])
            ret_nonstop = is_nonstop if random.random() < 0.7 else (not is_nonstop)
            return_segments = _build_segments(
                destination, origin, return_date, ret_hour,
                base_minutes, airline, ret_nonstop
            )
            return_total_min = sum(
                s["_duration_min"] for s in return_segments
            )
            itineraries.append({
                "duration": _format_iso_duration(return_total_min),
                "segments": [{k: v for k, v in s.items() if k != "_duration_min"}
                             for s in return_segments],
            })

        # ── Price calculation (tiered per-mile + fixed overhead) ──
        if distance < 500:
            base_price = distance * 0.25 + 50   # short haul premium
        elif distance < 1500:
            base_price = distance * 0.18 + 30   # medium haul
        else:
            base_price = distance * 0.12 + 80   # long haul
        cabin_mult = CABIN_MULTIPLIERS.get(cabin_class, 1.0)

        # Time-of-day adjustment
        if depart_hour < 7 or depart_hour > 20:
            time_mult = 0.85  # red-eye discount
        elif 8 <= depart_hour <= 10 or 16 <= depart_hour <= 18:
            time_mult = 1.12  # peak hours
        else:
            time_mult = 1.0

        # 1-stop discount
        stop_mult = 0.80 if not is_nonstop else 1.0

        price = base_price * cabin_mult * time_mult * stop_mult
        # Random variance ±15%
        price *= random.uniform(0.85, 1.15)
        # Round-trip multiplier
        if return_date:
            price *= 1.8

        # Per-person pricing
        price = max(price, 89.0)  # minimum fare
        total = round(price, 2)

        offer = {
            "id": str(i + 1),
            "source": "GDS",
            "lastTicketingDate": departure_date,
            "numberOfBookableSeats": random.randint(3, 9),
            "itineraries": itineraries,
            "price": {
                "currency": "USD",
                "total": f"{total:.2f}",
                "grandTotal": f"{total:.2f}",
            },
            "validatingAirlineCodes": [airline],
        }
        offers.append(offer)

    # Sort by price
    offers.sort(key=lambda x: float(x["price"]["grandTotal"]))
    # Re-number IDs after sort
    for i, offer in enumerate(offers):
        offer["id"] = str(i + 1)

    dictionaries = {"carriers": used_carriers}
    return offers, dictionaries, cabin_class


def _build_segments(origin, dest, date, depart_hour, base_minutes, airline, is_nonstop):
    """Build segment list for one direction of travel."""
    o = AIRPORTS.get(origin, {})
    d = AIRPORTS.get(dest, {})
    o_tz = o.get("tz", "America/New_York")
    d_tz = d.get("tz", "America/New_York")

    if is_nonstop:
        dep_str, arr_str = _make_times(o_tz, d_tz, date, depart_hour, base_minutes)
        return [{
            "departure": {"iataCode": origin, "at": dep_str},
            "arrival": {"iataCode": dest, "at": arr_str},
            "carrierCode": airline,
            "number": _random_flight_number(),
            "aircraft": {"code": random.choice(AIRCRAFT)},
            "operating": {"carrierCode": airline},
            "_duration_min": base_minutes,
        }]

    # 1-stop: pick a hub
    hub = _pick_connection_hub(origin, dest)
    hub_info = AIRPORTS.get(hub, {})
    hub_tz = hub_info.get("tz", "America/Chicago")

    # Leg 1: origin → hub
    dist1 = _haversine_miles(o["lat"], o["lng"], hub_info["lat"], hub_info["lng"])
    leg1_min = _flight_duration_minutes(dist1)
    dep1_str, arr1_str = _make_times(o_tz, hub_tz, date, depart_hour, leg1_min)

    # Layover: 1-3 hours
    layover_min = random.randint(60, 180)

    # Leg 2: hub → dest
    dist2 = _haversine_miles(hub_info["lat"], hub_info["lng"], d["lat"], d["lng"])
    leg2_min = _flight_duration_minutes(dist2)
    leg2_depart_hour = depart_hour + (leg1_min + layover_min) // 60
    leg2_depart_hour = leg2_depart_hour % 24
    dep2_str, arr2_str = _make_times(hub_tz, d_tz, date, leg2_depart_hour, leg2_min)

    total_min = leg1_min + layover_min + leg2_min

    return [
        {
            "departure": {"iataCode": origin, "at": dep1_str},
            "arrival": {"iataCode": hub, "at": arr1_str},
            "carrierCode": airline,
            "number": _random_flight_number(),
            "aircraft": {"code": random.choice(AIRCRAFT)},
            "operating": {"carrierCode": airline},
            "_duration_min": total_min,  # full trip time on first segment for duration calc
        },
        {
            "departure": {"iataCode": hub, "at": dep2_str},
            "arrival": {"iataCode": dest, "at": arr2_str},
            "carrierCode": airline,
            "number": _random_flight_number(),
            "aircraft": {"code": random.choice(AIRCRAFT)},
            "operating": {"carrierCode": airline},
            "_duration_min": 0,  # counted in first segment
        },
    ]


def mock_price_offer(offer):
    """Price an offer — always succeeds.

    Returns the Amadeus pricing response format with travelerPricings.
    Price may bump slightly (+1-3%) to simulate live pricing variance.
    """
    _maybe_delay(1, 5)
    # Slight price bump
    bump = random.uniform(1.00, 1.03)
    original_price = float(offer.get("price", {}).get("grandTotal", "0"))
    new_price = round(original_price * bump, 2)

    # Determine cabin class from context (default ECONOMY)
    cabin_class = "ECONOMY"

    priced_offer = dict(offer)
    priced_offer["price"] = {
        "currency": offer.get("price", {}).get("currency", "USD"),
        "total": f"{new_price:.2f}",
        "grandTotal": f"{new_price:.2f}",
    }

    # Build travelerPricings
    segments = []
    seg_id = 1
    for itin in offer.get("itineraries", []):
        for seg in itin.get("segments", []):
            segments.append({
                "segmentId": str(seg_id),
                "cabin": cabin_class,
                "class": CABIN_BOOKING_CLASS.get(cabin_class, "Y"),
                "includedCheckedBags": {
                    "quantity": CABIN_BAGS.get(cabin_class, 0),
                },
            })
            seg_id += 1

    priced_offer["travelerPricings"] = [{
        "travelerId": "1",
        "fareOption": "STANDARD",
        "travelerType": "ADULT",
        "fareDetailsBySegment": segments,
    }]

    return {"flightOffers": [priced_offer]}


def mock_create_order(offer, travelers):
    """Create a booking — always succeeds.

    Returns order data matching Amadeus format.
    """
    _maybe_delay(4, 9)
    return {
        "id": f"VO{uuid4().hex[:8].upper()}",
        "type": "flight-order",
        "associatedRecords": [
            {
                "reference": _random_pnr(),
                "creationDate": datetime.now().isoformat(),
                "originSystemCode": "VOYAGER",
            }
        ],
        "flightOffers": [offer],
        "travelers": travelers,
    }
