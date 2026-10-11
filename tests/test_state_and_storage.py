import hashlib
import pytest
from pydantic import ValidationError
from app.orchestrator.state import BatchState
from app.agents.code_agent import CodeAgent
from app.agents.test_agent import TestAgent as PipelineTestAgent
from app.adapters.llm import MockLLMAdapter


def test_state_roundtrip_and_independent_defaults(store):
    a = BatchState.new(batch_id='a', spec_path='spec.md', mode='manual')
    b = BatchState.new(batch_id='b', spec_path='spec.md')
    a.nodes['design'].status = 'succeeded'
    a.status = 'paused'
    a.current_node = 'code'
    store.save_state(a)
    assert store.load_state('a') == a
    assert b.nodes['design'].status == 'queued'
    with pytest.raises(ValidationError):
        BatchState.new(batch_id='c', spec_path='spec.md', mode='invalid')


def test_store_text_json_hash_and_upload(store):
    path = store.save_uploaded_spec(batch_id='a', filename=r'C:\fake\需求.md', content=b'# spec')
    assert path.name == 'a_需求.md'
    ref = store.write_json(store.batch_dir('a') / 'manifest.json', {'name':'中文'})
    assert store.read_json(store.resolve(ref.path)) == {'name':'中文'}
    assert ref.sha256 == hashlib.sha256(store.resolve(ref.path).read_bytes()).hexdigest()
    assert any(a['path'] == ref.path for a in store.list_artifacts('a'))


@pytest.mark.parametrize('path', ['../../escape', '/tmp/escape', r'..\escape'])
def test_artifact_path_safety(store, path):
    with pytest.raises(ValueError):
        store.resolve(path)


@pytest.mark.parametrize('batch_id', ['../escape', '/tmp/escape', 'a/b', r'a\b', '..'])
def test_batch_path_safety(store, batch_id):
    with pytest.raises(ValueError):
        store.batch_dir(batch_id)
    with pytest.raises(ValueError):
        store.load_state(batch_id)


def test_unknown_load_does_not_create_batch(store):
    with pytest.raises(FileNotFoundError):
        store.load_state('missing')
    assert not (store.generated_dir / 'missing').exists()


@pytest.mark.parametrize('agent_class,method,path', [
    (CodeAgent, '_safe_generated_path', 'src/../../outside.py'),
    (CodeAgent, '_safe_generated_path', 'src/a.txt'),
    (CodeAgent, '_safe_generated_path', r'src/..\escape.py'),
    (PipelineTestAgent, '_safe_test_path', 'tests/generated/../../escape.py'),
    (PipelineTestAgent, '_safe_test_path', 'tests/generated/helper.py'),
])
def test_generated_path_safety(store, agent_class, method, path):
    agent = agent_class(store=store, llm=MockLLMAdapter())
    with pytest.raises(ValueError):
        getattr(agent, method)(path, store.output_batch_dir('a'))


def test_symlink_cannot_escape_store(store, tmp_path):
    outside = tmp_path.parent / 'outside-artifact'
    outside.write_text('private')
    (store.root_dir / 'linked').symlink_to(outside)
    with pytest.raises(ValueError):
        store.resolve('linked')
    with pytest.raises(ValueError):
        store.write_text(store.root_dir / 'linked', 'overwrite')
    assert outside.read_text() == 'private'
