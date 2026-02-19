#!/usr/bin/env python3
"""
Actual API test for sellListings export - sends data to backend.
"""

import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set required environment variables
os.environ["FX_RATE_TO_USD"] = "0.05842"
os.environ["BACKEND_BASE_URL"] = "https://smartloansbackend.azurewebsites.net"
os.environ["BACKEND_WORKER_KEY"] = "your-actual-worker-key"  # Replace with actual key

from agents.ml_inventory.export.export_sell_listings import read_ndjson, build_sell_listings_payload, export_sell_listings
from agents.ml_inventory.config.settings import load_settings

if __name__ == "__main__":
    print("=" * 60)
    print("SELL LISTINGS EXPORT - ACTUAL API CALL")
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
    print()
    
    if not sell_listings:
        print("No items to export!")
        sys.exit(1)
    
    # Prepare payload
    export_payload = {"sellListings": sell_listings}
    
    # Show curl command equivalent
    import json
    payload_json = json.dumps(export_payload, ensure_ascii=False)
    print("Curl command equivalent:")
    print("-" * 60)
    print(f"""curl -X POST \\
  '{settings.sell_listings_backend_url}' \\
  -H 'accept: application/json' \\
  -H 'Content-Type: application/json' \\
  -H 'X-Worker-Key: {settings.backend_worker_key}' \\
  -d '{payload_json[:500]}...'""")
    print()
    
    # Perform actual export
    print("Performing actual POST...")
    print("-" * 60)
    
    export_result = export_sell_listings(
        payload=export_payload,
        url=settings.sell_listings_backend_url,
        worker_key=settings.backend_worker_key,
        timeout_sec=settings.http_timeout_sec
    )
    
    print(f"OK: {export_result.get('ok')}")
    print(f"Status Code: {export_result.get('status_code')}")
    print(f"Exported Count: {export_result.get('exported_count')}")
    print(f"Response: {export_result.get('response')}")

