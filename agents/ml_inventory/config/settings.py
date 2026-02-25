import logging
import os
from typing import Optional
from pydantic import BaseModel, Field, model_validator

_log = logging.getLogger(__name__)

# Ensure .env is loaded at import time
from dotenv import load_dotenv
load_dotenv()

# Module-level cache: FX rate is fetched once per process lifetime.
# This prevents repeated HTTP calls (one per load_settings() invocation) and
# makes the pipeline resilient to transient backend failures after the first
# successful fetch.
_fx_rate_cache: Optional[float] = None


class Settings(BaseModel):
    # ── LLM Models ────────────────────────────────────────────────────────────
    # Force OpenAI GPT models by default - NEVER use Gemini/Vertex
    # We use the "openai/" prefix to tell Google ADK to use OpenAI provider
    model_planner: str = Field(default="openai/gpt-4o")
    model_collector: str = Field(default="openai/gpt-4o-mini")
    model_qa: str = Field(default="openai/gpt-4o-mini")

    # ── LLM Cost Control ──────────────────────────────────────────────────────
    # Limit max tokens to reduce cost (None = use model default)
    max_tokens: Optional[int] = Field(default_factory=lambda: int(os.getenv("MAX_TOKENS", "8192")) if os.getenv("MAX_TOKENS") else None)
    # Temperature: lower = more deterministic = fewer tokens
    temperature: float = Field(default_factory=lambda: float(os.getenv("TEMPERATURE", "0.5")))
    # Enable caching to reduce costs (works with Gemini models)
    enable_caching: bool = Field(default_factory=lambda: os.getenv("ENABLE_CACHING", "1") in ("1", "true", "True", "TRUE"))

    # ── Retry Settings for 429 Errors ─────────────────────────────────────────
    # Max retry attempts for RESOURCE_EXHAUSTED errors
    max_retries: int = Field(default_factory=lambda: int(os.getenv("MAX_RETRIES", "3")))
    # Base delay for exponential backoff (seconds)
    retry_backoff_base: float = Field(default_factory=lambda: float(os.getenv("RETRY_BACKOFF_BASE", "2.0")))
    # Max delay between retries (seconds)
    retry_backoff_max: float = Field(default_factory=lambda: float(os.getenv("RETRY_BACKOFF_MAX", "60.0")))

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    # Minimum delay between LLM API calls to avoid rate limiting
    min_delay_between_calls: float = Field(default_factory=lambda: float(os.getenv("MIN_DELAY_BETWEEN_CALLS", "1.0")))

    # ── HTTP / Rate-limit ─────────────────────────────────────────────────────
    http_timeout_sec: float = Field(default_factory=lambda: float(os.getenv("HTTP_TIMEOUT_SEC", "25")))
    min_delay_sec: float = Field(default_factory=lambda: float(os.getenv("MIN_DELAY_SEC", "1.2")))
    jitter_sec: float = Field(default_factory=lambda: float(os.getenv("JITTER_SEC", "1.0")))

    # ── Persistence ───────────────────────────────────────────────────────────
    persist_mode: str = Field(default_factory=lambda: os.getenv("PERSIST_MODE", "backend"))
    out_ndjson: str = Field(default_factory=lambda: os.getenv("OUT_NDJSON", "out.ndjson"))

    # ── Backend API ───────────────────────────────────────────────────────────
    backend_base_url: str = Field(default_factory=lambda: os.getenv("BACKEND_BASE_URL", "").rstrip("/"))
    backend_worker_key: str = Field(default_factory=lambda: os.getenv("BACKEND_WORKER_KEY", ""))

    # ── Vertex AI / Gemini Credentials ────────────────────────────────────────
    # ALWAYS DISABLE Vertex AI - we use OpenAI only
    # This setting is ignored and forced to False
    google_genai_use_vertexai: bool = Field(default=False)
    google_cloud_project: str = Field(default="")
    google_cloud_location: str = Field(default="us-central1")
    google_application_credentials: str = Field(default="")

    # ── Sell Listings Export Configuration ────────────────────────────────────
    fx_rate_to_usd: float = Field(default_factory=lambda: _load_fx_rate())
    sell_listings_backend_url: str = Field(default_factory=lambda: _load_sell_listings_url())

    @model_validator(mode="after")
    def _propagate_vertex_env_vars(self) -> "Settings":
        """
        Always force Vertex AI to be disabled.
        We use OpenAI exclusively - never Gemini/Vertex.
        """
        # ALWAYS disable Vertex AI - we use OpenAI only
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "0"
        self.google_genai_use_vertexai = False
        
        # Clear Google Cloud settings since we're not using them
        if self.google_cloud_project:
            os.environ.setdefault("GOOGLE_CLOUD_PROJECT", self.google_cloud_project)
        if self.google_cloud_location:
            os.environ.setdefault("GOOGLE_CLOUD_LOCATION", self.google_cloud_location)
        if self.google_application_credentials:
            os.environ.setdefault(
                "GOOGLE_APPLICATION_CREDENTIALS", self.google_application_credentials
            )
        return self

