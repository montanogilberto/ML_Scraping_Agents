import json, re
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from typing import List, Dict, Any, Optional, Tuple

# ========== REGEX PATTERNS ==========
# Match item_id: MLM followed by 6-15 digits (standard listing ID) - with capture group
ITEM_ID_RE = re.compile(r"(MLM\d{6,15})")
# Match product_id in /p/MLMxxxx URLs (product catalog)
PRODUCT_ID_RE = re.compile(r"/p/(MLM\d+)")
# Match seller ID in various URL patterns
SELLER_CUSTID_RE = re.compile(r"_CustId_(\d+)")
SELLER_TIENDA_RE = re.compile(r"/tienda/(\d+)")

# ========== KEYWORD ALLOWLIST FOR TERMINAL-POS CATEGORIES ==========
RELEVANCE_KEYWORDS = [
    "terminal", "pos", "punto de venta", "punto de", "lector", 
    "lectores", "clip", "point", "scanner", "escaner", "impresora", 
    "ticket", "caja", "registradora", "balanza", "bascula", 
    "datafono", "tpv", "afip", "fiscal", "impresora fiscal",
    "terminal pago", "pago con tarjeta", "teclado", "monitor", 
    "touch", "pantalla tactil", "gaveta", "cajon", "money",
    "cash", "drawer", "barcode", "codigo de barras", "rfid",
    "biometrico", "huella", "face", "reconocimiento", "acceso",
    "control acceso", "visita", "asistencia", "reloj checador"
]

def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

# ========== HELPER FUNCTIONS ==========

def extract_item_id_from_url(url: str) -> Tuple[Optional[str], Optional[str], bool]:
    """
    Extract item_id and/or product_id from URL.
    
    Returns: (item_id, product_id, needs_enrichment)
    - item_id: Standard listing ID (MLM########) if found in URL path
    - product_id: Product catalog ID (MLM########) if found in /p/ path  
    - needs_enrichment: True if only product_id available (need detail scrape)
    """
    if not url:
        return None, None, False
    
    # First check for /p/MLMxxxx (product catalog URL)
    product_match = PRODUCT_ID_RE.search(url)
    if product_match:
        product_id = product_match.group(1)
        
        # If URL contains /p/, we should NOT also extract item_id from same URL
        # because /p/ URLs are product catalog pages, not listing pages
        # The item_id in the path is just for display, not the actual listing
        if "/p/" in url:
            return None, product_id, True
        
        # For other URLs that happen to have both patterns, check both
        item_match = ITEM_ID_RE.search(url)
        if item_match:
            return item_match.group(1), product_id, False
        
        return None, product_id, True
    
    # Check for standard listing URL /MLMxxxx (without /p/)
    item_match = ITEM_ID_RE.search(url)
    if item_match:
        return item_match.group(1), None, False
    
    # Check for wid parameter (sometimes used in ML URLs)
    try:
        wid = parse_qs(urlparse(url).query).get("wid", [None])[0]
        if wid and ITEM_ID_RE.match(wid):
            return wid, None, False
    except Exception:
        pass
    
    return None, None, False


def extract_seller_id_from_url(url: str) -> Optional[int]:
    """Extract seller_id from various URL patterns."""
    if not url:
        return None
    
    # Try /tienda/123456 pattern
    tienda_match = SELLER_TIENDA_RE.search(url)
    if tienda_match:
        return int(tienda_match.group(1))
    
    # Try _CustId_123456 pattern (legacy)
    custid_match = SELLER_CUSTID_RE.search(url)
    if custid_match:
        return int(custid_match.group(1))
    
    return None


def extract_title_from_card(card_soup, href: str) -> str:
    """
    Extract title from card element with multiple fallbacks.
    """
    # Method 1: Look for h2.ui-search-item__title
    h2 = card_soup.select_one("h2.ui-search-item__title")
    if h2:
        title = h2.get_text(strip=True)
        if title and len(title) >= 3:
            return title
    
    # Method 2: Look for h2 in various positions
    h2_any = card_soup.select_one("h2")
    if h2_any:
        title = h2_any.get_text(strip=True)
        if title and len(title) >= 3:
            return title
    
    # Method 3: Look for a[title] attribute
    link = card_soup.select_one("a[title]")
    if link:
        title = link.get("title", "")
        if title and len(title) >= 3:
            return title
    
    # Method 4: Look for img[alt]
    img = card_soup.select_one("img[alt]")
    if img:
        title = img.get("alt", "")
        if title and len(title) >= 3:
            return title
    
    # Method 5: Look for data attributes
    for attr in ["data-title", "data-item-title", "item-title"]:
        val = card_soup.get(attr, "")
        if val and len(val) >= 3:
            return val
    
    # Method 6: Try to get from the link's href as fallback
    if href:
        # Sometimes title is in URL path
        parsed = urlparse(href)
        path_parts = parsed.path.strip("/").split("/")
        if path_parts:
            # Last part might be the title slug
            last_part = path_parts[-1].replace("-", " ").replace("_", " ")
            if last_part and len(last_part) >= 3:
                return last_part
    
    # Method 7: Get from link text as last resort
    link = card_soup.select_one("a")
    if link:
        title = link.get_text(strip=True)
        if title and len(title) >= 3:
            return title
    
    return ""


