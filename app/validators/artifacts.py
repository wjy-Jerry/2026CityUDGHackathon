from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from app.agents.design_agent import DesignManifest
from app.storage.file_store import FileStore


class ValidationError(RuntimeError):
    def __init__(self, message: str, result: dict[str, Any] | None = None):
        super().__init__(message)
        self.result = result or {"passed": False, "summary": message}


def _require(result: dict[str, Any]) -> dict[str, Any]:
    if not result["passed"]:
        raise ValidationError(result.get("summary", "Quality checks failed"), result)
    return result


def _run(command: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # Do not inherit platform pytest flags, source paths or a real LLM credential.
    for key in ("PYTEST_ADDOPTS", "PYTHONPATH", "OPENAI_API_KEY"):
        env.pop(key, None)
    env["PYTHONPATH"] = str(cwd)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    try:
        return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        def decoded(value):
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")
        return subprocess.CompletedProcess(command, 124, decoded(exc.stdout), decoded(exc.stderr) + f"\nValidation timed out after {timeout}s")


def _copy_application(source: Path, destination: Path) -> None:
    def ignored(directory, names):
        excluded = {name for name in names if name in {"__pycache__", ".pytest_cache"}
                    or name.startswith(".coverage") or (Path(directory) / name).is_symlink()}
        if Path(directory) == source:
            excluded.update({"data", "coverage.json", "pytest-results.xml"})
        return excluded
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=ignored)



class DesignArtifactValidator:
    REQUIRED_FIELDS = set(DesignManifest.model_fields) - {"generation_profile", "requirements_summary"}

    def __init__(self, store: FileStore):
        self.store = store

    def validate(self, batch_id: str) -> dict[str, Any]:
        base = self.store.batch_dir(batch_id) / "概要设计"
        overview = base / "overview_design.md"
        if not overview.is_file() or not overview.read_text(encoding="utf-8").strip():
            raise ValidationError("overview_design.md is missing or empty")
        try:
            data = self.store.read_json(base / "design_manifest.json")
            missing = self.REQUIRED_FIELDS - set(data)
            if missing:
                raise ValueError(f"Missing fields: {sorted(missing)}")
            manifest = DesignManifest.model_validate(data)
            if not manifest.system_name.strip() or not manifest.modules or not manifest.api_endpoints:
                raise ValueError("System name, modules and API endpoints must be non-empty")
        except Exception as exc:
            raise ValidationError(f"Invalid design_manifest.json: {exc}") from exc
        return {"validator": "design", "passed": True, "score": 100,
                "checks": {"overview_design_non_empty": True, "manifest_schema_valid": True}}


SMOKE_SCRIPT = """
import importlib
import inspect
import json
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.api import app
assert isinstance(app, FastAPI), 'src.api.app must be FastAPI'
manifest = json.loads(Path('code_manifest.json').read_text(encoding='utf-8'))
assert 'src.api:app' in str(manifest.get('entrypoint', '')).split(), 'Manifest entrypoint must be src.api:app'
contract = manifest.get('storage_contract')
assert isinstance(contract, dict), 'Manifest storage_contract is required'
module_name = contract.get('module')
attribute = contract.get('service_attribute')
factory_path = contract.get('factory')
argument = contract.get('argument')
assert all(isinstance(value, str) and value for value in (module_name, attribute, factory_path, argument)), 'Manifest storage_contract fields must be non-empty'
module = importlib.import_module(module_name)
assert hasattr(module, attribute), f'Manifest storage service missing: {module_name}.{attribute}'
factory_module, separator, factory_name = factory_path.rpartition('.')
assert separator, f'Manifest storage factory must be a module-qualified path: {factory_path}'
factory = getattr(importlib.import_module(factory_module), factory_name)
assert callable(factory), f'Manifest storage factory is not callable: {factory_path}'
try:
    inspect.signature(factory).bind(**{argument: Path('data')})
except TypeError as exc:
    raise AssertionError(f'Manifest storage factory cannot accept {argument}: {exc}') from exc
with TestClient(app) as client:
    schema = client.get('/openapi.json')
    assert schema.status_code == 200, schema.text
    assert schema.json().get('paths'), 'No application routes'
    actual = {(method.upper(), path) for path, methods in schema.json()['paths'].items() for method in methods if method.lower() in {'get', 'post', 'put', 'patch', 'delete', 'head', 'options'}}
    routes = manifest.get('api_routes')
    assert isinstance(routes, list) and routes, 'Manifest api_routes must be a non-empty list'
    claimed = {(str(route['method']).upper(), route['path']) for route in routes}
    assert actual == claimed, f'Manifest api_routes disagree with FastAPI OpenAPI: missing={sorted(claimed - actual)}, undocumented={sorted(actual - claimed)}'
    health = client.get('/health') if '/health' in schema.json()['paths'] else None
    if health is not None:
        assert health.status_code == 200, health.text
print(json.dumps({'importable': True, 'startup_passed': True, 'routes': len(schema.json()['paths']), 'manifest_consistent': True}))
"""