def _load_fx_rate() -> float:
    """Load FX_RATE_TO_USD from cache, backend API, or env var.

    Priority:
      1. Module-level cache — avoids repeated HTTP calls across load_settings()
         invocations within the same process (e.g. one per tool function call).
      2. Backend API /exchange_rate_by_day — public endpoint, no auth required.
         Only needs BACKEND_BASE_URL (defaults to the known backend host).
      3. FX_RATE_TO_USD environment variable — manual override / CI fallback.

    Raises ValueError only when all three sources are unavailable.
    """
    global _fx_rate_cache
    from datetime import datetime, timezone

    # 1. Return cached value — prevents re-fetching on every load_settings() call
    if _fx_rate_cache is not None:
        _log.debug("FX rate served from cache: %s", _fx_rate_cache)
        return _fx_rate_cache

    # 2. Fetch from backend API.
    #    The /exchange_rate_by_day endpoint is PUBLIC — no X-Worker-Key needed.
    #    Use the same default URL as backend_api.py so the call works even when
    #    BACKEND_BASE_URL is absent from .env.
    #
    #    The backend only stores rates for dates that have been populated.
    #    If today has no entry (HTTP 500), we walk back up to 7 days to find
    #    the most recent available rate.
    _DEFAULT_BACKEND = "https://smartloansbackend.azurewebsites.net"
    base_url = os.getenv("BACKEND_BASE_URL", _DEFAULT_BACKEND).rstrip("/")

    if base_url:
        try:
            import requests
            from datetime import timedelta

            now_utc = datetime.now(timezone.utc)
            # Try today first, then walk back up to 7 days
            for days_back in range(8):
                candidate_date = (now_utc - timedelta(days=days_back)).strftime("%Y-%m-%d")
                try:
                    response = requests.post(
                        f"{base_url}/exchange_rate_by_day",
                        headers={"accept": "application/json", "Content-Type": "application/json"},
                        json={"exchangeRates": [{"asOfDate": candidate_date}]},
                        timeout=10,
                    )
                except Exception as req_exc:
                    _log.warning("Backend request failed for %s: %s", candidate_date, req_exc)
                    break  # Network error — no point retrying other dates

                if response.ok:
                    data = response.json()
                    rates = data.get("exchangeRates", [])
                    if rates:
                        rate = rates[0].get("rate")
                        if rate is not None:
                            _fx_rate_cache = float(rate)
                            _log.info(
                                "FX rate fetched from backend for %s (days_back=%d) and cached: %s",
                                candidate_date, days_back, _fx_rate_cache,
                            )
                            return _fx_rate_cache
                    # Response OK but no rate data — try previous day
                    _log.debug("No rate data for %s, trying previous day.", candidate_date)
                else:
                    # Non-2xx (e.g. 500 when date has no entry) — try previous day
                    _log.debug(
                        "Backend returned HTTP %s for %s, trying previous day.",
                        response.status_code, candidate_date,
                    )

            _log.warning(
                "Backend /exchange_rate_by_day had no rate for the last 8 days — "
                "falling back to FX_RATE_TO_USD env var."
            )
        except Exception as exc:
            _log.warning(
                "Backend FX rate fetch failed (%s) — falling back to FX_RATE_TO_USD env var.",
                exc,
            )

    # 3. Fall back to environment variable
    fx_rate = os.getenv("FX_RATE_TO_USD")
    if fx_rate is not None:
        try:
            _fx_rate_cache = float(fx_rate)
            _log.info("FX rate loaded from FX_RATE_TO_USD env var: %s", _fx_rate_cache)
            return _fx_rate_cache
        except ValueError:
            raise ValueError(
                f"Invalid FX_RATE_TO_USD value: '{fx_rate}'. Must be a valid float."
            )

    raise ValueError(
        "Could not determine FX_RATE_TO_USD. Tried: "
        "(1) module cache, "
        f"(2) backend API at {base_url}/exchange_rate_by_day, "
        "(3) FX_RATE_TO_USD env var. "
        "Please set FX_RATE_TO_USD in your .env file (e.g. 0.05842) "
        "or ensure BACKEND_BASE_URL is reachable."
    )

def _load_sell_listings_url() -> str:
    """Load SELL_LISTINGS_BACKEND_URL from env var, or derive from BACKEND_BASE_URL."""
    custom_url = os.getenv("SELL_LISTINGS_BACKEND_URL", "").strip()
    if custom_url:
        return custom_url.rstrip("/")
    base_url = os.getenv("BACKEND_BASE_URL", "").rstrip("/")
    if not base_url:
        raise ValueError("Either SELL_LISTINGS_BACKEND_URL or BACKEND_BASE_URL must be set.")
    return f"{base_url}/sellListings"

def load_settings() -> Settings:
    return Settings()


# Module-level settings cache to avoid repeated initialization
_settings_cache: Optional[Settings] = None


def _get_settings() -> Settings:
    """Get cached settings, loading from .env if needed."""
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = load_settings()
    return _settings_cache


def reset_settings_cache() -> Settings:
    """Reset the settings cache and return fresh settings.
    
    This is useful when we need to reload settings after environment
    variables have changed (e.g., after switching to fallback models).
    """
    global _settings_cache
    _settings_cache = None
    return _get_settings()
