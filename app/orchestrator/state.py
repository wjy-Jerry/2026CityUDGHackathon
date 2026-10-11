from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


NodeId = Literal["design", "code", "test"]
NodeStatus = Literal["queued", "running", "succeeded", "failed", "skipped"]
BatchStatus = Literal["queued", "running", "succeeded", "failed", "paused"]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class ArtifactRef(BaseModel):
    kind: str
    path: str
    sha256: str


class NodeState(BaseModel):
    node_id: NodeId
    status: NodeStatus = "queued"
    retries: int = 0
    duration_ms: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    inputs: list[ArtifactRef] = Field(default_factory=list)
    outputs: list[ArtifactRef] = Field(default_factory=list)
    quality_check_result: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None


class BatchState(BaseModel):
    batch_id: str
    spec_path: str
    mode: Literal["auto", "manual"] = "auto"
    sample_fixture: str | None = None
    status: BatchStatus = "queued"
    current_node: NodeId | None = None
    repair_attempts: int = 0
    nodes: dict[NodeId, NodeState]

    @classmethod
    def new(cls, *, batch_id: str, spec_path: str, mode: Literal["auto", "manual"] = "auto", sample_fixture: str | None = None) -> "BatchState":
        return cls(
            batch_id=batch_id,
            spec_path=spec_path,
            mode=mode,
            sample_fixture=sample_fixture,
            nodes={
                "design": NodeState(node_id="design"),
                "code": NodeState(node_id="code"),
                "test": NodeState(node_id="test"),
            },
        )


class ExecutionLogEntry(BaseModel):
    timestamp: str = Field(default_factory=now_iso)
    batch_id: str
    node_id: NodeId | None = None
    event: str
    level: Literal["INFO", "WARNING", "ERROR"] = "INFO"
    message: str
    duration_ms: int | None = None
    retry_index: int = 0
    error_class: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
