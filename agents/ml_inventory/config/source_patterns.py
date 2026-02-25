"""
Source URL Pattern Matcher

Loads and provides utilities for matching e-commerce source URLs.
Supports: MercadoLibre, Samsung, eBay, Amazon, AliExpress
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml


# Default path to patterns config
DEFAULT_PATTERNS_FILE = Path(__file__).parent / "source_patterns.yaml"

# Cache for loaded patterns
_patterns_cache: Optional[Dict] = None


def load_patterns(patterns_file: str = None) -> Dict:
    """
    Load source patterns from YAML configuration file.
    
    Args:
        patterns_file: Optional path to custom patterns file
        
    Returns:
        Dictionary of source patterns
    """
    global _patterns_cache
    
    if _patterns_cache is not None:
        return _patterns_cache
    
    file_path = patterns_file or DEFAULT_PATTERNS_FILE
    
    with open(file_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # Pre-compile regex patterns for performance
    for source in config.get('sources', []):
        if 'url_patterns' in source:
            for pattern_name, pattern_data in source['url_patterns'].items():
                if 'regex' in pattern_data:
                    pattern_data['_compiled'] = re.compile(
                        pattern_data['regex'], 
                        re.IGNORECASE
                    )
        
        # Compile category and search patterns
        if 'category_pattern' in source:
            source['_category_compiled'] = re.compile(
                source['category_pattern'], 
                re.IGNORECASE
            )
        if 'search_pattern' in source:
            source['_search_compiled'] = re.compile(
                source['search_pattern'], 
                re.IGNORECASE
            )
        if 'domain_pattern' in source:
            source['_domain_compiled'] = re.compile(
                source['domain_pattern'], 
                re.IGNORECASE
            )
    
    _patterns_cache = config
    return config


def get_source_by_domain(domain: str) -> Optional[Dict]:
    """
    Get source configuration by domain name.
    
    Args:
        domain: Domain string (e.g., 'mercadolibre.com.mx')
        
    Returns:
        Source configuration dict or None
    """
    config = load_patterns()
    domain_lower = domain.lower()
    
    for source in config.get('sources', []):
        domain_pattern = source.get('domain_pattern', '')
        if re.search(domain_pattern, domain_lower, re.IGNORECASE):
            return source
    
    return None


def identify_source(url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Identify source and URL type from a URL string.
    
    Args:
        url: Full URL to identify
        
    Returns:
        Tuple of (source_name, url_type) or (None, None)
    """
    config = load_patterns()
    url_lower = url.lower()
    
    # Try each source in priority order
    for source in config.get('sources', []):
        source_name = source.get('name')
        domain_pattern = source.get('domain_pattern', '')
        
        # Check if domain matches
        if not re.search(domain_pattern, url_lower, re.IGNORECASE):
            continue
        
        # Check URL patterns
        url_patterns = source.get('url_patterns', {})
        for pattern_name, pattern_data in url_patterns.items():
            compiled = pattern_data.get('_compiled')
            if compiled and compiled.search(url):
                return source_name, pattern_name
        
        # Check category pattern
        if source.get('_category_compiled'):
            if source['_category_compiled'].search(url):
                return source_name, 'category'
        
        # Check search pattern
        if source.get('_search_compiled'):
            if source['_search_compiled'].search(url):
                return source_name, 'search'
        
        # Domain matches but no specific pattern - it's likely a general page
        return source_name, 'general'
    
    return None, None


def extract_source_id(url: str, source: str = None) -> Optional[str]:
    """
    Extract the source-specific ID from a URL.
    
    Args:
        url: Full URL
        source: Optional source name (if known)
        
    Returns:
        Extracted ID or None
    """
    config = load_patterns()
    
    # If source not provided, try to identify
    if source is None:
        source, _ = identify_source(url)
        if source is None:
            return None
    
    # Find source config
    source_config = None
    for s in config.get('sources', []):
        if s.get('name') == source:
            source_config = s
            break
    
    if source_config is None:
        return None
    
    # Try each URL pattern
    url_patterns = source_config.get('url_patterns', {})
    for pattern_name, pattern_data in url_patterns.items():
        compiled = pattern_data.get('_compiled')
        if compiled:
            match = compiled.search(url)
            if match:
                return match.group(1)
    
    return None


def get_channel_for_source(source: str) -> Optional[str]:
    """
    Get the backend channel code for a source.
    
    Args:
        source: Source name
        
    Returns:
        Channel code or None
    """
    config = load_patterns()
    mapping = config.get('CHANNEL_TO_MARKET', {})
    return mapping.get(source)


def get_supported_channels() -> List[str]:
    """
    Get list of all supported channels.
    
    Returns:
        List of channel names
    """
    config = load_patterns()
    return config.get('SUPPORTED_CHANNELS', [])


def is_supported_url(url: str) -> bool:
    """
    Check if a URL is from a supported source.
    
    Args:
        url: URL to check
        
    Returns:
        True if URL is from a supported source
    """
    source, _ = identify_source(url)
    return source is not None


def get_source_info(source_name: str) -> Optional[Dict]:
    """
    Get full configuration for a source.
    
    Args:
        source_name: Name of the source
        
    Returns:
        Source configuration dict or None
    """
    config = load_patterns()
    
    for source in config.get('sources', []):
        if source.get('name') == source_name:
            return source
    
    return None


# Convenience function for quick source detection
def detect_source(url: str) -> str:
    """
    Detect source name from URL (convenience function).
    
    Args:
        url: URL to detect
        
    Returns:
        Source name or 'unknown'
    """
    source, _ = identify_source(url)
    return source or 'unknown'


if __name__ == "__main__":
    # Test the pattern matching
    test_urls = [
        "https://www.mercadolibre.com.mx/celular-samsung-galaxy-z-fold-7/p/MLM52050903",
        "https://www.samsung.com/mx/smartphones/galaxy-s26-ultra/",
        "https://www.ebay.com/sch/i.html?_nkw=samsung+s25",
        "https://www.amazon.com.mx/s?k=samsung+s25",
        "https://es.aliexpress.com/w/wholesale-samsung-s25.html",
    ]
    
    print("URL Source Detection Test")
    print("=" * 60)
    
    for url in test_urls:
        source, url_type = identify_source(url)
        source_id = extract_source_id(url, source)
        
        print(f"\nURL: {url}")
        print(f"  Source: {source}")
        print(f"  Type: {url_type}")
        print(f"  ID: {source_id}")

