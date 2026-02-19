"""
Central backend API client for the smartloans backend.

All HTTP calls to https://smartloansbackend.azurewebsites.net are defined here
and exported for use across the project.

Endpoints covered:
  - GET  /all_sellListings        → fetch every sell listing in the DB
  - POST /query_sellListings      → paginated / filtered sell-listing query
  - POST /sellListings            → upsert scraped sell listings (existing)
  - POST /exchange_rate_by_day    → fetch MXN→USD FX rate (existing)
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BackendApiClient
# ---------------------------------------------------------------------------

class BackendApiClient:
    """
    Thin HTTP client for the smartloans backend REST API.

    Args:
        base_url:    Base URL, e.g. "https://smartloansbackend.azurewebsites.net"
        worker_key:  Value for the X-Worker-Key header (if required by endpoint).
        timeout_sec: Default request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        worker_key: str = "",
        timeout_sec: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.worker_key = worker_key
        self.timeout_sec = timeout_sec

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _json_headers(self, include_worker_key: bool = False) -> Dict[str, str]:
        headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
        }
        if include_worker_key and self.worker_key:
            headers["X-Worker-Key"] = self.worker_key
        return headers

    def _handle_response(self, response: requests.Response) -> Dict[str, Any]:
        """Raise on HTTP error, then parse JSON body."""
        if response.status_code == 429:
            logger.warning("Rate limited (429)")
            raise requests.RequestException("Rate limited")
        if response.status_code >= 500:
            logger.warning(f"Server error {response.status_code}")
            raise requests.RequestException(f"Server error: {response.status_code}")
        if not response.ok:
            logger.error(
                f"Request failed {response.status_code}: {response.text[:500]}"
            )
            response.raise_for_status()
        try:
            return response.json()
        except json.JSONDecodeError:
            return {"raw_response": response.text[:1000]}

    # ------------------------------------------------------------------
    # GET /all_sellListings
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((requests.RequestException,)),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=30),
        reraise=True,
    )
    def get_all_sell_listings(self) -> Dict[str, Any]:
        """
        Fetch every sell listing stored in the backend database.

        Endpoint: GET /all_sellListings

        Returns:
            {
                "sellListings": [
                    {
                        "sellListingId": int,
                        "channel": str,
                        "market": str,
                        "channelItemId": str,
                        "title": str,
                        "sellPriceOriginal": float,
                        "currencyOriginal": str,
                        "sellPriceUsd": float,
                        "fxRateToUsd": float,
                        "fxAsOfDate": str,
                        "fulfillmentType": str | None,
                        "listingTimestamp": str,
                        "unifiedProductId": int | None,
                        "createdAt": str,
                        "updatedAt": str,
                        ...
                    },
                    ...
                ]
            }
        """
        url = f"{self.base_url}/all_sellListings"
        logger.info(f"GET {url}")
        response = requests.get(
            url,
            headers=self._json_headers(),
            timeout=self.timeout_sec,
        )
        data = self._handle_response(response)
        listings = data.get("sellListings", [])
        logger.info(f"Fetched {len(listings)} sell listings from backend")
        return data

    # ------------------------------------------------------------------
    # POST /query_sellListings
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((requests.RequestException,)),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=30),
        reraise=True,
    )
    def query_sell_listings(
        self,
        channel: str,
        market: str,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """
        Query sell listings with channel/market filters and pagination.

        Endpoint: POST /query_sellListings

        Args:
            channel:   Sales channel, e.g. "mercadolibre", "amazon"
            market:    Market code, e.g. "MX", "US"
            page:      Page number (1-based). Default: 1
            page_size: Records per page. Default: 50

        Returns:
            {
                "sellListingsQuery": [
                    {
                        "totalRows": int,
                        "page": int,
                        "pageSize": int,
                        "sellListingId": int,
                        "channel": str,
                        "market": str,
                        "channelItemId": str,
                        "title": str,
                        "sellPriceOriginal": float,
                        "currencyOriginal": str,
                        "sellPriceUsd": float,
                        "fxRateToUsd": float,
                        "fxAsOfDate": str,
                        "fulfillmentType": str | None,
                        "listingTimestamp": str,
                        "unifiedProductId": int | None,
                        "createdAt": str,
                        "updatedAt": str,
                        ...
                    },
                    ...
                ]
            }
        """
        url = f"{self.base_url}/query_sellListings"
        body = {
            "sellListings": [
                {
                    "channel": channel,
                    "market": market,
                    "page": page,
                    "pageSize": page_size,
                }
            ]
        }
        logger.info(
            f"POST {url} — channel={channel}, market={market}, "
            f"page={page}, pageSize={page_size}"
        )
        response = requests.post(
            url,
            json=body,
            headers=self._json_headers(),
            timeout=self.timeout_sec,
        )
        data = self._handle_response(response)
        rows = data.get("sellListingsQuery", [])
        logger.info(f"query_sell_listings returned {len(rows)} rows")
        return data

    # ------------------------------------------------------------------
    # POST /sellListings  (existing — moved here from export_sell_listings.py)
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((requests.RequestException,)),
        stop=stop_after_attempt(5),
        wait=wait_exponential_jitter(initial=1, max=60),
        reraise=True,
    )
    def post_sell_listings(
        self,
        payload: Dict[str, List[Dict[str, Any]]],
        url_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Upsert scraped sell listings into the backend database.

        Endpoint: POST /sellListings  (or url_override if provided)

        Args:
            payload:      Dict with key "sellListings" containing list of records.
            url_override: Optional full URL override (e.g. from SELL_LISTINGS_BACKEND_URL).

        Returns:
            {
                "ok": bool,
                "status_code": int,
                "exported_count": int,
                "response": dict,
            }
        """
        url = url_override or f"{self.base_url}/sellListings"
        count = len(payload.get("sellListings", []))
        logger.info(f"POST {url} — {count} listings")

        response = requests.post(
            url,
            json=payload,
            headers=self._json_headers(include_worker_key=True),
            timeout=self.timeout_sec,
        )

        if response.status_code == 429:
            logger.warning("Rate limited (429), retrying…")
            raise requests.RequestException("Rate limited")
        if response.status_code >= 500:
            logger.warning(f"Server error {response.status_code}, retrying…")
            raise requests.RequestException(f"Server error: {response.status_code}")
        if not response.ok:
            logger.error(
                f"post_sell_listings failed {response.status_code}: {response.text[:500]}"
            )
            response.raise_for_status()

        try:
            response_data = response.json()
        except json.JSONDecodeError:
            response_data = {"raw_response": response.text[:1000]}

        # Application-level error check
        if isinstance(response_data, dict):
            error_code = response_data.get("error", 0)
            if error_code != 0:
                msg = (
                    response_data.get("msg")
                    or response_data.get("message")
                    or "Unknown error"
                )
                logger.error(f"Backend error {error_code}: {msg}")
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "error": error_code,
                    "message": msg,
                    "response": response_data,
                }

        logger.info(f"Successfully posted {count} listings")
        return {
            "ok": True,
            "status_code": response.status_code,
            "exported_count": count,
            "response": response_data,
        }

    # ------------------------------------------------------------------
    # POST /exchange_rate_by_day  (existing — consolidated from settings.py)
    # ------------------------------------------------------------------

    def get_exchange_rate(self, as_of_date: str) -> Optional[float]:
        """
        Fetch the MXN→USD exchange rate for a given date.

        Endpoint: POST /exchange_rate_by_day

        Args:
            as_of_date: Date string in "YYYY-MM-DD" format.

        Returns:
            Exchange rate as float, or None if not found / request failed.
        """
        url = f"{self.base_url}/exchange_rate_by_day"
        try:
            response = requests.post(
                url,
                headers=self._json_headers(),
                json={"exchangeRates": [{"asOfDate": as_of_date}]},
                timeout=self.timeout_sec,
            )
            if response.ok:
                data = response.json()
                rates = data.get("exchangeRates", [])
                if rates:
                    rate = rates[0].get("rate")
                    if rate is not None:
                        return float(rate)
        except Exception as exc:
            logger.warning(f"get_exchange_rate failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Module-level convenience functions (use settings automatically)
# ---------------------------------------------------------------------------

def _make_client(timeout_sec: float = 30.0) -> BackendApiClient:
    """Instantiate BackendApiClient from environment / settings."""
    base_url = os.getenv("BACKEND_BASE_URL", "https://smartloansbackend.azurewebsites.net").rstrip("/")
    worker_key = os.getenv("BACKEND_WORKER_KEY", "")
    return BackendApiClient(base_url=base_url, worker_key=worker_key, timeout_sec=timeout_sec)


def get_all_sell_listings(timeout_sec: float = 30.0) -> Dict[str, Any]:
    """
    Convenience wrapper: GET /all_sellListings using env-configured client.

    Returns:
        {"sellListings": [...]}
    """
    return _make_client(timeout_sec).get_all_sell_listings()


def query_sell_listings(
    channel: str,
    market: str,
    page: int = 1,
    page_size: int = 50,
    timeout_sec: float = 30.0,
) -> Dict[str, Any]:
    """
    Convenience wrapper: POST /query_sellListings using env-configured client.

    Returns:
        {"sellListingsQuery": [...]}
    """
    return _make_client(timeout_sec).query_sell_listings(
        channel=channel,
        market=market,
        page=page,
        page_size=page_size,
    )


def post_sell_listings(
    payload: Dict[str, List[Dict[str, Any]]],
    url_override: Optional[str] = None,
    timeout_sec: float = 30.0,
) -> Dict[str, Any]:
    """
    Convenience wrapper: POST /sellListings using env-configured client.

    Returns:
        {"ok": bool, "status_code": int, "exported_count": int, "response": dict}
    """
    return _make_client(timeout_sec).post_sell_listings(
        payload=payload,
        url_override=url_override,
    )


def get_exchange_rate(as_of_date: str, timeout_sec: float = 10.0) -> Optional[float]:
    """
    Convenience wrapper: POST /exchange_rate_by_day using env-configured client.

    Returns:
        Exchange rate float or None.
    """
    return _make_client(timeout_sec).get_exchange_rate(as_of_date=as_of_date)
