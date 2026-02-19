import os
from pydantic import BaseModel, Field

class Settings(BaseModel):
    model_planner: str = Field(default_factory=lambda: os.getenv("MODEL_PLANNER","openai/gpt-4o-mini"))
    model_collector: str = Field(default_factory=lambda: os.getenv("MODEL_COLLECTOR","openai/gpt-4o-mini"))
    model_qa: str = Field(default_factory=lambda: os.getenv("MODEL_QA","openai/gpt-4o-mini"))
    http_timeout_sec: float = Field(default_factory=lambda: float(os.getenv("HTTP_TIMEOUT_SEC","25")))
    min_delay_sec: float = Field(default_factory=lambda: float(os.getenv("MIN_DELAY_SEC","1.2")))
    jitter_sec: float = Field(default_factory=lambda: float(os.getenv("JITTER_SEC","1.0")))
    persist_mode: str = Field(default_factory=lambda: os.getenv("PERSIST_MODE","file"))
    out_ndjson: str = Field(default_factory=lambda: os.getenv("OUT_NDJSON","out.ndjson"))
    backend_base_url: str = Field(default_factory=lambda: os.getenv("BACKEND_BASE_URL","").rstrip("/"))
    backend_worker_key: str = Field(default_factory=lambda: os.getenv("BACKEND_WORKER_KEY",""))

def load_settings() -> Settings:
    return Settings()
