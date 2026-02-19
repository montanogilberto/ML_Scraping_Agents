import json
from datetime import datetime, timezone
from typing import Any, Dict, List
from pydantic import TypeAdapter
from google.adk.tools.function_tool import FunctionTool
from ...config.settings import load_settings
from ...models.schemas import NormalizedItem, SellerRef, ListingCard
from .http_client import HttpClient
from .parsers import (
    parse_list_page, parse_next_url, parse_item_page, 
    seller_list_url, seller_list_url_v2, seller_category_url,
    extract_cards_from_listing_html,
    validate_and_filter_cards,
    compute_card_stats,
    resolve_click_tracker_url,
    is_click_tracker_url
)

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
        "cards_filtered_out": 0
    }
    
    for _ in range(max_pages):
        html = _client.get_html(url)
        
        # Use new robust card extraction
        raw_cards = extract_cards_from_listing_html(html)
        
        # Validate and filter cards
        filtered_cards, card_stats = validate_and_filter_cards(raw_cards, category_url)
        
        # Aggregate stats
        all_stats["cards_total"] += card_stats["total"]
        all_stats["cards_valid"] += card_stats["valid"]
        all_stats["cards_missing_title"] += card_stats["missing_title"]
        all_stats["cards_missing_id"] += card_stats["missing_item_id"]
        all_stats["cards_filtered_out"] += card_stats["filtered_out"]
        
        all_cards.extend(filtered_cards)
        
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
    cards_out = TypeAdapter(List[ListingCard]).validate_python(list(cards_uniq.values()))
    sellers_out = TypeAdapter(List[SellerRef]).validate_python(list(sellers.values()))
    
    # Recompute final stats after dedup
    final_stats = compute_card_stats(list(cards_uniq.values()))
    
    return {
        "category_url": category_url,
        "cards": [c.model_dump() for c in cards_out],
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
        "cards_missing_title": 0,
        "cards_missing_id": 0,
        "cards_filtered_out": 0,
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
                
                # Re-extract item_id from resolved URL
                if card.get("item_id") is None:
                    # Re-parse the resolved URL for item_id
                    item_id, product_id, needs_enrichment = _extract_item_id_from_url(resolved_url)
                    card["item_id"] = item_id
                    card["product_id"] = product_id
                    card["needs_enrichment"] = needs_enrichment
        
        # Validate and filter cards
        filtered_cards, card_stats = validate_and_filter_cards(raw_cards, primary_url)
        
        # Aggregate stats
        all_stats["cards_total"] += card_stats["total"]
        all_stats["cards_valid"] += card_stats["valid"]
        all_stats["cards_missing_title"] += card_stats["missing_title"]
        all_stats["cards_missing_id"] += card_stats["missing_item_id"]
        all_stats["cards_filtered_out"] += card_stats["filtered_out"]
        
        # Add seller_id to each card
        for c in filtered_cards:
            c["seller_id"] = seller_id
        
        out.extend(filtered_cards)
        nxt = parse_next_url(html)
        if not nxt:
            break
        url = nxt
    
    # Deduplicate by permalink
    cards_uniq = {c["permalink"]: c for c in out}
    card_count = len(cards_uniq)
    
    # Limit cards to max_cards (avoid context overflow)
    cards_limited = list(cards_uniq.values())[:max_cards]
    
    # Return sample_permalink for backward compatibility + limited cards
    sample_permalink = cards_limited[0]["permalink"] if cards_limited else None
    
    # Recompute final stats after dedup
    final_stats = compute_card_stats(cards_limited)
    final_stats["cards_click_tracker_resolved"] = all_stats["cards_click_tracker_resolved"]
    
    return {
        "seller_id": seller_id, 
        "seller_url": primary_url, 
        "card_count": card_count, 
        "cards": cards_limited,
        "sample_permalink": sample_permalink,
        "stats": final_stats
    }


def _extract_item_id_from_url(url: str):
    """Helper to extract item_id from URL - used in click tracker resolution."""
    import re
    ITEM_ID_RE = re.compile(r"(MLM\d{6,15})")
    PRODUCT_ID_RE = re.compile(r"/p/(MLM\d+)")
    
    if not url:
        return None, None, False
    
    # Check for /p/MLMxxxx (product catalog URL)
    product_match = PRODUCT_ID_RE.search(url)
    if product_match:
        product_id = product_match.group(1)
        if "/p/" in url:
            return None, product_id, True
        item_match = ITEM_ID_RE.search(url)
        if item_match:
            return item_match.group(1), product_id, False
        return None, product_id, True
    
    # Check for standard listing URL /MLMxxxx
    item_match = ITEM_ID_RE.search(url)
    if item_match:
        return item_match.group(1), None, False
    
    return None, None, False

@FunctionTool
def ml_scrape_item_detail(url: str) -> Dict[str, Any]:
    html=_client.get_html(url)
    item=parse_item_page(html,url)
    out=TypeAdapter(NormalizedItem).validate_python(item)
    return out.model_dump()

@FunctionTool
def ml_persist_items(items: List[Dict[str, Any]], mode: str = "file") -> Dict[str, Any]:
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
