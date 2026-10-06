import pytest
from app.adapters.llm import MockLLMAdapter
from app.agents.fallback import mock_design, generic_code, GENERIC_TESTS


class RepairLLM(MockLLMAdapter):
    available = True
    def __init__(self, remains_broken=False):
        self.code_calls = 0
        self.test_calls = 0
        self.remains_broken = remains_broken
        self.repair_prompt_seen = False

    def generate_json(self, **kwargs):
        schema = kwargs['schema']
        if schema.__name__ == 'CodeGenerationResult':
            self.code_calls += 1
            result = generic_code(mock_design('# Task Tracker\nWeb records'))
            result['manifest']['strategy'] = 'llm-structured-generation'
            if self.code_calls > 1:
                self.repair_prompt_seen = 'Structured repair feedback' in kwargs['user']
                assert 'test_records_lifecycle' in kwargs['user']
                assert 'def test_records_lifecycle' not in kwargs['user']
            if self.code_calls == 1 or self.remains_broken:
                result['files'][2]['content'] = result['files'][2]['content'].replace("{'status': 'ok'}", "{'status': 'defect'}")
            return schema.model_validate(result)
        if schema.__name__ == 'TestGenerationResult':
            self.test_calls += 1
            return schema.model_validate({'files':[{'path':'tests/generated/test_records.py','content':GENERIC_TESTS}],
                                          'test_plan_markdown':'# Test plan\nRecord lifecycle and validation'})
        return super().generate_json(**kwargs)


@pytest.mark.parametrize('remains_broken', [False, True])
def test_one_repair_attempt_keeps_original_tests_and_exposes_result(engine, remains_broken):
    llm = RepairLLM(remains_broken)
    engine.llm = llm
    state = engine.create_batch_from_bytes(filename='tasks.md', content=b'# Task Tracker\nWeb records')
    state = engine.run_batch(state.batch_id)
    assert state.repair_attempts == 1
    assert state.status == ('failed' if remains_broken else 'succeeded')
    assert llm.code_calls == 2 and llm.test_calls == 1
    assert llm.repair_prompt_seen
    assert (engine.store.batch_dir(state.batch_id) / 'repair_report.json').is_file()
    assert (engine.store.batch_dir(state.batch_id) / '单元测试/repair_before/pytest_output.txt').is_file()
    assert state.nodes['test'].quality_check_result['passed'] is not remains_broken
    if remains_broken:
        state = engine.retry_node(state.batch_id, 'test')
        assert state.status == 'failed' and state.repair_attempts == 1
        assert llm.code_calls == 2
