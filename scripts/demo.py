"""Run a repeatable full pipeline, quality report and portable ZIP."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from app.adapters.llm import MockLLMAdapter
from app.adapters.openai_adapter import OpenAIAdapter
from app.config import ROOT_DIR, get_settings
from app.demo_fixtures import SAMPLE_FIXTURE_ID
from app.orchestrator.engine import Orchestrator
from app.storage.file_store import FileStore
from app.storage.package import application_package
from app.validators.artifacts import validate_batch_smoke
from scripts.benchmark import BUSINESS_DEMO


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, help='Default: official vehicle reservation specification')
    parser.add_argument('--llm', action='store_true', help='Use configured real LLM instead of deterministic offline templates')
    parser.add_argument('--workspace', type=Path, default=ROOT_DIR, help='Artifact root (default: repository)')
    parser.add_argument('--sample-fixture', choices=[SAMPLE_FIXTURE_ID], help='Explicit deterministic sample fixture; default official demo selects vehicle_reservations')
    args = parser.parse_args()
    if args.llm and args.sample_fixture:
        parser.error('--llm and --sample-fixture select different generation modes')
    spec = args.spec or next((ROOT_DIR / 'problem').glob('试题成果验证*.md'))
    sample_fixture = args.sample_fixture or (SAMPLE_FIXTURE_ID if args.spec is None and not args.llm else None)
    if not spec.is_file():
        parser.error(f'Specification not found: {spec}')
    if args.llm:
        settings = get_settings()
        if not settings.has_openai_key or not all((settings.design_model, settings.code_model, settings.test_model)):
            parser.error('--llm requires OPENAI_API_KEY and DESIGN_MODEL/CODE_MODEL/TEST_MODEL')
        llm = OpenAIAdapter(settings)
    else:
        llm = MockLLMAdapter()
    store = FileStore(args.workspace)
    if store.root_dir != ROOT_DIR:
        shutil.copytree(ROOT_DIR / 'prompts', store.root_dir / 'prompts', dirs_exist_ok=True)
    engine = Orchestrator(store=store, llm=llm)
    state = engine.create_batch_from_path(spec, sample_fixture=sample_fixture)
    generation_label = f'deterministic sample fixture ({sample_fixture}); not arbitrary specification generation' if sample_fixture else ('real LLM generation' if args.llm else 'generic offline record pipeline')
    print(f'Batch: {state.batch_id}\nMode: {generation_label}', flush=True)
    state = engine.run_batch(state.batch_id)
    for node in state.nodes.values():
        print(f'{node.node_id}: {node.status}, retries={node.retries}, duration_ms={node.duration_ms}', flush=True)
        if node.error_message:
            print(node.error_message, flush=True)
    if state.status != 'succeeded':
        print(f'Pipeline failed; diagnostics: {store.batch_dir(state.batch_id)}')
        return 1
    validation = validate_batch_smoke(store, state.batch_id)
    root = store.output_batch_dir(state.batch_id)
    counts = validation['test']['test_counts']
    coverage = validation['test']['coverage_pct']
    print(f'Validation: {validation["passed"]}; tests={counts}; coverage={coverage}%')
    passed = validation['passed']
    if args.spec is None and sample_fixture == SAMPLE_FIXTURE_ID:
        with tempfile.TemporaryDirectory(prefix='official-demo-') as temp:
            target = Path(temp) / 'application'
            shutil.copytree(root, target)
            completed = subprocess.run([sys.executable, '-c', BUSINESS_DEMO], cwd=target, capture_output=True, text=True, timeout=30)
        result = {'passed':completed.returncode == 0, 'output':completed.stdout + completed.stderr}
        store.write_json(root / 'benchmark_result.json', result)
        store.write_json(store.batch_dir(state.batch_id) / 'benchmark_result.json', result)
        print(result['output'])
        passed = passed and result['passed']
    package = root.parent / f'{state.batch_id}.zip'
    package.write_bytes(application_package(root))
    print(f'Application: {root}\nPackage: {package}\nStart: cd "{root}" && python -m uvicorn src.api:app --port 8001')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
