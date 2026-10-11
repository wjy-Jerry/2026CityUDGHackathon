import json

import pytest

from app.adapters.llm import MockLLMAdapter
from app.agents.code_agent import CodeGenerationResult
from app.agents.fallback import generic_code, mock_design
from app.config import Settings


class FrontendRetryLLM:
    available = False

    def __init__(self, failure_kind):
        self.failure_kind = failure_kind
        self.delegate = MockLLMAdapter()
        self.settings = Settings(llm_strict=True)
        self.code_calls = 0
        self.retry_feedback = None

    def generate_text(self, **kwargs):
        return self.delegate.generate_text(**kwargs)

    def generate_json(self, **kwargs):
        schema = kwargs["schema"]
        if schema is not CodeGenerationResult:
            return self.delegate.generate_json(**kwargs)

        self.code_calls += 1
        result = generic_code(mock_design("# Task Tracker\nWeb interface for managing records"))
        result["manifest"]["strategy"] = "llm-structured-generation"
        if self.code_calls == 1:
            if self.failure_kind == "missing_file":
                result["files"] = [file for file in result["files"] if file["path"] != "frontend/index.html"]
            else:
                html = next(file for file in result["files"] if file["path"] == "frontend/index.html")
                html["content"] = "<html><body><h1>No form or button</h1></body></html>"
        else:
            prompt = kwargs["user"]
            marker = "# Automatic retry feedback (JSON)\n"
            assert marker in prompt
            feedback_json = prompt.split(marker, 1)[1].strip()
            assert feedback_json.startswith("{"), repr(feedback_json[:240])
            self.retry_feedback = json.loads(feedback_json)
        return schema.model_validate(result)


@pytest.mark.parametrize(
    ("failure_kind", "expected_category", "expected_summary"),
    [
        ("missing_file", "artifact_validation", "frontend/index.html"),
        ("missing_controls", "quality_validation", "requires form and button elements"),
    ],
)
def test_code_retry_receives_structured_frontend_feedback(
    engine, failure_kind, expected_category, expected_summary
):
    llm = FrontendRetryLLM(failure_kind)
    engine.llm = llm
    engine.max_retries = 1
    batch = engine.create_batch_from_bytes(
        filename="tasks.md",
        content=b"# Task Tracker\nA web interface is required.",
        mode="manual",
    )

    state = engine.run_batch(batch.batch_id)
    assert state.status == "paused" and state.current_node == "code"
    state = engine.advance_node(batch.batch_id)

    assert state.status == "paused" and state.current_node == "test", state.nodes["code"].error_message
    assert state.nodes["code"].status == "succeeded", state.nodes["code"].error_message
    assert llm.code_calls == 2
    assert llm.retry_feedback["category"] == expected_category
    assert expected_summary in llm.retry_feedback["summary"]
    assert (engine.store.output_batch_dir(batch.batch_id) / "frontend/index.html").is_file()
