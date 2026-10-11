import io
import subprocess
import sys
import zipfile
from app.storage.package import application_package
from app.validators.artifacts import validate_batch_smoke

from scripts.benchmark import BUSINESS_DEMO


def test_official_pipeline_generated_app_tests_coverage_and_portable_package(engine, official_batch, tmp_path):
    state = engine.run_batch(official_batch.batch_id)
    assert state.status == 'succeeded', state.model_dump()
    summary = validate_batch_smoke(engine.store, state.batch_id)
    assert summary['passed'], summary
    assert summary['test']['coverage_pct'] >= 80
    root = engine.store.output_batch_dir(state.batch_id)
    assert not (root / 'data').exists(), 'Validation must not mutate application data'
    package = zipfile.ZipFile(io.BytesIO(application_package(root)))
    extracted = tmp_path / 'unpacked'
    package.extractall(extracted)
    run = subprocess.run([sys.executable, '-c', BUSINESS_DEMO], cwd=extracted, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
