def chain_before_model(*fns):
    def _ch(callback_context, prompt=None, **kwargs):
        # Handle both positional and keyword argument calling styles
        if prompt is None and 'prompt' in kwargs:
            prompt = kwargs.pop('prompt')
        for fn in fns:
            result = fn(callback_context, prompt, **kwargs)
            if result is not None:
                prompt = result
        return prompt
    return _ch

def chain_after_model(*fns):
    def _ch(callback_context, response=None, **kwargs):
        # Handle both positional and keyword argument calling styles
        if response is None and 'response' in kwargs:
            response = kwargs.pop('response')
        for fn in fns:
            result = fn(callback_context, response, **kwargs)
            if result is not None:
                response = result
        return response
    return _ch
