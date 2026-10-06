from __future__ import annotations

import time
import uuid
import threading
from functools import wraps
from pathlib import Path
from typing import Literal

from app.adapters.llm import LLMAdapter
from app.adapters.openai_adapter import OpenAIAdapter
from app.agents.code_agent import CodeAgent
from app.agents.design_agent import DesignAgent
from app.agents.test_agent import TestAgent
from app.config import get_settings
from app.orchestrator.logger import ExecutionLogger
from app.orchestrator.state import ArtifactRef, BatchState, NodeId, now_iso
from app.storage.file_store import FileStore
from app.validators.artifacts import CodeValidator, DesignArtifactValidator, TestValidator, ValidationError


NODE_ORDER: list[NodeId] = ["design", "code", "test"]


def exclusive_execution(method):
    @wraps(method)
    def guarded(self, batch_id, *args, **kwargs):
        with self._guard:
            lock = self._locks.setdefault(batch_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise ValueError("Batch is already executing")
        try:
            return method(self, batch_id, *args, **kwargs)
        finally:
            lock.release()
    return guarded


class Orchestrator:
    def __init__(self, *, store: FileStore | None = None, llm: LLMAdapter | None = None, max_retries: int | None = None) -> None:
        self.store = store or FileStore()
        self.llm = llm or OpenAIAdapter()
        self.max_retries = get_settings().max_retries if max_retries is None else max_retries
        self.logger = ExecutionLogger(self.store)
        self._guard = threading.Lock()
        self._locks = {}
        self._scheduled = set()

    def schedule(self, background_tasks, action: str, batch_id: str, *args) -> None:
        state = self.store.load_state(batch_id)
        if action == "run" and state.status not in {"queued"}:
            raise ValueError("Only queued batches can be started; use advance or retry")
        if action == "retry":
            self._check_retry(state, args[0])
        with self._guard:
            if batch_id in self._scheduled or (batch_id in self._locks and self._locks[batch_id].locked()):
                raise ValueError("Batch is already scheduled or executing")
            self._scheduled.add(batch_id)
        background_tasks.add_task(self._dispatch, action, batch_id, args)

    def _dispatch(self, action, batch_id, args):
        try:
            {"run": self.run_batch, "advance": self.advance_node, "retry": self.retry_node}[action](batch_id, *args)
        finally:
            with self._guard:
                self._scheduled.discard(batch_id)

    def _check_retry(self, state, node_id):
        if node_id not in NODE_ORDER:
            raise ValueError(f"Unsupported node_id: {node_id}")
        if state.status == "running":
            raise ValueError("Cannot retry a running batch")
        if any(state.nodes[n].status != "succeeded" for n in NODE_ORDER[:NODE_ORDER.index(node_id)]):
            raise ValueError("Upstream dependencies must succeed before retrying this node")


    def create_batch_from_bytes(
        self,
        *,
        filename: str,
        content: bytes,
        mode: Literal["auto", "manual"] = "auto",
    ) -> BatchState:
        batch_id = self._new_batch_id()
        spec_path = self.store.save_uploaded_spec(batch_id=batch_id, filename=filename, content=content)
        state = BatchState.new(batch_id=batch_id, spec_path=self.store.relpath(spec_path), mode=mode)
        self.store.batch_dir(batch_id)
        self.store.save_state(state)
        self.store.init_logs(batch_id)
        self.logger.log(batch_id=batch_id, event="batch_created", message="Batch created", metadata={"mode": mode})
        return state

    def create_batch_from_path(self, spec_path: Path, mode: Literal["auto", "manual"] = "auto") -> BatchState:
        batch_id = self._new_batch_id()
        copied = self.store.copy_spec(batch_id=batch_id, source_path=spec_path)
        state = BatchState.new(batch_id=batch_id, spec_path=self.store.relpath(copied), mode=mode)
        self.store.batch_dir(batch_id)
        self.store.save_state(state)
        self.store.init_logs(batch_id)
        self.logger.log(batch_id=batch_id, event="batch_created", message="Batch created", metadata={"mode": mode})
        return state

    @exclusive_execution
    def run_batch(self, batch_id: str) -> BatchState:
        state = self.store.load_state(batch_id)
        if state.status == "succeeded":
            return state
        if state.status != "queued":
            raise ValueError("Use advance for paused batches or retry for failed batches")
        state.status = "running"
        self.store.save_state(state)
        mode_label = "manual" if state.mode == "manual" else "automatic"
        self.logger.log(batch_id=batch_id, event="batch_started", message=f"Pipeline started ({mode_label} mode)")
        return self._execute_pending_nodes(state)

    @exclusive_execution
    def advance_node(self, batch_id: str) -> BatchState:
        """Approve and run the next queued node in a paused manual-mode batch."""
        state = self.store.load_state(batch_id)
        if state.mode != "manual":
            raise ValueError("advance_node is only available for manual mode batches")
        if state.status != "paused":
            raise ValueError(f"Batch {batch_id} is not paused (status: {state.status})")
        state.status = "running"
        self.store.save_state(state)
        self.logger.log(
            batch_id=batch_id,
            node_id=state.current_node,
            event="node_approved",
            message=f"User approved execution of {state.current_node}",
        )
        return self._execute_pending_nodes(state)

    @exclusive_execution
    def retry_node(self, batch_id: str, node_id: NodeId) -> BatchState:
        state = self.store.load_state(batch_id)
        self._check_retry(state, node_id)
        start_index = NODE_ORDER.index(node_id)
        for downstream in NODE_ORDER[start_index:]:
            node = state.nodes[downstream]
            node.status = "queued"
            node.inputs = []
            node.outputs = []
            node.duration_ms = None
            node.retries = 0
            node.started_at = None
            node.finished_at = None
            node.error_message = None
            node.quality_check_result = {}
            if downstream != node_id:
                node.retries = 0
                node.outputs = []
        state.status = "running"
        state.current_node = node_id
        self.store.save_state(state)
        self.logger.log(batch_id=batch_id, node_id=node_id, event="node_retry_requested", message=f"Retry requested for {node_id}")
        return self._execute_pending_nodes(state)

    def _execute_pending_nodes(self, state: BatchState) -> BatchState:
        """Run all queued nodes in order, pausing after each one in manual mode."""
        for node_id in NODE_ORDER:
            if state.nodes[node_id].status != "queued":
                continue
            state = self._execute_node_with_retries(state, node_id)
            if node_id == "test" and state.nodes[node_id].status == "failed":
                state = self._maybe_repair(state)
            if state.nodes[node_id].status == "failed":
                state.status = "failed"
                state.current_node = node_id
                self.store.save_state(state)
                self._save_quality_summary(state)
                return state
            if state.mode == "manual":
                remaining = [n for n in NODE_ORDER if state.nodes[n].status == "queued"]
                if remaining:
                    state.status = "paused"
                    state.current_node = remaining[0]
                    self.store.save_state(state)
                    self.logger.log(
                        batch_id=state.batch_id,
                        node_id=remaining[0],
                        event="batch_paused",
                        message=f"Pipeline paused — waiting for approval to run {remaining[0]}",
                    )
                    return state
        state.status = "succeeded"
        state.current_node = None
        self.store.save_state(state)
        self._save_quality_summary(state)
        self.logger.log(batch_id=state.batch_id, event="batch_succeeded", message="Pipeline completed successfully")
        return state

    def _execute_node_with_retries(self, state: BatchState, node_id: NodeId) -> BatchState:
        while True:
            state = self._execute_node_once(state, node_id)
            node = state.nodes[node_id]
            if node.status == "succeeded":
                return state
            if node.retries >= self.max_retries:
                return state
            node.retries += 1
            node.status = "queued"
            self.store.save_state(state)
            self.logger.log(
                batch_id=state.batch_id,
                node_id=node_id,
                event="node_retry_scheduled",
                message=f"Retrying {node_id}",
                level="WARNING",
                retry_index=node.retries,
            )

    def _execute_node_once(self, state: BatchState, node_id: NodeId) -> BatchState:
        node = state.nodes[node_id]
        node.status = "running"
        node.started_at = now_iso()
        node.finished_at = None
        node.error_message = None
        node.quality_check_result = {}
        node.inputs = []
        state.status = "running"
        state.current_node = node_id
        self.store.save_state(state)
        self.logger.log(
            batch_id=state.batch_id,
            node_id=node_id,
            event="node_started",
            message=f"{node_id} started",
            retry_index=node.retries,
        )
        started = time.perf_counter()
        try:
            node.inputs = self._inputs_for(state, node_id)
            outputs = self._agent_for(node_id).run({"batch_id": state.batch_id, "spec_path": state.spec_path})
            node.outputs = [output for output in outputs if isinstance(output, ArtifactRef)]
            node.quality_check_result = self._validator_for(node_id).validate(state.batch_id)
            node.status = "succeeded"
            node.finished_at = now_iso()
            duration_ms = int((time.perf_counter() - started) * 1000)
            node.duration_ms = duration_ms
            self.store.save_state(state)
            self.logger.log(
                batch_id=state.batch_id,
                node_id=node_id,
                event="node_succeeded",
                message=f"{node_id} succeeded",
                duration_ms=duration_ms,
                retry_index=node.retries,
                metadata=self._llm_metadata_for(node_id),
            )
            return state
        except Exception as exc:
            if isinstance(exc, ValidationError):
                node.quality_check_result = exc.result
            node.status = "failed"
            node.finished_at = now_iso()
            node.error_message = str(exc)
            duration_ms = int((time.perf_counter() - started) * 1000)
            node.duration_ms = duration_ms
            self.store.save_state(state)
            self.logger.log(
                batch_id=state.batch_id,
                node_id=node_id,
                event="node_failed",
                message=str(exc),
                level="ERROR",
                duration_ms=duration_ms,
                retry_index=node.retries,
                error_class=exc.__class__.__name__,
                metadata=self._llm_metadata_for(node_id),
            )
            return state

    def _save_quality_summary(self, state):
        report = {nid: state.nodes[nid].quality_check_result for nid in NODE_ORDER}
        plan = self.store.batch_dir(state.batch_id) / "单元测试" / "test_plan.md"
        report["test_plan_exists"] = plan.is_file() and bool(plan.read_text(encoding="utf-8").strip())
        report["passed"] = state.status == "succeeded" and all(report[n].get("passed", False) for n in NODE_ORDER) and report["test_plan_exists"]
        report["repair_attempts"] = state.repair_attempts
        self.store.write_json(self.store.batch_dir(state.batch_id) / "quality_report.json", report)
        self.store.write_json(self.store.output_batch_dir(state.batch_id) / "quality_report.json", report)

    def _maybe_repair(self, state: BatchState) -> BatchState:
        quality = state.nodes["test"].quality_check_result
        counts = quality.get("test_counts", {})
        if (state.repair_attempts >= 1 or not getattr(self.llm, "available", False)
                or counts.get("failed", 0) == 0 or counts.get("errors", 0) != 0):
            return state
        # Persist only structured failure metadata, never another agent's context/source.
        import xml.etree.ElementTree as ET
        path = self.store.batch_dir(state.batch_id) / "单元测试" / "pytest-results.xml"
        failures = []
        if path.is_file():
            for case in ET.parse(path).findall(".//testcase"):
                failure = case.find("failure")
                if failure is not None:
                    failures.append({"test": case.get("name"), "class": case.get("classname"),
                                     "message": (failure.get("message") or "Assertion failed")[:1500]})
        diagnostic_dir = self.store.batch_dir(state.batch_id) / "单元测试"
        for name in ("pytest_output.txt", "coverage.json", "pytest-results.xml", "test_result.json"):
            original = diagnostic_dir / name
            if original.is_file():
                self.store.write_text(diagnostic_dir / "repair_before" / name, original.read_text(encoding="utf-8"))
        state.repair_attempts += 1
        report = {"attempt": state.repair_attempts, "test_counts": counts, "failures": failures,
                  "instruction": "Review whether these assertions reveal a genuine implementation defect. Regenerate code from design only when justified; do not weaken tests."}
        ref = self.store.write_json(self.store.batch_dir(state.batch_id) / "repair_report.json", report)
        self.store.save_state(state)
        self.logger.log(batch_id=state.batch_id, node_id="code", event="repair_started",
                        message="One bounded code repair from persisted failure metadata", metadata={"artifact": ref.path})
        state = self._execute_node_once(state, "code")
        if state.nodes["code"].status == "succeeded":
            # Keep the original test suite: repairing must not replace assertions.
            try:
                quality = TestValidator(self.store).validate(state.batch_id)
                state.nodes["test"].status = "succeeded"
                state.nodes["test"].error_message = None
            except ValidationError as exc:
                quality = exc.result
                state.nodes["test"].error_message = str(exc)
            state.nodes["test"].quality_check_result = quality
        else:
            state.nodes["test"].error_message = "Code repair failed validation"
        state.nodes["test"].finished_at = now_iso()
        self.store.save_state(state)
        self.logger.log(batch_id=state.batch_id, node_id="test", event="repair_finished",
                        message=f"Repair test result: {state.nodes['test'].status}")
        return state

    def _inputs_for(self, state: BatchState, node_id: NodeId) -> list[ArtifactRef]:
        if node_id == "design":
            spec = self.store.resolve(state.spec_path)
            return [self.store.artifact_for(spec, kind="spec_md")]
        if node_id == "code":
            return state.nodes["design"].outputs
        return state.nodes["design"].outputs + state.nodes["code"].outputs

    def _agent_for(self, node_id: NodeId):
        if node_id == "design":
            return DesignAgent(store=self.store, llm=self.llm)
        if node_id == "code":
            return CodeAgent(store=self.store, llm=self.llm)
        return TestAgent(store=self.store, llm=self.llm)

    def _validator_for(self, node_id: NodeId):
        if node_id == "design":
            return DesignArtifactValidator(self.store)
        if node_id == "code":
            return CodeValidator(self.store)
        return TestValidator(self.store)

    def _llm_metadata_for(self, node_id: NodeId) -> dict[str, str | bool | None]:
        last_call_metadata = getattr(self.llm, "last_call_metadata", None)
        if last_call_metadata:
            return last_call_metadata
        settings = getattr(self.llm, "settings", None)
        provider = getattr(self.llm, "provider_name", self.llm.__class__.__name__)
        model = None
        if settings is not None:
            if node_id == "design":
                model = getattr(settings, "design_model", None)
            elif node_id == "code":
                model = getattr(settings, "code_model", None)
            elif node_id == "test":
                model = getattr(settings, "test_model", None)
        return {
            "llm_provider": provider,
            "llm_model": model,
            "llm_strict": bool(getattr(settings, "llm_strict", False)) if settings is not None else False,
        }

    def _new_batch_id(self) -> str:
        return f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
