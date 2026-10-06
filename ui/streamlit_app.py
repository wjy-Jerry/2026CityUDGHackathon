from __future__ import annotations

from pathlib import Path

import requests
import streamlit as st

st.set_page_config(page_title='AI Agent 开发流水线', page_icon='⚙️', layout='wide')
API_BASE = st.sidebar.text_input('Backend URL', 'http://127.0.0.1:8000').rstrip('/')
TIMEOUT = st.sidebar.number_input('Request timeout (seconds)', 20, 300, 120)
ICONS = {'queued':'⏳', 'running':'⚙️', 'succeeded':'✅', 'failed':'❌', 'paused':'⏸️'}
LABELS = {'design':'Design Agent', 'code':'Code Agent', 'test':'Test Agent'}


def api(method, path, **kwargs):
    try:
        response = requests.request(method, API_BASE + path, timeout=TIMEOUT, **kwargs)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        detail = ''
        if exc.response is not None:
            try:
                detail = exc.response.json().get('detail', '')
            except ValueError:
                detail = exc.response.text[:300]
        st.error(f'Backend: {detail or exc}')
        return None


st.title('AI Agent 全链路自动化开发')
st.caption('产品规格 → 概要设计 → 可运行应用 → 自动测试 → 质量验证')
st.session_state.setdefault('batch_id', '')

with st.form('create'):
    uploaded = st.file_uploader('上传产品规格说明书 (.md)', type=['md'])
    use_official = st.checkbox('使用官方员工临时车辆预约规格', value=True)
    mode = st.selectbox('执行模式', ['auto', 'manual'], help='manual 在设计完成后暂停，逐步审批代码和测试节点。')
    submitted = st.form_submit_button('创建并启动流水线', type='primary')
    if submitted:
        if uploaded:
            name, content = uploaded.name, uploaded.getvalue()
        elif use_official:
            spec = next((Path(__file__).resolve().parents[1] / 'problem').glob('试题成果验证*.md'))
            name, content = spec.name, spec.read_bytes()
        else:
            name, content = '', b''
        if not content:
            st.warning('请上传规格或选择官方示例。')
        else:
            response = api('POST', '/api/v1/batches', files={'file':(name, content, 'text/markdown')}, data={'mode':mode})
            if response is not None:
                st.session_state.batch_id = response.json()['batch_id']
                if api('POST', f"/api/v1/batches/{st.session_state.batch_id}/run") is not None:
                    st.success('流水线已启动。状态每 2 秒更新。')

st.text_input('当前批次 ID', key='batch_id')


