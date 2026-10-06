from __future__ import annotations

import shutil
import json
from textwrap import dedent
from pathlib import Path

from pydantic import BaseModel, Field

from app.agents.base import BaseAgent


BUSINESS_TEST = r'''
from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from src import api as business_api
from src.csv_repository import CSVRepository
from src.models import ReservationCreate
from src.services import DISABLED_MESSAGE, PAYMENT_SUCCESS_MESSAGE, ReservationService


def make_request(day: date, campus: str = "Weifang", plate_no: str = "鲁A12345") -> ReservationCreate:
    return ReservationCreate(
        name="Alice",
        employee_id="E001",
        mobile="13800000000",
        campus=campus,
        reservation_date=day,
        plate_no=plate_no,
    )


def api_payload(day: date, campus: str = "Weifang", plate_no: str = "鲁A12345") -> dict[str, str]:
    return {
        "name": "Alice",
        "employee_id": "E001",
        "mobile": "13800000000",
        "campus": campus,
        "reservation_date": day.isoformat(),
        "plate_no": plate_no,
    }


def test_fastapi_endpoints_use_local_csv_service(tmp_path, monkeypatch):
    monkeypatch.setattr(business_api, "service", ReservationService(tmp_path))
    client = TestClient(business_api.app)
    day = date.today() + timedelta(days=1)

    assert client.get("/health").json()["status"] == "ok"
    assert len(client.get("/campuses").json()) == 4

    created = client.post("/reservations", json=api_payload(day, plate_no="鲁A54321"))
    assert created.status_code == 200
    body = created.json()
    assert body["success"] is True
    reservation_id = body["reservation_id"]

    assert len(client.get("/reservations").json()) == 1
    paid = client.post(f"/reservations/{reservation_id}/pay")
    assert paid.status_code == 200
    assert paid.json()["message"] == PAYMENT_SUCCESS_MESSAGE
    cancelled = client.post(f"/reservations/{reservation_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["success"] is True


def test_csv_repository_initialization_append_and_update(tmp_path):
    repo = CSVRepository(tmp_path)
    repo.initialize()
    repo.append("internal_vehicle_archive.csv", {"plate_no": "鲁A12345", "owner": "Alice", "remark": "demo"})
    assert repo.read_all("internal_vehicle_archive.csv")[0]["owner"] == "Alice"
    count = repo.update("internal_vehicle_archive.csv", lambda row: row["plate_no"] == "鲁A12345", {"owner": "Bob"})
    assert count == 1
    assert repo.read_all("internal_vehicle_archive.csv")[0]["owner"] == "Bob"


def test_successful_reservation_and_advance_payment(tmp_path):
    service = ReservationService(tmp_path)
    result = service.create_reservation(make_request(date.today() + timedelta(days=1)))
    assert result["success"] is True
    payment = service.advance_payment(result["reservation_id"])
    assert payment["message"] == PAYMENT_SUCCESS_MESSAGE
    assert service.repository.read_all("payment_records.csv")[0]["status"] == "success"


def test_quota_exceeded(tmp_path):
    service = ReservationService(tmp_path)
    day = date.today() + timedelta(days=1)
    service.repository.update("campus_configs.csv", lambda row: row["campus"] == "Weifang", {"weekday_quota": 1, "rest_day_quota": 1})
    assert service.create_reservation(make_request(day, plate_no="鲁A12345"))["success"] is True
    result = service.create_reservation(make_request(day, plate_no="鲁A12346"))
    assert result["success"] is False
    assert "名额已满" in result["message"]


def test_reservation_date_outside_next_seven_days_fails(tmp_path):
    service = ReservationService(tmp_path)
    result = service.create_reservation(make_request(date.today() + timedelta(days=8)))
    assert result["success"] is False
    assert "未来7天内" in result["message"]


def test_same_plate_number_cannot_reserve_twice_same_day(tmp_path):
    service = ReservationService(tmp_path)
    day = date.today() + timedelta(days=1)
    assert service.create_reservation(make_request(day, campus="Weifang", plate_no="鲁A12345"))["success"] is True
    result = service.create_reservation(make_request(day, campus="Qingdao", plate_no="鲁A12345"))
    assert result["success"] is False
    assert "同一车牌" in result["message"]


def test_disabled_campus_fails(tmp_path):
    service = ReservationService(tmp_path)
    service.set_campus_enabled("Weifang", False)
    result = service.create_reservation(make_request(date.today() + timedelta(days=1)))
    assert result["success"] is False
    assert result["message"] == DISABLED_MESSAGE


def test_cancellation_releases_quota(tmp_path):
    service = ReservationService(tmp_path)
    day = date.today() + timedelta(days=1)
    service.repository.update("campus_configs.csv", lambda row: row["campus"] == "Weifang", {"weekday_quota": 1, "rest_day_quota": 1})
    first = service.create_reservation(make_request(day, plate_no="鲁A12345"))
    assert first["success"] is True
    cancel = service.cancel_reservation(first["reservation_id"])
    assert cancel["success"] is True
    second = service.create_reservation(make_request(day, plate_no="鲁A12346"))
    assert second["success"] is True
    ketuo = service.repository.read_all("ketuo_reservation_archive.csv")
    assert ketuo[0]["status"] == "cancelled"

def test_internal_archive_plate_normalization_and_payment_idempotency(tmp_path):
    service = ReservationService(tmp_path)
    day = date.today() + timedelta(days=1)
    service.repository.append('internal_vehicle_archive.csv', {'plate_no':'鲁A12345','owner':'Alice'})
    assert not service.create_reservation(make_request(day))['success']
    created = service.create_reservation(make_request(day, plate_no=' 鲁a54321 '))
    assert created['success']
    first = service.advance_payment(created['reservation_id'])
    assert service.advance_payment(created['reservation_id'])['payment_id'] == first['payment_id']
    assert len(service.repository.read_all('payment_records.csv')) == 1
    assert service.repository.read_all('payment_records.csv')[0]['campus'] == 'Weifang'
    assert not service.cancel_reservation('missing')['success']
    assert not service.advance_payment('missing')['success']


def test_availability_and_configuration_api(tmp_path, monkeypatch):
    monkeypatch.setattr(business_api, 'service', ReservationService(tmp_path))
    with TestClient(business_api.app) as client:
        day = (date.today() + timedelta(days=1)).isoformat()
        response = client.put('/campuses/Weifang', json={'weekday_quota':0,'rest_day_quota':0,'enabled':True,'instruction':'Closed quota'})
        assert response.status_code == 200 and response.json()['success']
        assert client.get('/availability', params={'campus':'Weifang','reservation_date':day}).json()['remaining'] == 0
        assert client.get('/availability', params={'campus':'Unknown','reservation_date':day}).status_code == 404
        assert client.put('/campuses/Unknown', json={'weekday_quota':1,'rest_day_quota':1,'enabled':True}).status_code == 404
        assert client.put('/campuses/Weifang', json={'weekday_quota':-1,'rest_day_quota':1,'enabled':True}).status_code == 422
        assert client.post('/reservations', json={}).status_code == 422


def test_repository_invalid_table_and_field(tmp_path):
    import pytest
    repo = CSVRepository(tmp_path)
    with pytest.raises(ValueError):
        repo.read_all('unknown.csv')
    repo.append('internal_vehicle_archive.csv', {'plate_no':'鲁A12345'})
    with pytest.raises(ValueError):
        repo.update('internal_vehicle_archive.csv', lambda row: True, {'unknown':'x'})
    assert repo.update('internal_vehicle_archive.csv', lambda row: False, {'owner':'x'}) == 0


def test_frontend_is_served_and_has_controls(tmp_path, monkeypatch):
    monkeypatch.setattr(business_api, 'service', ReservationService(tmp_path))
    with TestClient(business_api.app) as client:
        response = client.get('/')
        assert response.status_code == 200
        assert '<form' in response.text and '<button' in response.text
        assert client.get('/app.js').status_code == 200


def test_concurrent_reservations_cannot_exceed_quota(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    service = ReservationService(tmp_path)
    day = date.today() + timedelta(days=1)
    service.repository.update('campus_configs.csv', lambda row: row['campus'] == 'Weifang', {'weekday_quota':1,'rest_day_quota':1})
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda plate: service.create_reservation(make_request(day, plate_no=plate)), ['鲁A12345','鲁A12346','鲁A12347']))
    assert sum(result['success'] for result in results) == 1
    assert len(service.repository.read_all('reservations.csv')) == 1

'''


