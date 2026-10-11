"""Unrelated spec exercises the generic pipeline without another domain fixture."""
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

from app.storage.package import application_package
from app.validators.artifacts import validate_batch_smoke

SPEC = Path(__file__).resolve().parents[1] / 'samples' / 'task_tracker.md'


def test_generic_task_tracker_pipeline_and_package(engine, tmp_path):
    batch = engine.create_batch_from_path(SPEC)
    state = engine.run_batch(batch.batch_id)
    assert state.status == 'succeeded', state.model_dump()
    root = engine.store.output_batch_dir(batch.batch_id)
    design = engine.store.read_json(engine.store.batch_dir(batch.batch_id) / '概要设计/design_manifest.json')
    manifest = engine.store.read_json(root / 'code_manifest.json')
    assert design['generation_profile'] == 'generic-records'
    assert manifest['strategy'] == 'generic-template-fallback'
    assert not manifest.get('sample_fixture')
    assert (root / 'src/api.py').is_file() and (root / 'tests/generated/test_records.py').is_file()
    report = validate_batch_smoke(engine.store, batch.batch_id)
    assert report['passed'], report
    assert report['test']['test_counts']['passed'] == 3
    assert report['test']['coverage_pct'] >= 80
    package = zipfile.ZipFile(io.BytesIO(application_package(root)))
    target = tmp_path / 'unpacked'
    package.extractall(target)
    check = subprocess.run([sys.executable, '-c', '''
from fastapi.testclient import TestClient
from src.api import app
with TestClient(app) as client:
    assert client.get('/health').status_code == 200
    created = client.post('/records', json={'title':'Review draft','description':'Team'}).json()
    assert client.get('/records').json() == [created]
    assert client.delete('/records/' + created['id']).status_code == 200
    assert client.delete('/records/' + created['id']).status_code == 404
'''], cwd=target, capture_output=True, text=True, timeout=30)
    assert check.returncode == 0, check.stdout + check.stderr


def test_vehicle_keywords_do_not_select_sample_fixture(engine):
    batch = engine.create_batch_from_bytes(filename='keywords.md', content=b'# Vehicle reservation records\nList records')
    state = engine.run_batch(batch.batch_id)
    assert state.status == 'succeeded', state.model_dump()
    manifest = engine.store.read_json(engine.store.output_batch_dir(batch.batch_id) / 'code_manifest.json')
    assert manifest['strategy'] == 'generic-template-fallback'
    assert not manifest.get('sample_fixture')