def extract_price_from_card(card_soup) -> Optional[float]:
    """Extract price from card element."""
    # Try various price selectors
    price_selectors = [
        "span.price-tag-fraction",
        "span.andes-money-amount__fraction",
        "div.ui-price__part--integer span.andes-money-amount__fraction",
        "span.ui-search-price__part--fraction",
        "[data-price]"
    ]
    
    for selector in price_selectors:
        el = card_soup.select_one(selector)
        if el:
            price_text = el.get_text(strip=True).replace(",", "")
            try:
                return float(price_text)
            except ValueError:
                continue
    
    return None


def is_relevant_card(title: str, category_url: str = "") -> bool:
    """
    Check if card is relevant based on keyword allowlist.
    For terminal-pos categories, filter out irrelevant items.
    """
    if not title:
        return True  # Keep items without title for now, filter later
    
    title_lower = title.lower()
    
    # Check against keyword allowlist
    for keyword in RELEVANCE_KEYWORDS:
        if keyword in title_lower:
            return True
    
    # If no keywords matched, it's likely irrelevant
    return False


# ========== MAIN PARSING FUNCTIONS ==========

def extract_cards_from_listing_html(html: str) -> List[Dict[str, Any]]:
    """
    Extract product cards from MercadoLibre listing page HTML.
    
    Uses strict selectors to avoid capturing ads, recommendations, etc.
    
    Returns list of card dictionaries with:
    - permalink: Product URL
    - title: Product title
    - item_id: Listing ID (MLM########) if available
    - product_id: Product catalog ID (MLM########) if /p/ URL
    - needs_enrichment: True if only product_id available
    - price_mxn: Price if available
    """
    soup = BeautifulSoup(html, "lxml")
    cards = []
    
    # Primary selector: li.ui-search-layout__item (modern ML layout)
    # Fallback: li[data-component="item"]
    card_elements = soup.select("li.ui-search-layout__item")
    
    if not card_elements:
        # Fallback to older layout
        card_elements = soup.select("li.ui-search-result")
    
    if not card_elements:
        # Fallback: any li with item data
        card_elements = soup.select("li.ui-search-result__item")
    
    for card in card_elements:
        # Find the main link in this card
        link = card.select_one("a[href*='mercadolibre.com.mx']")
        if not link:
            continue
        
        href = link.get("href", "")
        if not href:
            continue
        
        # Skip non-product URLs
        if "/tienda/" in href and "/tienda/" in href:
            continue  # Skip store pages
        if "/publi/" in href or "/advertising/" in href:
            continue  # Skip ads
        
        # Extract IDs
        item_id, product_id, needs_enrichment = extract_item_id_from_url(href)
        
        # Extract title with fallbacks
        title = extract_title_from_card(card, href)
        
        # Extract price
        price_mxn = extract_price_from_card(card)
        
        card_dict = {
            "permalink": href.split("#")[0],  # Remove fragment
            "title": title,
            "item_id": item_id,
            "product_id": product_id,
            "needs_enrichment": needs_enrichment,
            "price_mxn": price_mxn,
            "seller_id": None,  # Will be filled by caller
            "currency": "MXN",
            "filtered_out": False
        }
        
        cards.append(card_dict)
    
    # If no cards found with li selectors, try the old link-based approach
    # but with stricter filtering
    if not cards:
        cards = _fallback_extract_cards(soup)
    
    return cards


