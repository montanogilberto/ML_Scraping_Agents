import json
import logging
import re
import secrets

logger = logging.getLogger("adk_observer")

# Known pipeline state keys that must contain plain JSON (no markdown fences).
# After each agent writes its output_key, we strip any ``` fences so that
# downstream agents always receive parseable JSON.
_JSON_STATE_KEYS = frozenset({
    "plan", "category_raw", "seller_raw", "items_raw", "qa", "final_payload"
})


def _strip_markdown_json(value: str) -> str:
    """Strip markdown code fences from a JSON string.

    Handles both:
      ```json          ```
      { ... }    and   { ... }
      ```              ```
    Returns the original string unchanged if no fences are detected.
    """
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    # Remove opening fence: ```json or ``` (with optional trailing whitespace/newline)
    stripped = re.sub(r'^```(?:json)?\s*\n?', '', stripped)
    # Remove closing fence
    stripped = re.sub(r'\n?```\s*$', '', stripped)
    return stripped.strip()


# Safe JSON defaults for every pipeline state key.
# These are written ONLY when the key is absent from session state,
# so a successfully-written output_key is never overwritten.
_PIPELINE_STATE_DEFAULTS: dict[str, str] = {
    "plan":         "{}",
    "category_raw": '{"categories": []}',
    "seller_raw":   '{"sellers": [], "sample_permalinks": [], "notes": []}',
    "items_raw":    '{"items": [], "errors": [], "stats": {"total_attempted": 0, "total_enriched": 0, "enrichment_errors": 0}}',
    "qa":           '{"ok": false, "stats": {}, "recommendations": []}',
}


def _summarise_items_raw(raw_value: str) -> str:
    """Return a short diagnostic string for the items_raw state value."""
    if not raw_value:
        return "items_raw=<empty>"
    try:
        data = json.loads(raw_value)
        items = data.get("items", [])
        errors = data.get("errors", [])
        stats = data.get("stats", {})
        return (
            f"items_raw: items={len(items)}, errors={len(errors)}, "
            f"stats={stats}"
        )
    except (json.JSONDecodeError, TypeError):
        preview = str(raw_value)[:120].replace("\n", " ")
        return f"items_raw=<invalid JSON> preview={preview!r}"


def before_agent_callback(callback_context, **kwargs):
    rid = getattr(callback_context, "request_id", None) or secrets.token_hex(6)
    callback_context.request_id = rid

    agent_name = getattr(callback_context, "agent_name", None) or "?"
    logger.info("[before_agent] rid=%s agent=%s", rid, agent_name)

    # Guard: ensure every pipeline variable referenced in instruction templates
    # exists in session state.  ADK raises KeyError (not a soft miss) when a
    # {placeholder} is absent, which crashes the entire pipeline.  Setting a
    # safe default here lets downstream agents degrade gracefully instead.
    state = getattr(callback_context, "state", None)
    if state is not None:
        for key, default in _PIPELINE_STATE_DEFAULTS.items():
            if key not in state:
                logger.warning(
                    "[before_agent] rid=%s agent=%s — state key '%s' missing; injecting default.",
                    rid, agent_name, key,
                )
                state[key] = default

        # Log items_raw summary before Exporter runs so we can diagnose data loss
        if agent_name in ("Exporter", "QARefiner"):
            logger.info(
                "[before_agent] rid=%s agent=%s - %s",
                rid, agent_name, _summarise_items_raw(state.get("items_raw", "")),
            )


def after_agent_callback(callback_context, result=None, **kwargs):
    if result is None and "result" in kwargs:
        result = kwargs.get("result")

    agent_name = getattr(callback_context, "agent_name", None) or "?"
    rid = getattr(callback_context, "request_id", "?")
    logger.info("[after_agent] rid=%s agent=%s", rid, agent_name)

    state = getattr(callback_context, "state", None)
    if state is not None:
        # Strip markdown code fences from every known JSON output key.
        # LLMs sometimes wrap their JSON output in ```json ... ``` despite
        # instructions to the contrary.  Cleaning here ensures downstream
        # agents always receive parseable JSON regardless of which agent wrote
        # the value.
        for key in _JSON_STATE_KEYS:
            if key in state and isinstance(state[key], str):
                cleaned = _strip_markdown_json(state[key])
                if cleaned != state[key]:
                    logger.info(
                        "[after_agent] rid=%s agent=%s — stripped markdown fences from state key '%s'",
                        rid, agent_name, key,
                    )
                    state[key] = cleaned

        # Log items_raw summary after ItemExtractor so we can confirm data was written
        if agent_name == "ItemExtractor":
            logger.info(
                "[after_agent] rid=%s agent=%s - %s",
                rid, agent_name, _summarise_items_raw(state.get("items_raw", "")),
            )


def before_model_callback(callback_context, prompt, **kwargs):
    return prompt


def after_model_callback(callback_context, response, **kwargs):
    return response
