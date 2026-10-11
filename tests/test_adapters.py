from types import SimpleNamespace
import pytest
from pydantic import BaseModel
from app.adapters.openai_adapter import OpenAIAdapter
from app.adapters.llm import LLMError
from app.agents.design_agent import DesignManifest
from app.agents.fallback import mock_design
from app.config import Settings, get_settings


class MinimalResponse(BaseModel):
    ok: bool


def test_no_key_defaults_mock_and_strict_missing_model_errors(monkeypatch):
    for key in ('OPENAI_API_KEY', 'LLM_STRICT'):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    assert not get_settings().llm_strict
    adapter = OpenAIAdapter(Settings(llm_strict=True))
    with pytest.raises(LLMError):
        adapter.generate_text(system='', user='', metadata={'node_id':'code'})
    get_settings.cache_clear()


def test_chat_schema_retry_is_schema_specific_and_bounded():
    import json
    adapter = OpenAIAdapter(Settings(openai_base_url='https://example.invalid/v1', design_model='configured-model', llm_strict=True))
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        content = '{}' if len(calls) == 1 else json.dumps(mock_design('# Tasks'))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    adapter._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = adapter.generate_json(system='Design only', user='spec', schema=DesignManifest, metadata={'node_id':'design'})
    assert result.system_name == 'Tasks' and len(calls) == 2
    assert 'src/api.py' not in calls[1]['messages'][-1]['content']


def test_llm_failure_is_visible_in_fallback_metadata():
    adapter = OpenAIAdapter(Settings(design_model='configured-model'))
    def fail(**kwargs):
        raise RuntimeError('service unavailable')
    adapter._client = SimpleNamespace(responses=SimpleNamespace(create=fail))
    text = adapter.generate_text(system='', user='# 产品规格说明书\n# Task App', metadata={'node_id':'design'})
    assert text.startswith('# Task App')
    assert adapter.last_call_metadata['llm_fallback_used']
    assert 'service unavailable' in adapter.last_call_metadata['llm_fallback_reason']


def test_empty_chat_response_records_safe_model_and_finish_metadata():
    secret = "sk-test-do-not-log"
    adapter = OpenAIAdapter(Settings(
        openai_api_key=secret,
        openai_base_url="https://openrouter.ai/api/v1",
        code_model="openrouter/free",
        code_max_output_tokens=4096,
        llm_strict=True,
    ))
    response = SimpleNamespace(
        id="response-123",
        model="provider/free-model",
        choices=[SimpleNamespace(
            finish_reason="length",
            message=SimpleNamespace(content=None, refusal=None),
        )],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=4096, total_tokens=4216),
    )
    calls = []
    adapter._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or response))
    )

    with pytest.raises(LLMError, match="finish_reason=length") as error:
        adapter.generate_json(system="schema", user="app", schema=MinimalResponse, metadata={"node_id": "code"})

    metadata = adapter.last_call_metadata
    assert metadata["llm_model"] == "openrouter/free"
    assert metadata["llm_response_model"] == "provider/free-model"
    assert metadata["llm_finish_reason"] == "length"
    assert metadata["llm_completion_count"] == 4096
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["max_tokens"] == 4096
    assert secret not in str(error.value)
    assert secret not in repr(metadata)
    assert not any("key" in key.lower() or "token" in key.lower() for key in metadata)


def test_code_output_limit_is_optional_and_applies_only_to_code_json():
    adapter = OpenAIAdapter(Settings(
        openai_base_url="https://openrouter.ai/api/v1",
        code_model="openrouter/free",
        code_max_output_tokens=8192,
        llm_strict=True,
    ))
    calls = []
    response = SimpleNamespace(
        id="response-456",
        model="provider/free-model",
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"ok":true}', refusal=None))],
        usage=None,
    )
    adapter._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or response))
    )

    result = adapter.generate_json(system="schema", user="app", schema=MinimalResponse, metadata={"node_id": "code"})

    assert result.ok
    assert calls[0]["max_tokens"] == 8192
    assert calls[0]["response_format"] == {"type": "json_object"}
