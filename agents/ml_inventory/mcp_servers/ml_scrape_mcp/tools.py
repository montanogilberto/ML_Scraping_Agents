import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import TypeAdapter
from google.adk.tools.function_tool import FunctionTool
from ...config.settings import load_settings
from ...models.schemas import NormalizedItem, SellerRef, ListingCard
from .http_client import HttpClient
from .parsers import (
    parse_list_page, parse_next_url, parse_item_page,
    seller_list_url, seller_list_url_v2, seller_category_url,
    extract_cards_from_listing_html,
    resolve_click_tracker_url,
    is_click_tracker_url,
    # Three-layer decision architecture
    extract_ids,
    compute_channel_item_id,
    classify_filter,
    compute_needs_enrichment,
    assemble_card,
    compute_card_stats_v2,
)

logger = logging.getLogger(__name__)

def now_utc():
    """Generate UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

_settings=load_settings()
_client=HttpClient(timeout_sec=_settings.http_timeout_sec, min_delay_sec=_settings.min_delay_sec, jitter_sec=_settings.jitter_sec)

@FunctionTool
def ml_scrape_category(category_url: str, max_pages: int = 3) -> Dict[str, Any]:
    url = category_url
    all_cards = []
    sellers = {}
    all_stats = {
        "cards_total": 0,
        "cards_valid": 0,
        "cards_missing_title": 0,
        "cards_missing_id": 0,
        "cards_filtered_out": 0,
        "cards_needs_enrichment": 0,
        "cards_ready": 0
    }
    
    for _ in range(max_pages):
        html = _client.get_html(url)
        
        # Use new robust card extraction
        raw_cards = extract_cards_from_listing_html(html)
        
        # Process each card with the new 3-layer architecture
        processed_cards = []
        for card in raw_cards:
            # Use assemble_card for full processing
            processed_card = assemble_card(
                permalink=card.get("permalink", ""),
                title=card.get("title", ""),
                price_mxn=card.get("price_mxn"),
                currency=card.get("currency", "MXN"),
                seller_id=card.get("seller_id"),
                allow_refurbished=False,  # Default: filter out refurbished
                allow_bundles=False,       # Default: filter out bundles
                allow_locked=False         # Default: filter out locked phones
            )
            processed_cards.append(processed_card)
        
        # Aggregate stats
        page_stats = compute_card_stats_v2(processed_cards)
        all_stats["cards_total"] += page_stats["total"]
        all_stats["cards_valid"] += page_stats["valid"]
        all_stats["cards_filtered_out"] += page_stats["filtered_out"]
        all_stats["cards_needs_enrichment"] += page_stats["needs_enrichment"]
        all_stats["cards_ready"] += page_stats["ready"]
        
        all_cards.extend(processed_cards)
        
        # Extract sellers using legacy parser (for backward compat)
        _, seller_refs = parse_list_page(html, source_url=url)
        for s in seller_refs:
            sellers[int(s["seller_id"])] = s
        
        nxt = parse_next_url(html)
        if not nxt:
            break
        url = nxt
    
    # Deduplicate by permalink
    cards_uniq = {c["permalink"]: c for c in all_cards}
    all_cards_list = list(cards_uniq.values())

    # Recompute final stats after dedup (covers all cards including filtered)
    final_stats = compute_card_stats_v2(all_cards_list)

    # Validate all cards through Pydantic
    cards_out = TypeAdapter(List[ListingCard]).validate_python(all_cards_list)
    sellers_out = TypeAdapter(List[SellerRef]).validate_python(list(sellers.values()))

    # Return ONLY valid cards (filtered_out=False) in the cards array.
    # This includes catalog products (item_id=null, product_id=MLM...) which are
    # valid listings that need enrichment — they must NOT be excluded.
    # filtered_out=True cards (refurbished, accessories, etc.) are excluded here;
    # their counts are still visible in stats.filtered_out.
    valid_cards = [c for c in cards_out if not c.filtered_out]

    return {
        "category_url": category_url,
        "cards": [c.model_dump() for c in valid_cards],
        "sellers": [s.model_dump() for s in sellers_out],
        "stats": final_stats
    }

@FunctionTool
def ml_scrape_seller_inventory(
    seller_id: int, 
    max_pages: int = 5,
    seller_listing_url: str = None,
    category_id: str = "AD",
    max_cards: int = 20
) -> Dict[str, Any]:
    """
    Scrape seller inventory with optional category scoping.
    
    Args:
        seller_id: The seller ID to scrape
        max_pages: Maximum pages to scrape per seller
        seller_listing_url: Optional explicit URL to scrape. If provided, uses this URL.
                          If not provided, uses category-scoped URL: _CustId_{seller_id}_PrCategId_{category_id}
        category_id: Category code for scoping. "AD" = all categories. 
                    Common: "MLM1953" (electronics), "MLM1499" (computing), etc.
        max_cards: Maximum cards to return (for context limits). Default 20.
    
    Returns:
        Dictionary with seller_id, seller_url, cards (limited), sample_permalink, stats
    """
    # Determine the URL to scrape
    if seller_listing_url:
        # Use explicitly provided URL
        primary_url = seller_listing_url
        fallback_url = seller_list_url_v2(seller_id) if "/tienda/" not in seller_listing_url else None
    else:
        # Use category-scoped URL pattern (NEW DEFAULT - fixes irrelevant items issue)
        primary_url = seller_category_url(seller_id, category_id)
        # Also try the /tienda/ pattern as fallback
        fallback_url = seller_list_url(seller_id)
    
    url = primary_url
    out = []
    all_stats = {
        "cards_total": 0,
        "cards_valid": 0,
        "cards_filtered_out": 0,
        "cards_needs_enrichment": 0,
        "cards_ready": 0,
        "cards_click_tracker_resolved": 0
    }
    
    for _ in range(max_pages):
        try:
            # Try primary URL first, fallback if 404
            html = _client.get_html_with_fallback(url, [fallback_url] if fallback_url else [])
        except Exception as e:
            # If all URLs fail, return empty result with error info
            return {
                "seller_id": seller_id, 
                "seller_url": primary_url, 
                "card_count": 0, 
                "cards": [],
                "sample_permalink": None, 
                "error": str(e),
                "stats": all_stats
            }
        
        # Use new robust card extraction
        raw_cards = extract_cards_from_listing_html(html)
        
        # Resolve click-tracker URLs and re-extract item_ids if needed
        for card in raw_cards:
            if is_click_tracker_url(card.get("permalink", "")):
                resolved_url = resolve_click_tracker_url(card["permalink"])
                card["original_url"] = card["permalink"]
                card["permalink"] = resolved_url
                all_stats["cards_click_tracker_resolved"] += 1
        
        # Process each card with the new 3-layer architecture
        # Pass seller_id since we know it from the context
        processed_cards = []
        for card in raw_cards:
            processed_card = assemble_card(
                permalink=card.get("permalink", ""),
                title=card.get("title", ""),
                price_mxn=card.get("price_mxn"),
                currency=card.get("currency", "MXN"),
                seller_id=seller_id,  # We know the seller from the scrape context
                allow_refurbished=False,  # Default: filter out refurbished
                allow_bundles=False,       # Default: filter out bundles
                allow_locked=False         # Default: filter out locked phones
            )
            processed_cards.append(processed_card)
        
        # Aggregate stats
        page_stats = compute_card_stats_v2(processed_cards)
        all_stats["cards_total"] += page_stats["total"]
        all_stats["cards_valid"] += page_stats["valid"]
        all_stats["cards_filtered_out"] += page_stats["filtered_out"]
        all_stats["cards_needs_enrichment"] += page_stats["needs_enrichment"]
        all_stats["cards_ready"] += page_stats["ready"]
        
        out.extend(processed_cards)
        nxt = parse_next_url(html)
        if not nxt:
            break
        url = nxt
    
    # Deduplicate by permalink
    cards_uniq = {c["permalink"]: c for c in out}
    card_count = len(cards_uniq)
    all_cards_list = list(cards_uniq.values())

    # Recompute final stats after dedup (covers all cards including filtered)
    final_stats = compute_card_stats_v2(all_cards_list)
    final_stats["cards_click_tracker_resolved"] = all_stats["cards_click_tracker_resolved"]

    # Return ONLY valid cards (filtered_out=False), limited to max_cards.
    # Catalog products (item_id=null, product_id=MLM...) are valid and included.
    valid_cards = [c for c in all_cards_list if not c.get("filtered_out", False)]
    cards_limited = valid_cards[:max_cards]

    # sample_permalink: first valid card's URL (used by ItemExtractor)
    sample_permalink = cards_limited[0]["permalink"] if cards_limited else None

    return {
        "seller_id": seller_id,
        "seller_url": primary_url,
        "card_count": card_count,
        "cards": cards_limited,
        "sample_permalink": sample_permalink,
        "stats": final_stats
    }


@FunctionTool
def ml_scrape_item_detail(url: str) -> Dict[str, Any]:
    """
    Scrape an item detail page and return a NormalizedItem dict.

    Supports all MercadoLibre URL types:
      - articulo.mercadolibre.com.mx/MLM-XXXXXXXX  (item_id)
      - mercadolibre.com.mx/.../p/MLMxxxxxxxx       (product_id / catalog)
      - mercadolibre.com.mx/.../up/MLMUxxxxxxxx     (up_id / unified product)

    Always returns a dict — never raises.  On failure, returns:
      {"ok": False, "error": "<message>", "url": "<url>", "permalink": "<url>"}
    so the caller can log the error and continue processing other items.
    """
    import logging as _logging
    import re as _re
    _log = _logging.getLogger("ml_scrape_item_detail")

    # Normalise LLM-mangled URLs where "://" was dropped and replaced with ".".
    # e.g. "https.mercadolibre.com.mx/..." → "https://mercadolibre.com.mx/..."
    url = _re.sub(r'^(https?)\.', r'\1://', url)

    try:
        html = _client.get_html(url)
        item = parse_item_page(html, url)
        out = TypeAdapter(NormalizedItem).validate_python(item)
        return out.model_dump()
    except Exception as exc:
        _log.warning("ml_scrape_item_detail failed for %s: %s", url, exc)
        return {
            "ok": False,
            "error": str(exc),
            "url": url,
            "permalink": url,
        }

@FunctionTool
def ml_persist_items(items: List[Dict[str, Any]], mode: str = "") -> Dict[str, Any]:
    # Add captured_at_utc if missing from any item
    timestamp = now_utc()
    for item in items:
        if "captured_at_utc" not in item or not item["captured_at_utc"]:
            item["captured_at_utc"] = timestamp
    
    settings=load_settings()
    mode=mode or settings.persist_mode
    norm=TypeAdapter(List[NormalizedItem]).validate_python(items)
    if mode=="stdout":
        for it in norm: print(json.dumps(it.model_dump(), ensure_ascii=False))
        return {"ok":True,"mode":"stdout","count":len(norm)}
    if mode=="file":
        with open(settings.out_ndjson,"a",encoding="utf-8") as f:
            for it in norm: f.write(json.dumps(it.model_dump(), ensure_ascii=False)+"\n")
        return {"ok":True,"mode":"file","path":settings.out_ndjson,"count":len(norm)}
    if mode=="backend":
        import requests
        if not settings.backend_base_url or not settings.backend_worker_key:
            return {"ok":False,"error":"Missing BACKEND_BASE_URL/BACKEND_WORKER_KEY"}
        r=requests.post(
            f"{settings.backend_base_url}/scrape/items/batch",
            headers={"accept":"application/json","content-type":"application/json","X-Worker-Key":settings.backend_worker_key},
            json={"items":[it.model_dump() for it in norm]},
            timeout=settings.http_timeout_sec
        )
        return {"ok":r.ok,"status_code":r.status_code,"body":(r.text[:1000] if r.text else ""), "count":len(norm)}
    return {"ok":False,"error":f"Unknown mode: {mode}"}


@FunctionTool
def ml_get_all_sell_listings() -> Dict[str, Any]:
    """
    Fetch every sell listing stored in the backend database.

    Use this to validate whether scraped products already exist in the DB
    before exporting new listings.

    Calls: GET /all_sellListings

    Returns:
        {
            "ok": bool,
            "total": int,
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
                    "updatedAt": str
                },
                ...
            ]
        }
    """
    from ...api.backend_api import BackendApiClient

    settings = load_settings()
    client = BackendApiClient(
        base_url=settings.backend_base_url,
        worker_key=settings.backend_worker_key,
        timeout_sec=settings.http_timeout_sec,
    )
    try:
        data = client.get_all_sell_listings()
        listings = data.get("sellListings", [])
        return {
            "ok": True,
            "total": len(listings),
            "sellListings": listings,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "sellListings": []}


@FunctionTool
def ml_query_sell_listings(
    channel: str,
    market: str,
    page: int = 1,
    page_size: int = 50,
) -> Dict[str, Any]:
    """
    Query sell listings from the backend database with channel/market filters
    and pagination.

    Use this to check whether specific scraped products (by channel + market)
    already exist in the DB before exporting.

    Calls: POST /query_sellListings

    Args:
        channel:   Sales channel, e.g. "mercadolibre", "amazon"
        market:    Market code, e.g. "MX", "US"
        page:      Page number (1-based). Default: 1
        page_size: Records per page. Default: 50

    Returns:
        {
            "ok": bool,
            "total_rows": int,
            "page": int,
            "page_size": int,
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
                    "updatedAt": str
                },
                ...
            ]
        }
    """
    from ...api.backend_api import BackendApiClient

    settings = load_settings()
    client = BackendApiClient(
        base_url=settings.backend_base_url,
        worker_key=settings.backend_worker_key,
        timeout_sec=settings.http_timeout_sec,
    )
    try:
        data = client.query_sell_listings(
            channel=channel,
            market=market,
            page=page,
            page_size=page_size,
        )
        rows = data.get("sellListingsQuery", [])
        total_rows = rows[0].get("totalRows", len(rows)) if rows else 0
        return {
            "ok": True,
            "total_rows": total_rows,
            "page": page,
            "page_size": page_size,
            "sellListingsQuery": rows,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "sellListingsQuery": []}


@FunctionTool
def ml_export_sell_listings(
    items: Optional[List[Dict[str, Any]]] = None,
    path: str = "out.ndjson",
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Export scraped items to the sellListings backend API.

    Accepts items either directly (in-memory list) or from an NDJSON file.
    Transforms MercadoLibre items into the format required by the backend SQL
    Server stored procedure sp_sellListings (UPSERT action).

    Supports all three MercadoLibre identity types:
      - product_id  (/p/MLMxxxxx  — catalog product pages)
      - up_id       (/up/MLMUxxxx — unified product pages)
      - item_id     (articulo listing pages)

    Args:
        items:   Optional list of scraped item dicts from the pipeline
                 (e.g. items_raw.items). When provided, `path` is ignored.
        path:    Path to the NDJSON file to read when `items` is not provided.
                 Default: "out.ndjson"
        dry_run: If True, only transform and return a preview without POSTing.
                 Default: False (perform actual POST)

    Returns:
        dry_run=True  → payload preview with skip_reasons
        dry_run=False → {ok, as_of, existing_count, new_count, skipped, skip_reasons}
    """
    from ...export.export_sell_listings import (
        read_ndjson,
        build_sell_listings_payload,
        export_sell_listings as _export_sell_listings,
    )

    settings = load_settings()

    # ------------------------------------------------------------------
    # Resolve items: prefer in-memory list, fall back to NDJSON file
    # ------------------------------------------------------------------
    # DEBUG: Log what we're receiving
    logger.info(f"ml_export_sell_listings called with items={items is not None}, path={path}")
    
    if items is not None:
        logger.info(f"Received items list with {len(items)} items")
        # Log first item as sample
        if items:
            logger.info(f"Sample item keys: {list(items[0].keys()) if items else 'empty'}")
            logger.info(f"Sample item: {json.dumps(items[0], ensure_ascii=False)[:500]}...")
        if not items:
            return {
                "ok": True,
                "as_of": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "skipped": True,
                "reason": "no items to export",
                "new_count": 0,
                "existing_count": 0,
                "skipped_count": 0,
                "skip_reasons": {},
            }
    else:
        try:
            items = read_ndjson(path)
        except FileNotFoundError:
            return {"ok": False, "error": f"File not found: {path}"}
        except Exception as e:
            return {"ok": False, "error": f"Failed to read NDJSON: {str(e)}"}

        if not items:
            return {"ok": False, "error": "No items found in input file"}

    # ------------------------------------------------------------------
    # Dry-run: transform only, return preview (no HTTP POST)
    # ------------------------------------------------------------------
    if dry_run:
        payload_result = build_sell_listings_payload(
            items=items,
            fx_rate_to_usd=settings.fx_rate_to_usd,
        )
        metadata = payload_result.get("_metadata", {})
        sell_listings = payload_result.get("sellListings", [])
        preview = sell_listings[:2] if len(sell_listings) > 2 else sell_listings
        return {
            "ok": True,
            "dry_run": True,
            "total_items": metadata.get("total", 0),
            "emitted": metadata.get("emitted", 0),
            "skipped": metadata.get("skipped", 0),
            "skip_reasons": metadata.get("skip_reasons", {}),
            "fx_rate_to_usd": metadata.get("fx_rate_to_usd"),
            "fx_as_of_date": metadata.get("fx_as_of_date"),
            "backend_url": settings.sell_listings_backend_url,
            "preview": preview,
            "note": "Dry run — no data sent to backend",
        }

    # ------------------------------------------------------------------
    # Live export: delegate to export_sell_listings(cards) which handles
    # transformation, identity resolution, needs_enrichment check,
    # existing-listing query (with retry + graceful degradation on 500),
    # deduplication, and POST with exponential backoff.
    # ------------------------------------------------------------------
    logger.info("=" * 80)
    logger.info("ML_EXPORT_SELL_LISTINGS - Starting export with %d items", len(items))
    logger.info("=" * 80)
    
    try:
        result = _export_sell_listings(cards=items)
        
        # DEBUG: Log the result
        logger.info("Export result: ok=%s, new_count=%d, existing_count=%d, skipped=%d",
            result.get("ok"),
            result.get("new_count", 0),
            result.get("existing_count", 0),
            result.get("skipped", 0)
        )
        logger.info("Skip reasons: %s", result.get("skip_reasons", {}))
        logger.info("=" * 80)
        
        return {
            "ok": result.get("ok", False),
            "as_of": result.get("as_of"),
            "existing_count": result.get("existing_count", 0),
            "new_count": result.get("new_count", 0),
            "skipped": result.get("skipped", 0),
            "skip_reasons": result.get("skip_reasons", {}),
        }
    except Exception as exc:
        logger.error("Export exception: %s", exc)
        logger.info("=" * 80)
        return {
            "ok": False,
            "error": f"Export failed: {exc}",
            "as_of": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "new_count": 0,
            "existing_count": 0,
            "skipped": len(items),
            "skip_reasons": {"export_exception": len(items)},
        }
