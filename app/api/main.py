from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.orchestrator.best_of import BestOfSelector
from app.orchestrator.engine import Orchestrator
from app.orchestrator.state import NodeId
from app.storage.file_store import FileStore
from app.storage.package import application_package
from app.validators.artifacts import validate_batch_smoke


class BestOfRequest(BaseModel):
    batch_ids: list[str] = Field(min_length=2, description="At least 2 batch IDs to compare")


app = FastAPI(title="AI Agent Development Pipeline API")
store = FileStore()
orchestrator = Orchestrator(store=store)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "ai-agent-development-pipeline"}


@app.post("/api/v1/batches")
async def create_batch(file: UploadFile = File(...), mode: str = Form("auto"), sample_fixture: str = Form("")) -> dict[str, object]:
    if not file.filename or not file.filename.endswith(".md"):
        raise HTTPException(status_code=400, detail={"message": "Please upload a Markdown .md file"})
    if mode not in {"auto", "manual"}:
        raise HTTPException(status_code=400, detail={"message": "mode must be auto or manual"})
    content = await file.read()
    if not content.strip():
        raise HTTPException(status_code=400, detail={"message": "Uploaded specification is empty"})
    try:
        content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, detail={"message": "Specification must be UTF-8 encoded"}) from exc
    try:
        state = orchestrator.create_batch_from_bytes(filename=file.filename, content=content, mode=mode, sample_fixture=sample_fixture or None)  # type: ignore[arg-type]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc
    return {"batch_id": state.batch_id, "state": state.model_dump(mode="json")}


@app.post("/api/v1/batches/{batch_id}/run", status_code=202)
def run_batch(batch_id: str, background_tasks: BackgroundTasks) -> dict[str, str]:
    _load_state_or_404(batch_id)
    _schedule(background_tasks, "run", batch_id)
    return {"batch_id": batch_id, "status": "accepted"}


@app.get("/api/v1/batches/{batch_id}")
def get_batch(batch_id: str) -> dict[str, object]:
    state = _load_state_or_404(batch_id)
    return state.model_dump(mode="json")


@app.get("/api/v1/batches/{batch_id}/logs")
def get_logs(batch_id: str) -> list[dict[str, object]]:
    _load_state_or_404(batch_id)
    path = store.log_path(batch_id)
    if not path.exists():
        return []
    return store.read_json(path)


@app.get("/api/v1/batches/{batch_id}/artifacts")
def get_artifacts(batch_id: str) -> list[dict[str, str]]:
    _load_state_or_404(batch_id)
    return store.list_artifacts(batch_id)


@app.get("/api/v1/batches/{batch_id}/download")
def download_artifact(batch_id: str, path: str) -> FileResponse:
    _load_state_or_404(batch_id)
    try:
        target = store.resolve(path)
    except ValueError as exc:
        raise HTTPException(400, detail={"message": str(exc)}) from exc
    batch_root = store.batch_dir(batch_id).resolve()
    if batch_root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail={"message": "Artifact not found"})
    return FileResponse(target, filename=target.name)


@app.post("/api/v1/batches/{batch_id}/advance", status_code=202)
def advance_batch(batch_id: str, background_tasks: BackgroundTasks) -> dict[str, str]:
    state = _load_state_or_404(batch_id)
    if state.mode != "manual":
        raise HTTPException(status_code=400, detail={"message": "advance is only available for manual mode batches"})
    if state.status != "paused":
        raise HTTPException(status_code=400, detail={"message": f"Batch is not paused (status: {state.status})"})
    _schedule(background_tasks, "advance", batch_id)
    return {"batch_id": batch_id, "node_id": state.current_node, "status": "accepted"}


@app.post("/api/v1/batches/{batch_id}/retry/{node_id}", status_code=202)
def retry_node(batch_id: str, node_id: NodeId, background_tasks: BackgroundTasks) -> dict[str, str]:
    _load_state_or_404(batch_id)
    _schedule(background_tasks, "retry", batch_id, node_id)
    return {"batch_id": batch_id, "node_id": node_id, "status": "accepted"}


