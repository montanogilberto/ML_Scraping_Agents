import logging, secrets
logger = logging.getLogger("adk_observer")

def before_agent_callback(callback_context, **kwargs):
    rid = getattr(callback_context, "request_id", None) or secrets.token_hex(6)
    callback_context.request_id = rid
    logger.info("[before_agent] rid=%s", rid)

def after_agent_callback(callback_context, result=None, **kwargs):
    if result is None and 'result' in kwargs:
        result = kwargs.get('result')
    logger.info("[after_agent] rid=%s", getattr(callback_context,"request_id","?"))

def before_model_callback(callback_context, prompt, **kwargs):
    return prompt

def after_model_callback(callback_context, response, **kwargs):
    return response
