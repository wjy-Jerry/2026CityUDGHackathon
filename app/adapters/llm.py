from __future__ import annotations

from typing import Any, Protocol, TypeVar

from pydantic import BaseModel


SchemaT = TypeVar("SchemaT", bound=BaseModel)


class LLMAdapter(Protocol):
    def generate_text(self, *, system: str, user: str, metadata: dict[str, str] | None = None) -> str:
        ...

    def generate_json(
        self,
        *,
        system: str,
        user: str,
        schema: type[SchemaT],
        metadata: dict[str, str] | None = None,
    ) -> SchemaT:
        ...


class LLMError(RuntimeError):
    """Clear wrapper for model-call failures."""


class MockLLMAdapter:
    """Deterministic adapter for demos and tests without an API key."""

    def generate_text(self, *, system: str, user: str, metadata: dict[str, str] | None = None) -> str:
        from app.agents.fallback import mock_overview
        spec = user.split("# 产品规格说明书\n")[-1].split("\n\n# 已生成")[0]
        return mock_overview(spec, (metadata or {}).get("sample_fixture") or None)

    def generate_json(
        self,
        *,
        system: str,
        user: str,
        schema: type[SchemaT],
        metadata: dict[str, str] | None = None,
    ) -> SchemaT:
        from app.agents.fallback import mock_design
        if schema.__name__ != "DesignManifest":
            raise LLMError("Offline mode uses explicit code/test templates")
        spec = user.split("# 产品规格说明书\n")[-1].split("\n\n# 已生成")[0]
        return schema.model_validate(mock_design(spec, (metadata or {}).get("sample_fixture") or None))
