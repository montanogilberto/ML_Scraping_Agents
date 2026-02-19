# TODO: Vertex AI / Gemini Migration

## Steps

- [x] 1. Update `agents/ml_inventory/config/settings.py`:
  - [x] Add Vertex AI credential fields (google_genai_use_vertexai, google_cloud_project, google_cloud_location, google_application_credentials)
  - [x] Change model defaults to `gemini-2.5-pro`
  - [x] Change persist_mode default to `backend`
  - [x] Add model_post_init to propagate Vertex AI env vars

- [x] 2. Update `.env`:
  - [x] Change MODEL_PLANNER, MODEL_COLLECTOR, MODEL_QA to `gemini-2.5-pro`
  - [x] Change PERSIST_MODE to `backend`
  - [x] Fix GOOGLE_APPLICATION_CREDENTIALS to macOS path

## Status
- [x] All steps complete
