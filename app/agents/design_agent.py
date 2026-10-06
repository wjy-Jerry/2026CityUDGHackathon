from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.base import BaseAgent


class DesignManifest(BaseModel):
    generation_profile: str = "llm"
    requirements_summary: list[str] = Field(default_factory=list)
    system_name: str
    modules: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list)
    api_endpoints: list[str] = Field(default_factory=list)
    csv_tables: list[str] = Field(default_factory=list)
    validation_rules: list[str] = Field(default_factory=list)
    frontend_requirements: list[str] = Field(default_factory=list)
    pages: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)


class DesignAgent(BaseAgent):
    agent_name = "DesignAgent"
    prompt_file = "design_agent.md"

    def run(self, input_context: dict[str, str]) -> list[object]:
        batch_id = input_context["batch_id"]
        spec_path = input_context["spec_path"]
        spec_text = self.read_text(spec_path)
        prompt = self.load_prompt()
        selected_fixture = input_context.get("sample_fixture") or self.store.load_state(batch_id).sample_fixture or ""
        metadata = {"batch_id": batch_id, "node_id": "design", "sample_fixture": selected_fixture}
        if metadata["sample_fixture"]:
            from app.adapters.llm import MockLLMAdapter
            adapter = MockLLMAdapter()
        else:
            adapter = self.llm

        overview_user = (
            "以下是产品规格说明书，请生成完整的概要设计文档（overview_design.md）。\n"
            "严格按照系统指令中规定的八章结构输出，每章缺一不可。\n"
            "输出为工程文档风格，内容精确到足以让下游 CodeAgent 无需猜测即可编写代码。\n"
            "不得遗漏规格书中的任何功能需求，包括前端 UI、管理后台、外部系统集成。\n\n"
            f"# 产品规格说明书\n{spec_text}"
        )
        overview = adapter.generate_text(system=prompt, user=overview_user, metadata=metadata)

        manifest_user = (
            "根据下方的产品规格说明书和已生成的概要设计文档，输出 design_manifest.json。\n"
            "所有字段必须用来自规格书的具体值填充，不得留空列表（除非规格书确实无该内容）。\n"
            "business_rules 和 acceptance_criteria 必须完整，每条规则/标准独立成条。\n"
            "frontend_requirements 和 pages 必须列出设计文档中定义的所有前端页面及控件。\n\n"
            f"# 产品规格说明书\n{spec_text}\n\n"
            f"# 已生成的概要设计文档\n{overview}"
        )
        manifest = adapter.generate_json(system=prompt, user=manifest_user, schema=DesignManifest, metadata=metadata)

        output_dir = self.batch_artifact_dir(batch_id, "概要设计")
        overview_ref = self.store.write_text(output_dir / "overview_design.md", overview)
        manifest_ref = self.store.write_json(output_dir / "design_manifest.json", manifest.model_dump(mode="json"))
        return [overview_ref, manifest_ref]