class CodeValidator:
    REQUIRED_FILES = {"__init__.py", "api.py"}
    FRONTEND_KEYWORDS = ("前端", "web", "b/s", "bs架构", "浏览器", "页面", "按钮", "后台")

    def __init__(self, store: FileStore):
        self.store = store

    def validate(self, batch_id: str) -> dict[str, Any]:
        root = self.store.output_batch_dir(batch_id)
        files = sorted((root / "src").rglob("*.py"))
        missing = self.REQUIRED_FILES - {p.name for p in (root / "src").glob("*.py")}
        if missing:
            raise ValidationError(f"src/ missing required files: {sorted(missing)}")
        for path in files:
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except SyntaxError as exc:
                raise ValidationError(f"Python syntax error: {exc}") from exc
        frontend_required = self._spec_requires_frontend(batch_id)
        frontend = root / "frontend" / "index.html"
        if frontend_required:
            if not frontend.is_file():
                raise ValidationError("frontend/index.html is required")
            html = frontend.read_text(encoding="utf-8").lower()
            if not all(re.search(r"<" + marker + r"\b", html) for marker in ("form", "button")):
                raise ValidationError("frontend/index.html requires form and button elements")
        with tempfile.TemporaryDirectory(prefix="pipeline-smoke-") as temp:
            target = Path(temp) / "application"
            _copy_application(root, target)
            completed = _run([sys.executable, "-c", SMOKE_SCRIPT], target, 30)
        output = completed.stdout + completed.stderr
        result = {"validator": "code", "passed": completed.returncode == 0,
                  "score": 100 if completed.returncode == 0 else 0,
                  "checks": {"python_syntax_valid": True, "fastapi_app_importable": completed.returncode == 0,
                             "application_smoke_passed": completed.returncode == 0,
                             "manifest_consistent": completed.returncode == 0,
                             "frontend_required": frontend_required, "frontend_present": frontend.is_file()},
                  "files": [p.relative_to(root).as_posix() for p in files],
                  "smoke_output": output[-6000:], "summary": output[-6000:]}
        manifest_path = root / "code_manifest.json"
        if manifest_path.is_file():
            manifest = self.store.read_json(manifest_path)
            result["generation_strategy"] = manifest.get("strategy")
            result["limitations"] = manifest.get("limitations", [])
        return _require(result)

    def _spec_requires_frontend(self, batch_id: str) -> bool:
        state = self.store.load_state(batch_id)
        text = self.store.resolve(state.spec_path).read_text(encoding="utf-8").lower()
        manifest = self.store.read_json(self.store.batch_dir(batch_id) / "概要设计" / "design_manifest.json")
        return bool(manifest.get("frontend_requirements") or manifest.get("pages")) or any(k in text for k in self.FRONTEND_KEYWORDS)


