# TODO: Fix "Model gpt-4o not found" error

## Issue
Google ADK doesn't automatically recognize OpenAI models like `gpt-4o`. They need to be prefixed with the provider (e.g., `openai/gpt-4o`).

## Plan
- [x] 1. Update `settings.py` - Change model defaults to use prefixed format
- [x] 2. Update `inventory_pipeline.py` - Update fallback model constants

## Status: Completed


