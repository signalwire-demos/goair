"""Amadeus Self-Service API client with OAuth2 token lifecycle."""

import time
import logging
import requests

logger = logging.getLogger(__name__)


class AmadeusClient:
    """Manages Amadeus API authentication and requests."""

    def __init__(self, client_id, client_secret, base_url):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.token = None
        self.token_expiry = 0

    def _ensure_token(self):
        """Refresh bearer token if expired (tokens last 30 minutes)."""
        if time.time() < self.token_expiry - 60:
            return
        try:
            resp = requests.post(
                f"{self.base_url}/v1/security/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                }
            )
            resp.raise_for_status()
            data = resp.json()
            self.token = data["access_token"]
            self.token_expiry = time.time() + data["expires_in"]
            logger.info("Amadeus token refreshed")
        except Exception as e:
            logger.error(f"Amadeus token refresh failed: {e}")
            raise

    def _request(self, method, path, params=None, json_body=None,
                 extra_headers=None, retries=3):
        """Authenticated request with retry on 500s.

        Sandbox rate limit is 1 req/100ms and throws intermittent 500s.
        """
        self._ensure_token()
        headers = {"Authorization": f"Bearer {self.token}"}
        if extra_headers:
            headers.update(extra_headers)

        url = f"{self.base_url}{path}"

        for attempt in range(retries + 1):
            resp = requests.request(
                method, url,
                headers=headers,
                params=params or {},
                json=json_body,
            )
            if resp.status_code < 500 or attempt == retries:
                if resp.status_code >= 400:
                    # Log Amadeus error details before raising
                    try:
                        err_body = resp.json()
                        errors = err_body.get("errors", [])
                        for e in errors:
                            logger.error(
                                f"Amadeus {resp.status_code}: "
                                f"[{e.get('code')}] {e.get('title', '')} - "
                                f"{e.get('detail', '')} "
                                f"(source: {e.get('source', {})})"
                            )
                    except Exception:
                        logger.error(f"Amadeus {resp.status_code}: {resp.text[:500]}")
                resp.raise_for_status()
                return resp.json()

            # Sandbox 500 — wait and retry
            wait = 0.5 * (attempt + 1)
            logger.warning(f"Amadeus {resp.status_code} on {method} {path}, "
                           f"retry {attempt + 1}/{retries} in {wait}s")
            time.sleep(wait)

    def _get(self, path, params=None):
        """Authenticated GET request with retry."""
        return self._request("GET", path, params=params)

    def _post(self, path, json_body, extra_headers=None):
        """Authenticated POST request with retry."""
        return self._request("POST", path, json_body=json_body,
                             extra_headers=extra_headers)

    # --- Airport & Location APIs ---

    def airport_city_search(self, keyword, sub_type="AIRPORT,CITY"):
        """Keyword search for airports and cities.

        GET /v1/reference-data/locations
        Returns IATA codes with analytics.travelers.score for ranking.
        """
        try:
            data = self._get("/v1/reference-data/locations", {
                "subType": sub_type,
                "keyword": keyword,
                "page[limit]": 10,
            })
            return data.get("data", [])
        except Exception as e:
            logger.error(f"airport_city_search failed: {e}")
            return []

    def airport_nearest_relevant(self, lat, lng, radius=100):
        """Proximity search for airports near coordinates.

        GET /v1/reference-data/locations/airports
        Returns airports sorted by traveler relevance.
        """
        try:
            data = self._get("/v1/reference-data/locations/airports", {
                "latitude": lat,
                "longitude": lng,
                "radius": radius,
                "page[limit]": 10,
                "sort": "relevance",
            })
            return data.get("data", [])
        except Exception as e:
            logger.error(f"airport_nearest_relevant failed: {e}")
            return []

    # --- Flight Search APIs ---

    def flight_offers_search(self, origin, destination, departure_date,
                              return_date=None, adults=1, cabin_class="ECONOMY",
                              max_results=5):
        """Search for flight offers.

        GET /v2/shopping/flight-offers
        Returns (offers, dictionaries, actual_cabin) tuple.
        If the requested cabin class fails, retries with ECONOMY as fallback.
        """
        params = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": departure_date,
            "adults": adults,
            "travelClass": cabin_class,
            "max": max_results,
            "currencyCode": "USD",
        }
        if return_date:
            params["returnDate"] = return_date

        try:
            data = self._get("/v2/shopping/flight-offers", params)
            return data.get("data", []), data.get("dictionaries", {}), cabin_class
        except Exception as e:
            logger.error(f"flight_offers_search failed for {cabin_class}: {e}")

            # Retry with ECONOMY if a premium cabin class failed
            if cabin_class != "ECONOMY":
                logger.info(f"Retrying search with ECONOMY (was {cabin_class})")
                params["travelClass"] = "ECONOMY"
                try:
                    data = self._get("/v2/shopping/flight-offers", params)
                    return data.get("data", []), data.get("dictionaries", {}), "ECONOMY"
                except Exception as e2:
                    logger.error(f"flight_offers_search ECONOMY retry also failed: {e2}")

            return [], {}, cabin_class

    def flight_offers_price(self, offer):
        """Confirm live price on a specific flight offer.

        POST /v1/shopping/flight-offers/pricing
        Locks the fare and returns confirmed total, taxes, baggage, conditions.
        """
        try:
            data = self._post(
                "/v1/shopping/flight-offers/pricing",
                {
                    "data": {
                        "type": "flight-offers-pricing",
                        "flightOffers": [offer],
                    }
                },
            )
            return data.get("data", {})
        except Exception as e:
            logger.error(f"flight_offers_price failed: {e}")
            return None

    def flight_create_order(self, offer, travelers):
        """Create a flight booking (test PNR in sandbox).

        POST /v1/booking/flight-orders
        """
        try:
            data = self._post("/v1/booking/flight-orders", {
                "data": {
                    "type": "flight-order",
                    "flightOffers": [offer],
                    "travelers": travelers,
                    "remarks": {
                        "general": [
                            {"subType": "GENERAL_MISCELLANEOUS", "text": "VOYAGER AI BOOKING"}
                        ]
                    },
                }
            })
            return data.get("data", {})
        except Exception as e:
            logger.error(f"flight_create_order failed: {e}")
            return None

    def flight_cheapest_dates(self, origin, destination, departure_date=None):
        """Find cheapest travel dates for a route.

        GET /v1/shopping/flight-dates
        Returns a date/price matrix.
        """
        params = {
            "origin": origin,
            "destination": destination,
        }
        if departure_date:
            params["departureDate"] = departure_date

        try:
            data = self._get("/v1/shopping/flight-dates", params)
            return data.get("data", [])
        except Exception as e:
            logger.error(f"flight_cheapest_dates failed: {e}")
            return []
