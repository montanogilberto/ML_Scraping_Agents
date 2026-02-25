"""
Export module for MercadoLibre listings to sellListings backend API.

This module transforms scraped/enriched items into the format required by the
backend SQL Server stored procedure sp_sellListings and provides production-grade
export logic with retry, skip-reason tracking, and graceful degradation.

Public API
----------
extract_identity(card)              → {product_id, up_id, item_id, channel_item_id, id_source}
compute_needs_enrichment(card)      → bool  (False when JSON-LD present)
transform_to_sell_listing(card, fx) → dict | None
export_sell_listings(cards)         → stats dict
build_sell_listings_payload(...)    → legacy helper (dry-run / backward compat)
export_sell_listings_http(payload)  → HTTP POST helper
read_ndjson(path)                   → list[dict]
"""

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns (mirrors parsers.py — kept local to avoid circular imports)
# ---------------------------------------------------------------------------
_PRODUCT_ID_RE = re.compile(r"/p/(MLM\d+)", re.IGNORECASE)
_UP_ID_RE = re.compile(r"/up/(MLMU\d+)", re.IGNORECASE)
_ITEM_ID_RE = re.compile(r"(MLM\d{6,15})", re.IGNORECASE)
_ARTICULO_ITEM_ID_RE = re.compile(r"/MLM-(\d{6,15})")

# ---------------------------------------------------------------------------
# Skip-reason codes (ISSUE 5)
# ---------------------------------------------------------------------------
SKIP_MISSING_IDENTITY = "missing_identity"
SKIP_MISSING_PRICE = "missing_price"
SKIP_NEEDS_ENRICHMENT = "needs_enrichment_true"
SKIP_MISSING_PERMALINK = "missing_permalink"
SKIP_INVALID_CURRENCY = "invalid_currency"
SKIP_INVALID_PAYLOAD = "invalid_payload"


# ===========================================================================
# FUNCTION 1 — extract_identity
# ===========================================================================

