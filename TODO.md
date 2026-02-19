# Task: Fix Seller Scraping Pipeline for POS-Scoped Results

## Root Cause Summary
The seller inventory scraping returns irrelevant data because:
1. Uses `/tienda/{seller_id}` URL pattern (entire seller store) instead of category-scoped `_CustId_{seller_id}_PrCategId_AD`
2. No click-tracker URL resolution (click1.mercadolibre.com.mx → actual URL)
3. Returns only sample_permalink instead of limited cards array for proper context

## Implementation Plan

### Step 1: Update parsers.py
- [ ] Add `resolve_click_tracker_url()` function to follow redirects
- [ ] Add `seller_category_url()` function for scoped seller URLs  
- [ ] Update `extract_item_id_from_url()` to handle resolved URLs

### Step 2: Update tools.py
- [ ] Modify `ml_scrape_seller_inventory` to accept optional `seller_listing_url` parameter
- [ ] Change default URL pattern to `_CustId_{seller_id}_PrCategId_AD`
- [ ] Add click-tracker resolution in card extraction loop
- [ ] Return limited `cards` array (max 20) + `sample_permalink`
- [ ] Update stats to include missing_titles, missing_item_id

### Step 3: Update inventory_pipeline.py
- [ ] Update SellerScout instruction to build category-scoped URLs
- [ ] Pass category context from category_raw to seller scraping
- [ ] Update output format to handle cards array

### Step 4: Test & Verify
- [ ] Verify scoped seller URL returns POS items
- [ ] Confirm filtered_out count decreases
- [ ] Validate cards array contains relevant items

