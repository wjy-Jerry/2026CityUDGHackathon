from __future__ import annotations

import hashlib
import json
import shutil
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR
from app.orchestrator.state import ArtifactRef, BatchState, ExecutionLogEntry


class FileStore:
    """Small filesystem store for specs, batch status, logs, and artifacts."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self.root_dir = (root_dir or ROOT_DIR).resolve()
        self.pending_dir = self.root_dir / "docs" / "待生成"
        self.generated_dir = self.root_dir / "docs" / "已生成"
        self.output_dir = self.root_dir / "output"
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def relpath(self, path: Path) -> str:
        return path.resolve().relative_to(self.root_dir).as_posix()

    def resolve(self, relative_path: str) -> Path:
        if "\\" in relative_path or Path(relative_path).is_absolute():
            raise ValueError("Artifact paths must be project-relative POSIX paths")
        target = (self.root_dir / relative_path).resolve()
        if self.root_dir not in target.parents and target != self.root_dir:
            raise ValueError("Path escapes project root")
        return target

    def batch_dir(self, batch_id: str) -> Path:
        self.validate_batch_id(batch_id)
        path = self.generated_dir / batch_id
        self.relpath(path)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def output_batch_dir(self, batch_id: str) -> Path:
        self.validate_batch_id(batch_id)
        path = self.output_dir / batch_id
        self.relpath(path)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_uploaded_spec(self, *, batch_id: str, filename: str, content: bytes) -> Path:
        self.validate_batch_id(batch_id)
        safe_name = Path(filename.replace("\\", "/")).name or "spec.md"
        if not safe_name.endswith(".md"):
            safe_name = f"{safe_name}.md"
        spec_path = self.pending_dir / f"{batch_id}_{safe_name}"
        spec_path.write_bytes(content)
        return spec_path

    def copy_spec(self, *, batch_id: str, source_path: Path) -> Path:
        self.validate_batch_id(batch_id)
        target = self.pending_dir / f"{batch_id}_{source_path.name}"
        shutil.copy2(source_path, target)
        return target

    def write_text(self, path: Path, content: str) -> ArtifactRef:
        self.relpath(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return self.artifact_for(path)

    def write_json(self, path: Path, data: Any) -> ArtifactRef:
        return self.write_text(path, json.dumps(data, ensure_ascii=False, indent=2))

    @staticmethod
    def validate_batch_id(batch_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", batch_id):
            raise ValueError("Invalid batch_id")

    def read_json(self, path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    def artifact_for(self, path: Path, kind: str | None = None) -> ArtifactRef:
        return ArtifactRef(kind=kind or self.infer_kind(path), path=self.relpath(path), sha256=self.sha256(path))

    def infer_kind(self, path: Path) -> str:
        name = path.name
        if name == "overview_design.md":
            return "design_md"
        if name == "design_manifest.json":
            return "design_manifest"
        if name == "code_manifest.json":
            return "code_manifest"
        if name == "test_plan.md":
            return "test_plan"
        if name.endswith(".md"):
            return "markdown"
        if name.endswith(".json"):
            return "json"
        return "file"

    def sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def status_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "batch_status.json"

    def log_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "execution_log.json"

    def save_state(self, state: BatchState) -> None:
        self.write_json(self.status_path(state.batch_id), state.model_dump(mode="json"))

    def load_state(self, batch_id: str) -> BatchState:
        self.validate_batch_id(batch_id)
        return BatchState.model_validate(self.read_json(self.generated_dir / batch_id / "batch_status.json"))

    def init_logs(self, batch_id: str) -> None:
        path = self.log_path(batch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("[]", encoding="utf-8")

    def append_log(self, entry: ExecutionLogEntry) -> None:
        path = self.log_path(entry.batch_id)
        logs = []
        if path.exists():
            logs = json.loads(path.read_text(encoding="utf-8") or "[]")
        logs.append(entry.model_dump(mode="json"))
        self.write_json(path, logs)

    def list_artifacts(self, batch_id: str) -> list[dict[str, str]]:
        base = self.batch_dir(batch_id)
        artifacts: list[dict[str, str]] = []
        for path in sorted(base.rglob("*")):
            if path.is_file():
                ref = self.artifact_for(path)
                artifacts.append(ref.model_dump())
        return artifacts
