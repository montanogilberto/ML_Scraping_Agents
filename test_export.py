#!/usr/bin/env python3
"""
Test script for sellListings export transformation logic.

This script verifies:
1. channelItemId extraction: /p/MLM... → /up/MLMU... → SHA1(permalink)
2. sellPriceUsd calculation: price_mxn * fxRateToUsd
"""

import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.ml_inventory.export.export_sell_listings import (
    parse_channel_item_id,
    build_sell_listings_payload
)

def test_channel_item_id_extraction():
    """Test channelItemId extraction from URLs."""
    print("=" * 60)
    print("Testing channelItemId extraction")
    print("=" * 60)
    
    test_cases = [
        # (permalink, expected_type)
        ("https://www.mercadolibre.com.mx/detergente-en-polvo-roma-multiusos-biodegradable-1-kg/p/MLM32624978", "MLM32624978"),
        ("https://www.mercadolibre.com.mx/kit-para-tatuar-profesional-maquina-para-tatuar-alta-calidad/p/MLM46062703", "MLM46062703"),
        ("https://www.mercadolibre.com.mx/buzon-de-sugerencias-urna-de-acrilico-transparente-para-don/up/MLMU3674594112", "MLMU3674594112"),
        ("https://www.mercadolibre.com.mx/filamento-3d-creality-pla-de-175mm-y-2kg-blanco-y-negro/up/MLMU3485454951", "MLMU3485454951"),
        ("https://articulo.mercadolibre.com.mx/MLM-1373603054-placa-tubo-oem-24581370-24585919-93381894-24579093-24585380-_JM", "sha1"),  # Should use SHA1
    ]
    
    all_passed = True
    for permalink, expected in test_cases:
        result = parse_channel_item_id(permalink)
        
        if expected == "sha1":
            # Should be a 40-character hex string (SHA1)
            is_sha1 = len(result) == 40 and all(c in '0123456789abcdef' for c in result)
            status = "PASS" if is_sha1 else "FAIL"
            if not is_sha1:
                all_passed = False
            print(f"  {status}: {permalink[:60]}...")
            print(f"        Got SHA1: {result[:20]}...")
        else:
            status = "PASS" if result == expected else "FAIL"
            if result != expected:
                all_passed = False
            print(f"  {status}: {permalink[:60]}...")
            print(f"        Expected: {expected}, Got: {result}")
    
    return all_passed


def test_price_calculation():
    """Test USD price calculation."""
    print("\n" + "=" * 60)
    print("Testing price calculation")
    print("=" * 60)
    
    test_items = [
        {"permalink": "https://example.com/p/MLM123", "title": "Test Item", "price_mxn": 100.0, "currency": "MXN", "captured_at_utc": "2026-02-19T00:59:55Z"},
        {"permalink": "https://example.com/p/MLM456", "title": "Expensive Item", "price_mxn": 29438.0, "currency": "MXN", "captured_at_utc": "2026-02-19T00:59:55Z"},
        {"permalink": "https://example.com/p/MLM789", "title": "Cheap Item", "price_mxn": 42.37, "currency": "MXN", "captured_at_utc": "2026-02-19T00:59:55Z"},
    ]
    
    fx_rate = 0.05842
    
    all_passed = True
    for item in test_items:
        result = build_sell_listings_payload([item], fx_rate)
        sell_listing = result["sellListings"][0]
        
        expected_usd = round(item["price_mxn"] * fx_rate, 6)
        actual_usd = sell_listing["sellPriceUsd"]
        
        status = "PASS" if abs(expected_usd - actual_usd) < 0.000001 else "FAIL"
        if status == "FAIL":
            all_passed = False
            
        print(f"  {status}: {item['title']}")
        print(f"        MXN: {item['price_mxn']}, FX Rate: {fx_rate}")
        print(f"        Expected USD: {expected_usd}, Got: {actual_usd}")
    
    return all_passed


def test_dry_run():
    """Test dry run output format."""
    print("\n" + "=" * 60)
    print("Testing dry run output")
    print("=" * 60)
    
    # Set up required env vars for testing
    os.environ["FX_RATE_TO_USD"] = "0.05842"
    os.environ["BACKEND_BASE_URL"] = "https://test.example.com"
    os.environ["BACKEND_WORKER_KEY"] = "test-key"
    
    from agents.ml_inventory.config.settings import load_settings
    
    settings = load_settings()
    print(f"  FX Rate: {settings.fx_rate_to_usd}")
    print(f"  Backend URL: {settings.sell_listings_backend_url}")
    
    return True


def main():
    print("\n" + "=" * 60)
    print("SELL LISTINGS EXPORT - TRANSFORMATION TESTS")
    print("=" * 60 + "\n")
    
    # Run tests
    results = []
    
    results.append(("channelItemId Extraction", test_channel_item_id_extraction()))
    results.append(("Price Calculation", test_price_calculation()))
    results.append(("Settings Loading", test_dry_run()))
    
    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    
    all_passed = True
    for name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status}: {name}")
        if not passed:
            all_passed = False
    
    print("\n" + "=" * 60)
    if all_passed:
        print("All tests passed!")
    else:
        print("Some tests failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()

