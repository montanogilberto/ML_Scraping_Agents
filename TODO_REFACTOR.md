# TODO: Refactor ml_scrape_category Tool

## Goal
Refactor the logic so that identity extraction, enrichment decision, and filtering decision are strictly separated, deterministic, and explainable.

## Steps

- [x] 1. Update schemas.py - Add channel_item_id, id_source, filtered_reasons, up_id fields to ListingCard
- [x] 2. Update parsers.py - Add 5 new modular functions:
  - [x] extract_ids(permalink) - Identity extraction layer (+ articulo URL fix, no early returns)
  - [x] compute_channel_item_id(...) - Determine channel_item_id and id_source
  - [x] classify_filter(...) - Filtering decision layer with business rules only
  - [x] compute_needs_enrichment(...) - Enrichment decision layer (fixed UP condition)
  - [x] assemble_card(...) - Assemble final card with all decisions
- [x] 3. Update tools.py - Uses assemble_card() and compute_card_stats_v2() throughout
- [x] 4. Update statistics calculation - Matches new contract (total, valid, needs_enrichment, ready)
- [x] 5. Fix BUNDLE_KEYWORDS - Removed false-positive standalone "regalo"
- [x] 6. Clean up extract_cards_from_listing_html / _fallback_extract_cards - Removed stale ID extraction

## Implementation Details

### Identity Extraction Layer
- Extract item_id from /MLM- URLs (articulo)
- Extract product_id from /p/MLM URLs (catalog)
- Extract up_id from /up/MLMU URLs (unified product)
- Priority: product_id → item_id → up_id → SHA1(permalink)

### Enrichment Decision Layer
- needs_enrichment = true if:
  - item_id is null
  - seller_id is null
  - listing came from catalog (/p/)
  - listing came from UP (/up/) and seller unknown

### Filtering Decision Layer
- filtered_out = true ONLY if:
  - refurbished items when allow_refurbished=false (keyword: reacondicionado)
  - bundled products when allow_bundles=false (keywords: de regalo, + airpods)
  - carrier locked when allow_locked=false (keywords: at&t, telcel, solo at&t)
  - accessory-only listings (keywords: funda, case, mica, protector, cargador)
  - invalid data: missing title, missing price, invalid URL

### Statistics
- total = all cards
- valid = cards where filtered_out == false
- needs_enrichment = cards where needs_enrichment == true
- ready = cards where filtered_out == false AND needs_enrichment == false

