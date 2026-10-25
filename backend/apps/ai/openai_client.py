from django.conf import settings
from openai import OpenAI, OpenAIError

from apps.sources.models import SourceConfiguration


class OpenAIBackendError(RuntimeError):
    pass


class OpenAICompatibleClient:
    def __init__(self, *, api_key=None, base_url=None, model=None, client=None):
        config = SourceConfiguration.effective_settings(
            "openai",
            {
                "api_key": settings.OPENAI_API_KEY,
                "base_url": settings.OPENAI_BASE_URL,
                "model": settings.OPENAI_MODEL,
            },
        )
        self.model = model or config["model"]
        try:
            self.client = client or OpenAI(
                api_key=api_key or config["api_key"],
                base_url=base_url or config["base_url"] or None,
            )
        except OpenAIError as exc:
            raise OpenAIBackendError("AI provider could not be initialized; check its configuration.") from exc

    def complete(self, *, system, user, temperature=0.2, model=None, reasoning_level=None):
        return self.complete_messages(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            model=model,
            reasoning_level=reasoning_level,
        )

    def complete_messages(self, *, messages, temperature=0.2, model=None, reasoning_level=None):
        selected_model = model or self.model
        request = {
            "model": selected_model,
            "messages": messages,
        }
        if temperature is not None and self._supports_temperature(selected_model):
            request["temperature"] = temperature
        if reasoning_level:
            request["reasoning_effort"] = reasoning_level
        try:
            response = self.client.chat.completions.create(**request)
        except Exception as exc:
            # OpenAI-compatible providers do not expose a model-capabilities
            # endpoint consistently. Retry once when an otherwise unknown model
            # explicitly rejects temperature, then keep the compatible request.
            if "temperature" in request and self._temperature_is_unsupported(exc):
                request.pop("temperature")
                try:
                    response = self.client.chat.completions.create(**request)
                except Exception as retry_exc:
                    raise OpenAIBackendError(str(retry_exc)) from retry_exc
            else:
                raise OpenAIBackendError(str(exc)) from exc

        message = response.choices[0].message
        return (message.content or "").strip()

    @staticmethod
    def _supports_temperature(model):
        """Return whether a model accepts a non-default temperature setting."""
        normalized_model = (model or "").strip().lower()
        return not normalized_model.startswith(("gpt-5", "o1", "o3", "o4-mini"))

    @staticmethod
    def _temperature_is_unsupported(exc):
        message = str(exc).lower()
        return "temperature" in message and (
            "unsupported" in message or "only the default" in message
        )
