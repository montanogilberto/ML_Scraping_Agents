# ML Scraping Agents (MercadoLibre Inventory Pipeline)

Enterprise-grade multi-agent scraping pipeline built with Google's Agent Development Kit (ADK) to extract, validate, enrich, and export inventory data from MercadoLibre.

This system uses a structured sequential agent workflow with MCP scraping tools, validation layers, and backend integration.

---

# Overview

This project implements an autonomous multi-agent system that:

* Scrapes inventory from MercadoLibre sellers and categories
* Extracts structured item data
* Validates and refines extracted content
* Exports results to backend API, NDJSON, or stdout
* Enables scalable, automated product intelligence pipelines

Built using:

* Google ADK (Agent Development Kit)
* Python 3.12+
* MCP Tool Servers
* BeautifulSoup parsing
* FastAPI backend integration
* Enterprise pipeline architecture

---

# Goal

The primary goal of this system is to create a scalable and reliable inventory extraction pipeline that supports:

* Arbitrage intelligence
* Competitive analysis
* Inventory monitoring
* Automated product ingestion
* ML feature pipelines
* Enterprise product data lakes

This system integrates with backend services such as:

* Azure Functions Workers
* FastAPI Backend
* SQL Server / Delta Lake
* ML Scoring Pipelines

---

# Architecture

The system uses a sequential multi-agent pipeline:

Planner → CategoryScout → SellerScout → ItemExtractor → QARefiner → Exporter

Each agent performs a specialized task.

---

# Architecture Diagram

```
                ┌──────────────────────────┐
                │        Input JSON        │
                │ category, seller, query │
                └─────────────┬────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │     Planner     │
                    │ creates plan   │
                    └────────┬────────┘
                             │
                             ▼
                ┌─────────────────────────┐
                │    CategoryScout       │
                │ discovers sellers     │
                └──────────┬────────────┘
                           │
                           ▼
                ┌─────────────────────────┐
                │     SellerScout        │
                │ discovers items       │
                └──────────┬────────────┘
                           │
                           ▼
                ┌─────────────────────────┐
                │    ItemExtractor       │
                │ extracts full details │
                └──────────┬────────────┘
                           │
                           ▼
                ┌─────────────────────────┐
                │      QARefiner         │
                │ validates data        │
                └──────────┬────────────┘
                           │
                           ▼
                ┌─────────────────────────┐
                │       Exporter         │
                │ stdout / file / API   │
                └──────────┬────────────┘
                           │
                           ▼
                  ┌─────────────────────┐
                  │   Backend API      │
                  │ FastAPI / SQL     │
                  └─────────────────────┘
```

---

# Project Structure

```
ml-inventory-adk-scrape/
│
├── agent.py
├── requirements.txt
├── README.md
├── TODO.md
├── BACKEND_CONTRACT.md
│
├── agents/
│   └── ml_inventory/
│       ├── agent.py
│       ├── runner.py
│       │
│       ├── workflows/
│       │   └── inventory_pipeline.py
│       │
│       ├── callbacks/
│       │   ├── observer.py
│       │   ├── guardian.py
│       │   └── composer.py
│       │
│       ├── models/
│       │   └── schemas.py
│       │
│       ├── config/
│       │   └── settings.py
│       │
│       └── mcp_servers/
│           └── ml_scrape_mcp/
│               ├── tools.py
│               ├── http_client.py
│               └── parsers.py
```

---

# Installation

## 1. Clone repository

```
git clone https://github.com/montanogilberto/ML_Scraping_Agents.git
cd ML_Scraping_Agents
```

---

## 2. Create virtual environment

Mac / Linux:

```
python3 -m venv .venv
source .venv/bin/activate
```

Windows:

```
python -m venv .venv
.venv\Scripts\activate
```

---

## 3. Install dependencies

```
pip install -r requirements.txt
```

---

## 4. Configure environment variables

Create `.env` file:

```
BACKEND_API_URL=https://yourbackend.azurewebsites.net
EXPORT_MODE=stdout
MAX_ITEMS=100
REQUEST_TIMEOUT=30
```

---

## 5. Configure Google ADK authentication (if using Vertex AI)

```
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account.json"
export GOOGLE_CLOUD_PROJECT="your-project-id"
export GOOGLE_CLOUD_LOCATION="us-central1"
```

---

# Usage

## Run pipeline

```
python agent.py
```

or

```
python -m agents.ml_inventory.runner
```

---

## Example input

```
{
  "site": "MLM",
  "category": "MLM1055",
  "limit": 10,
  "export_mode": "stdout"
}
```

---

## Output modes

Supported export modes:

* stdout
* file (NDJSON)
* backend API

---

# Data Flow

```
MercadoLibre
     ↓
HTTP Client
     ↓
Parsers
     ↓
Agents Pipeline
     ↓
Validation
     ↓
Exporter
     ↓
Backend API / File / stdout
```

---

# MCP Tool Servers

The system uses MCP tools for scraping:

* ml_scrape_category
* ml_scrape_seller_inventory
* ml_scrape_item_detail
* ml_persist_items

Located in:

```
agents/ml_inventory/mcp_servers/ml_scrape_mcp/
```

---

# Security

Never commit:

* .env
* service account JSON
* API keys
* secrets

Use `.gitignore`.

---

# Enterprise Integration

Compatible with:

* Azure Functions
* FastAPI backend
* SQL Server
* Delta Lake
* Databricks
* ML pipelines

---

# Roadmap

Future improvements:

* Parallel agents
* Retry queues
* Proxy rotation
* Headless browser support
* ML scoring integration
* Distributed execution

---

# License

Private / Enterprise Use

---

# Author

Gilberto Montaño
Enterprise Arbitrage and Automation Systems Architect

---

# Support

For issues or enterprise integration support, contact repository owner.

---

