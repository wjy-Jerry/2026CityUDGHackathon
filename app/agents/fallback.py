"""Transparent offline templates; these do not replace real requirement reasoning."""
from __future__ import annotations

import json
import re
from html import escape


def mock_design(spec: str, sample_fixture: str | None = None) -> dict:
    title = next((line.lstrip('# ').strip() for line in spec.splitlines() if line.startswith('#')), 'Generated Record Application')
    from app.demo_fixtures import SAMPLE_FIXTURE_ID
    if sample_fixture not in (None, SAMPLE_FIXTURE_ID):
        raise ValueError(f"Unknown sample fixture: {sample_fixture}")
    vehicle = sample_fixture == SAMPLE_FIXTURE_ID
    requirements = [line.strip('- *| ') for line in spec.splitlines() if line.strip()][:40]
    common = {'system_name': title, 'generation_profile': 'sample-fixture:vehicle_reservations' if vehicle else 'generic-records',
              'requirements_summary': requirements,
              'frontend_requirements': ['HTML form, records table, action buttons'] if re.search(r'web|前端|浏览器|B/S|BS架构|页面', spec, re.I) else [],
              'pages': ['Application'] if re.search(r'web|前端|浏览器|B/S|BS架构|页面', spec, re.I) else []}
    if vehicle:
        from app.demo_fixtures.vehicle_reservations import design_details
        common.update(design_details())
    else:
        common.update(modules=['record management', 'CSV persistence'], entities=['Record'],
                      business_rules=['Record title must be non-empty', 'Unknown records return 404'],
                      api_endpoints=['GET /health', 'GET /records', 'POST /records', 'DELETE /records/{record_id}'],
                      csv_tables=['records.csv'], validation_rules=['non_empty_title'],
                      acceptance_criteria=['Create and list a record', 'Delete a record', 'Reject invalid input'])
    return common


def mock_overview(spec: str, sample_fixture: str | None = None) -> str:
    m = mock_design(spec, sample_fixture)
    vehicle = sample_fixture is not None
    sections = [
        ('引言', f"系统：{m['system_name']}\n来源需求摘要：\n" + '\n'.join(m['requirements_summary'])),
        ('总体设计', 'FastAPI + CSV + optional HTML. ' + ('Explicit deterministic vehicle reservation sample fixture.' if vehicle else 'Generic offline record CRUD pipeline; domain-specific rules require real LLM generation.') + ' Authentication is not implemented in offline templates.'),
        ('接口设计', '\n'.join(m['api_endpoints'])),
        ('数据结构设计', '\n'.join(m['entities'] + m['csv_tables'])),
        ('业务规则设计', '\n'.join(m['business_rules'])),
        ('出错处理设计', 'Input validation: 422. ' + ('Vehicle business errors: success=false with a message. ' if vehicle else 'Unknown records: 404. ') + 'Storage exceptions surface as server errors.'),
        ('集成模拟设计', 'Local CSV only. No hardware, payment gateway or external services.'),
        ('验收标准', '\n'.join(m['acceptance_criteria'])),
    ]
    return '# ' + m['system_name'] + '\n\n' + '\n\n'.join(f'## 第{i}章 {title}\n{body}' for i, (title, body) in enumerate(sections, 1))


