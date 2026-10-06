from __future__ import annotations

import shutil
import time
import uuid
from typing import Any

from app.orchestrator.state import ArtifactRef, BatchState, now_iso
from app.storage.file_store import FileStore


class BestOfSelector:
    """Score completed batches on design quality and create a new batch seeded with the winner's design.

    Flow:
      1. score_design() for each candidate batch_id
      2. pick_winner() to select the highest-scoring one
      3. create_seeded_batch() to copy that design into a fresh batch (code + test still queued)
      4. Caller runs the new batch via Orchestrator — it skips design and starts from code
    """

    def __init__(self, store: FileStore) -> None:
        self.store = store

    def score_design(self, batch_id: str) -> dict[str, Any]:
        """Return a quality score dict for a single batch's design artifacts.

        Score breakdown (0–100):
          - overview_richness : min(char_count // 50, 30)   → max 30
          - business_rules    : min(count * 5, 25)          → max 25
          - api_endpoints     : min(count * 4, 20)          → max 20
          - modules           : min(count * 3, 15)          → max 15
          - entities          : min(count * 2, 10)          → max 10
        """
        try:
            state = self.store.load_state(batch_id)
        except Exception as exc:
            return {"batch_id": batch_id, "score": -1, "reason": f"batch not found: {exc}", "breakdown": {}}

        if state.nodes["design"].status != "succeeded":
            return {
                "batch_id": batch_id,
                "score": 0,
                "reason": f"design node status is '{state.nodes['design'].status}'",
                "breakdown": {},
            }

        try:
            base = self.store.batch_dir(batch_id) / "概要设计"
            overview_path = base / "overview_design.md"
            manifest_path = base / "design_manifest.json"

            if not overview_path.exists() or not manifest_path.exists():
                return {"batch_id": batch_id, "score": 0, "reason": "design artifacts missing", "breakdown": {}}

            overview_text = overview_path.read_text(encoding="utf-8")
            manifest = self.store.read_json(manifest_path)

            breakdown: dict[str, int] = {
                "overview_richness": min(len(overview_text) // 50, 30),
                "business_rules": min(len(manifest.get("business_rules", [])) * 5, 25),
                "api_endpoints": min(len(manifest.get("api_endpoints", [])) * 4, 20),
                "modules": min(len(manifest.get("modules", [])) * 3, 15),
                "entities": min(len(manifest.get("entities", [])) * 2, 10),
            }
            return {
                "batch_id": batch_id,
                "score": sum(breakdown.values()),
                "reason": "scored",
                "breakdown": breakdown,
            }
        except Exception as exc:
            return {"batch_id": batch_id, "score": -1, "reason": str(exc), "breakdown": {}}

    def score_batches(self, batch_ids: list[str]) -> list[dict[str, Any]]:
        return [self.score_design(bid) for bid in batch_ids]

    def pick_winner(self, scores: list[dict[str, Any]]) -> dict[str, Any] | None:
        valid = [s for s in scores if s["score"] > 0 and s.get("reason") == "scored"]
        return max(valid, key=lambda s: s["score"]) if valid else None

    def create_seeded_batch(self, winner_batch_id: str) -> BatchState:
        """Create a new batch whose design node is pre-populated from winner_batch_id.

        The new batch's code and test nodes remain queued so the orchestrator
        runs them fresh against the seeded design.
        """
        winner_state = self.store.load_state(winner_batch_id)
        new_batch_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}_bestof"

        winner_spec = self.store.resolve(winner_state.spec_path)
        new_spec = self.store.copy_spec(batch_id=new_batch_id, source_path=winner_spec)

        state = BatchState.new(
            batch_id=new_batch_id,
            spec_path=self.store.relpath(new_spec),
            mode="auto",
        )

        # Copy design artifacts so CodeAgent can read them by batch_id directory convention
        src_design_dir = self.store.batch_dir(winner_batch_id) / "概要设计"
        dst_design_dir = self.store.batch_dir(new_batch_id) / "概要设计"
        dst_design_dir.mkdir(parents=True, exist_ok=True)

        outputs: list[ArtifactRef] = []
        for src_file in sorted(src_design_dir.iterdir()):
            if src_file.is_file():
                dst_file = dst_design_dir / src_file.name
                shutil.copy2(src_file, dst_file)
                outputs.append(self.store.artifact_for(dst_file))

        # Mark design node as already done — engine will skip it and start from code
        design_node = state.nodes["design"]
        design_node.status = "succeeded"
        design_node.started_at = now_iso()
        design_node.finished_at = now_iso()
        design_node.outputs = outputs
        design_node.quality_check_result = {
            "validator": "design",
            "passed": True,
            "score": 100,
            "note": f"seeded from batch {winner_batch_id}",
        }

        self.store.save_state(state)
        self.store.init_logs(new_batch_id)
        return state
