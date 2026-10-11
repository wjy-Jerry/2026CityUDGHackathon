"""Check the interface supplied to TestAgent against the generated application."""
import pytest

from app.agents.code_agent import CodeAgent, CodeGenerationResult
from app.agents.design_agent import DesignAgent
from app.agents.fallback import generic_code, mock_design
from app.validators.artifacts import CodeValidator, ValidationError


@pytest.mark.parametrize(("change", "expected"), [
    (lambda result: result["manifest"].update(api_routes=[{"method": "GET", "path": "/health"}, "GET /tasks", ["POST", "/tasks"]]), "api_routes"),
    (lambda result: result["manifest"].update(entrypoint="uvicorn src.missing:app"), "src.api:app"),
    (lambda result: result["manifest"].update(storage_contract={"module": "src.api", "service_attribute": "service"}), "storage_contract"),
])
def test_code_generation_result_rejects_malformed_manifest_structures(change, expected):
    result = generic_code(mock_design("# Task Tracker\nWeb task management"))
    change(result)
    with pytest.raises(ValueError, match=expected):
        CodeGenerationResult.model_validate(result)


@pytest.mark.parametrize(('change', 'expected'), [
    (lambda m: m['api_routes'].append({'method':'POST','path':'/invented'}), 'Manifest api_routes disagree'),
    (lambda m: m['api_routes'].pop(), 'Manifest api_routes disagree'),
    (lambda m: m.update(entrypoint='uvicorn src.missing:app'), 'Manifest entrypoint'),
    (lambda m: m['storage_contract'].update(service_attribute='missing_service'), 'Manifest storage service missing'),
    (lambda m: m['storage_contract'].update(factory='src.services.MissingService'), 'MissingService'),
    (lambda m: m['storage_contract'].update(argument='unknown_arg'), 'cannot accept unknown_arg'),
])
def test_manifest_claims_are_checked_against_running_app(engine, change, expected):
    batch = engine.create_batch_from_bytes(filename='tasks.md', content=b'# Tasks\nWeb records')
    context = {'batch_id':batch.batch_id, 'spec_path':batch.spec_path}
    DesignAgent(store=engine.store, llm=engine.llm).run(context)
    CodeAgent(store=engine.store, llm=engine.llm).run(context)
    root = engine.store.output_batch_dir(batch.batch_id)
    manifest_path = root / 'code_manifest.json'
    manifest = engine.store.read_json(manifest_path)
    change(manifest)
    engine.store.write_json(manifest_path, manifest)
    with pytest.raises(ValidationError, match=expected):
        CodeValidator(engine.store).validate(batch.batch_id)


def test_malformed_persisted_manifest_is_rejected_before_application_smoke(engine, official_batch, monkeypatch):
    context = {"batch_id": official_batch.batch_id, "spec_path": official_batch.spec_path}
    DesignAgent(store=engine.store, llm=engine.llm).run(context)
    CodeAgent(store=engine.store, llm=engine.llm).run(context)
    root = engine.store.output_batch_dir(official_batch.batch_id)
    manifest_path = root / "code_manifest.json"
    manifest = engine.store.read_json(manifest_path)
    manifest["api_routes"][1] = "GET /reservations"
    engine.store.write_json(manifest_path, manifest)

    def smoke_must_not_run(*args, **kwargs):
        pytest.fail("application smoke ran before malformed manifest validation")

    monkeypatch.setattr("app.validators.artifacts._run", smoke_must_not_run)
    with pytest.raises(ValidationError, match=r"Invalid code_manifest\.json: api_routes\.1"):
        CodeValidator(engine.store).validate(official_batch.batch_id)
