from __future__ import annotations
import json
from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.artifacts import InMemoryArtifactService
from .agent import root_agent

def run_once(request_json: dict) -> dict:
    load_dotenv()
    runner = Runner(agent=root_agent, session_service=InMemorySessionService(), artifact_service=InMemoryArtifactService())
    return runner.run(json.dumps(request_json, ensure_ascii=False))
