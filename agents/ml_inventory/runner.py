from __future__ import annotations
import json
import os

# Load .env FIRST, before any other imports
from dotenv import load_dotenv
load_dotenv()

# Settings now automatically force OpenAI-only mode
# GOOGLE_GENAI_USE_VERTEXAI is always set to "0" in settings

# Now import after env vars are configured
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.artifacts import InMemoryArtifactService
from .agent import root_agent

def run_once(request_json: dict) -> dict:
    """Run the pipeline with automatic fallback retry for 429 errors."""
    from .workflows.inventory_pipeline import run_with_fallback_retry
    return run_with_fallback_retry(request_json)