@app.post("/api/v1/validate")
def validate(payload: dict[str, str]) -> dict[str, object]:
    batch_id = payload.get("batch_id")
    if not batch_id:
        raise HTTPException(status_code=400, detail={"message": "batch_id is required"})
    state = _load_state_or_404(batch_id)
    if state.status in {"queued", "running", "paused"}:
        raise HTTPException(409, detail={"message": "Finish the pipeline before validation"})
    try:
        return {"batch_id": batch_id, "validation": validate_batch_smoke(store, batch_id)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc), "error_class": exc.__class__.__name__}) from exc


@app.get("/api/v1/batches")
def list_batches() -> list[dict[str, object]]:
    """Return summary of all batches, sorted newest-first."""
    generated_dir = store.generated_dir
    if not generated_dir.exists():
        return []
    summaries = []
    for batch_dir in sorted(generated_dir.iterdir(), reverse=True):
        status_file = batch_dir / "batch_status.json"
        if not batch_dir.is_dir() or not status_file.exists():
            continue
        try:
            data = store.read_json(status_file)
            summaries.append({
                "batch_id": data.get("batch_id"),
                "status": data.get("status"),
                "current_node": data.get("current_node"),
                "spec_path": data.get("spec_path"),
            })
        except Exception:
            continue
    return summaries


@app.get("/api/v1/batches/{batch_id}/score")
def score_batch(batch_id: str) -> dict[str, object]:
    """Return the design quality score for a single succeeded batch."""
    _load_state_or_404(batch_id)
    selector = BestOfSelector(store)
    return selector.score_design(batch_id)


@app.post("/api/v1/batches/best-of", status_code=202)
def best_of_batches(payload: BestOfRequest, background_tasks: BackgroundTasks) -> dict[str, object]:
    """Score each batch's design artifact, seed a new batch from the winner, and run code + test.

    The new batch skips design (pre-seeded from the winner) and starts fresh
    from code generation, so it produces internally consistent code and tests.
    """
    for bid in payload.batch_ids:
        _load_state_or_404(bid)

    selector = BestOfSelector(store)
    scores = selector.score_batches(payload.batch_ids)
    winner = selector.pick_winner(scores)

    if winner is None:
        raise HTTPException(
            status_code=400,
            detail={"message": "No batch with a valid succeeded design node found", "scores": scores},
        )

    new_state = selector.create_seeded_batch(winner["batch_id"])
    _schedule(background_tasks, "run", new_state.batch_id)

    return {
        "new_batch_id": new_state.batch_id,
        "winner_batch_id": winner["batch_id"],
        "winner_score": winner["score"],
        "winner_breakdown": winner.get("breakdown", {}),
        "scores": scores,
        "status": "accepted",
    }


@app.get("/api/v1/batches/{batch_id}/package")
def package_batch(batch_id: str) -> StreamingResponse:
    """Bundle output/{batch_id}/ into a downloadable zip."""
    _load_state_or_404(batch_id)
    out_dir = store.output_batch_dir(batch_id)
    files = [p for p in out_dir.rglob("*") if p.is_file()]
    if not files:
        raise HTTPException(
            status_code=404,
            detail={"message": "No generated package found — has the pipeline completed successfully?"},
        )
    buf = io.BytesIO(application_package(out_dir))
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{batch_id}.zip"'},
    )


def _load_state_or_404(batch_id: str):
    try:
        return store.load_state(batch_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail={"message": "Batch not found"}) from exc


def _schedule(background_tasks, action, batch_id, *args):
    try:
        orchestrator.schedule(background_tasks, action, batch_id, *args)
    except ValueError as exc:
        raise HTTPException(409, detail={"message": str(exc)}) from exc
