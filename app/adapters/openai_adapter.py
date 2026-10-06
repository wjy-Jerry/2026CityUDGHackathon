from __future__ import annotations

import json
from json import JSONDecodeError
from typing import Any

from pydantic import BaseModel, ValidationError

from app.adapters.llm import LLMError, MockLLMAdapter, SchemaT
from app.config import Settings, get_settings


class OpenAIAdapter:
    """OpenAI SDK adapter with a deterministic fallback when unavailable."""

    def __init__(self, settings: Settings | None = None, fallback: MockLLMAdapter | None = None) -> None:
        self.settings = settings or get_settings()
        self.fallback = fallback or MockLLMAdapter()
        self._client: Any | None = None
        self._client_error: Exception | None = None
        self._last_call_metadata: dict[str, Any] = {}
        if self.settings.has_openai_key:
            try:
                from openai import OpenAI

                kwargs: dict[str, Any] = {"api_key": self.settings.openai_api_key or "", "timeout": 60.0, "max_retries": 1}
                if self.settings.openai_base_url:
                    kwargs["base_url"] = self.settings.openai_base_url
                self._client = OpenAI(**kwargs)
            except Exception as exc:
                self._client_error = exc
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def provider_name(self) -> str:
        if not self.available:
            return "mock"
        if self.settings.openai_base_url:
            return "openai-compatible"
        return "openai"

    @property
    def last_call_metadata(self) -> dict[str, Any]:
        return dict(self._last_call_metadata)

    def _model_for(self, metadata: dict[str, str] | None) -> str | None:
        node_id = (metadata or {}).get("node_id")
        if node_id == "design":
            return self.settings.design_model
        if node_id == "code":
            return self.settings.code_model
        if node_id == "test":
            return self.settings.test_model
        return self.settings.design_model or self.settings.code_model or self.settings.test_model

    def generate_text(self, *, system: str, user: str, metadata: dict[str, str] | None = None) -> str:
        model = self._model_for(metadata)
        self._start_call_metadata(model)
        if not self.available or not model:
            if self.settings.llm_strict:
                raise LLMError(self._unavailable_message("text", metadata, model))
            self._mark_fallback(self._unavailable_message("text", metadata, model))
            return self.fallback.generate_text(system=system, user=user, metadata=metadata)
        try:
            if self._use_chat_completions:
                response = self._client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                )
                return self._chat_content(response)
            response = self._client.responses.create(
                model=model,
                input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                metadata={k: v for k, v in (metadata or {}).items() if "key" not in k.lower()},
            )
            return response.output_text
        except Exception as exc:
            if self.settings.llm_strict:
                raise LLMError(f"OpenAI text generation failed: {exc.__class__.__name__}: {exc}") from exc
            try:
                self._mark_fallback(f"{exc.__class__.__name__}: {exc}")
                return self.fallback.generate_text(system=system, user=user, metadata=metadata)
            except Exception as fallback_exc:
                raise LLMError(
                    f"OpenAI text generation failed and fallback failed: {exc.__class__.__name__}: {fallback_exc}"
                ) from fallback_exc

    def generate_json(
        self,
        *,
        system: str,
        user: str,
        schema: type[SchemaT],
        metadata: dict[str, str] | None = None,
    ) -> SchemaT:
        model = self._model_for(metadata)
        self._start_call_metadata(model)
        if not self.available or not model:
            if self.settings.llm_strict:
                raise LLMError(self._unavailable_message("JSON", metadata, model))
            self._mark_fallback(self._unavailable_message("JSON", metadata, model))
            return self.fallback.generate_json(system=system, user=user, schema=schema, metadata=metadata)
        try:
            if self._use_chat_completions:
                return self._generate_chat_json(model=model, system=system, user=user, schema=schema)
            response = self._client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": f"{system}\nReturn JSON matching this schema: {schema.model_json_schema()}"},
                    {"role": "user", "content": user},
                ],
                text={"format": {"type": "json_object"}},
                metadata={k: v for k, v in (metadata or {}).items() if "key" not in k.lower()},
            )
            return schema.model_validate(json.loads(response.output_text))
        except Exception as exc:
            if self.settings.llm_strict:
                raise LLMError(f"OpenAI JSON generation failed: {exc.__class__.__name__}: {exc}") from exc
            try:
                self._mark_fallback(f"{exc.__class__.__name__}: {exc}")
                return self.fallback.generate_json(system=system, user=user, schema=schema, metadata=metadata)
            except Exception as fallback_exc:
                raise LLMError(
                    f"OpenAI JSON generation failed and fallback failed: {exc.__class__.__name__}: {fallback_exc}"
                ) from fallback_exc

    @property
    def _use_chat_completions(self) -> bool:
        return bool(self.settings.openai_base_url)

    def _chat_content(self, response: Any) -> str:
        content = response.choices[0].message.content
        if not content:
            raise LLMError("Chat completion returned empty content")
        return content

    def _generate_chat_json(self, *, model: str, system: str, user: str, schema: type[SchemaT]) -> SchemaT:
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        messages = [
            {
                "role": "system",
                "content": (
                    f"{system}\n\n"
                    "Return one valid JSON object only. Do not use Markdown fences. "
                    "The JSON object must validate against this schema exactly:\n"
                    f"{schema_json}"
                ),
            },
            {"role": "user", "content": user},
        ]
        last_content = ""
        last_error: Exception | None = None
        for attempt in range(2):
            response = self._client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
            )
            last_content = self._chat_content(response)
            try:
                return schema.model_validate(json.loads(last_content))
            except (JSONDecodeError, ValidationError) as exc:
                last_error = exc
                if attempt == 1:
                    break
                messages.extend(
                    [
                        {"role": "assistant", "content": last_content[:12000]},
                        {
                            "role": "user",
                            "content": (
                                "The previous JSON failed validation. Return a corrected JSON object only.\n"
                                f"Validation error:\n{exc}\n\n"
                                "Follow the supplied schema exactly. Preserve all required fields and complete file contents."

                            ),
                        },
                    ]
                )
        raise last_error or LLMError("Chat completion JSON generation failed")

    def _start_call_metadata(self, model: str | None) -> None:
        self._last_call_metadata = {
            "llm_provider": self.provider_name,
            "llm_model": model,
            "llm_strict": self.settings.llm_strict,
            "llm_fallback_used": False,
            "llm_fallback_reason": None,
        }

    def _mark_fallback(self, reason: str) -> None:
        self._last_call_metadata.update(
            {
                "llm_provider": "mock",
                "llm_fallback_used": True,
                "llm_fallback_reason": reason,
            }
        )

    def _unavailable_message(self, kind: str, metadata: dict[str, str] | None, model: str | None) -> str:
        node_id = (metadata or {}).get("node_id")
        missing = []
        if not self.available:
            missing.append("client")
        if not model:
            missing.append("model")
        detail = ", ".join(missing) or "unknown"
        if self._client_error is not None:
            detail = f"{detail}; client init failed with {self._client_error.__class__.__name__}: {self._client_error}"
        return f"OpenAI {kind} generation unavailable for node {node_id}: missing {detail}"
