from __future__ import annotations

import json
import re
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
            safe_error = self._safe_exception_summary(exc)
            if self.settings.llm_strict:
                raise LLMError(f"OpenAI text generation failed: {safe_error}") from exc
            try:
                self._mark_fallback(safe_error)
                return self.fallback.generate_text(system=system, user=user, metadata=metadata)
            except Exception as fallback_exc:
                raise LLMError(
                    f"OpenAI text generation failed and fallback failed: {safe_error}; "
                    f"{self._safe_exception_summary(fallback_exc)}"
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
                code_limit = self.settings.code_max_output_tokens if (metadata or {}).get("node_id") == "code" else None
                return self._generate_chat_json(
                    model=model,
                    system=system,
                    user=user,
                    schema=schema,
                    max_output_tokens=code_limit,
                )
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
            safe_error = self._safe_exception_summary(exc)
            if self.settings.llm_strict:
                raise LLMError(f"OpenAI JSON generation failed: {safe_error}") from exc
            try:
                self._mark_fallback(safe_error)
                return self.fallback.generate_json(system=system, user=user, schema=schema, metadata=metadata)
            except Exception as fallback_exc:
                raise LLMError(
                    f"OpenAI JSON generation failed and fallback failed: {safe_error}; "
                    f"{self._safe_exception_summary(fallback_exc)}"
                ) from fallback_exc

    @property
    def _use_chat_completions(self) -> bool:
        return bool(self.settings.openai_base_url)

    def _chat_content(self, response: Any) -> str:
        self._record_chat_response_metadata(response)
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise LLMError("Chat completion returned no choices")
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if not content:
            finish_reason = self._last_call_metadata.get("llm_finish_reason") or "unknown"
            refusal = bool(getattr(message, "refusal", None))
            raise LLMError(
                f"Chat completion returned empty content (finish_reason={finish_reason}, "
                f"refusal_present={str(refusal).lower()})"
            )
        return content

    def _record_chat_response_metadata(self, response: Any) -> None:
        choices = getattr(response, "choices", None) or []
        choice = choices[0] if choices else None
        message = getattr(choice, "message", None)
        values = {
            "llm_response_id": getattr(response, "id", None),
            "llm_response_model": getattr(response, "model", None),
            "llm_finish_reason": getattr(choice, "finish_reason", None),
            "llm_refusal_present": bool(getattr(message, "refusal", None)),
        }
        usage = getattr(response, "usage", None)
        for source, target in (("prompt_tokens", "llm_prompt_count"),
                               ("completion_tokens", "llm_completion_count"),
                               ("total_tokens", "llm_total_count")):
            count = getattr(usage, source, None)
            if count is not None:
                values[target] = count
        self._last_call_metadata.update({key: value for key, value in values.items() if value is not None})

    def _validation_error_summary(self, exc: Exception) -> list[dict[str, str]]:
        if isinstance(exc, ValidationError):
            errors = exc.errors(include_input=False)
            return [
                {
                    "path": ".".join(str(part) for part in error.get("loc", ())) or "response",
                    "type": str(error.get("type", "validation_error")),
                    "message": str(error.get("msg", "Invalid value"))[:240],
                }
                for error in errors[:8]
            ]
        return [{"path": "response", "type": "invalid_json", "message": "Response was not valid JSON."}]

    def _safe_exception_summary(self, exc: Exception) -> str:
        message = str(exc)
        secret = self.settings.openai_api_key
        if secret:
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", message)
        return f"{exc.__class__.__name__}: {message[:1200]}"

    def _generate_chat_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[SchemaT],
        max_output_tokens: int | None = None,
    ) -> SchemaT:
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
            request: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "response_format": {"type": "json_object"},
            }
            if max_output_tokens is not None:
                request["max_tokens"] = max_output_tokens
            response = self._client.chat.completions.create(**request)
            last_content = self._chat_content(response)
            try:
                return schema.model_validate(json.loads(last_content))
            except (JSONDecodeError, ValidationError) as exc:
                last_error = exc
                feedback = self._validation_error_summary(exc)
                self._last_call_metadata["llm_validation_errors"] = feedback
                if attempt == 1:
                    break
                feedback_json = json.dumps(feedback, ensure_ascii=False, sort_keys=True)
                messages.extend(
                    [
                        {"role": "assistant", "content": last_content[:12000]},
                        {
                            "role": "user",
                            "content": (
                                "The previous JSON failed validation. Return a corrected JSON object only.\n"
                                f"Structured validation feedback:\n{feedback_json}\n\n"
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
