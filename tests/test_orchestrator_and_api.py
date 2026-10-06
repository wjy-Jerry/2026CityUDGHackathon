import io
import zipfile
import pytest
from fastapi.testclient import TestClient
from app.api import main
from app.agents.design_agent import DesignAgent
from app.orchestrator.best_of import BestOfSelector

SPEC = b'# Task Tracker\nWeb application: add, list and delete task records.'


def test_auto_pipeline_and_idempotent_completed_run(engine):
    batch = engine.create_batch_from_bytes(filename='tasks.md', content=SPEC)
    state = engine.run_batch(batch.batch_id)
    assert state.status == 'succeeded'
    assert all(n.status == 'succeeded' for n in state.nodes.values())
    assert all(n.duration_ms is not None for n in state.nodes.values())
    assert state.nodes['test'].quality_check_result['coverage_pct'] >= 80
    assert engine.run_batch(batch.batch_id) == state


def test_manual_transitions(engine):
    batch = engine.create_batch_from_bytes(filename='tasks.md', content=SPEC, mode='manual')
    state = engine.run_batch(batch.batch_id)
    assert state.status == 'paused' and state.current_node == 'code'
    assert state.nodes['test'].status == 'queued'
    with pytest.raises(ValueError):
        engine.run_batch(batch.batch_id)
    state = engine.advance_node(batch.batch_id)
    assert state.status == 'paused' and state.current_node == 'test'
    assert engine.advance_node(batch.batch_id).status == 'succeeded'
    with pytest.raises(ValueError):
        engine.advance_node(batch.batch_id)


def test_bounded_retries_and_downstream_reset(engine, monkeypatch):
    batch = engine.create_batch_from_bytes(filename='tasks.md', content=SPEC)
    engine.max_retries = 1
    original = DesignAgent.run
    calls = []
    def fail_once(self, context):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('temporary outage')
        return original(self, context)
    monkeypatch.setattr(DesignAgent, 'run', fail_once)
    state = engine.run_batch(batch.batch_id)
    assert state.status == 'succeeded' and len(calls) == 2
    assert state.nodes['design'].retries == 1
    previous = state.nodes['design'].outputs
    state = engine.retry_node(batch.batch_id, 'code')
    assert state.status == 'succeeded' and state.nodes['design'].outputs == previous
    assert state.nodes['code'].retries == 0


def test_failed_run_stays_failed_and_dependency_retry_rejected(engine, monkeypatch):
    batch = engine.create_batch_from_bytes(filename='tasks.md', content=SPEC)
    def fail(*args):
        raise RuntimeError('permanent outage')
    monkeypatch.setattr(DesignAgent, 'run', fail)
    assert engine.run_batch(batch.batch_id).status == 'failed'
    with pytest.raises(ValueError):
        engine.run_batch(batch.batch_id)
    with pytest.raises(ValueError):
        engine.retry_node(batch.batch_id, 'code')


def test_api_lifecycle(engine, monkeypatch):
    monkeypatch.setattr(main, 'store', engine.store)
    monkeypatch.setattr(main, 'orchestrator', engine)
    with TestClient(main.app) as client:
        assert client.get('/health').status_code == 200
        for filename, content, mode in [('a.txt', SPEC, 'auto'), ('a.md', b'', 'auto'), ('a.md', SPEC, 'bad'), ('a.md', b'\xff', 'auto')]:
            assert client.post('/api/v1/batches', files={'file':(filename, content)}, data={'mode':mode}).status_code == 400
        response = client.post('/api/v1/batches', files={'file':('tasks.md', SPEC)})
        assert response.status_code == 200
        bid = response.json()['batch_id']
        base = '/api/v1/batches/' + bid
        assert client.get(base + '/package').status_code == 404
        assert client.post(base + '/run').status_code == 202
        assert client.get(base).json()['status'] == 'succeeded'
        assert client.post(base + '/run').status_code == 409
        assert client.post(base + '/advance').status_code == 400
        assert client.get('/api/v1/batches').json()[0]['batch_id'] == bid
        artifacts = client.get(base + '/artifacts').json()
        assert any(a['kind'] == 'design_manifest' for a in artifacts)
        assert client.get(base + '/download', params={'path':artifacts[0]['path']}).status_code == 200
        assert client.get(base + '/download', params={'path':'../secret'}).status_code == 400
        assert client.get(base + '/download', params={'path':'requirements.txt'}).status_code == 404
        validation = client.post('/api/v1/validate', json={'batch_id':bid}).json()['validation']
        assert validation['passed'] and validation['test']['test_counts']['passed'] >= 2
        assert client.post('/api/v1/validate', json={}).status_code == 400
        assert client.get('/api/v1/batches/missing').status_code == 404
        package = zipfile.ZipFile(io.BytesIO(client.get(base + '/package').content))
        assert {'src/api.py', 'requirements.txt', 'pytest.ini', 'quality_report.json'} <= set(package.namelist())
        assert not any('__pycache__' in n or n.startswith('data/') for n in package.namelist())
        assert client.get(base + '/logs').json()


def test_schedule_rejects_duplicate(engine):
    from fastapi import BackgroundTasks
    state = engine.create_batch_from_bytes(filename='a.md', content=SPEC)
    tasks = BackgroundTasks()
    engine.schedule(tasks, 'run', state.batch_id)
    with pytest.raises(ValueError):
        engine.schedule(tasks, 'run', state.batch_id)


def test_best_of_rejects_failed_design(engine):
    state = engine.create_batch_from_bytes(filename='a.md', content=SPEC)
    selector = BestOfSelector(engine.store)
    assert selector.pick_winner(selector.score_batches([state.batch_id])) is None