class GeneratedTestFile(BaseModel):
    path: str = Field(pattern=r"^tests/generated/(conftest|test_.+)\.py$", description="conftest.py or test_*.py under tests/generated/")
    content: str = Field(min_length=1, description="Complete pytest file contents")


class TestGenerationResult(BaseModel):
    files: list[GeneratedTestFile] = Field(min_length=1)
    test_plan_markdown: str = Field(min_length=1)


class TestAgent(BaseAgent):
    agent_name = "TestAgent"
    prompt_file = "test_agent.md"

    def run(self, input_context: dict[str, str]) -> list[object]:
        batch_id = input_context["batch_id"]
        spec_path = input_context["spec_path"]
        result = self._generate_tests(batch_id=batch_id, spec_path=spec_path)
        out_root = self.store.output_batch_dir(batch_id)
        generated_dir = out_root / "tests" / "generated"
        if generated_dir.exists():
            shutil.rmtree(generated_dir)
        generated_dir.mkdir(parents=True, exist_ok=True)
        init_ref = self.store.write_text(generated_dir / "__init__.py", "")
        test_refs = [init_ref]
        for generated_file in result.files:
            target = self._safe_test_path(generated_file.path, base_dir=out_root)
            test_refs.append(self.store.write_text(target, generated_file.content))

        output_dir = self.batch_artifact_dir(batch_id, "单元测试")
        plan_ref = self.store.write_text(output_dir / "test_plan.md", result.test_plan_markdown)

        snapshot_dir = output_dir / "tests_snapshot"
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
        shutil.copytree(generated_dir, snapshot_dir)
        snapshot_refs = [
            self.store.artifact_for(path, kind="tests_snapshot")
            for path in sorted(snapshot_dir.rglob("*.py"))
            if path.is_file()
        ]
        return [*test_refs, plan_ref, *snapshot_refs]

    def _generate_tests(self, *, batch_id: str, spec_path: str) -> TestGenerationResult:
        prompt = self.load_prompt()
        overview = self._optional_batch_text(batch_id, "概要设计", "overview_design.md")
        design_manifest = self._optional_batch_text(batch_id, "概要设计", "design_manifest.json")
        code_manifest = self._optional_batch_text(batch_id, "代码生成", "code_manifest.json")
        if json.loads(code_manifest).get("strategy") == "generic-template-fallback":
            from app.agents.fallback import GENERIC_TESTS
            return TestGenerationResult(files=[GeneratedTestFile(path="tests/generated/test_records.py", content=GENERIC_TESTS)],
                                        test_plan_markdown="# Test Plan\n\nRecord lifecycle, CSV persistence, invalid input and missing records.")
        if self._is_template_code_manifest(code_manifest):
            return self._template_generation_result()
        source_index = self._source_index(batch_id)
        user = (
            "Generate isolated pytest tests from persisted design and interface metadata only. "
            "Use storage_contract in code_manifest to instantiate the service with tmp_path; "
            "do not guess imports or silently fall back to shared data. "
            "Return JSON files and test_plan_markdown.\n"
            f"# Design overview\n{overview}\n# Design manifest\n{design_manifest}\n"
            f"# Code interface manifest\n{code_manifest}\n# Source file paths (no code text)\n{source_index}"
        )
        metadata = {"batch_id": batch_id, "node_id": "test"}
        try:
            return self.llm.generate_json(system=prompt, user=user, schema=TestGenerationResult, metadata=metadata)
        except Exception:
            if self._strict_mode():
                raise
            raise RuntimeError("Cannot safely generate fallback tests for arbitrary LLM code; retry in LLM mode")

    def _strict_mode(self) -> bool:
        adapter_settings = getattr(self.llm, "settings", None)
        return bool(adapter_settings is not None and getattr(adapter_settings, "llm_strict", False))

    def _optional_batch_text(self, batch_id: str, dirname: str, filename: str) -> str:
        path = self.store.batch_dir(batch_id) / dirname / filename
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def _is_template_code_manifest(self, manifest_text: str) -> bool:
        if not manifest_text.strip():
            return False
        try:
            data = json.loads(manifest_text)
        except json.JSONDecodeError:
            return False
        return str(data.get("strategy", "")) == "template-fallback"

    def _source_index(self, batch_id: str) -> str:
        src_dir = self.store.output_batch_dir(batch_id) / "src"
        if not src_dir.exists():
            src_dir = self.store.root_dir / "src"
        return "\n".join("- src/" + path.relative_to(src_dir).as_posix() for path in sorted(src_dir.rglob("*.py")))

    def _safe_test_path(self, generated_path: str, base_dir: Path | None = None) -> Path:
        if "\\" in generated_path:
            raise ValueError("Generated paths must use POSIX separators")
        candidate = Path(generated_path)
        if candidate.is_absolute():
            raise ValueError(f"Generated path must be relative: {generated_path}")
        if any(part in {"..", "", ".git"} for part in candidate.parts):
            raise ValueError(f"Unsafe generated path: {generated_path}")
        if len(candidate.parts) < 3 or candidate.parts[:2] != ("tests", "generated"):
            raise ValueError(f"Generated test path must be under tests/generated/: {generated_path}")
        root = base_dir or self.store.root_dir
        target = (root / candidate).resolve()
        if not target.is_relative_to(root.resolve()):
            raise ValueError("Generated test path escapes application root")
        test_root = (root / "tests" / "generated").resolve()
        if target != test_root and test_root not in target.parents:
            raise ValueError(f"Generated test path escapes tests/generated/: {generated_path}")
        if target.suffix != ".py" or (not target.name.startswith("test_") and target.name != "conftest.py"):
            raise ValueError(f"Generated test file must be conftest.py or test_*.py: {generated_path}")
        return target

    def _template_generation_result(self) -> TestGenerationResult:
        return TestGenerationResult(
            files=[
                GeneratedTestFile(
                    path="tests/generated/test_business_reservation.py",
                    content=dedent(BUSINESS_TEST).lstrip("\n"),
                )
            ],
            test_plan_markdown=(
                "# Test Plan\n\n"
                "- Validate CSV repository initialization, append, query, and update.\n"
                "- Validate reservation success, quota limits, date window, duplicate plate, disabled campus, cancellation, and payment.\n"
                "- Tests use only local temporary CSV files and never call an LLM.\n"
            ),
        )
