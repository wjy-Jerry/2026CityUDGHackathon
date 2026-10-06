"""Check the interface supplied to TestAgent against the generated application."""
import pytest

from app.agents.code_agent import CodeAgent
from app.agents.design_agent import DesignAgent
from app.validators.artifacts import CodeValidator, ValidationError


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