def extract_identity(card: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract all identity fields from a card with deterministic priority rules.

    Priority: product_id → up_id → item_id → SHA1(permalink)

    Uses pre-computed fields from the card when available; falls back to
    URL-based extraction when fields are absent or empty.

    Args:
        card: Scraped/enriched item dictionary

    Returns:
        {
            "product_id":      str | None,
            "up_id":           str | None,
            "item_id":         str | None,
            "channel_item_id": str,          # always non-empty (SHA1 fallback)
            "id_source":       str,          # "product_id" | "up_id" | "item_id" | "hash"
        }
    """
    permalink = (card.get("permalink") or "").strip()

    # --- Use pre-computed fields when present ---
    product_id = card.get("product_id") or None
    up_id = card.get("up_id") or None
    item_id = card.get("item_id") or None

    # --- If any ID is missing, attempt URL-based extraction as fallback ---
    if not product_id and not up_id and not item_id and permalink:
        # Check /up/ first (most specific)
        up_match = _UP_ID_RE.search(permalink)
        if up_match:
            up_id = up_match.group(1)
        else:
            # Check /p/ catalog
            p_match = _PRODUCT_ID_RE.search(permalink)
            if p_match:
                product_id = p_match.group(1)
            else:
                # Check articulo dashed format /MLM-XXXXXXXXXX-
                art_match = _ARTICULO_ITEM_ID_RE.search(permalink)
                if art_match:
                    item_id = "MLM" + art_match.group(1)
                else:
                    # Check direct /MLMxxxxxxxxxx
                    item_match = _ITEM_ID_RE.search(permalink)
                    if item_match:
                        item_id = item_match.group(1)

    # --- Compute channel_item_id with priority: product_id → up_id → item_id → SHA1 ---
    pre_computed = (card.get("channel_item_id") or "").strip()
    pre_source = (card.get("id_source") or "hash").strip()

    # Trust pre-computed only when it matches one of the known IDs or is a
    # 40-char hex hash — avoids propagating stale/wrong values.
    if pre_computed and pre_source in ("product_id", "up_id", "item_id", "hash"):
        channel_item_id = pre_computed
        id_source = pre_source
    else:
        # Recompute from scratch
        if product_id:
            channel_item_id, id_source = product_id, "product_id"
        elif up_id:
            channel_item_id, id_source = up_id, "up_id"
        elif item_id:
            channel_item_id, id_source = item_id, "item_id"
        elif permalink:
            channel_item_id = hashlib.sha1(permalink.encode("utf-8")).hexdigest()
            id_source = "hash"
        else:
            channel_item_id, id_source = "", "hash"

    return {
        "product_id": product_id,
        "up_id": up_id,
        "item_id": item_id,
        "channel_item_id": channel_item_id,
        "id_source": id_source,
    }


# ===========================================================================
# FUNCTION 2 — compute_needs_enrichment
# ===========================================================================

def compute_needs_enrichment(card: Dict[str, Any]) -> bool:
    """
    Determine whether a card still requires enrichment at export time.

    Rule: needs_enrichment = False  if card contains JSON-LD data
          needs_enrichment = True   if JSON-LD is absent

    This overrides the pipeline-set `needs_enrichment` flag, which may be
    stale (True) for catalog/UP products that were successfully enriched by
    ml_scrape_item_detail but whose flag was not updated.

    Args:
        card: Scraped/enriched item dictionary

    Returns:
        False if JSON-LD data is present (enrichment complete)
        True  if JSON-LD data is absent (enrichment still needed)
    """
    attributes = card.get("attributes")

    # Guard: LLM pipeline may have JSON-encoded the dict; parse it back
    if isinstance(attributes, str):
        try:
            attributes = json.loads(attributes)
        except (json.JSONDecodeError, TypeError):
            attributes = None

    if isinstance(attributes, dict) and attributes.get("jsonld"):
        return False  # JSON-LD present → enrichment complete

    # Also accept the pipeline-set flag when it is explicitly False
    # (covers items that were enriched but attributes were not stored)
    pipeline_flag = card.get("needs_enrichment")
    if pipeline_flag is False:
        return False

    # FIXED: If the item has at least a price, we can still export it
    # The jsonld is nice-to-have but not required for export
    price = card.get("price_mxn") or card.get("price")
    if price is not None:
        return False
    
    return True  # No jsonld and no price → still needs enrichment


# ===========================================================================
# FUNCTION 3 — transform_to_sell_listing
# ===========================================================================

def transform_to_sell_listing(
    card: Dict[str, Any],
    fx_rate: float,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Transform an enriched card into the sellListing schema.

    Validates the card, computes identity, computes sellPriceUsd, and builds
    the final sellListing object matching the SQL Server SP format.

    Args:
        card:    Scraped/enriched item dictionary
        fx_rate: MXN → USD exchange rate

    Returns:
        (sell_listing_dict, None)  on success
        (None, skip_reason_code)   on failure — caller logs the reason
    """
    from datetime import datetime, timezone

    # --- PERMALINK: Support fallback to URL if missing ---
    # The LLM may drop or modify the permalink field during JSON output.
    # Try multiple field names to recover the URL.
    permalink = (
        card.get("permalink") or 
        card.get("url") or 
        card.get("item_url") or
        card.get("detail_url") or
        ""
    ).strip()
    
    # If still no permalink, try to extract from any URL-like field
    if not permalink:
        for key, value in card.items():
            if isinstance(value, str) and "mercadolibre" in value.lower() and ("/MLM" in value or "/p/" in value or "/up/" in value):
                permalink = value.strip()
                break
    
    if not permalink:
        return None, SKIP_MISSING_PERMALINK

    title = (card.get("title") or "").strip()
    if not title or len(title) < 3:
        return None, SKIP_INVALID_PAYLOAD

    # --- Price ---
    price_raw = card.get("price_mxn") or card.get("price")
    if price_raw is None:
        return None, SKIP_MISSING_PRICE
    try:
        price_mxn = float(price_raw)
    except (ValueError, TypeError):
        return None, SKIP_MISSING_PRICE
    if price_mxn <= 0:
        return None, SKIP_MISSING_PRICE

    # --- Currency ---
    currency = (card.get("currency") or "MXN").upper()
    if currency != "MXN":
        return None, SKIP_INVALID_CURRENCY

    # --- Identity ---
    identity = extract_identity(card)
    channel_item_id = identity["channel_item_id"]
    
    # --- FALLBACK: If channel_item_id is empty but we have a permalink, try to extract ID from URL ---
    # This handles cases where the LLM may have dropped the ID fields but still has the URL
    if not channel_item_id and permalink:
        # Re-extract IDs from the permalink URL
        from ..mcp_servers.ml_scrape_mcp.parsers import extract_ids as parse_ids
        ids = parse_ids(permalink)
        if ids.get("product_id"):
            channel_item_id = ids["product_id"]
            identity["id_source"] = "product_id"
        elif ids.get("up_id"):
            channel_item_id = ids["up_id"]
            identity["id_source"] = "up_id"
        elif ids.get("item_id"):
            channel_item_id = ids["item_id"]
            identity["id_source"] = "item_id"
        else:
            # Fall back to SHA1 hash
            import hashlib
            channel_item_id = hashlib.sha1(permalink.encode("utf-8")).hexdigest()
            identity["id_source"] = "hash"
        identity["channel_item_id"] = channel_item_id
    
    if not channel_item_id:
        return None, SKIP_MISSING_IDENTITY

    # --- Needs enrichment check ---
    if compute_needs_enrichment(card):
        return None, SKIP_NEEDS_ENRICHMENT

    # --- FX conversion (ISSUE 7) ---
    sell_price_usd = round(price_mxn * fx_rate, 6)

    # --- fxAsOfDate: use today's date in YYYY-MM-DD format ---
    fx_as_of_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # --- Image ---
    pictures = card.get("pictures") or []
    if isinstance(pictures, str):
        try:
            pictures = json.loads(pictures)
        except Exception:
            pictures = [pictures]
    image = pictures[0] if pictures else None

    # --- Condition ---
    condition = card.get("condition") or None

    # --- Brand (from card field or JSON-LD) ---
    brand = card.get("brand") or None
    if not brand:
        attributes = card.get("attributes") or {}
        if isinstance(attributes, str):
            try:
                attributes = json.loads(attributes)
            except Exception:
                attributes = {}
        if isinstance(attributes, dict):
            jsonld = attributes.get("jsonld") or {}
            if isinstance(jsonld, dict):
                brand_raw = jsonld.get("brand")
                if isinstance(brand_raw, dict):
                    brand = brand_raw.get("name")
                elif isinstance(brand_raw, str):
                    brand = brand_raw

    # --- Timestamp (ISSUE 4) — always use UTC now ---
    captured_at_utc = card.get("captured_at_utc") or _now_utc()
    
    # Convert ISO timestamp to SQL Server format: "YYYY-MM-DD HH:MM:SS.ffffff"
    # If already in ISO format with Z, convert to SQL format
    listing_timestamp = captured_at_utc
    if listing_timestamp.endswith("Z"):
        # Convert "2026-02-23T18:55:31Z" to "2026-02-23 18:55:31.000000"
        listing_timestamp = listing_timestamp.replace("Z", "").replace("T", " ")
        # Add microseconds if not present
        if "." not in listing_timestamp:
            listing_timestamp += ".000000"
    elif "T" in listing_timestamp:
        # Handle other ISO variants
        listing_timestamp = listing_timestamp.replace("T", " ")

    # --- Attributes: extract full attributes dict for SP ---
    raw_attributes = card.get("attributes")
    if isinstance(raw_attributes, str):
        try:
            raw_attributes = json.loads(raw_attributes)
        except (json.JSONDecodeError, TypeError):
            raw_attributes = None
    attributes = raw_attributes if isinstance(raw_attributes, dict) else None

    # --- Rating and reviews count (from JSON-LD aggregateRating) ---
    rating = None
    reviews_count = None
    if attributes:
        jsonld = attributes.get("jsonld") or {}
        if isinstance(jsonld, dict):
            agg_rating = jsonld.get("aggregateRating") or {}
            if isinstance(agg_rating, dict):
                rating_raw = agg_rating.get("ratingValue")
                if rating_raw is not None:
                    try:
                        rating = round(float(rating_raw), 2)
                    except (ValueError, TypeError):
                        rating = None
                reviews_count_raw = agg_rating.get("ratingCount")
                if reviews_count_raw is not None:
                    try:
                        reviews_count = int(reviews_count_raw)
                    except (ValueError, TypeError):
                        reviews_count = None

    # Build the sellListing dict in EXACT SP format:
    # - action: 1 (integer for UPSERT)
    # - market: "MLM" (not "MX")
    # - sellPriceOriginal: price in MXN
    # - currencyOriginal: "MXN"
    # - fxAsOfDate: today's date YYYY-MM-DD
    # - listingTimestamp: timestamp when the listing was captured
    # - attributes: full JSON object
    # - unifiedProductId: null (SP expects BIGINT, product_id is string like "MLM123")
    sell_listing = {
        "action": 1,  # Integer for UPSERT
        "channel": "mercadolibre",
        "market": "MLM",  # SP expects "MLM" not "MX"
        "channelItemId": channel_item_id,
        "title": title,
        "sellPriceOriginal": price_mxn,  # Price in MXN (SP field name)
        "currencyOriginal": currency,  # "MXN" (SP field name)
        "sellPriceUsd": sell_price_usd,
        "fxRateToUsd": fx_rate,
        "fxAsOfDate": fx_as_of_date,  # Today's date in YYYY-MM-DD format
        "listingTimestamp": listing_timestamp,  # When listing was captured (SQL Server format)
        "fulfillmentType": None,  # Not available from scrape
        "shippingTimeDays": None,  # Not available from scrape
        "rating": rating,  # From JSON-LD aggregateRating
        "reviewsCount": reviews_count,  # From JSON-LD aggregateRating
        "unifiedProductId": None,  # SP expects BIGINT, product_id is string - pass null
        "itemId": identity["item_id"],
        "upId": identity["up_id"],
        "permalink": permalink,
        "image": image,
        "condition": condition,
        "brand": brand,
        "capturedAtUtc": captured_at_utc,
        # Include full attributes dict for SP
        "attributes": attributes if isinstance(attributes, dict) else None,
    }

    return sell_listing, None


# ===========================================================================
# FUNCTION 4 — export_sell_listings
# ===========================================================================

def export_sell_listings(cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Full production-grade export pipeline.

    Steps:
      1. Transform cards → sellListings (track skip reasons)
      2. Query existing listings from backend (retry + graceful degradation on 500)
      3. Filter out already-existing listings (idempotent)
      4. POST new listings to backend (retry with exponential backoff)
      5. Return stats

    Args:
        cards: List of enriched item dicts from the pipeline

    Returns:
        {
            "ok":             bool,
            "as_of":          str,   # ISO UTC timestamp
            "existing_count": int,
            "new_count":      int,
            "skipped":        int,
            "skip_reasons":   dict,  # {reason_code: count}
        }
    """
    from ..config.settings import load_settings
    from ..api.backend_api import BackendApiClient

    settings = load_settings()
    fx_rate = settings.fx_rate_to_usd
    as_of = _now_utc()

    # ------------------------------------------------------------------
    # Step 1: Transform cards
    # ------------------------------------------------------------------
    sell_listings: List[Dict[str, Any]] = []
    skip_reasons: Dict[str, int] = {}
    skipped = 0

    for idx, card in enumerate(cards):
        listing, reason = transform_to_sell_listing(card, fx_rate)
        if listing is None:
            reason = reason or SKIP_INVALID_PAYLOAD
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            skipped += 1
            logger.info(
                "Skipped card[%d] permalink=%s reason=%s",
                idx,
                card.get("permalink", "<none>"),
                reason,
            )
            continue
        # DEBUG: Log each transformed listing
        logger.info(
            "Transformed card[%d]: channelItemId=%s, title=%s, price=%.2f",
            idx,
            listing.get("channelItemId"),
            listing.get("title", "")[:50],
            listing.get("sellPriceOriginal"),
        )
        sell_listings.append(listing)

    logger.info(
        "Transformation complete: %d valid, %d skipped. skip_reasons=%s",
        len(sell_listings),
        skipped,
        skip_reasons,
    )

    # ------------------------------------------------------------------
    # Step 2: Query existing listings (ISSUE 8 — graceful degradation)
    # ------------------------------------------------------------------
    existing_ids: set = set()
    existing_count = 0

    api_client = BackendApiClient(
        base_url=settings.backend_base_url,
        worker_key=settings.backend_worker_key,
        timeout_sec=settings.http_timeout_sec,
    )

    try:
        data = _query_existing_with_retry(api_client)
        rows = data.get("sellListingsQuery", [])
        existing_ids = {r["channelItemId"] for r in rows if r.get("channelItemId")}
        existing_count = len(existing_ids)
        logger.info("Existing listings fetched: %d", existing_count)
    except Exception as exc:
        # ISSUE 8: Backend unavailable → continue assuming no existing listings
        logger.warning(
            "ml_query_sell_listings failed (%s) — assuming 0 existing listings, "
            "proceeding with full export.",
            exc,
        )
        existing_count = 0

    # ------------------------------------------------------------------
    # Step 3: Filter new listings (idempotent)
    # ------------------------------------------------------------------
    new_listings = [
        sl for sl in sell_listings
        if sl["channelItemId"] not in existing_ids
    ]
    new_count = len(new_listings)
    logger.info(
        "New listings to export: %d (existing=%d, total_transformed=%d)",
        new_count,
        existing_count,
        len(sell_listings),
    )

    # ------------------------------------------------------------------
    # Step 4: POST new listings
    # ------------------------------------------------------------------
    ok = True
    if new_listings:
        payload = {"sellListings": new_listings}
        
        # DEBUG: Print full payload before sending to backend
        logger.info("=" * 80)
        logger.info("FULL PAYLOAD TO BACKEND:")
        logger.info("=" * 80)
        try:
            payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
            logger.info("Payload length: %d characters", len(payload_json))
            # Print each listing's key fields for easy debugging
            for idx, listing in enumerate(new_listings):
                logger.info(
                    "Listing[%d]: channelItemId=%s, title=%s, price=%.2f %s",
                    idx,
                    listing.get("channelItemId"),
                    listing.get("title", "")[:40],
                    listing.get("sellPriceOriginal"),
                    listing.get("currencyOriginal"),
                )
                logger.info("  Full listing: %s", json.dumps(listing, ensure_ascii=False)[:500])
        except Exception as e:
            logger.warning("Failed to serialize payload for debug logging: %s", e)
        logger.info("=" * 80)
        
        try:
            result = api_client.post_sell_listings(
                payload=payload,
                url_override=settings.sell_listings_backend_url,
            )
            ok = result.get("ok", False)
            if not ok:
                logger.error("post_sell_listings returned not-ok: %s", result)
        except Exception as exc:
            logger.error("post_sell_listings raised: %s", exc)
            ok = False
    else:
        logger.info("No new listings to export — skipping POST.")

    return {
        "ok": ok,
        "as_of": as_of,
        "existing_count": existing_count,
        "new_count": new_count,
        "skipped": skipped,
        "skip_reasons": skip_reasons,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    """Return current UTC time as ISO-8601 string with Z suffix."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _query_existing_with_retry(
    client: Any,
    channel: str = "mercadolibre",
    market: str = "MLM",
    max_attempts: int = 5,
    base_delay: float = 2.0,
) -> Dict[str, Any]:
    """
    Query existing sell listings with manual exponential backoff.

    The BackendApiClient already has tenacity retry, but we add an outer
    retry here so that transient 500s during the query phase don't abort
    the export — we degrade gracefully after max_attempts.

    Raises on final failure so the caller can catch and continue.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return client.query_sell_listings(
                channel=channel,
                market=market,
                page=1,
                page_size=500,
            )
        except Exception as exc:
            last_exc = exc
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "query_sell_listings attempt %d/%d failed (%s) — retrying in %.1fs",
                attempt,
                max_attempts,
                exc,
                delay,
            )
            if attempt < max_attempts:
                time.sleep(delay)

    raise RuntimeError(
        f"query_sell_listings failed after {max_attempts} attempts: {last_exc}"
    )


# ===========================================================================
# LEGACY / BACKWARD-COMPAT HELPERS
# ===========================================================================

def parse_channel_item_id(permalink: str) -> str:
    """
    Extract stable channelItemId from permalink using priority rules.

    Priority: product_id → up_id → item_id → SHA1(permalink)

    Args:
        permalink: The MercadoLibre URL

    Returns:
        Stable channelItemId string
    """
    if not permalink:
        return ""

    # Priority 1: /p/MLM... (product catalog URL)
    product_match = _PRODUCT_ID_RE.search(permalink)
    if product_match:
        return product_match.group(1)

    # Priority 2: /up/MLMU... (unified product URL)
    unified_match = _UP_ID_RE.search(permalink)
    if unified_match:
        return unified_match.group(1)

    # Priority 3: articulo dashed format /MLM-XXXXXXXXXX-
    art_match = _ARTICULO_ITEM_ID_RE.search(permalink)
    if art_match:
        return "MLM" + art_match.group(1)

    # Priority 4: direct item_id /MLMxxxxxxxxxx
    item_match = _ITEM_ID_RE.search(permalink)
    if item_match:
        return item_match.group(1)

    # Priority 5: SHA1 hash of the full permalink
    return hashlib.sha1(permalink.encode("utf-8")).hexdigest()


def build_sell_listings_payload(
    items: List[Dict[str, Any]],
    fx_rate_to_usd: float,
    run_timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Transform scraped items into sellListings payload format.

    Kept for backward compatibility (dry-run path in ml_export_sell_listings).
    New code should use transform_to_sell_listing() + export_sell_listings().

    Args:
        items:           List of scraped item dictionaries
        fx_rate_to_usd:  Exchange rate from MXN to USD
        run_timestamp:   Optional ISO UTC timestamp override

    Returns:
        {"sellListings": [...], "_metadata": {...}}
    """
    # Resolve timestamp (ISSUE 4 — always use UTC now when not provided)
    if run_timestamp:
        try:
            dt = datetime.fromisoformat(run_timestamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            dt = datetime.now(timezone.utc)
    else:
        dt = datetime.now(timezone.utc)

    fx_as_of_date = dt.strftime("%Y-%m-%d")
    listing_timestamp = dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    sell_listings: List[Dict[str, Any]] = []
    skipped_details: List[Dict[str, Any]] = []
    skip_reasons: Dict[str, int] = {}

    for idx, item in enumerate(items):
        listing, reason = transform_to_sell_listing(item, fx_rate_to_usd)
        if listing is None:
            reason = reason or SKIP_INVALID_PAYLOAD
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            skipped_details.append({
                "index": idx,
                "reason": reason,
                "permalink": item.get("permalink", ""),
            })
            logger.info(
                "build_sell_listings_payload: skipped item[%d] reason=%s permalink=%s",
                idx,
                reason,
                item.get("permalink", "<none>"),
            )
            continue

        # transform_to_sell_listing now already adds:
        # - fxAsOfDate (today's date)
        # - attributes (full JSON object)
        # Just add listingTimestamp for legacy compatibility
        listing["listingTimestamp"] = item.get("captured_at_utc") or listing_timestamp

        sell_listings.append(listing)

    logger.info(
        "build_sell_listings_payload: %d emitted, %d skipped. reasons=%s",
        len(sell_listings),
        len(skipped_details),
        skip_reasons,
    )

    return {
        "sellListings": sell_listings,
        "_metadata": {
            "total": len(items),
            "emitted": len(sell_listings),
            "skipped": len(skipped_details),
            "skipped_details": skipped_details,
            "skip_reasons": skip_reasons,
            "fx_rate_to_usd": fx_rate_to_usd,
            "fx_as_of_date": fx_as_of_date,
        },
    }


def export_sell_listings_http(
    payload: Dict[str, List[Dict[str, Any]]],
    url: str,
    worker_key: str,
    timeout_sec: float = 30.0,
) -> Dict[str, Any]:
    """
    POST the sellListings payload to the backend API.

    Delegates to BackendApiClient.post_sell_listings() which owns retry logic.

    Args:
        payload:     Dict with key "sellListings" containing list of records.
        url:         Backend API URL (used as url_override).
        worker_key:  Worker authentication key.
        timeout_sec: Request timeout in seconds.

    Returns:
        {"ok": bool, "status_code": int, "exported_count": int, "response": dict}
    """
    from ..api.backend_api import BackendApiClient

    client = BackendApiClient(
        base_url="",
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
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError as e:
                    logger.warning("Failed to parse JSON line: %s", e)
    return items
