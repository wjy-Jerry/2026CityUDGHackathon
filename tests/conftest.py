from pathlib import Path
import shutil
import pytest
from app.adapters.llm import MockLLMAdapter
from app.orchestrator.engine import Orchestrator
from app.storage.file_store import FileStore
from app.demo_fixtures import SAMPLE_FIXTURE_ID

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_SPEC = next((ROOT / 'problem').glob('试题成果验证*.md'))

@pytest.fixture
def store(tmp_path):
    shutil.copytree(ROOT / 'prompts', tmp_path / 'prompts')
    return FileStore(tmp_path)

@pytest.fixture
def engine(store):
    return Orchestrator(store=store, llm=MockLLMAdapter(), max_retries=0)

@pytest.fixture
def official_batch(engine):
    return engine.create_batch_from_path(OFFICIAL_SPEC, sample_fixture=SAMPLE_FIXTURE_ID)