def _fallback_extract_cards(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """
    Fallback extraction using link scanning with strict filters.
    Used when standard card selectors fail.
    """
    cards = []
    seen_permalinks = set()
    
    # Only look at links within main content areas
    main_content = soup.select_one("#root-app, main.ui-search-main, div.ui-search-main")
    if main_content:
        links = main_content.select("a[href*='mercadolibre.com.mx']")
    else:
        links = soup.select("a[href*='mercadolibre.com.mx']")
    
    for link in links:
        href = link.get("href", "").split("#")[0]
        
        # Skip duplicates
        if href in seen_permalinks:
            continue
        seen_permalinks.add(href)
        
        # Skip non-product URLs
        if "/tienda/" in href:
            continue
        if "/_CustId_" in href:
            continue
        if "/publi/" in href or "/advertising/" in href:
            continue
        
        # Extract IDs - but only accept /p/ or /MLM patterns
        if "/p/" not in href and "/MLM" not in href:
            continue
        
        item_id, product_id, needs_enrichment = extract_item_id_from_url(href)
        
        # Skip if no valid ID at all
        if not item_id and not product_id:
            continue
        
        # Try to get title from link
        title = link.get("title", "")
        if not title:
            title = link.get_text(strip=True)
        
        if len(title) < 3:
            continue
        
        card_dict = {
            "permalink": href,
            "title": title,
            "item_id": item_id,
            "product_id": product_id,
            "needs_enrichment": needs_enrichment,
            "price_mxn": None,
            "seller_id": None,
            "currency": "MXN",
            "filtered_out": False
        }
        
        cards.append(card_dict)
    
    return cards


def validate_and_filter_cards(cards: List[Dict[str, Any]], category_url: str = "") -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Validate cards and filter based on relevance.
    
    Returns: (filtered_cards, stats_dict)
    """
    stats = {
        "total": len(cards),
        "valid": 0,
        "missing_title": 0,
        "missing_item_id": 0,
        "filtered_out": 0
    }
    
    filtered_cards = []
    
    for card in cards:
        # Check for missing title
        if not card.get("title") or len(card.get("title", "")) < 3:
            stats["missing_title"] += 1
        
        # Check for missing item_id (but product_id is OK if needs_enrichment)
        if not card.get("item_id") and not card.get("product_id"):
            stats["missing_item_id"] += 1
        
        # Check relevance - filter out irrelevant items
        if card.get("title") and not is_relevant_card(card.get("title", ""), category_url):
            card["filtered_out"] = True
            stats["filtered_out"] += 1
            filtered_cards.append(card)
        else:
            stats["valid"] += 1
            filtered_cards.append(card)
    
    return filtered_cards, stats


def compute_card_stats(cards: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Compute statistics from card list.
    """
    stats = {
        "total": len(cards),
        "valid": 0,
        "missing_title": 0,
        "missing_item_id": 0,
        "filtered_out": 0
    }
    
    for card in cards:
        if card.get("filtered_out"):
            stats["filtered_out"] += 1
        
        if not card.get("title") or len(card.get("title", "")) < 3:
            stats["missing_title"] += 1
        
        if not card.get("item_id") and not card.get("product_id"):
            stats["missing_item_id"] += 1
        
        if card.get("title") and (card.get("item_id") or card.get("product_id")) and not card.get("filtered_out"):
            stats["valid"] += 1
    
    return stats


# ========== LEGACY FUNCTIONS (for backward compatibility) ==========

def extract_item_id(url: str) -> Optional[str]:
    """Legacy function - returns item_id only."""
    item_id, _, _ = extract_item_id_from_url(url)
    return item_id


def seller_list_url(seller_id: int) -> str:
    """Primary URL pattern for seller stores (MercadoLibre) - UNSCOPED (entire store)"""
    return f"https://listado.mercadolibre.com.mx/tienda/{seller_id}"


def seller_list_url_v2(seller_id: int) -> str:
    """Alternative URL pattern using www domain"""
    return f"https://www.mercadolibre.com.mx/tienda/{seller_id}"


def seller_list_url_v3(seller_id: int) -> str:
    """Legacy fallback - no longer works but kept for reference"""
    return f"https://listado.mercadolibre.com.mx/_CustId_{seller_id}"


def seller_category_url(seller_id: int, category_id: str = "AD") -> str:
    """
    Category-scoped seller listing URL.
    
    This returns seller items ONLY within a specific category, avoiding irrelevant items
    from the seller's other inventory (auto parts, etc.).
    
    Args:
        seller_id: The seller ID (e.g., 1785384134)
        category_id: Category code. "AD" = all categories, or specific like "MLM1953" for electronics
        
    Returns:
        Category-scoped seller URL like: https://listado.mercadolibre.com.mx/_CustId_1785384134_PrCategId_AD
    """
    return f"https://listado.mercadolibre.com.mx/_CustId_{seller_id}_PrCategId_{category_id}"


# ========== CLICK TRACKER RESOLUTION ==========

def is_click_tracker_url(url: str) -> bool:
    """Check if URL is a MercadoLibre click tracker redirect URL."""
    if not url:
        return False
    return "click" in url.lower() and "mercadolibre" in url.lower()


def resolve_click_tracker_url(url: str, timeout: int = 10) -> str:
    """
    Resolve a click tracker URL to the final MercadoLibre URL.
    
    MercadoLibre uses click tracker URLs like:
    - https://click1.mercadolibre.com.mx/...
    - https://mercadoclickservices.mercadolibre.com.mx/...
    
    These are 302 redirects to the actual product URL.
    
    Args:
        url: The click tracker URL
        timeout: Request timeout in seconds
        
    Returns:
        The resolved final URL, or original URL if resolution fails
    """
    if not is_click_tracker_url(url):
        return url
    
    try:
        import requests
        # Use HEAD first (faster, no body), follow redirects
        response = requests.head(url, timeout=timeout, allow_redirects=True)
        if response.ok:
            return response.url
        # Fallback to GET if HEAD fails
        response = requests.get(url, timeout=timeout, allow_redirects=True, stream=True)
        if response.ok:
            return response.url
    except Exception:
        pass
    
    # If resolution fails, return original URL
    return url


def parse_next_url(html: str) -> Optional[str]:
    """Extract next page URL from listing page."""
    soup = BeautifulSoup(html, "lxml")
    
    # Try modern pagination
    a = soup.select_one("a[rel='next']")
    if not a:
        # Try aria-label based selectors
        a = soup.select_one("a[title='Siguiente']")
    if not a:
        a = soup.select_one("a[aria-label*='Siguiente']")
    if not a:
        # Try pagination button
        a = soup.select_one("li.andes-pagination__button--next a")
    if not a:
        # Try older pattern
        a = soup.select_one(".pagination__next a")
    
    return a.get("href") if a and a.get("href") else None


def parse_list_page(html: str, source_url: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Parse MercadoLibre listing page.
    
    Returns: (cards, seller_refs)
    - cards: List of card dictionaries
    - seller_refs: List of seller reference dictionaries
    """
    soup = BeautifulSoup(html, "lxml")
    
    # Extract cards using new robust method
    cards = extract_cards_from_listing_html(html)
    
    # Extract sellers from page
    sellers = set()
    
    # Look for seller store links
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        
        if "/tienda/" in href:
            seller_id = extract_seller_id_from_url(href)
            if seller_id:
                sellers.add(seller_id)
        
        if "/_CustId_" in href:
            seller_id = extract_seller_id_from_url(href)
            if seller_id:
                sellers.add(seller_id)
    
    # Deduplicate cards by permalink
    cards_uniq = {c["permalink"]: c for c in cards}
    cards = list(cards_uniq.values())
    
    # Build seller references
    seller_refs = [
        {
            "seller_id": sid,
            "seller_url": seller_list_url(sid),
            "source_url": source_url
        }
        for sid in sellers
    ]
    
    return cards, seller_refs


def parse_item_page(html: str, url: str) -> Dict[str, Any]:
    """
    Parse individual item detail page.
    Extract full item details including item_id, seller_id, price, etc.
    """
    soup = BeautifulSoup(html, "lxml")
    
    # Extract item_id and product_id from URL
    item_id, product_id, needs_enrichment = extract_item_id_from_url(url)
    
    # Extract title
    title = None
    if soup.find("h1"):
        title = soup.find("h1").get_text(strip=True)
    if not title and soup.find("title"):
        title = soup.find("title").get_text(strip=True)
    if not title:
        title = "unknown"
    
    # Extract price
    price_mxn = None
    price_selectors = [
        "span.price-tag-fraction",
        "span.andes-money-amount__fraction",
        "div.ui-price__part--integer span.andes-money-amount__fraction"
    ]
    for selector in price_selectors:
        el = soup.select_one(selector)
        if el:
            price_text = el.get_text(strip=True).replace(",", "")
            try:
                price_mxn = float(price_text)
                break
            except ValueError:
                continue
    
    # Extract condition
    condition = None
    condition_el = soup.select_one("span.andes-badge, span[itemprop='condition']")
    if condition_el:
        condition = condition_el.get_text(strip=True).lower()
    
    # Extract pictures and attributes from ld+json
    attributes = None
    pictures = None
    for s in soup.find_all("script"):
        if "ld+json" not in (s.get("type") or "").lower():
            continue
        try:
            data = json.loads(s.string or "{}")
            if isinstance(data, list):
                data = next((x for x in data if isinstance(x, dict)), {})
            if isinstance(data, dict):
                attributes = {"jsonld": data}
                img = data.get("image")
                if isinstance(img, list):
                    pictures = [str(x) for x in img]
                elif isinstance(img, str):
                    pictures = [img]
                break
        except Exception:
            continue
    
    return {
        "permalink": url,
        "title": title,
        "item_id": item_id,
        "product_id": product_id,
        "needs_enrichment": needs_enrichment,
        "seller_id": None,
        "price_mxn": price_mxn,
        "currency": "MXN",
        "condition": condition,
        "pictures": pictures,
        "attributes": attributes,
        "raw_snippet": html[:2500],
        "captured_at_utc": now_utc()
    }

