import hashlib
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
# Match UP ID in /up/MLMUxxxx URLs (unified product)
UP_ID_RE = re.compile(r"/up/(MLMU\d+)")
# Match seller ID in various URL patterns
SELLER_CUSTID_RE = re.compile(r"_CustId_(\d+)")
SELLER_TIENDA_RE = re.compile(r"/tienda/(\d+)")
# Match item_id in articulo URLs: /MLM-4714040498- → MLM4714040498
# Articulo URLs use dashes: articulo.mercadolibre.com.mx/MLM-4714040498-title-_JM
ARTICULO_ITEM_ID_RE = re.compile(r"/MLM-(\d{6,15})")

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
        
        # Extract title with fallbacks
        title = extract_title_from_card(card, href)

        # Extract price
        price_mxn = extract_price_from_card(card)

        # NOTE: item_id / product_id / needs_enrichment / filtered_out are NOT
        # set here.  Identity extraction and all decision layers are owned
        # exclusively by assemble_card() via extract_ids(), so we only pass
        # the raw scraped fields.  This avoids stale/duplicate ID extraction.
        card_dict = {
            "permalink": href.split("#")[0],  # Remove URL fragment
            "title": title,
            "price_mxn": price_mxn,
            "seller_id": None,  # Filled by caller (tools.py) when known
            "currency": "MXN",
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
        
        # Try to get title from link
        title = link.get("title", "")
        if not title:
            title = link.get_text(strip=True)

        if len(title) < 3:
            continue

        # NOTE: Identity extraction is owned by assemble_card() / extract_ids().
        # Raw cards only carry scraped fields; no ID or decision fields here.
        card_dict = {
            "permalink": href,
            "title": title,
            "price_mxn": None,
            "seller_id": None,
            "currency": "MXN",
        }
        
        cards.append(card_dict)
    
    return cards


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


# =============================================================================
# NEW MODULAR FUNCTIONS - Three-Layer Decision Architecture
# =============================================================================

def extract_ids(permalink: str) -> Dict[str, Any]:
    """
    LAYER 1: Identity Extraction Layer

    Extract ALL identifiers from permalink using deterministic, independent rules.
    Each identifier type is checked independently — no early returns.
    This step MUST NOT set filtered_out.

    URL type patterns (mutually exclusive in practice):
      - Articulo:  articulo.mercadolibre.com.mx/MLM-4714040498-title → item_id = MLM4714040498
      - Direct:    mercadolibre.com.mx/MLM4714040498                 → item_id = MLM4714040498
      - Catalog:   mercadolibre.com.mx/p/MLM1054106937               → product_id = MLM1054106937
      - UP:        mercadolibre.com.mx/up/MLMU3779491406             → up_id = MLMU3779491406
      - wid param: ?wid=MLM4714040498                                → item_id = MLM4714040498

    Args:
        permalink: The MercadoLibre product URL

    Returns:
        Dictionary with keys:
        - item_id (Optional[str]): Standard listing ID, e.g. MLM4714040498
        - product_id (Optional[str]): Catalog product ID, e.g. MLM1054106937
        - up_id (Optional[str]): Unified product ID, e.g. MLMU3779491406
        - is_catalog_product (bool): True if URL contains /p/
        - is_up_product (bool): True if URL contains /up/
    """
    result = {
        "item_id": None,
        "product_id": None,
        "up_id": None,
        "is_catalog_product": False,
        "is_up_product": False,
    }

    if not permalink:
        return result

    # --- Check 1: UP (unified product) URLs /up/MLMUxxxx ---
    up_match = UP_ID_RE.search(permalink)
    if up_match:
        result["up_id"] = up_match.group(1)
        result["is_up_product"] = True
        # UP URLs are a distinct type — no item_id or product_id expected
        return result

    # --- Check 2: Catalog product URLs /p/MLMxxxx ---
    product_match = PRODUCT_ID_RE.search(permalink)
    if product_match:
        result["product_id"] = product_match.group(1)
        result["is_catalog_product"] = True
        # Catalog URLs never carry a direct item_id in the path
        return result

    # --- Check 3: Articulo URLs /MLM-XXXXXXXXXX- (dashed format) ---
    # e.g. articulo.mercadolibre.com.mx/MLM-4714040498-iphone-15-_JM
    # The digits are separated from MLM by a dash; reconstruct as MLM + digits.
    articulo_match = ARTICULO_ITEM_ID_RE.search(permalink)
    if articulo_match:
        result["item_id"] = "MLM" + articulo_match.group(1)
        return result

    # --- Check 4: Direct item URLs /MLMxxxxxxxxxx (no dash) ---
    # e.g. mercadolibre.com.mx/MLM4714040498 or path segment containing MLM######
    item_match = ITEM_ID_RE.search(permalink)
    if item_match:
        result["item_id"] = item_match.group(1)
        return result

    # --- Check 5: wid query parameter ---
    # Some ML URLs encode the item_id as ?wid=MLM4714040498
    try:
        wid = parse_qs(urlparse(permalink).query).get("wid", [None])[0]
        if wid and ITEM_ID_RE.match(wid):
            result["item_id"] = wid
            return result
    except Exception:
        pass

    # No identifier found — channel_item_id will fall back to SHA1(permalink)
    return result


def compute_channel_item_id(
    item_id: Optional[str],
    product_id: Optional[str],
    up_id: Optional[str],
    permalink: str
) -> Tuple[str, str]:
    """
    LAYER 1 (continued): Compute channel_item_id with priority rules
    
    Priority: product_id → item_id → up_id → SHA1(permalink)
    
    Args:
        item_id: Standard listing ID
        product_id: Product catalog ID
        up_id: Unified product ID
        permalink: Full URL for hash fallback
        
    Returns:
        Tuple of (channel_item_id, id_source)
        id_source is one of: "item_id", "product_id", "up_id", "hash"
    """
    # Priority 1: product_id (catalog URLs)
    if product_id:
        return product_id, "product_id"
    
    # Priority 2: item_id (standard listing URLs)
    if item_id:
        return item_id, "item_id"
    
    # Priority 3: up_id (unified product URLs)
    if up_id:
        return up_id, "up_id"
    
    # Priority 4: SHA1 hash of permalink
    if permalink:
        sha1_hash = hashlib.sha1(permalink.encode('utf-8')).hexdigest()
        return sha1_hash, "hash"
    
    # Fallback: empty string (will be filtered later)
    return "", "hash"


# ========== FILTERING KEYWORDS ==========

# Refurbished keyword (Spanish)
REFURBISHED_KEYWORDS = ["reacondicionado", "reacondicionada"]

# Bundle keywords (Spanish)
# NOTE: "regalo" alone is intentionally excluded — too broad, causes false positives.
# Only "de regalo" (as a gift/bundle) matches the business rule.
BUNDLE_KEYWORDS = ["de regalo", "+ airpods", "incluye airpods", "incluye airepods", "incluye regalo"]

# Carrier locked keywords (Spanish)
LOCKED_KEYWORDS = ["at&t", "telcel", "solo at&t", "solo telcel", "bloqueado", "locked"]

# Accessory-only keywords (Spanish)
ACCESSORY_KEYWORDS = [
    "funda", "case", "mica", "protector", "cargador", "cable", 
    "auricular", "audifonos", "headset", "speaker", "bocina",
    "adaptador", "hub", "dock", "stylus", "lapiz", "pencil",
    "strap", "correa", "brazo", "mount", "soporte", "holder",
    "skin", "cover", "wraps", "film", "tempered glass"
]


def classify_filter(
    title: str,
    price_mxn: Optional[float],
    permalink: str,
    allow_refurbished: bool = False,
    allow_bundles: bool = False,
    allow_locked: bool = False
) -> Tuple[bool, List[str]]:
    """
    LAYER 2: Filtering Decision Layer
    
    Filtering must ONLY apply business rules, not parser completeness.
    Missing item_id, missing seller_id, or catalog products must NEVER cause filtering.
    
    Args:
        title: Product title
        price_mxn: Price in MXN
        permalink: Product URL
        allow_refurbished: Whether to allow refurbished items (default: False)
        allow_bundles: Whether to allow bundled products (default: False)
        allow_locked: Whether to allow carrier-locked phones (default: False)
        
    Returns:
        Tuple of (filtered_out, filtered_reasons)
        - filtered_out: True if listing violates any business rule
        - filtered_reasons: List of explanations for filtering
    """
    filtered_reasons = []
    
    # 1. Check for invalid/missing data (business rule violations)
    
    # Missing title
    if not title or len(title.strip()) < 3:
        filtered_reasons.append("missing_title")
        return True, filtered_reasons
    
    # Missing price
    if price_mxn is None or price_mxn <= 0:
        filtered_reasons.append("missing_price")
        return True, filtered_reasons
    
    # Invalid URL (must contain mercadolibre and valid ID pattern)
    if not permalink or "mercadolibre" not in permalink.lower():
        filtered_reasons.append("invalid_url")
        return True, filtered_reasons
    
    title_lower = title.lower()
    
    # 2. Check refurbished items (if not allowed)
    if not allow_refurbished:
        for keyword in REFURBISHED_KEYWORDS:
            if keyword in title_lower:
                filtered_reasons.append("refurbished_not_allowed")
                return True, filtered_reasons
    
    # 3. Check bundled products (if not allowed)
    if not allow_bundles:
        for keyword in BUNDLE_KEYWORDS:
            if keyword in title_lower:
                filtered_reasons.append("bundle_not_allowed")
                return True, filtered_reasons
    
    # 4. Check carrier locked phones (if not allowed)
    if not allow_locked:
        for keyword in LOCKED_KEYWORDS:
            if keyword in title_lower:
                filtered_reasons.append("carrier_locked_not_allowed")
                return True, filtered_reasons
    
    # 5. Check accessory-only listings
    for keyword in ACCESSORY_KEYWORDS:
        if keyword in title_lower:
            filtered_reasons.append("accessory_only")
            return True, filtered_reasons
    
    # If none of the business rules triggered filtering, keep the listing
    return False, filtered_reasons


def compute_needs_enrichment(
    item_id: Optional[str],
    seller_id: Optional[int],
    is_catalog_product: bool,
    is_up_product: bool,
) -> bool:
    """
    LAYER 2: Enrichment Decision Layer

    Determines whether a listing requires a downstream enrichment step to
    obtain complete data.  This is a PIPELINE CONTINUATION decision, not a
    filtering decision.  needs_enrichment MUST NOT affect filtered_out.

    Rules (any True → needs_enrichment = True):
      1. item_id is None  — no direct listing ID; enrichment needed to resolve it
      2. seller_id is None — seller identity unknown; enrichment needed
      3. is_catalog_product — /p/ URLs aggregate multiple sellers; always enrich
      4. is_up_product — /up/ URLs are unified products; always enrich

    Args:
        item_id: Standard listing ID extracted from URL (None for /p/ and /up/)
        seller_id: Seller ID if known from scrape context, else None
        is_catalog_product: True when URL contains /p/ (catalog page)
        is_up_product: True when URL contains /up/ (unified product page)

    Returns:
        True if listing needs enrichment, False if all required data is present
    """
    # Rule 1: No direct item_id → must enrich to resolve listing details
    if item_id is None:
        return True

    # Rule 2: Seller unknown → must enrich to identify seller
    if seller_id is None:
        return True

    # Rule 3: Catalog product (/p/) → always enrich for item-level data
    # (catalog pages aggregate multiple sellers; item_id is not stable here)
    if is_catalog_product:
        return True

    # Rule 4: Unified product (/up/) → always enrich
    # (UP pages never carry a direct item_id; enrichment resolves the listing)
    if is_up_product:
        return True

    # All required data is present — no enrichment needed
    return False


def assemble_card(
    permalink: str,
    title: str,
    price_mxn: Optional[float],
    currency: str = "MXN",
    seller_id: Optional[int] = None,
    allow_refurbished: bool = False,
    allow_bundles: bool = False,
    allow_locked: bool = False
) -> Dict[str, Any]:
    """
    Assemble a complete card with all three decision layers applied.
    
    This function applies:
    1. Identity Extraction Layer - Extract item_id, product_id, up_id
    2. Enrichment Decision Layer - Determine if enrichment is needed
    3. Filtering Decision Layer - Apply business rules
    
    Args:
        permalink: Product URL
        title: Product title
        price_mxn: Price in MXN
        currency: Currency code (default: MXN)
        seller_id: Seller ID if available
        allow_refurbished: Whether to allow refurbished items
        allow_bundles: Whether to allow bundled products
        allow_locked: Whether to allow carrier-locked phones
        
    Returns:
        Dictionary representing the assembled card with all fields:
        - permalink, title, item_id, product_id, up_id
        - channel_item_id, id_source
        - seller_id, price_mxn, currency
        - needs_enrichment, filtered_out, filtered_reasons
    """
    # Layer 1: Identity Extraction
    ids = extract_ids(permalink)
    item_id = ids["item_id"]
    product_id = ids["product_id"]
    up_id = ids["up_id"]
    is_catalog_product = ids["is_catalog_product"]
    is_up_product = ids["is_up_product"]
    
    # Compute channel_item_id and id_source
    channel_item_id, id_source = compute_channel_item_id(
        item_id, product_id, up_id, permalink
    )
    
    # Layer 2: Enrichment Decision
    needs_enrichment = compute_needs_enrichment(
        item_id, seller_id, is_catalog_product, is_up_product
    )
    
    # Layer 3: Filtering Decision (business rules only)
    filtered_out, filtered_reasons = classify_filter(
        title=title,
        price_mxn=price_mxn,
        permalink=permalink,
        allow_refurbished=allow_refurbished,
        allow_bundles=allow_bundles,
        allow_locked=allow_locked
    )
    
    # Assemble final card
    card = {
        "permalink": permalink.split("#")[0],  # Remove fragment
        "title": title,
        "item_id": item_id,
        "product_id": product_id,
        "up_id": up_id,
        "channel_item_id": channel_item_id,
        "id_source": id_source,
        "seller_id": seller_id,
        "price_mxn": price_mxn,
        "currency": currency,
        "needs_enrichment": needs_enrichment,
        "filtered_out": filtered_out,
        "filtered_reasons": filtered_reasons
    }
    
    return card


def compute_card_stats_v2(cards: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Compute statistics from card list using the new contract.
    
    Statistics:
    - total: all cards
    - valid: cards where filtered_out == false
    - needs_enrichment: cards where needs_enrichment == true
    - ready: cards where filtered_out == false AND needs_enrichment == false
    
    Args:
        cards: List of card dictionaries
        
    Returns:
        Dictionary with statistics
    """
    stats = {
        "total": len(cards),
        "valid": 0,
        "needs_enrichment": 0,
        "ready": 0,
        "filtered_out": 0
    }
    
    for card in cards:
        filtered_out = card.get("filtered_out", False)
        needs_enrichment = card.get("needs_enrichment", False)
        
        if filtered_out:
            stats["filtered_out"] += 1
        else:
            stats["valid"] += 1
        
        if needs_enrichment:
            stats["needs_enrichment"] += 1
        
        # Ready = valid AND not needs enrichment
        if not filtered_out and not needs_enrichment:
            stats["ready"] += 1
    
    return stats

