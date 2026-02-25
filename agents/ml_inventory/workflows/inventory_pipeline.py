import logging
import os
import random
import time
from typing import Any, Optional

from google.adk.agents.llm_agent import LlmAgent
from google.adk.agents import SequentialAgent
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from ..config.settings import load_settings, reset_settings_cache
from ..callbacks.observer import before_agent_callback, after_agent_callback, before_model_callback, after_model_callback
from ..callbacks.guardian import GuardianDeContenido
from ..callbacks.composer import chain_before_model, chain_after_model
from ..mcp_servers.ml_scrape_mcp.tools import (
    ml_scrape_category,
    ml_scrape_seller_inventory,
    ml_scrape_item_detail,
    ml_persist_items,
    ml_export_sell_listings,
    ml_get_all_sell_listings,
    ml_query_sell_listings,
)

# Lazy-load settings to avoid None at import time
# The ADK module loader may import before env vars are loaded
_settings = None

def _get_settings():
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings

# Global rate limiting
_last_api_call_time = 0.0


def _rate_limit():
    """Apply rate limiting between LLM API calls to avoid 429 errors."""
    global _last_api_call_time
    current_time = time.time()
    elapsed = current_time - _last_api_call_time
    if elapsed < _get_settings().min_delay_between_calls:
        sleep_time = _get_settings().min_delay_between_calls - elapsed
        _log.debug("Rate limiting: sleeping %.2fs", sleep_time)
        time.sleep(sleep_time)
    _last_api_call_time = time.time()


# Fallback models (OpenAI) to use when 429 RESOURCE_EXHAUSTED is hit
# Use the "openai/" prefix to tell Google ADK to use OpenAI provider
_FALLBACK_MODEL_PLANNER = "openai/gpt-4o"
_FALLBACK_MODEL_COLLECTOR = "openai/gpt-4o-mini"
_FALLBACK_MODEL_QA = "openai/gpt-4o-mini"

# Track if we've already switched to fallback models
_fallback_active = False


def _get_fallback_settings():
    """
    Get settings with fallback models for retry after 429 errors.
    Uses OpenAI models instead of Gemini to avoid rate limits.
    """
    global _fallback_active, _settings
    
    # CRITICAL: Disable Vertex AI to use OpenAI
    # This MUST be set before creating new settings
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "0"
    
    # Reset the settings cache to get fresh settings with OpenAI models
    # This ensures we don't use cached Gemini model names
    reset_settings_cache()
    
    # Also reset the pipeline's local settings cache
    _settings = None
    
    # Get fresh settings (which will now use defaults since we disabled Vertex AI)
    settings = _get_settings()
    
    # Override models to use OpenAI explicitly
    settings.model_planner = _FALLBACK_MODEL_PLANNER
    settings.model_collector = _FALLBACK_MODEL_COLLECTOR
    settings.model_qa = _FALLBACK_MODEL_QA
    settings.google_genai_use_vertexai = False
    
    # Update the pipeline's local cache too
    _settings = settings
    
    _log.warning(
        "429 RESOURCE_EXHAUSTED detected! Switching to fallback models: %s, %s, %s",
        _FALLBACK_MODEL_PLANNER,
        _FALLBACK_MODEL_COLLECTOR,
        _FALLBACK_MODEL_QA
    )
    
    return settings


def _is_resource_exhausted_error(exception: Exception) -> bool:
    """Check if the exception is a 429 RESOURCE_EXHAUSTED error."""
    error_str = str(exception).lower()
    return "429" in error_str and "resource_exhausted" in error_str


def _create_retry_decorator():
    """Create a retry decorator for handling 429 RESOURCE_EXHAUSTED errors."""
    return retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(_get_settings().max_retries),
        wait=wait_exponential(
            multiplier=_get_settings().retry_backoff_base,
            max=_get_settings().retry_backoff_max,
            exponent=2
        ),
        reraise=True,
    )


_log = logging.getLogger(__name__)

