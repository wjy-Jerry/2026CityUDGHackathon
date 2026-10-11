"""Official two-record verification; deliberately separate from agents."""
BUSINESS_DEMO = r'''
from datetime import date, timedelta
from pathlib import Path
from fastapi.testclient import TestClient
from src.api import app

with TestClient(app) as client:
    day = (date.today() + timedelta(days=1)).isoformat()
    payload = {'name':'Alice','employee_id':'E001','mobile':'13800000000','campus':'Dongguan','reservation_date':day,'plate_no':'鲁G12345'}
    assert client.get('/').status_code == 200
    client.put('/campuses/Dongguan', json={'weekday_quota':2,'rest_day_quota':2,'enabled':True,'instruction':'Demo'})
    a = client.post('/reservations',json=payload).json()
    b = client.post('/reservations',json={**payload,'plate_no':'鲁G12345E','employee_id':'E002'}).json()
    assert a['success'] and b['success']
    assert client.get('/availability',params={'campus':'Dongguan','reservation_date':day}).json()['remaining'] == 0
    assert not client.post('/reservations',json={**payload,'plate_no':'鲁G12346'}).json()['success']
    assert client.post('/reservations/'+a['reservation_id']+'/pay').json()['success']
    assert client.post('/reservations/'+b['reservation_id']+'/cancel').json()['success']
    assert client.get('/availability',params={'campus':'Dongguan','reservation_date':day}).json()['remaining'] == 1
    import csv
    payments = list(csv.DictReader(Path('data/payment_records.csv').open()))
    archive = list(csv.DictReader(Path('data/ketuo_reservation_archive.csv').open()))
    assert len(payments) == 1 and payments[0]['campus'] == 'Dongguan'
    assert archive[1]['status'] == 'cancelled'
print('Two reservations, quota, payment and cancellation verified')
'''
