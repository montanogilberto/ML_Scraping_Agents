MercadoLibre Inventory Scraping (ADK Multi-Agent)

Enterprise-grade multi-agent scraping pipeline built with **Google's Agent Development Kit (ADK)** and **Gemini 2.5 Pro** (Vertex AI) to extract, validate, enrich, and export MercadoLibre Mexico inventory data into the SmartLoans backend.

---

## Overview

This project implements an autonomous multi-agent system that:

- Scrapes inventory from MercadoLibre Mexico (MLM) sellers and categories
- Extracts structured item data with a 3-layer card classification architecture
- Validates and refines extracted content via a QA agent
- Exports results directly to the SmartLoans backend API (`/sellListings`)
- Deduplicates against existing DB records before export
- Converts MXN prices to USD using live FX rates from the backend

Built using:

- **Google ADK** (Agent Development Kit) — sequential multi-agent orchestration
- **Gemini 2.5 Pro** via **Vertex AI** (Google Cloud project `agent-gcp-2026-487121`)
- **Python 3.12+**
- **MCP Tool Servers** — custom scraping tools as ADK FunctionTools
- **BeautifulSoup + lxml** — HTML parsing
- **Pydantic v2** — data validation and schema enforcement
- **Tenacity** — retry logic with exponential backoff
- **SmartLoans Backend** — Azure-hosted FastAPI (`smartloansbackend.azurewebsites.net`)

---

## Goal

The primary goal is a scalable, reliable inventory extraction pipeline that supports:

- **Arbitrage intelligence** — identify price gaps across channels
- **Competitive analysis** — monitor MercadoLibre seller pricing
- **Inventory monitoring** — track product availability and condition
- **Automated product ingestion** — feed the SmartLoans sell listings database
- **FX-normalized pricing** — all prices stored in USD via live MXN→USD rates

---

## Architecture

The system uses a **sequential multi-agent pipeline** orchestrated by Google ADK:

```
Planner → CategoryScout → SellerScout → ItemExtractor → QARefiner → Exporter
```

Each agent performs a specialized task with its own model, tools, and output key.

---

## Architecture Diagram

```
              ┌──────────────────────────────┐
              │         Input JSON           │
              │  category_urls, seller_ids  │
              └──────────────┬───────────────┘
                             │
                             ▼
                   ┌──────────────────┐
                   │     Planner      │
                   │  validates &     │
                   │  creates plan   │
                   └────────┬─────────┘
                            │  output_key: plan
                            ▼
               ┌────────────────────────┐
               │     CategoryScout      │
               │  ml_scrape_category   │
               │  discovers sellers    │
               └──────────┬────────────┘
                          │  output_key: category_raw
                          ▼
               ┌────────────────────────┐
               │      SellerScout       │
               │ ml_scrape_seller_     │
               │    inventory          │
               │  discovers items      │
               └──────────┬────────────┘
                          │  output_key: seller_raw
                          ▼
               ┌────────────────────────┐
               │     ItemExtractor      │
               │ ml_scrape_item_detail │
               │  extracts full data   │
               └──────────┬────────────┘
                          │  output_key: items_raw
                          ▼
               ┌────────────────────────┐
               │       QARefiner        │
               │  validates & computes │
               │  stats + recs         │
               └──────────┬────────────┘
                          │  output_key: qa
                          ▼
               ┌────────────────────────┐
               │        Exporter        │
               │ ml_query_sell_        │
               │   listings (dedup)    │
               │ ml_export_sell_       │
               │   listings (upsert)   │
               └──────────┬────────────┘
                          │  output_key: final_payload
                          ▼
               ┌────────────────────────┐
               │   SmartLoans Backend   │
               │  POST /sellListings   │
               │  Azure / FastAPI      │
               └────────────────────────┘
```

---

## Project Structure