def build_root_agent():
    guardian=GuardianDeContenido()
    secure_before=chain_before_model(guardian.before_model_callback, before_model_callback)
    secure_after=chain_after_model(after_model_callback)

    def wrap(a: LlmAgent) -> LlmAgent:
        """Wrap an LlmAgent with callbacks, rate limiting, and cost control settings."""
        # Add rate limiting before model calls (enabled for all agents)
        original_before_model = a.before_model_callback
        
        def wrapped_before_model(callback_context, prompt=None, **kwargs):
            # Handle both positional and keyword argument calling styles
            if prompt is None and 'prompt' in kwargs:
                prompt = kwargs.pop('prompt')
            # Apply rate limiting to avoid 429 errors
            _rate_limit()
            # Call secure_before chain (includes guardian and observer callbacks)
            return secure_before(callback_context, prompt, **kwargs)
        
        a.before_agent_callback = before_agent_callback
        a.after_agent_callback = after_agent_callback
        a.before_model_callback = wrapped_before_model
        a.after_model_callback = secure_after
        
        # Apply cost control settings via generate_content_config
        # Note: These settings are applied to control token usage and caching
        settings = _get_settings()
        a.generate_content_config = types.GenerateContentConfig(
            max_output_tokens=settings.max_tokens,
            temperature=settings.temperature,
        )
        
        return a

    planner=wrap(LlmAgent(
        name="Planner",
        model=_get_settings().model_planner,
        instruction=(
            f"Validate input JSON and output ONLY JSON plan with fields: "
            "{{country, site, category_urls, seed_seller_ids, limits:{{max_pages_per_category,max_sellers_per_category,max_pages_per_seller,max_items_total}}, persist:{{mode}}}} "
            f'persist.mode MUST ALWAYS be "{_get_settings().persist_mode}" — ignore and override any persist.mode value from the input JSON. '
            'If invalid, output {{"error":"..."}}.'
        ),
        output_key="plan",
    ))

    category_scout=wrap(LlmAgent(
        name="CategoryScout",
        model=_get_settings().model_collector,
        tools=[ml_scrape_category],
        instruction=(
            "You MUST call ml_scrape_category for each plan.category_urls. "
            "Use max_pages=plan.limits.max_pages_per_category. "
            'Return ONLY JSON: {"categories":[{"category_url":...,"sellers":[...],"cards":[...]}]}.'

"\n\n"
"Plan:\n{plan}"
        ),
        output_key="category_raw",
    ))

    seller_scout=wrap(LlmAgent(
        name="SellerScout",
        model=_get_settings().model_collector,
        tools=[ml_scrape_seller_inventory],
        instruction=(
            "You are SellerScout.\n"
            "\n"
            "Goal: Produce a plan to continue scraping even when no sellers are extracted from category pages.\n"
            "\n"
            "--- STEP 1: Build seller_ids ---\n"
            "Collect unique seller_ids from TWO sources:\n"
            "  a) plan.seed_seller_ids  (always present, may be empty list)\n"
            "  b) category_raw.categories[*].sellers[*].seller_id  (optional — may be absent or empty)\n"
            "Deduplicate. Limit to 500 total.\n"
            "\n"
            "--- STEP 2: Build permalinks ---\n"
            "Collect unique permalinks from category listing cards.\n"
            "Check each category in category_raw.categories[*] and collect from the FIRST path that exists:\n"
            "  a) category_raw.categories[*].cards[*].permalink\n"
            "  b) category_raw.categories[*].items[*].permalink\n"
            "  c) category_raw.categories[*].listings[*].permalink\n"
            "Deduplicate across all categories.\n"
            "\n"
            "--- STEP 3: Decide path ---\n"
            "\n"
            "PATH A — seller_ids is NON-EMPTY:\n"
            "  For each seller_id, call ml_scrape_seller_inventory with:\n"
            "    - seller_id: the seller ID\n"
            "    - category_id: 'AD'\n"
            "    - max_pages: plan.limits.max_pages_per_seller\n"
            "    - max_cards: 20\n"
            "  Collect results. Build sellers list: [{seller_id, sample_permalink}].\n"
            "  Also include sample_permalinks: collect all sample_permalink values from sellers (deduplicated).\n"
            "  Add note: 'seller_path: scraped N sellers via ml_scrape_seller_inventory'\n"
            "\n"
            "PATH B — seller_ids is EMPTY but permalinks is NON-EMPTY:\n"
            "  DO NOT halt. DO NOT call ml_scrape_seller_inventory.\n"
            "  Return sellers=[] and sample_permalinks with up to min(plan.limits.max_items_total, 50) permalinks.\n"
            "  Add note: 'permalink_fallback: no sellers found, returning N category card permalinks'\n"
            "\n"
            "PATH C — BOTH seller_ids and permalinks are EMPTY:\n"
            "  Return sellers=[] and sample_permalinks=[].\n"
            "  Add note: 'empty: no sellers and no category cards found'\n"
            "\n"
            "--- OUTPUT ---\n"
            "Output JSON ONLY. No markdown. No explanations outside JSON.\n"
            "{\n"
            '  "sellers": [ { "seller_id": <int>, "sample_permalink": "<url>" } ],\n'
            '  "sample_permalinks": [ "<url>", ... ],\n'
            '  "notes": [ "<short note>", ... ]\n'
            "}\n"

"\n\n"
"Plan:\n{plan}\n\n"
"Category (use category_url to extract category_id):\n{category_raw}"
        ),
        output_key="seller_raw",
    ))

    item_extractor=wrap(LlmAgent(
        name="ItemExtractor",
        model=_get_settings().model_collector,
        tools=[ml_scrape_item_detail],
        instruction=(
                "You are ItemExtractor.\n"
                "\n"
                "Your job is to enrich items by calling ml_scrape_item_detail for each permalink and produce a complete accounting of successes and failures.\n"
                "\n"
                "You MUST NEVER silently drop a permalink. Every permalink processed MUST produce exactly one record in either \"items\" or \"errors\".\n"
                "\n"
                "--------------------------------------------------\n"
                "STEP 1: Build permalink list\n"
                "--------------------------------------------------\n"
                "\n"
                "PATH A — seller_raw.sellers is NON-EMPTY:\n"
                "  - Collect unique permalinks from seller_raw.sellers[*].sample_permalink.\n"
                "  - Deduplicate.\n"
                "  - Limit to plan.limits.max_items_total.\n"
                "\n"
                "PATH B — seller_raw.sellers is EMPTY and seller_raw.sample_permalinks is NON-EMPTY:\n"
                "  - Use seller_raw.sample_permalinks as the permalink list.\n"
                "  - Deduplicate.\n"
                "  - Limit to plan.limits.max_items_total.\n"
                "\n"
                "PATH C — BOTH seller_raw.sellers and seller_raw.sample_permalinks are EMPTY:\n"
                "  - Return immediately:\n"
                "  {\n"
                "    \"items\": [],\n"
                "    \"errors\": [\n"
                "      {\"stage\": \"ItemExtractor\", \"error\": \"no_input\"},\n"
                "    ],\n"
                "    \"stats\": {\n"
                "      \"total_attempted\": 0,\n"
                "      \"total_enriched\": 0,\n"
                "      \"enrichment_errors\": 0\n"
                "    }\n"
                "  }\n"
                "\n"
                "--------------------------------------------------\n"
                "STEP 2: Enrich EVERY permalink\n"
                "--------------------------------------------------\n"
                "\n"
                "For EACH permalink in the list:\n"
                "\n"
                "  1. Call the tool:\n"
                "       ml_scrape_item_detail(url=<permalink>)\n"
                "\n"
                "  2. Classify the result using STRICT rules:\n"
                "\n"
                "     CASE A — result is NOT a dict/object (null, None, string, list, etc):\n"
                "         Append to errors:\n"
                "         {\n"
                "           \"permalink\": <permalink>,\n"
                "           \"stage\": \"ItemExtractor\",\n"
                "           \"error\": \"invalid_tool_result_type\"\n"
                "         }\n"
                "         CONTINUE.\n"
                "\n"
                "     CASE B — result contains key \"error\":\n"
                "         Append to errors:\n"
                "         {\n"
                "           \"permalink\": <permalink>,\n"
                "           \"stage\": \"ItemExtractor\",\n"
                "           \"error\": result[\"error\"]\n"
                "         }\n"
                "         CONTINUE.\n"
                "\n"
                "     CASE C — result contains key \"ok\" and result[\"ok\"] is false:\n"
                "         Append to errors:\n"
                "         {\n"
                "           \"permalink\": <permalink>,\n"
                "           \"stage\": \"ItemExtractor\",\n"
                "           \"error\": result.get(\"message\", \"tool_returned_ok_false\")\n"
                "         }\n"
                "         CONTINUE.\n"
                "\n"
                "     CASE D — result is a dict and does NOT contain key \"error\":\n"
                "         This is a SUCCESS.\n"
                "\n"
                "         Ensure traceability:\n"
                "           If result does NOT contain key \"permalink\":\n"
                "               Set result[\"permalink\"] = <permalink>\n"
                "\n"
                "         Append the full result dict to items.\n"
                "\n"
                "  3. NEVER skip a permalink.\n"
                "     NEVER stop early.\n"
                "     ALWAYS classify the result.\n"
                "\n"
                "--------------------------------------------------\n"
                "STEP 3: Stats (STRICT ACCOUNTING)\n"
                "--------------------------------------------------\n"
                "\n"
                "Let:\n"
                "\n"
                "  attempted = number of permalinks processed\n"
                "  enriched  = len(items)\n"
                "  errors_n  = len(errors)\n"
                "\n"
                "You MUST enforce:\n"
                "\n"
                "  stats.total_attempted = attempted\n"
                "  stats.total_enriched = enriched\n"
                "  stats.enrichment_errors = errors_n\n"
                "\n"
                "MANDATORY INVARIANT:\n"
                "\n"
                "  attempted MUST equal enriched + errors_n\n"
                "\n"
                "This invariant MUST ALWAYS hold.\n"
                "\n"
                "--------------------------------------------------\n"
                "STEP 4: Output format\n"
                "--------------------------------------------------\n"
                "\n"
                "Return JSON ONLY. No markdown. No explanations.\n"
                "{\n"
                "  \"items\": [ ...NormalizedItem dicts... ],\n"
                "  \"errors\": [\n"
                "    {\"permalink\": \"...\", \"stage\": \"ItemExtractor\", \"error\": \"...\"},\n"
                "  ],\n"
                "  \"stats\": {\n"
                "    \"total_attempted\": <int>,\n"
                "    \"total_enriched\": <int>,\n"
                "    \"enrichment_errors\": <int>\n"
                "  }\n"
                "}\n"
                "\n"
                "\n"
                "Plan:\n"
                "{plan}\n"
                "\n"
                "Sellers:\n"
                "{seller_raw}"
        ),
        output_key="items_raw",
    ))

    qa=wrap(LlmAgent(
        name="QARefiner",
        model=_get_settings().model_qa,
        instruction=(
            "Compute stats and recommendations from the pipeline data.\n"
            "Return ONLY JSON — no markdown, no explanations outside JSON.\n"
            "\n"
            "Output shape:\n"
            "{\n"
            '  "ok": <bool>,\n'
            '  "stats": {\n'
            '    "categories": {\n'
            '      "total_in_plan": <n>,\n'
            '      "total_scraped": <n>,\n'
            '      "items_found": <n>,\n'
            '      "categories_with_zero_items": <n>\n'
            '    },\n'
            '    "sellers": {\n'
            '      "total_in_plan": <n>,\n'
            '      "total_scraped": <n>\n'
            '    },\n'
            '    "items": {\n'
            '      "total_from_categories": <n>,\n'
            '      "total_to_enrich": <n>,\n'
            '      "total_enriched": <n>,\n'
            '      "enrichment_errors": <n>,\n'
            '      "enrichment_rate": "<pct>%"\n'
            '    },\n'
            '    "missing_titles": <n>,\n'
            '    "missing_item_id": <n>,\n'
            '    "missing_identity": <n>\n'
            '  },\n'
            '  "recommendations": ["..."]\n'
            "}\n"
            "\n"
            "Stat definitions:\n"
            "  categories.items_found = total cards in category_raw (all categories combined)\n"
            "  items.total_from_categories = same as categories.items_found\n"
            "  items.total_to_enrich = count of cards where needs_enrichment=true\n"
            "  items.total_enriched = len(items_raw.items) — successfully enriched items\n"
            "  items.enrichment_errors = items_raw.stats.enrichment_errors if present, else len(items_raw.errors)\n"
            "  items.enrichment_rate = total_enriched / total_to_enrich * 100 (as string '12.00%')\n"
            "  missing_titles = count of items_raw.items where title is null, empty, or 'unknown'\n"
            "  missing_item_id = count of items_raw.items where item_id is null (backward compat)\n"
            "  missing_identity = count of items_raw.items where item_id, product_id, up_id, AND channel_item_id are ALL null/empty\n"
            "    (a card with product_id or up_id is NOT missing_identity even if item_id is null)\n"
            "\n"
            "ok = true ONLY if: items.total_enriched > 0 AND missing_identity == 0\n"

"\n\n"
"Plan:\n{plan}\n\n"
"Category:\n{category_raw}\n\n"
"Sellers:\n{seller_raw}\n\n"
"Items:\n{items_raw}"
        ),
        output_key="qa",
    ))

    exporter=wrap(LlmAgent(
        name="Exporter",
        model=_get_settings().model_qa,
        tools=[ml_get_all_sell_listings, ml_query_sell_listings, ml_export_sell_listings],
        instruction=(
            "You are Exporter. Your PRIMARY goal is to export items to the backend.\n"
            "\n"
            "--- STEP 1: Extract items from items_raw ---\n"
            "Parse items_raw as JSON. Extract the 'items' array.\n"
            "items_raw is a JSON string with shape: {\"items\":[...],\"errors\":[...],\"stats\":{...}}\n"
            "The items array is items_raw.items (the top-level 'items' key).\n"
            "\n"
            "--- STEP 2: Query existing listings (OPTIONAL — do NOT block on failure) ---\n"
            "Try to call ml_query_sell_listings(channel='mercadolibre', market='MLM').\n"
            "  - If ok=true: set existing_count = len(sellListingsQuery), new_count = len(items) - existing_count.\n"
            "  - If ok=false OR any error occurs: set existing_count=0, new_count=len(items).\n"
            "  - NEVER let a query failure prevent the export. Always proceed to Step 3.\n"
            "\n"
            "--- STEP 3: Export items (REQUIRED — always execute) ---\n"
            "Call ml_export_sell_listings(items=<items_array>) where <items_array> is the list from Step 1.\n"
            "  - If items_array is empty: call ml_export_sell_listings(items=[]) and record its result.\n"
            "  - This step is MANDATORY regardless of Step 2 outcome.\n"
            "\n"
            "--- OUTPUT ---\n"
            "Return ONLY JSON — no markdown, no explanations outside JSON.\n"
            "{\n"
            '  "status": "exported" | "skipped" | "error",\n'
            '  "as_of": "<ISO UTC timestamp>",\n'
            '  "existing_count": <n>,\n'
            '  "new_count": <n>,\n'
            '  "export": <result from ml_export_sell_listings>\n'
            "}\n"

"\n\n"
"Plan:\n{plan}\n\n"
"QA:\n{qa}\n\n"
"Items:\n{items_raw}"
        ),
        output_key="final_payload",
    ))

    return SequentialAgent(name="MLInventoryScrapePipeline", sub_agents=[planner, category_scout, seller_scout, item_extractor, qa, exporter])


