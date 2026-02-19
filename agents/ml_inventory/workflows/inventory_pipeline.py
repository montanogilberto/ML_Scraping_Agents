from google.adk.agents.llm_agent import LlmAgent
from google.adk.agents import SequentialAgent
from ..config.settings import load_settings
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

settings=load_settings()

def build_root_agent():
    guardian=GuardianDeContenido()
    secure_before=chain_before_model(guardian.before_model_callback, before_model_callback)
    secure_after=chain_after_model(after_model_callback)

    def wrap(a: LlmAgent)->LlmAgent:
        a.before_agent_callback=before_agent_callback
        a.after_agent_callback=after_agent_callback
        a.before_model_callback=secure_before
        a.after_model_callback=secure_after
        return a

    planner=wrap(LlmAgent(
        name="Planner",
        model=settings.model_planner,
        instruction=(
            "Validate input JSON and output ONLY JSON plan with fields: "
            "{{country, site, category_urls, seed_seller_ids, limits:{{max_pages_per_category,max_sellers_per_category,max_pages_per_seller,max_items_total}}, persist:{{mode}}}} "
            'If invalid, output {{"error":"..."}}.'
        ),
        output_key="plan",
    ))

    category_scout=wrap(LlmAgent(
        name="CategoryScout",
        model=settings.model_collector,
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
        model=settings.model_collector,
        tools=[ml_scrape_seller_inventory],
        instruction=(
            "Build unique seller list from plan.seed_seller_ids + category_raw.categories[*].sellers[*].seller_id. "
            "Limit total sellers to 500. "
            "IMPORTANT: For each seller, use category-scoped URL to get POS-relevant items! "
            "Call ml_scrape_seller_inventory with: "
            "  - seller_id: the seller ID "
            "  - category_id: 'AD' (default) for all categories, or use category from category_raw "
            "  - max_pages: plan.limits.max_pages_per_seller "
            "  - max_cards: 20 (to stay within context limits) "
            "The new URL pattern is: https://listado.mercadolibre.com.mx/_CustId_{{seller_id}}_PrCategId_{{category_id}} "
            "This returns only items in the specified category, filtering out irrelevant items (auto parts, etc.). "
            'Return ONLY JSON: {"sellers":[{"seller_id":...,"seller_url":...,"card_count":...,"cards":[...],"sample_permalink":...,"stats":{...}}]}. '
            "Include up to 20 cards per seller in the cards array - these are the POS-relevant items for enrichment."

"\n\n"
"Plan:\n{plan}\n\n"
"Category (use category_url to extract category_id):\n{category_raw}"
        ),
        output_key="seller_raw",
    ))

    item_extractor=wrap(LlmAgent(
        name="ItemExtractor",
        model=settings.model_collector,
        tools=[ml_scrape_item_detail],
        instruction=(
            "Extract unique permalinks from seller_raw.sellers[*].sample_permalink. "
            "Stop at plan.limits.max_items_total (e.g., 100 items). "
            "Call ml_scrape_item_detail(url=...) for each. "
            'Return ONLY JSON: {"items":[...],"errors":[]}. Return max 100 items to stay within context limits.'

"\n\n"
"Plan:\n{plan}\n\n"
"Sellers (use sample_permalinks only):\n{seller_raw}"
        ),
        output_key="items_raw",
    ))

    qa=wrap(LlmAgent(
        name="QARefiner",
        model=settings.model_qa,
        instruction=(
            "Compute stats and recommendations. Return ONLY JSON with "
            "{ok, stats:{categories,sellers,items,missing_titles,missing_item_id}, recommendations:[...]}. "
            "Use plan, category_raw, seller_raw, items_raw."

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
        model=settings.model_qa,
        tools=[ml_get_all_sell_listings, ml_query_sell_listings, ml_export_sell_listings],
        instruction=(
            "Before exporting, validate which items already exist in the database:\n"
            "1. Call ml_query_sell_listings(channel='mercadolibre', market='MX') to fetch "
            "existing listings for this channel/market from the backend DB.\n"
            "2. Compare the returned channelItemIds against items_raw.items to identify "
            "new vs already-existing items.\n"
            "3. Call ml_export_sell_listings(items=items_raw.items) to send ALL scraped items "
            "to the sellListings backend API (the backend handles upsert). Do NOT write to a file.\n"
            "Return ONLY JSON: {status, as_of, plan, stats, items, existing_count, new_count, export} "
            "where export is the result returned by ml_export_sell_listings "
            "(fields: ok, status_code, exported_count, emitted, skipped, fx_rate_to_usd, fx_as_of_date, backend_response), "
            "existing_count is the number of items already in DB, "
            "new_count is the number of genuinely new items."

"\n\n"
"Plan:\n{plan}\n\n"
"QA:\n{qa}\n\n"
"Items:\n{items_raw}"
        ),
        output_key="final_payload",
    ))

    return SequentialAgent(name="MLInventoryScrapePipeline", sub_agents=[planner, category_scout, seller_scout, item_extractor, qa, exporter])