```
ml-inventory-adk-scrape/
│
├── agent.py                        # Entry point (stdin JSON → pipeline → stdout)
├── requirements.txt
├── README.md
├── BACKEND_CONTRACT.md             # Backend API contract reference
├── TODO.md
├── .env                            # Environment variables (not committed)
├── agent-gcp-2026-487121-*.json   # Vertex AI service account key (not committed)
│
├── agents/
│   └── ml_inventory/
│       ├── agent.py                # Exports root_agent for ADK
│       ├── runner.py               # run_once() helper
│       │
│       ├── workflows/
│       │   └── inventory_pipeline.py   # build_root_agent() — all 6 agents
│       │
│       ├── callbacks/
│       │   ├── observer.py         # before/after model & agent logging
│       │   ├── guardian.py         # GuardianDeContenido — content safety
│       │   └── composer.py         # chain_before_model / chain_after_model
│       │
│       ├── models/
│       │   └── schemas.py          # Pydantic schemas: NormalizedItem, ListingCard, etc.
│       │
│       ├── config/
│       │   └── settings.py         # Settings BaseModel (all env vars)
│       │
│       ├── api/
│       │   └── backend_api.py      # BackendApiClient — all HTTP calls to backend
│       │
│       ├── export/
│       │   └── export_sell_listings.py  # MXN→USD transform + POST /sellListings
│       │
│       └── mcp_servers/
│           └── ml_scrape_mcp/
│               ├── tools.py        # ADK FunctionTools (7 tools)
│               ├── http_client.py  # HttpClient with rate limiting & retries
│               └── parsers.py      # HTML parsers + 3-layer card architecture
│
├── test_dry_run.py
├── test_export.py
└── test_actual_export.py
```

---

## MCP Tools

All scraping tools are registered as ADK `FunctionTool` instances in `tools.py`:

| Tool | Description |
|---|---|
| `ml_scrape_category` | Scrapes a MercadoLibre category URL, returns cards + seller refs |
| `ml_scrape_seller_inventory` | Scrapes a seller's inventory with optional category scoping |
| `ml_scrape_item_detail` | Fetches full item detail page and returns a `NormalizedItem` |
| `ml_persist_items` | Persists items to `stdout`, `file` (NDJSON), or `backend` API |
| `ml_get_all_sell_listings` | `GET /all_sellListings` — fetch all DB listings for dedup |
| `ml_query_sell_listings` | `POST /query_sellListings` — paginated channel/market query |
| `ml_export_sell_listings` | Transforms items + POSTs to `POST /sellListings` (upsert) |

---

## Data Models

Defined in `agents/ml_inventory/models/schemas.py`:

| Model | Purpose |
|---|---|
| `InventoryRequest` | Input schema: country, site, category_urls, seed_seller_ids, limits |
| `Limits` | Scraping limits: max pages, sellers, items |
| `SellerRef` | Seller reference: seller_id, seller_url |
| `ListingCard` | Listing card from category/seller pages with 3-layer ID classification |
| `NormalizedItem` | Full enriched item: title, price_mxn, condition, pictures, attributes |

---

## Backend API

All calls go to `https://smartloansbackend.azurewebsites.net` via `BackendApiClient`:

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/all_sellListings` | Fetch all sell listings (dedup check) |
| `POST` | `/query_sellListings` | Paginated query by channel + market |
| `POST` | `/sellListings` | Upsert scraped listings (main export) |
| `POST` | `/exchange_rate_by_day` | Fetch MXN→USD FX rate for a date |
| `POST` | `/scrape/items/batch` | Batch ingest raw `NormalizedItem` records |

Authentication: `X-Worker-Key` header required for write endpoints.

---

## Installation

### 1. Clone repository

```bash
git clone <repo-url>
cd ml-inventory-adk-scrape
```

### 2. Create virtual environment

**Mac / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows:**
```bash
python -m venv .venv
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file in the project root:

```env
# ── LLM Models (Gemini via Vertex AI) ─────────────────────────────────────
MODEL_PLANNER=gemini-2.5-pro
MODEL_COLLECTOR=gemini-2.5-pro
MODEL_QA=gemini-2.5-pro

# ── HTTP Settings ──────────────────────────────────────────────────────────
HTTP_TIMEOUT_SEC=25
HTTP_RETRIES=3
MIN_DELAY_SEC=1.2
JITTER_SEC=1.0

# ── Persistence ────────────────────────────────────────────────────────────
PERSIST_MODE=backend
OUT_NDJSON=out.ndjson

# ── SmartLoans Backend ─────────────────────────────────────────────────────
BACKEND_BASE_URL=https://smartloansbackend.azurewebsites.net
BACKEND_WORKER_KEY=<your-worker-key>

# ── Vertex AI / Gemini Credentials ────────────────────────────────────────
GOOGLE_GENAI_USE_VERTEXAI=1
GOOGLE_CLOUD_PROJECT=agent-gcp-2026-487121
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_APPLICATION_CREDENTIALS=/path/to/agent-gcp-2026-487121-service-account.json
```

