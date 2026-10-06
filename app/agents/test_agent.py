from __future__ import annotations

import shutil
import json
from pathlib import Path

from pydantic import BaseModel, Field

from app.agents.base import BaseAgent





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
        from app.demo_fixtures import SAMPLE_FIXTURE_ID
        if self.store.load_state(batch_id).sample_fixture == SAMPLE_FIXTURE_ID:
            from app.demo_fixtures.vehicle_reservations import test_result
            if json.loads(code_manifest).get("sample_fixture") != SAMPLE_FIXTURE_ID or json.loads(code_manifest).get("strategy") != "sample-fixture":
                raise ValueError("Code manifest does not match selected sample fixture")
            return test_result()
        if json.loads(code_manifest).get("strategy") == "generic-template-fallback":
            from app.agents.fallback import GENERIC_TESTS
            return TestGenerationResult(files=[GeneratedTestFile(path="tests/generated/test_records.py", content=GENERIC_TESTS)],
                                        test_plan_markdown="# Test Plan\n\nRecord lifecycle, CSV persistence, invalid input and missing records.")
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
