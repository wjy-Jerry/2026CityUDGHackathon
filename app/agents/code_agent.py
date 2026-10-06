from __future__ import annotations

import shutil
import json
from pathlib import Path

from pydantic import BaseModel, Field

from app.agents.base import BaseAgent





class GeneratedCodeFile(BaseModel):
    path: str = Field(
        pattern=r"^(src/.+\.py|frontend/.+\.(html|css|js))$",
        description="Project-relative backend Python path under src/ or static frontend path under frontend/",
    )
    content: str = Field(min_length=1, description="Complete file contents")


class CodeGenerationResult(BaseModel):
    files: list[GeneratedCodeFile] = Field(min_length=1)
    manifest: dict[str, object] = Field(default_factory=dict)


class CodeAgent(BaseAgent):
    agent_name = "CodeAgent"
    prompt_file = "code_agent.md"

    def run(self, input_context: dict[str, str]) -> list[object]:
        batch_id = input_context["batch_id"]
        spec_path = input_context["spec_path"]
        result = self._generate_code(batch_id=batch_id, spec_path=spec_path)

        out_root = self.store.output_batch_dir(batch_id)
        src_dir = out_root / "src"
        if src_dir.exists():
            shutil.rmtree(src_dir)
        src_dir.mkdir(parents=True, exist_ok=True)
        frontend_dir = out_root / "frontend"
        if frontend_dir.exists():
            shutil.rmtree(frontend_dir)
        written_refs = []
        for generated_file in result.files:
            target = self._safe_generated_path(generated_file.path, base_dir=out_root)
            written_refs.append(self.store.write_text(target, generated_file.content))

        code_manifest = self._code_manifest(result)
        output_dir = self.batch_artifact_dir(batch_id, "代码生成")
        manifest_ref = self.store.write_json(output_dir / "code_manifest.json", code_manifest)

        snapshot_dir = output_dir / "src_snapshot"
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
        shutil.copytree(src_dir, snapshot_dir)
        snapshot_refs = [
            self.store.artifact_for(path, kind="src_snapshot")
            for path in sorted(snapshot_dir.rglob("*.py"))
            if path.is_file()
        ]
        frontend_snapshot_refs = []
        if frontend_dir.exists():
            frontend_snapshot_dir = output_dir / "frontend_snapshot"
            if frontend_snapshot_dir.exists():
                shutil.rmtree(frontend_snapshot_dir)
            shutil.copytree(frontend_dir, frontend_snapshot_dir)
            frontend_snapshot_refs = [
                self.store.artifact_for(path, kind="frontend_snapshot")
                for path in sorted(frontend_snapshot_dir.rglob("*"))
                if path.is_file()
            ]
        self.store.write_text(out_root / "pytest.ini", "[pytest]\npythonpath = .\ntestpaths = tests/generated\n")
        self.store.write_text(out_root / "requirements.txt", "fastapi>=0.111.0\nuvicorn>=0.30.0\npydantic>=2.7.0\nhttpx>=0.27.0\npytest>=8.2.0\npytest-cov>=5.0.0\n")
        self.store.write_json(out_root / "code_manifest.json", code_manifest)
        (out_root / "README.md").write_text(self._readme(batch_id, code_manifest), encoding="utf-8")

        return [manifest_ref, *written_refs, *snapshot_refs, *frontend_snapshot_refs]

    def _generate_code(self, *, batch_id: str, spec_path: str) -> CodeGenerationResult:
        from app.demo_fixtures import SAMPLE_FIXTURE_ID
        if self.store.load_state(batch_id).sample_fixture == SAMPLE_FIXTURE_ID:
            from app.demo_fixtures.vehicle_reservations import code_result
            design = json.loads(self._optional_batch_text(batch_id, "概要设计", "design_manifest.json"))
            if design.get("generation_profile") != f"sample-fixture:{SAMPLE_FIXTURE_ID}":
                raise ValueError("Design manifest does not match selected sample fixture")
            return code_result(design["system_name"])
        prompt = self.load_prompt()
        spec_text = self.read_text(spec_path)
        overview = self._optional_batch_text(batch_id, "概要设计", "overview_design.md")
        manifest = self._optional_batch_text(batch_id, "概要设计", "design_manifest.json")
        frontend_required = bool(json.loads(manifest).get("frontend_requirements") or json.loads(manifest).get("pages")) or any(
            kw in (overview + manifest)
            for kw in ("Web", "B/S", "浏览器", "前端", "页面", "表单", "按钮", "后台")
        )
        frontend_instruction = (
            "IMPORTANT: The design mandates a frontend. You MUST generate frontend/index.html "
            "(and frontend/styles.css, frontend/app.js). The HTML must be a complete, usable UI — "
            "not a placeholder — implementing every workflow described in the design overview.\n"
            if frontend_required
            else "Generate a frontend only if the design overview or design manifest explicitly documents frontend pages.\n"
        )
        user = (
            "根据下方的概要设计文档和设计清单，生成完整、可运行的应用系统代码。\n"
            "概要设计文档是你的主要权威输入，产品规格说明书作为补充背景参考。\n"
            "返回 JSON，不含 Markdown、不含 prose。\n"
            "每个后端文件路径必须在 src/ 下；若需要前端，文件路径在 frontend/ 下。\n"
            f"{frontend_instruction}"
            "每个 files[].content 必须是完整的可运行源代码或静态资源，不得为空或片段。\n"
            "必须包含的后端文件：src/__init__.py、src/api.py。推荐拆分：src/models.py、src/services.py、src/storage.py 或 src/repository.py。\n"
            "若生成前端，frontend/index.html 为必须；frontend/styles.css 和 frontend/app.js 强烈推荐。\n"
            "files[] 中禁止包含：CSV 数据文件、JSON 数据文件、Markdown 文档、配置文件、二进制文件。种子数据用 Python 代码在启动时初始化。\n"
            "禁止调用外部 API、shell 命令、网络服务，禁止从环境变量读取业务参数。\n"
            "代码遵循 PEP 8 命名规范，包含必要的错误处理，结构清晰，每个文件职责单一。\n\n"
            "manifest 字段必须填写以下所有 key（TestAgent 将以此作为结构化接口）：\n"
            "  system_name: 与规格书一致的系统名称\n"
            "  api_routes: [{path, method, summary, request_fields:[{name,type}], response_fields:[{name,type}], error_cases:[str]}]\n"
            "  data_models: [{name, fields:[{name,type,description}]}]\n"
            "  business_rules: 完整的业务规则描述列表（每条为完整中文句子）\n"
            "  csv_tables: 所有 CSV 存储文件名列表\n"
            "  frontend_pages: [{path, name, purpose, controls:[str]}]，每个页面一条\n"
            "  storage_contract: {module, service_attribute, factory, argument}; factory accepts data_dir for isolated tests\n"
            "  run_instructions: 本地启动所需的完整命令和 URL 列表\n\n"
            f"# 产品规格说明书（背景参考）\n{spec_text}\n\n"
            f"# 概要设计文档（主要权威输入）\n{overview}\n\n"
            f"# 设计清单 design_manifest.json\n{manifest}\n"
        )
        repair = self.store.batch_dir(batch_id) / "repair_report.json"
        if repair.is_file():
            user += "\n# Structured repair feedback\n" + repair.read_text(encoding="utf-8")
        metadata = {"batch_id": batch_id, "node_id": "code"}
        try:
            return self.llm.generate_json(system=prompt, user=user, schema=CodeGenerationResult, metadata=metadata)
        except Exception:
            if self._strict_mode():
                raise
            design = json.loads(manifest)
            from app.agents.fallback import generic_code
            return CodeGenerationResult.model_validate(generic_code(design))

    def _strict_mode(self) -> bool:
        adapter_settings = getattr(self.llm, "settings", None)
        return bool(adapter_settings is not None and getattr(adapter_settings, "llm_strict", False))

    def _optional_batch_text(self, batch_id: str, dirname: str, filename: str) -> str:
        path = self.store.batch_dir(batch_id) / dirname / filename
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def _safe_generated_path(self, generated_path: str, base_dir: Path | None = None) -> Path:
        if "\\" in generated_path:
            raise ValueError("Generated paths must use POSIX separators")
        candidate = Path(generated_path)
        if candidate.is_absolute():
            raise ValueError(f"Generated path must be relative: {generated_path}")
        if any(part in {"..", "", ".git"} for part in candidate.parts):
            raise ValueError(f"Unsafe generated path: {generated_path}")
        if not candidate.parts or candidate.parts[0] not in {"src", "frontend"}:
            raise ValueError(f"Generated path must be under src/ or frontend/: {generated_path}")
        root = base_dir or self.store.root_dir
        target = (root / candidate).resolve()
        if not target.is_relative_to(root.resolve()):
            raise ValueError("Generated path escapes application root")
        allowed_root = (root / candidate.parts[0]).resolve()
        if target != allowed_root and allowed_root not in target.parents:
            raise ValueError(f"Generated path escapes {candidate.parts[0]}/: {generated_path}")
        if candidate.parts[0] == "src" and target.suffix != ".py":
            raise ValueError(f"Generated backend file must be a Python file: {generated_path}")
        if candidate.parts[0] == "frontend" and target.suffix not in {".html", ".css", ".js"}:
            raise ValueError(f"Generated frontend file must be .html, .css, or .js: {generated_path}")
        return target

    def _readme(self, batch_id: str, manifest: dict) -> str:
        name = manifest.get("system_name", "Generated Application")
        has_frontend = bool(manifest.get("frontend_root"))
        run_cmds = manifest.get("run_instructions") or ["uvicorn src.api:app --reload --reload-dir src"]
        backend_cmd = next((c for c in run_cmds if "uvicorn" in c or "python" in c), run_cmds[0])
        lines = [
            f"# {name}",
            "",
            f"> 由 AI Agent 开发流水线自动生成。批次 ID: `{batch_id}`",
            f"> 生成方式：{manifest.get('strategy', 'unknown')}" + (f" ({manifest['sample_fixture']} 确定性示例；不代表任意规格自动实现)" if manifest.get('sample_fixture') else ""),
            "",
            "## 快速启动",
            "",
            "```bash",
            "pip install -r requirements.txt",
            f"{backend_cmd}",
            "```",
            "",
            "后端文档：访问启动命令对应的端口下的 /docs。",
            "",
        ]
        if has_frontend:
            lines += ["## 前端", "", "按 `code_manifest.json` 的运行指令访问前端。离线模板由后端同源提供，默认 http://127.0.0.1:8001 。", ""]
        lines += [
            "## 运行测试",
            "",
            "```bash",
            "pytest tests/",
            "```",
            "",
            "## 目录结构",
            "",
            "```",
            "src/         后端 Python 源代码",
        ]
        if has_frontend:
            lines.append("frontend/    前端 HTML/CSS/JS")
        lines += ["tests/       pytest 测试套件", "```", ""]
        return "\n".join(lines)

    def _code_manifest(self, result: CodeGenerationResult) -> dict[str, object]:
        manifest: dict[str, object] = {"strategy": "llm-structured-generation"}
        manifest.update(result.manifest)
        manifest["source_root"] = "src"
        manifest["modules"] = sorted(file.path for file in result.files)
        manifest.setdefault("entrypoint", "uvicorn src.api:app --reload")
        frontend_modules = sorted(file.path for file in result.files if file.path.startswith("frontend/"))
        if frontend_modules:
            manifest.setdefault("frontend_root", "frontend")
            manifest.setdefault("frontend_files", frontend_modules)
            manifest.setdefault(
                "run_instructions",
                ["uvicorn src.api:app --reload", "open frontend/index.html in a browser"],
            )
        return manifest
