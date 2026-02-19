"""
Export module for MercadoLibre listings to sellListings backend API.

This module transforms scraped items into the format required by the backend
SQL Server stored procedure sp_sellListings.
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)

# Regex patterns for extracting channelItemId
PRODUCT_ID_RE = re.compile(r"/p/(MLM\d+)", re.IGNORECASE)
UNIFIED_PRODUCT_RE = re.compile(r"/up/(MLMU\d+)", re.IGNORECASE)
ITEM_ID_RE = re.compile(r"(MLM\d+)", re.IGNORECASE)


def parse_channel_item_id(permalink: str) -> str:
    """
    Extract stable channelItemId from permalink using priority rules:
    1. If URL contains "/p/MLM..." → use that product ID
    2. Else if URL contains "/up/MLMU..." → use that unified product ID
    3. Otherwise generate SHA1 hash of the permalink
    
    Args:
        permalink: The MercadoLibre URL
        
    Returns:
        Stable channelItemId string
    """
    if not permalink:
        # Fallback to empty string (will be filtered out later)
        return ""
    
    # Priority 1: Check for /p/MLM... (product catalog URL)
    product_match = PRODUCT_ID_RE.search(permalink)
    if product_match:
        return product_match.group(1)
    
    # Priority 2: Check for /up/MLMU... (unified product URL)
    unified_match = UNIFIED_PRODUCT_RE.search(permalink)
    if unified_match:
        return unified_match.group(1)
    
    # Priority 3: Generate SHA1 hash of the full permalink
    # Use the original URL as-is (before any parsing)
    sha1_hash = hashlib.sha1(permalink.encode('utf-8')).hexdigest()
    return sha1_hash


def build_sell_listings_payload(
    items: List[Dict[str, Any]], 
    fx_rate_to_usd: float,
    run_timestamp: Optional[str] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Transform scraped items into sellListings payload format.
    
    Args:
        items: List of scraped item dictionaries (from NDJSON or normalized)
        fx_rate_to_usd: Exchange rate from MXN to USD
        run_timestamp: Optional ISO UTC timestamp. If not provided, uses current UTC time.
        
    Returns:
        Dictionary with "sellListings" key containing list of transformed records
    """
    # Use provided timestamp or generate current UTC timestamp
    if run_timestamp:
        # Parse the timestamp to get fxAsOfDate and listingTimestamp
        try:
            dt = datetime.fromisoformat(run_timestamp.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            dt = datetime.now(timezone.utc)
    else:
        dt = datetime.now(timezone.utc)
    
    # Format fxAsOfDate as YYYY-MM-DD
    fx_as_of_date = dt.strftime("%Y-%m-%d")
    
    # Format listingTimestamp as ISO-8601 UTC
    listing_timestamp = dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    
    sell_listings = []
    skipped = []
    
    for idx, item in enumerate(items):
        # Handle both normalized dicts (price_mxn) and raw dicts (price)
        price_original = item.get("price_mxn") or item.get("price")
        currency_original = item.get("currency", "MXN")
        
        # Extract required fields
        permalink = item.get("permalink", "")
        title = item.get("title", "")
        
        # Skip invalid items
        if not permalink:
            skipped.append({"index": idx, "reason": "missing permalink"})
            continue
        if not title:
            skipped.append({"index": idx, "reason": "missing title"})
            continue
        if price_original is None:
            skipped.append({"index": idx, "reason": "missing price", "permalink": permalink})
            continue
        
        # Parse price
        try:
            sell_price_original = float(price_original)
        except (ValueError, TypeError):
            skipped.append({"index": idx, "reason": "invalid price", "price": price_original})
            continue
        
        # Extract channelItemId using priority rules
        channel_item_id = parse_channel_item_id(permalink)
        
        # Calculate USD price
        sell_price_usd = round(sell_price_original * fx_rate_to_usd, 6)
        
        # Get listing timestamp from captured_at_utc or use run timestamp
        item_timestamp = item.get("captured_at_utc") or listing_timestamp
        if item_timestamp:
            # Ensure proper ISO format
            try:
                item_dt = datetime.fromisoformat(item_timestamp.replace('Z', '+00:00'))
                item_listing_timestamp = item_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            except (ValueError, AttributeError):
                item_listing_timestamp = listing_timestamp
        else:
            item_listing_timestamp = listing_timestamp
        
        # Build the sellListing record
        sell_listing = {
            "channel": "mercadolibre",
            "market": "MX",
            "channelItemId": channel_item_id,
            "title": title,
            "sellPriceOriginal": sell_price_original,
            "currencyOriginal": currency_original,
            "sellPriceUsd": sell_price_usd,
            "fxRateToUsd": fx_rate_to_usd,
            "fxAsOfDate": fx_as_of_date,
            "fulfillmentType": None,
            "shippingTimeDays": None,
            "rating": None,
            "reviewsCount": None,
            "listingTimestamp": item_listing_timestamp,
            "unifiedProductId": None,
            "action": "1"
        }
        
        sell_listings.append(sell_listing)
    
    logger.info(f"Built {len(sell_listings)} sellListings, skipped {len(skipped)} invalid items")
    if skipped:
        logger.debug(f"Skipped items: {skipped}")
    
    return {
        "sellListings": sell_listings,
        "_metadata": {
            "total": len(items),
            "emitted": len(sell_listings),
            "skipped": len(skipped),
            "skipped_details": skipped,
            "fx_rate_to_usd": fx_rate_to_usd,
            "fx_as_of_date": fx_as_of_date
        }
    }


def export_sell_listings(
    payload: Dict[str, List[Dict[str, Any]]],
    url: str,
    worker_key: str,
    timeout_sec: float = 30.0
) -> Dict[str, Any]:
    """
    POST the sellListings payload to the backend API with retry logic.

    Delegates to BackendApiClient.post_sell_listings() which owns the retry
    logic and HTTP error handling.

    Args:
        payload: The sellListings payload dictionary
        url: Backend API URL (used as url_override)
        worker_key: Worker authentication key
        timeout_sec: Request timeout in seconds

    Returns:
        Response dictionary with status and body
    """
    from ..api.backend_api import BackendApiClient

    client = BackendApiClient(
        base_url="",          # not used — url_override takes precedence
        worker_key=worker_key,
        timeout_sec=timeout_sec,
    )
    return client.post_sell_listings(payload=payload, url_override=url)


def read_ndjson(file_path: str) -> List[Dict[str, Any]]:
    """
    Read NDJSON file and return list of item dictionaries.
    
    Args:
        file_path: Path to the NDJSON file
        
    Returns:
        List of parsed item dictionaries
    """
    items = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse JSON line: {e}")
    return items

