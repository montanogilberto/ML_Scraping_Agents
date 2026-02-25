from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class Limits(BaseModel):
    max_pages_per_category: int = 3
    max_sellers_per_category: int = 50
    max_pages_per_seller: int = 5
    max_items_total: int = 300

class PersistConfig(BaseModel):
    mode: str = "backend"

class InventoryRequest(BaseModel):
    country: str = "MX"
    site: str = "MLM"
    category_urls: List[str] = Field(default_factory=list)
    seed_seller_ids: List[int] = Field(default_factory=list)
    limits: Limits = Field(default_factory=Limits)
    persist: PersistConfig = Field(default_factory=PersistConfig)

class SellerRef(BaseModel):
    seller_id: int
    seller_url: str
    source_url: Optional[str] = None

class ListingCard(BaseModel):
    permalink: str
    title: str
    item_id: Optional[str] = None
    product_id: Optional[str] = None  # For /p/MLM URLs (product catalog)
    up_id: Optional[str] = None  # For /up/MLMU URLs (unified product)
    channel_item_id: str = ""  # Computed from item_id/product_id/up_id with priority
    id_source: str = "hash"  # One of: "item_id", "product_id", "up_id", "hash"
    needs_enrichment: bool = False   # True if only product_id available (need detail scrape)
    seller_id: Optional[int] = None
    price_mxn: Optional[float] = None
    currency: str = "MXN"
    filtered_out: bool = False  # Marked True if filtered by relevance
    filtered_reasons: List[str] = Field(default_factory=list)  # Explanation for filtering

class NormalizedItem(BaseModel):
    source: str = "mercadolibre_scrape"
    permalink: str
    title: str
    item_id: Optional[str] = None
    product_id: Optional[str] = None  # For /p/MLM URLs (product catalog)
    up_id: Optional[str] = None       # For /up/MLMU URLs (unified product)
    channel_item_id: str = ""         # Computed stable ID (product_id > up_id > item_id > SHA1)
    id_source: str = "hash"           # One of: "product_id", "up_id", "item_id", "hash"
    needs_enrichment: bool = False    # True if enrichment step is needed; False after JSON-LD found
    seller_id: Optional[int] = None
    price_mxn: Optional[float] = None
    currency: str = "MXN"
    condition: Optional[str] = None
    brand: Optional[str] = None       # Extracted from JSON-LD brand field
    pictures: Optional[List[str]] = None
    attributes: Optional[Dict[str, Any]] = None
    raw_snippet: Optional[str] = None
    captured_at_utc: str