def _rebuild_agents_with_fallback():
    """
    Rebuild all agents with fallback models (OpenAI) after 429 error.
    This allows the pipeline to continue when Gemini rate limit is hit.
    """
    global _settings
    _fallback_active = True
    
    # Ensure settings are set to use OpenAI
    _get_fallback_settings()
    
    _log.warning("Rebuilding agents with fallback OpenAI models after 429 error")
    return build_root_agent()


def run_with_fallback_retry(request_json: dict) -> dict:
    """
    Run the pipeline with automatic fallback to OpenAI models when 429 is hit.
    
    This wraps the ADK runner with retry logic that:
    1. First attempts to run with current settings (Gemini by default)
    2. If 429 RESOURCE_EXHAUSTED error is caught, rebuild agents with OpenAI models
    3. Retry the request with fallback models
    
    Args:
        request_json: The input JSON for the pipeline
        
    Returns:
        The final result from the pipeline (either with original or fallback models)
    """
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.artifacts import InMemoryArtifactService
    
    max_fallback_retries = 2
    
    for attempt in range(max_fallback_retries + 1):
        try:
            # Get current settings (may have changed if fallback was triggered)
            settings = _get_settings()
            
            # Log the model being used
            _log.info(
                "Running pipeline with models: planner=%s, collector=%s, qa=%s (attempt %d/%d)",
                settings.model_planner,
                settings.model_collector,
                settings.model_qa,
                attempt + 1,
                max_fallback_retries + 1
            )
            
            # Build the root agent with current settings
            agent = build_root_agent()
            
            # Create runner and execute
            runner = Runner(
                agent=agent,
                session_service=InMemorySessionService(),
                artifact_service=InMemoryArtifactService()
            )
            
            # Run and return result
            return runner.run(request_json)
            
        except Exception as exc:
            # Check if this is a 429 RESOURCE_EXHAUSTED error
            if _is_resource_exhausted_error(exc) and attempt < max_fallback_retries:
                _log.warning(
                    "429 RESOURCE_EXHAUSTED error on attempt %d: %s. Retrying with fallback models...",
                    attempt + 1,
                    str(exc)[:200]
                )
                
                # Switch to fallback settings
                _get_fallback_settings()
                
                # Continue to next attempt (will use fallback models)
                continue
            else:
                # Re-raise if not a 429 error or no more retries
                raise