class TestValidator:
    __test__ = False
    COVERAGE_THRESHOLD = 80

    def __init__(self, store: FileStore):
        self.store = store

    def validate(self, batch_id: str) -> dict[str, Any]:
        root = self.store.output_batch_dir(batch_id)
        tests = list((root / "tests" / "generated").rglob("test_*.py"))
        if not tests:
            raise ValidationError("generated pytest files are missing")
        with tempfile.TemporaryDirectory(prefix="pipeline-tests-") as temp:
            target = Path(temp) / "application"
            _copy_application(root, target)
            # Explicit config avoids inheriting platform testpaths from a parent directory.
            config = target / "pytest-validation.ini"
            config.write_text("[pytest]\npythonpath = .\n", encoding="utf-8")
            completed = _run([sys.executable, "-m", "pytest", "-p", "pytest_cov", "-c", str(config),
                              "-q", "tests/generated", "--continue-on-collection-errors", "--cov=src", "--cov-branch",
                              "--cov-report=term-missing", "--cov-report=json:coverage.json",
                              "--junitxml=pytest-results.xml"], target)
            output = completed.stdout + "\n" + completed.stderr
            coverage = None
            if (target / "coverage.json").is_file():
                coverage = json.loads((target / "coverage.json").read_text())["totals"]["percent_covered"]
            collection_errors = 0
            counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "total": 0}
            if (target / "pytest-results.xml").is_file():
                cases = ET.parse(target / "pytest-results.xml").findall(".//testcase")
                for case in cases:
                    kind = ("errors" if case.find("error") is not None else "failed" if case.find("failure") is not None
                            else "skipped" if case.find("skipped") is not None else "passed")
                    counts[kind] += 1
                    error = case.find("error")
                    if error is not None and "collection" in (error.get("message", "") + error.get("type", "")).lower():
                        collection_errors += 1
                counts["total"] = len(cases)
            coverage_passed = coverage is not None and coverage >= self.COVERAGE_THRESHOLD
            tests_passed = completed.returncode == 0 and counts["passed"] > 0 and counts["failed"] == counts["errors"] == 0
            result = {"validator": "test", "passed": coverage_passed and tests_passed,
                      "score": round(coverage or 0, 2), "coverage_pct": round(coverage, 2) if coverage is not None else None,
                      "coverage_threshold": self.COVERAGE_THRESHOLD, "pytest_exit_code": completed.returncode,
                      "checks": {"generated_tests_exist": True, "tests_collection_passed": collection_errors == 0 and completed.returncode in (0, 1),
                                 "coverage_threshold_passed": coverage_passed, "all_tests_passed": tests_passed},
                      "test_counts": counts, "collection_errors": collection_errors, "summary": output[-10000:]}
            report_dir = self.store.batch_dir(batch_id) / "单元测试"
            self.store.write_text(report_dir / "pytest_output.txt", output)
            for name in ("coverage.json", "pytest-results.xml"):
                if (target / name).is_file():
                    self.store.write_text(report_dir / name, (target / name).read_text(encoding="utf-8"))
        self.store.write_json(report_dir / "test_result.json", result)
        return _require(result)


def validate_batch_smoke(store: FileStore, batch_id: str) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name, validator in (("design", DesignArtifactValidator), ("code", CodeValidator), ("test", TestValidator)):
        try:
            results[name] = validator(store).validate(batch_id)
        except ValidationError as exc:
            results[name] = {"validator": name, **exc.result, "passed": False}
        except Exception as exc:
            results[name] = {"validator": name, "passed": False, "summary": f"{type(exc).__name__}: {exc}"}
    plan = store.batch_dir(batch_id) / "单元测试" / "test_plan.md"
    results["test_plan_exists"] = plan.is_file() and bool(plan.read_text(encoding="utf-8").strip())
    results["passed"] = all(results[n]["passed"] for n in ("design", "code", "test")) and results["test_plan_exists"]
    store.write_json(store.batch_dir(batch_id) / "quality_report.json", results)
    store.write_json(store.output_batch_dir(batch_id) / "quality_report.json", results)
    return results