GENERIC_SERVICE = '''
import csv
import os
import uuid
import threading
from functools import wraps
from pathlib import Path

def synchronized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped

class RecordService:
    def __init__(self, data_dir):
        self._lock = threading.RLock()
        self.path = Path(data_dir) / 'records.csv'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write([])

    @synchronized
    def list(self):
        with self.path.open(encoding='utf-8', newline='') as handle:
            return list(csv.DictReader(handle))

    def _write(self, rows):
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=['id', 'title', 'description'])
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, self.path)

    @synchronized
    def create(self, title, description):
        row = {'id': uuid.uuid4().hex, 'title': title, 'description': description}
        self._write(self.list() + [row])
        return row

    @synchronized
    def delete(self, record_id):
        rows = self.list()
        remaining = [r for r in rows if r['id'] != record_id]
        if len(rows) == len(remaining):
            return False
        self._write(remaining)
        return True
'''
GENERIC_API = '''
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from src.services import RecordService

app = FastAPI(title=SYSTEM_NAME)
service = RecordService(Path(__file__).resolve().parents[1] / 'data')

class RecordCreate(BaseModel):
    title: str = Field(min_length=1)
    description: str = ''

@app.get('/health')
def health():
    return {'status': 'ok'}

@app.get('/records')
def records():
    return service.list()

@app.post('/records')
def create(request: RecordCreate):
    return service.create(request.title, request.description)

@app.delete('/records/{record_id}')
def delete(record_id: str):
    if not service.delete(record_id):
        raise HTTPException(404, 'Record not found')
    return {'success': True}

frontend = Path(__file__).resolve().parents[1] / 'frontend'
if frontend.is_dir():
    app.mount('/', StaticFiles(directory=frontend, html=True), name='frontend')
'''
GENERIC_HTML = '''<!doctype html><html><meta charset="utf-8"><title>TITLE</title>
<h1>TITLE</h1><p>Offline record template. Domain-specific rules require LLM mode.</p>
<form><label>Title<input name="title" required></label><label>Description<input name="description"></label><button>Add record</button></form>
<p id="message"></p><table id="records"></table><script src="/app.js"></script></html>'''
GENERIC_JS = '''
const form=document.querySelector('form'), table=document.querySelector('table'), message=document.querySelector('#message');
async function refresh(){const rows=await (await fetch('/records')).json();table.replaceChildren();for(const row of rows){const tr=document.createElement('tr');for(const field of ['title','description']){const td=document.createElement('td');td.textContent=row[field];tr.append(td);}const td=document.createElement('td'), button=document.createElement('button');button.textContent='Delete';button.onclick=async()=>{try{const r=await fetch('/records/'+row.id,{method:'DELETE'});if(!r.ok)throw Error(await r.text());await refresh();}catch(e){message.textContent=e.message;}};td.append(button);tr.append(td);table.append(tr);}}
form.onsubmit=async e=>{e.preventDefault();try{const r=await fetch('/records',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(form)))});if(!r.ok)throw Error(await r.text());message.textContent='Saved';await refresh();}catch(e){message.textContent=e.message;}};refresh().catch(e=>message.textContent=e.message);
'''
GENERIC_TESTS = '''
import pytest
from fastapi.testclient import TestClient
from src import api
from src.services import RecordService

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, 'service', RecordService(tmp_path))
    with TestClient(api.app) as client:
        yield client

def test_records_lifecycle(client):
    assert client.get('/health').json()['status'] == 'ok'
    assert client.get('/records').json() == []
    row = client.post('/records', json={'title':'Task', 'description':'Review'}).json()
    assert client.get('/records').json() == [row]
    assert client.delete('/records/'+row['id']).json()['success']
    assert client.delete('/records/'+row['id']).status_code == 404
    assert client.get('/records').json() == []

def test_invalid_record(client):
    assert client.post('/records', json={'title':''}).status_code == 422

def test_parallel_creation_persists_every_record(client):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(lambda n: api.service.create('Task ' + str(n), 'Concurrent'), range(6)))
    stored = client.get('/records').json()
    assert len(stored) == 6
    assert {r['id'] for r in stored} == {r['id'] for r in rows}

'''


def generic_code(manifest: dict) -> dict:
    name = manifest.get('system_name', 'Record Application')
    files = [{'path':'src/__init__.py','content':'"""Generated record application."""'},
             {'path':'src/services.py','content':GENERIC_SERVICE},
             {'path':'src/api.py','content':GENERIC_API.replace('SYSTEM_NAME', repr(name))}]
    if manifest.get('frontend_requirements'):
        files += [{'path':'frontend/index.html','content':GENERIC_HTML.replace('TITLE', escape(name))},
                  {'path':'frontend/app.js','content':GENERIC_JS}]
    return {'files':files, 'manifest':{'system_name':name,'strategy':'generic-template-fallback',
                                      'api_routes':[{'path':r.split()[1], 'method':r.split()[0]} for r in manifest['api_endpoints']],
                                      'storage_contract':{'module':'src.api','service_attribute':'service','factory':'src.services.RecordService','argument':'data_dir'},
                                      'limitations':['Offline generic CRUD template; domain-specific requirements need LLM mode'],
                                      'run_instructions':['python -m uvicorn src.api:app --port 8001', 'http://127.0.0.1:8001'],
                                      'csv_tables':['records.csv']}}
