"""LLM client with primary/fallback provider support via litellm."""

from __future__ import annotations

import litellm


class LLMClient:
    """Sends chat completion requests with automatic fallback."""

    def __init__(self, config: dict):
        # config has 'primary' (required) and 'fallback' (optional)
        # Each provider dict has: provider, model, endpoint (optional), api_key (optional)
        self.primary = config["primary"]
        self.fallback = config.get("fallback")
        self.timeout = config.get("timeout", 30)

    def chat(self, messages: list[dict], **kwargs) -> str:
        """Send chat completion request. Try primary, fall back if timeout/error."""
        try:
            return self._call(self.primary, messages, **kwargs)
        except Exception:
            if self.fallback:
                return self._call(self.fallback, messages, **kwargs)
            raise

    def _call(self, provider: dict, messages: list[dict], **kwargs) -> str:
        """Call litellm.completion with the given provider config."""
        model = self._build_model_string(provider)
        call_kwargs: dict = {
            "model": model,
            "messages": messages,
            "timeout": self.timeout,
            **kwargs,
        }
        if provider.get("api_key"):
            call_kwargs["api_key"] = provider["api_key"]
        if provider.get("endpoint"):
            call_kwargs["api_base"] = provider["endpoint"]

        response = litellm.completion(**call_kwargs)
        return response.choices[0].message.content

    def update_config(self, config: dict):
        """Hot-swap LLM configuration. Takes effect on next chat() call."""
        self.primary = config["primary"]
        self.fallback = config.get("fallback")
        if "timeout" in config:
            self.timeout = config["timeout"]

    def _build_model_string(self, provider: dict) -> str:
        """Build litellm model string like 'ollama/deepseek-coder-v3'."""
        return f"{provider['provider']}/{provider['model']}"
