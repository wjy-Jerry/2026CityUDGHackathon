from types import SimpleNamespace
import pytest
from app.adapters.openai_adapter import OpenAIAdapter
from app.adapters.llm import LLMError
from app.agents.design_agent import DesignManifest
from app.agents.fallback import mock_design
from app.config import Settings, get_settings


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