### 5. Vertex AI service account

Place your Google Cloud service account JSON key in the project root and set `GOOGLE_APPLICATION_CREDENTIALS` to its absolute path. The service account must have the **Vertex AI User** role on project `agent-gcp-2026-487121`.

---

## Usage

### Run pipeline via stdin

```bash
echo '{
  "country": "MX",
  "site": "MLM",
  "category_urls": ["https://listado.mercadolibre.com.mx/celulares-smartphones/"],
  "seed_seller_ids": [],
  "limits": {
    "max_pages_per_category": 3,
    "max_sellers_per_category": 50,
    "max_pages_per_seller": 5,
    "max_items_total": 100
  },
  "persist": { "mode": "backend" }
}' | python agent.py
```

### Run with ADK CLI

```bash
adk run agents.ml_inventory
```

### Run with ADK Web UI

```bash
adk web
```

---

## Example Input

```json
{
  "country": "MX",
  "site": "MLM",
  "category_urls": [
    "https://listado.mercadolibre.com.mx/celulares-smartphones/"
  ],
  "seed_seller_ids": [123456789],
  "limits": {
    "max_pages_per_category": 3,
    "max_sellers_per_category": 50,
    "max_pages_per_seller": 5,
    "max_items_total": 100
  },
  "persist": {
    "mode": "backend"
  }
}
```

---

## Output Modes

| Mode | Description |
|---|---|
| `backend` | **Default.** POSTs to `POST /sellListings` with MXN→USD conversion |
| `file` | Appends NDJSON records to `out.ndjson` |
| `stdout` | Prints JSON records to stdout |

---

## Settings Reference

All settings are loaded via `agents/ml_inventory/config/settings.py` (`Settings` BaseModel):

| Setting | Env Var | Default | Description |
|---|---|---|---|
| `model_planner` | `MODEL_PLANNER` | `gemini-2.5-pro` | LLM for Planner agent |
| `model_collector` | `MODEL_COLLECTOR` | `gemini-2.5-pro` | LLM for CategoryScout, SellerScout, ItemExtractor |
| `model_qa` | `MODEL_QA` | `gemini-2.5-pro` | LLM for QARefiner and Exporter |
| `http_timeout_sec` | `HTTP_TIMEOUT_SEC` | `25` | HTTP request timeout |
| `min_delay_sec` | `MIN_DELAY_SEC` | `1.2` | Minimum delay between requests |
| `jitter_sec` | `JITTER_SEC` | `1.0` | Random jitter added to delay |
| `persist_mode` | `PERSIST_MODE` | `backend` | Output mode: backend / file / stdout |
| `out_ndjson` | `OUT_NDJSON` | `out.ndjson` | NDJSON output path (file mode) |
| `backend_base_url` | `BACKEND_BASE_URL` | — | SmartLoans backend base URL |
| `backend_worker_key` | `BACKEND_WORKER_KEY` | — | X-Worker-Key for backend auth |
| `google_genai_use_vertexai` | `GOOGLE_GENAI_USE_VERTEXAI` | `1` | Use Vertex AI backend |
| `google_cloud_project` | `GOOGLE_CLOUD_PROJECT` | — | GCP project ID |
| `google_cloud_location` | `GOOGLE_CLOUD_LOCATION` | `us-central1` | GCP region |
| `google_application_credentials` | `GOOGLE_APPLICATION_CREDENTIALS` | — | Path to service account JSON |
| `fx_rate_to_usd` | `FX_RATE_TO_USD` | auto | MXN→USD rate (fetched from backend or env) |

---

## Security

**Never commit:**

- `.env`
- Service account JSON (`agent-gcp-2026-487121-*.json`)
- API keys or worker keys

Both are listed in `.gitignore`.

---

## Roadmap

- Parallel agent execution for faster scraping
- Proxy rotation support
- Headless browser fallback for JS-rendered pages
- ML scoring integration (price anomaly detection)
- Distributed execution via Azure Functions
- Retry queue for failed item enrichments

---

## License

Private / Enterprise Use — SmartLoans

---

## Author

**Gilberto Montaño**  
Enterprise Arbitrage and Automation Systems Architect
