class GuardianDeContenido:
    def before_model_callback(self, callback_context, prompt: str, **kwargs) -> str:
        return prompt
