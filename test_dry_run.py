#!/usr/bin/env python3
"""
Dry-run test for sellListings export with actual out.ndjson data.
"""

import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set required environment variables
os.environ["FX_RATE_TO_USD"] = "0.05842"
os.environ["BACKEND_BASE_URL"] = "https://smartloansbackend.azurewebsites.net"
os.environ["BACKEND_WORKER_KEY"] = "test-worker-key"

from agents.ml_inventory.export.export_sell_listings import read_ndjson, build_sell_listings_payload
from agents.ml_inventory.config.settings import load_settings

if __name__ == "__main__":
    print("=" * 60)
    print("DRY RUN TEST - SELL LISTINGS EXPORT")
    print("=" * 60)
    print()
    
    settings = load_settings()
    
    # Read items from NDJSON
    items = read_ndjson("out.ndjson")
    print(f"Loaded {len(items)} items from out.ndjson")
    print()
    
    # Build payload
    result = build_sell_listings_payload(
        items=items,
        fx_rate_to_usd=settings.fx_rate_to_usd
    )
    
    metadata = result.get("_metadata", {})
    sell_listings = result.get("sellListings", [])
    
    print(f"Total items: {metadata.get('total', 0)}")
    print(f"Emitted: {metadata.get('emitted', 0)}")
    print(f"Skipped: {metadata.get('skipped', 0)}")
    print(f"FX Rate: {metadata.get('fx_rate_to_usd')}")
    print(f"FX As Of Date: {metadata.get('fx_as_of_date')}")
    print()
    
    # Show first 2 records as preview
    print("Preview (first 2 records):")
    print("-" * 60)
    import json
    for i, listing in enumerate(sell_listings[:2]):
        print(json.dumps(listing, indent=2, ensure_ascii=False))
        print()

