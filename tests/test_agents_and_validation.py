import json
from pathlib import Path
import pytest
from app.adapters.llm import MockLLMAdapter
from app.adapters.openai_adapter import OpenAIAdapter
from app.config import Settings
from app.agents.design_agent import DesignAgent
from app.agents.code_agent import CodeAgent
from app.agents.test_agent import TestAgent as PipelineTestAgent
from app.validators.artifacts import CodeValidator, TestValidator as GeneratedTestValidator, ValidationError, validate_batch_smoke


def generate(engine, batch):
    context = {'batch_id':batch.batch_id, 'spec_path':batch.spec_path}
    for agent in (DesignAgent, CodeAgent, PipelineTestAgent):
        refs = agent(store=engine.store, llm=engine.llm).run(context)
        assert refs and all(engine.store.resolve(r.path).is_file() for r in refs)
    return engine.store.output_batch_dir(batch.batch_id)


def test_offline_agent_fallbacks_and_structured_contract(engine, official_batch):
    root = generate(engine, official_batch)
    manifest = engine.store.read_json(root / 'code_manifest.json')
    assert manifest['strategy'] == 'sample-fixture'
    assert manifest['sample_fixture'] == 'vehicle_reservations'
    design = engine.store.read_json(engine.store.batch_dir(official_batch.batch_id) / '概要设计/design_manifest.json')
    assert manifest['system_name'] == design['system_name']
    assert official_batch.batch_id in (root / 'README.md').read_text()
    assert manifest['storage_contract']['service_attribute'] == 'service'
    assert (root / 'frontend/index.html').is_file()
    index = PipelineTestAgent(store=engine.store, llm=engine.llm)._source_index(official_batch.batch_id)
    assert 'src/api.py' in index and 'def ' not in index and '```' not in index
    assert OpenAIAdapter(Settings()).generate_text(system='', user='# 产品规格说明书\n# Task Manager', metadata={'node_id':'design'}).startswith('# Task Manager')


def test_failing_tests_cannot_pass_with_high_coverage(engine):
    batch = engine.create_batch_from_bytes(filename='task.md', content=b'# Tasks\nWeb records')
    root = generate(engine, batch)
    tests = root / 'tests/generated/test_records.py'
    tests.write_text(tests.read_text() + '\ndef test_genuine_failure():\n    assert 1 == 2\n')
    with pytest.raises(ValidationError) as error:
        GeneratedTestValidator(engine.store).validate(batch.batch_id)
    result = error.value.result
    assert result['coverage_pct'] >= 80
    assert not result['passed'] and not result['checks']['all_tests_passed']
    assert result['test_counts']['failed'] == 1
    summary = validate_batch_smoke(engine.store, batch.batch_id)
    assert not summary['passed'] and summary['code']['passed']
    assert (root / 'quality_report.json').is_file()


def test_collection_error_and_import_failure_reported(engine):
    batch = engine.create_batch_from_bytes(filename='task.md', content=b'# Tasks\nWeb records')
    root = generate(engine, batch)
    (root / 'tests/generated/test_broken.py').write_text('import missing_generated_module\n')
    with pytest.raises(ValidationError) as error:
        GeneratedTestValidator(engine.store).validate(batch.batch_id)
    assert error.value.result['test_counts']['errors'] >= 1
    assert not error.value.result['checks']['tests_collection_passed']
    (root / 'src/api.py').write_text('app = object()\n')
    with pytest.raises(ValidationError) as error:
        CodeValidator(engine.store).validate(batch.batch_id)
    assert not error.value.result['checks']['application_smoke_passed']


def test_low_coverage_and_no_tests_fail(engine):
    batch = engine.create_batch_from_bytes(filename='task.md', content=b'# Tasks\nWeb records')
    root = generate(engine, batch)
    (root / 'tests/generated/test_records.py').write_text('def test_only_health():\n    from src.api import health\n    assert health()["status"] == "ok"\n')
    with pytest.raises(ValidationError) as error:
        GeneratedTestValidator(engine.store).validate(batch.batch_id)
    assert not error.value.result['checks']['coverage_threshold_passed']


def test_testagent_prompt_contains_no_source_context(engine, monkeypatch):
    batch = engine.create_batch_from_bytes(filename='task.md', content=b'# Tasks\nWeb records')
    root = generate(engine, batch)
    path = engine.store.batch_dir(batch.batch_id) / '代码生成/code_manifest.json'
    manifest = json.loads(path.read_text())
    manifest['strategy'] = 'llm-structured-generation'
    path.write_text(json.dumps(manifest))
    marker = 'SOURCE_CONTEXT_MUST_NOT_LEAK'
    with (root / 'src/api.py').open('a') as handle:
        handle.write('\n# ' + marker)
    class RecordingLLM:
        def generate_json(self, **kwargs):
            assert marker not in kwargs['user']
            assert 'storage_contract' in kwargs['user']
            return kwargs['schema'].model_validate({'files':[{'path':'tests/generated/test_health.py','content':'def test_health():\n    assert 1 == 1'}], 'test_plan_markdown':'Health test'})
    PipelineTestAgent(store=engine.store, llm=RecordingLLM()).run({'batch_id':batch.batch_id, 'spec_path':batch.spec_path})