@st.fragment(run_every='2s')
def dashboard():
    bid = st.session_state.batch_id
    if not bid:
        st.info('选择规格并启动流水线，或填入已有批次 ID。')
        return
    base = '/api/v1/batches/' + bid
    response = api('GET', base)
    if response is None:
        return
    state = response.json()
    status = state['status']
    st.subheader(f"{ICONS.get(status, '')} 流水线：{status}")
    st.caption(f"模式：{state['mode']} · 当前节点：{state.get('current_node') or '—'} · 修复次数：{state.get('repair_attempts', 0)}/1")
    columns = st.columns(4)
    for col, nid in zip(columns[:3], LABELS):
        node = state['nodes'][nid]
        with col:
            st.metric(LABELS[nid], f"{ICONS.get(node['status'], '')} {node['status']}")
            duration = node.get('duration_ms')
            st.caption(f"耗时：{duration / 1000:.2f}s" if duration is not None else '耗时：—')
            st.caption(f"重试：{node['retries']} · 产物：{len(node['outputs'])}")
            if node.get('error_message'):
                with st.expander('失败详情'):
                    st.code(node['error_message'], language=None)
    quality = state['nodes']['test'].get('quality_check_result', {})
    with columns[3]:
        passed = status == 'succeeded' and quality.get('passed', False)
        st.metric('Validation', '✅ passed' if passed else '❌ failed' if status == 'failed' else '⏳ pending')
    code_quality = state['nodes']['code'].get('quality_check_result', {})
    if 'fallback' in code_quality.get('generation_strategy', ''):
        st.info('本批次使用确定性离线模板。领域需求的完整实现应使用真实 LLM 模式；离线模板限制见下方。')
        for limitation in code_quality.get('limitations', []):
            st.caption(limitation)
    completed = sum(n['status'] == 'succeeded' for n in state['nodes'].values())
    st.progress(completed / 3, text=f'{completed}/3 Agent 节点完成')
    if quality:
        counts = quality.get('test_counts', {})
        a, b, c, d = st.columns(4)
        a.metric('通过测试', counts.get('passed', 0))
        b.metric('失败 / 错误', f"{counts.get('failed', 0)} / {counts.get('errors', 0)}")
        coverage = quality.get('coverage_pct')
        c.metric('覆盖率', f'{coverage:.2f}%' if coverage is not None else '无法测量')
        d.metric('覆盖率要求 ≥80%', '✅' if quality.get('checks', {}).get('coverage_threshold_passed') else '❌')
        if not quality.get('checks', {}).get('all_tests_passed', False):
            st.error('功能测试没有全部通过。覆盖率达标不能替代功能正确性。')
    if state['mode'] == 'manual' and status == 'paused':
        if st.button(f"审批并运行 {state['current_node']}"):
            api('POST', base + '/advance')
            st.rerun(scope='fragment')
    if status == 'queued' and st.button('启动批次'):
        api('POST', base + '/run')
        st.rerun(scope='fragment')
    if status in {'failed', 'succeeded'}:
        left, right = st.columns(2)
        with left:
            if st.button('重新执行端到端验证'):
                with st.spinner('启动应用并运行生成测试…'):
                    result = api('POST', '/api/v1/validate', json={'batch_id':bid})
                if result is not None:
                    st.session_state['validation_' + bid] = result.json()['validation']
        with right:
            retry = st.selectbox('从此节点重试', list(LABELS))
            if st.button('重试并重新生成下游'):
                api('POST', base + '/retry/' + retry)
                st.rerun(scope='fragment')
    validation = st.session_state.get('validation_' + bid)
    if validation:
        if validation['passed']:
            st.success('独立端到端验证通过：设计、应用启动、生成测试和覆盖率。')
        else:
            st.error('独立端到端验证失败，请展开质量诊断。')
        with st.expander('质量诊断'):
            st.json(validation)
    if state['nodes']['code']['status'] == 'succeeded':
        with st.expander('下载应用与运行方式', expanded=status == 'succeeded'):
            st.code(f'cd output/{bid}\npython -m uvicorn src.api:app --port 8001', language='bash')
            st.caption('生成应用：http://127.0.0.1:8001 · 与平台端口 8000 分开。Mock 模板的功能边界见 README 和 code_manifest。')
            if st.button('准备应用 ZIP'):
                package = api('GET', base + '/package')
                if package is not None:
                    st.session_state['package_' + bid] = package.content
            if 'package_' + bid in st.session_state:
                st.download_button('下载应用 ZIP', st.session_state['package_' + bid], bid + '.zip', 'application/zip')
    with st.expander('高级：日志、Artifact 与原始状态'):
        st.json(state)
        logs = api('GET', base + '/logs')
        if logs is not None:
            st.dataframe(logs.json())
        artifacts = api('GET', base + '/artifacts')
        if artifacts is not None:
            st.dataframe(artifacts.json())
            choices = [a['path'] for a in artifacts.json()]
            if choices:
                path = st.selectbox('Artifact', choices)
                if st.button('读取 Artifact'):
                    artifact = api('GET', base + '/download', params={'path':path})
                    if artifact is not None:
                        st.download_button('下载所选 Artifact', artifact.content, Path(path).name)
                        st.code(artifact.text[:15000], language=None)


dashboard()
