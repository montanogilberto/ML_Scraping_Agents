import os
from pydantic import BaseModel, Field, model_validator

class Settings(BaseModel):
    # ── LLM Models ────────────────────────────────────────────────────────────
    model_planner: str = Field(default_factory=lambda: os.getenv("MODEL_PLANNER", "gemini-2.5-pro"))
    model_collector: str = Field(default_factory=lambda: os.getenv("MODEL_COLLECTOR", "gemini-2.5-pro"))
    model_qa: str = Field(default_factory=lambda: os.getenv("MODEL_QA", "gemini-2.5-pro"))

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
    google_genai_use_vertexai: bool = Field(
        default_factory=lambda: os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "1") in ("1", "true", "True", "TRUE")
    )
    google_cloud_project: str = Field(
        default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT", "")
    )
    google_cloud_location: str = Field(
        default_factory=lambda: os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    )
    google_application_credentials: str = Field(
        default_factory=lambda: os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    )

    # ── Sell Listings Export Configuration ────────────────────────────────────
    fx_rate_to_usd: float = Field(default_factory=lambda: _load_fx_rate())
    sell_listings_backend_url: str = Field(default_factory=lambda: _load_sell_listings_url())

    @model_validator(mode="after")
    def _propagate_vertex_env_vars(self) -> "Settings":
        """
        Ensure Vertex AI env vars are set in the process environment so that
        Google ADK and google-auth libraries pick them up automatically,
        even if they were loaded from .env after the process started.
        """
        if self.google_genai_use_vertexai:
            os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "1")
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
    """Load FX_RATE_TO_USD from backend API or env var.
    
    First tries to fetch from backend endpoint exchange_rate_by_day,
    then falls back to environment variable.
    """
    from datetime import datetime, timezone
    
    # Try to fetch from backend first
    base_url = os.getenv("BACKEND_BASE_URL", "").rstrip("/")
    worker_key = os.getenv("BACKEND_WORKER_KEY", "")
    
    if base_url and worker_key:
        try:
            import requests
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            response = requests.post(
                f"{base_url}/exchange_rate_by_day",
                headers={"accept": "application/json", "Content-Type": "application/json"},
                json={"exchangeRates": [{"asOfDate": today}]},
                timeout=10
            )
            if response.ok:
                data = response.json()
                rates = data.get("exchangeRates", [])
                if rates:
                    rate = rates[0].get("rate")
                    if rate:
                        return float(rate)
        except Exception:
            pass  # Fall back to env var
    
    # Fall back to environment variable
    fx_rate = os.getenv("FX_RATE_TO_USD")
    if fx_rate is None:
        raise ValueError("Required environment variable FX_RATE_TO_USD is not set. Please set it to the current MXN to USD exchange rate (e.g., 0.05842), or configure BACKEND_BASE_URL and BACKEND_WORKER_KEY to fetch it automatically.")
    try:
        return float(fx_rate)
    except ValueError:
        raise ValueError(f"Invalid FX_RATE_TO_USD value: '{fx_rate}'. Must be a valid float.")

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
