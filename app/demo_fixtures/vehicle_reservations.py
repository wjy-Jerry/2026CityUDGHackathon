"""Deterministic official vehicle reservation demo fixture; not generic generation."""
from __future__ import annotations

from html import escape
from textwrap import dedent

from app.demo_fixtures import SAMPLE_FIXTURE_ID

BUSINESS_FILES: dict[str, str] = {
    "__init__.py": '"""Generated employee vehicle reservation business system."""\n',
    "models.py": r'''
from __future__ import annotations

from datetime import date
from pydantic import BaseModel, Field


class ReservationCreate(BaseModel):
    name: str = Field(min_length=1)
    employee_id: str = Field(min_length=1)
    mobile: str = Field(min_length=5)
    campus: str
    reservation_date: date
    plate_no: str


class ReservationCancel(BaseModel):
    reservation_id: str


class ReservationResponse(BaseModel):
    success: bool
    message: str
    reservation_id: str | None = None
''',
    "csv_repository.py": r'''
from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Callable


DEFAULT_SCHEMAS: dict[str, list[str]] = {
    "campus_configs.csv": ["campus", "weekday_quota", "rest_day_quota", "enabled", "instruction"],
    "reservations.csv": [
        "reservation_id", "name", "employee_id", "mobile", "campus", "reservation_date", "plate_no", "status"
    ],
    "ketuo_reservation_archive.csv": ["reservation_id", "plate_no", "campus", "reserve_date", "status", "remark"],
    "payment_records.csv": ["payment_id", "reservation_id", "plate_no", "campus", "amount", "status", "created_at"],
    "internal_vehicle_archive.csv": ["plate_no", "owner", "remark"],
}


class CSVRepository:
    def __init__(self, base_dir: Path | str = "data", schemas: dict[str, list[str]] | None = None) -> None:
        self.base_dir = Path(base_dir)
        self.schemas = schemas or DEFAULT_SCHEMAS
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def initialize(self) -> None:
        for table, headers in self.schemas.items():
            path = self.path_for(table)
            if not path.exists():
                self._atomic_write(path, [], headers)

    def path_for(self, table: str) -> Path:
        if table not in self.schemas:
            raise ValueError(f"Unknown CSV table: {table}")
        return self.base_dir / table

    def read_all(self, table: str) -> list[dict[str, str]]:
        path = self.path_for(table)
        if not path.exists():
            self._atomic_write(path, [], self.schemas[table])
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def query(self, table: str, predicate: Callable[[dict[str, str]], bool]) -> list[dict[str, str]]:
        return [row for row in self.read_all(table) if predicate(row)]

    def append(self, table: str, row: dict[str, object]) -> dict[str, str]:
        rows = self.read_all(table)
        normalized = self._normalize(table, row)
        rows.append(normalized)
        self._atomic_write(self.path_for(table), rows, self.schemas[table])
        return normalized

    def update(
        self,
        table: str,
        predicate: Callable[[dict[str, str]], bool],
        changes: dict[str, object],
    ) -> int:
        rows = self.read_all(table)
        count = 0
        for row in rows:
            if predicate(row):
                for key, value in changes.items():
                    if key not in self.schemas[table]:
                        raise ValueError(f"Unknown field {key} for table {table}")
                    row[key] = str(value)
                count += 1
        if count:
            self._atomic_write(self.path_for(table), rows, self.schemas[table])
        return count

    def _normalize(self, table: str, row: dict[str, object]) -> dict[str, str]:
        headers = self.schemas[table]
        return {header: str(row.get(header, "")) for header in headers}

    def _atomic_write(self, path: Path, rows: list[dict[str, str]], headers: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            for row in rows:
                writer.writerow({header: row.get(header, "") for header in headers})
        os.replace(temp_path, path)
''',
    "services.py": r'''
from __future__ import annotations

import re
import uuid
import threading
from functools import wraps
from datetime import date, datetime, timedelta
from pathlib import Path

from src.csv_repository import CSVRepository
from src.models import ReservationCreate


CAMPUSES = ["Weifang", "Qingdao", "Rongcheng", "Dongguan"]
DISABLED_MESSAGE = "当前园区暂不开放预约"
PAYMENT_SUCCESS_MESSAGE = "缴费成功，离厂时无需支付"


def synchronized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


class ReservationService:
    def __init__(self, data_dir: Path | str = "data") -> None:
        self._lock = threading.RLock()
        self.repository = CSVRepository(data_dir)
        self.repository.initialize()
        self.ensure_default_campus_configs()

    def ensure_default_campus_configs(self) -> None:
        if self.repository.read_all("campus_configs.csv"):
            return
        for campus in CAMPUSES:
            self.repository.append(
                "campus_configs.csv",
                {
                    "campus": campus,
                    "weekday_quota": 2,
                    "rest_day_quota": 1,
                    "enabled": "true",
                    "instruction": f"{campus} campus temporary reservation",
                },
            )

    def list_campuses(self) -> list[dict[str, str]]:
        return self.repository.read_all("campus_configs.csv")

    def list_reservations(self) -> list[dict[str, str]]:
        return self.repository.read_all("reservations.csv")

    @synchronized
    def create_reservation(self, request: ReservationCreate) -> dict[str, object]:
        config = self._campus_config(request.campus)
        if not config or config.get("enabled", "").lower() != "true":
            return {"success": False, "message": DISABLED_MESSAGE, "reservation_id": None}
        if not self._valid_plate(request.plate_no):
            return {"success": False, "message": "车牌号格式不正确", "reservation_id": None}
        if not self._within_next_seven_days(request.reservation_date):
            return {"success": False, "message": "预约日期必须在未来7天内", "reservation_id": None}
        if self.repository.query("internal_vehicle_archive.csv", lambda row: row["plate_no"].strip().upper() == request.plate_no.strip().upper()):
            return {"success": False, "message": "已有内部车辆档案，禁止重复预约", "reservation_id": None}
        if self._duplicate_plate(request.plate_no, request.reservation_date):
            return {"success": False, "message": "同一车牌同一天只能预约一个园区", "reservation_id": None}
        if self._active_count(request.campus, request.reservation_date) >= self._quota(config, request.reservation_date):
            return {"success": False, "message": "当日预约名额已满", "reservation_id": None}

        reservation_id = uuid.uuid4().hex[:12]
        row = {
            "reservation_id": reservation_id,
            "name": request.name,
            "employee_id": request.employee_id,
            "mobile": request.mobile,
            "campus": request.campus,
            "reservation_date": request.reservation_date.isoformat(),
            "plate_no": request.plate_no.strip().upper(),
            "status": "success",
        }
        self.repository.append("reservations.csv", row)
        self.repository.append(
            "ketuo_reservation_archive.csv",
            {
                "reservation_id": reservation_id,
                "plate_no": request.plate_no.strip().upper(),
                "campus": request.campus,
                "reserve_date": request.reservation_date.isoformat(),
                "status": "pending",
                "remark": f"{request.name}/{request.employee_id}/{request.mobile}",
            },
        )
        return {"success": True, "message": "预约成功", "reservation_id": reservation_id}

    @synchronized
    def cancel_reservation(self, reservation_id: str) -> dict[str, object]:
        matches = self.repository.query(
            "reservations.csv",
            lambda row: row["reservation_id"] == reservation_id and row["status"] == "success",
        )
        if not matches:
            return {"success": False, "message": "未找到可取消的预约", "reservation_id": reservation_id}
        self.repository.update(
            "reservations.csv",
            lambda row: row["reservation_id"] == reservation_id,
            {"status": "cancelled"},
        )
        self.repository.update(
            "ketuo_reservation_archive.csv",
            lambda row: row["reservation_id"] == reservation_id,
            {"status": "cancelled"},
        )
        return {"success": True, "message": "取消成功", "reservation_id": reservation_id}

    @synchronized
    def advance_payment(self, reservation_id: str) -> dict[str, object]:
        matches = self.repository.query(
            "reservations.csv",
            lambda row: row["reservation_id"] == reservation_id and row["status"] == "success",
        )
        if not matches:
            return {"success": False, "message": "未找到可缴费的预约", "reservation_id": reservation_id}
        reservation = matches[0]
        existing = self.repository.query("payment_records.csv", lambda row: row["reservation_id"] == reservation_id)
        if existing:
            return {"success": True, "message": PAYMENT_SUCCESS_MESSAGE, "reservation_id": reservation_id, "payment_id": existing[0]["payment_id"]}
        payment_id = uuid.uuid4().hex[:12]
        self.repository.append(
            "payment_records.csv",
            {
                "payment_id": payment_id,
                "reservation_id": reservation_id,
                "plate_no": reservation["plate_no"],
                "campus": reservation["campus"],
                "amount": "20.00",
                "status": "success",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        return {
            "success": True,
            "message": PAYMENT_SUCCESS_MESSAGE,
            "reservation_id": reservation_id,
            "payment_id": payment_id,
        }

    def availability(self, campus: str, reservation_date: date) -> dict[str, object]:
        config = self._campus_config(campus)
        if not config:
            raise ValueError("Unknown campus")
        quota = self._quota(config, reservation_date) if config["enabled"].lower() == "true" else 0
        return {"campus": campus, "date": reservation_date.isoformat(),
                "remaining": max(0, quota - self._active_count(campus, reservation_date)), "instruction": config["instruction"]}

    @synchronized
    def configure_campus(self, campus: str, weekday_quota: int, rest_day_quota: int, enabled: bool, instruction: str) -> dict[str, object]:
        if not self._campus_config(campus):
            raise ValueError("Unknown campus")
        self.repository.update("campus_configs.csv", lambda row: row["campus"] == campus,
                               {"weekday_quota": weekday_quota, "rest_day_quota": rest_day_quota,
                                "enabled": str(enabled).lower(), "instruction": instruction})
        return {"success": True}

    def set_campus_enabled(self, campus: str, enabled: bool) -> None:
        self.repository.update("campus_configs.csv", lambda row: row["campus"] == campus, {"enabled": str(enabled).lower()})

    def _campus_config(self, campus: str) -> dict[str, str] | None:
        matches = self.repository.query("campus_configs.csv", lambda row: row["campus"] == campus)
        return matches[0] if matches else None

    def _quota(self, config: dict[str, str], reservation_date: date) -> int:
        key = "weekday_quota" if reservation_date.weekday() < 5 else "rest_day_quota"
        return int(config[key])

    def _active_count(self, campus: str, reservation_date: date) -> int:
        day = reservation_date.isoformat()
        return len(
            self.repository.query(
                "reservations.csv",
                lambda row: row["campus"] == campus and row["reservation_date"] == day and row["status"] == "success",
            )
        )

    def _duplicate_plate(self, plate_no: str, reservation_date: date) -> bool:
        day = reservation_date.isoformat()
        plate = plate_no.strip().upper()
        return bool(
            self.repository.query(
                "reservations.csv",
                lambda row: row["plate_no"].upper() == plate and row["reservation_date"] == day and row["status"] == "success",
            )
        )

    def _within_next_seven_days(self, reservation_date: date) -> bool:
        today = date.today()
        return today <= reservation_date <= today + timedelta(days=7)

    def _valid_plate(self, plate_no: str) -> bool:
        value = plate_no.strip().upper()
        if len(value) not in {7, 8}:
            return False
        return bool(re.match(r"^[\u4e00-\u9fa5][A-Z][A-Z0-9]{5,6}$", value))
''',
    "api.py": r'''
from __future__ import annotations

from pathlib import Path

from datetime import date
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware

from src.models import ReservationCreate
from src.services import ReservationService


app = FastAPI(title="Employee Temporary Vehicle Reservation System")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
service = ReservationService(Path(__file__).resolve().parents[1] / "data")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "employee-vehicle-reservation"}


@app.get("/campuses")
def campuses() -> list[dict[str, str]]:
    return service.list_campuses()


@app.get("/reservations")
def reservations() -> list[dict[str, str]]:
    return service.list_reservations()


@app.post("/reservations")
def create_reservation(request: ReservationCreate) -> dict[str, object]:
    return service.create_reservation(request)


@app.post("/reservations/{reservation_id}/cancel")
def cancel_reservation(reservation_id: str) -> dict[str, object]:
    return service.cancel_reservation(reservation_id)


@app.post("/reservations/{reservation_id}/pay")
def advance_payment(reservation_id: str) -> dict[str, object]:
    return service.advance_payment(reservation_id)

class CampusUpdate(BaseModel):
    weekday_quota: int = Field(ge=0)
    rest_day_quota: int = Field(ge=0)
    enabled: bool
    instruction: str = ""

@app.get("/availability")
def availability(campus: str, reservation_date: date):
    try:
        return service.availability(campus, reservation_date)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

@app.put("/campuses/{campus}")
def configure(campus: str, request: CampusUpdate):
    try:
        return service.configure_campus(campus, **request.model_dump())
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

frontend = Path(__file__).resolve().parents[1] / "frontend"
if frontend.is_dir():
    app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")

''',
}


FRONTEND_FILES: dict[str, str] = {
    "index.html": r'''
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>员工临时车辆预约管理系统</title>
  <link rel="stylesheet" href="./styles.css">
</head>
<body>
  <header class="topbar">
    <div>
      <h1>员工临时车辆预约管理系统</h1>
      <p>员工预约、取消、提前缴费与管理员园区配额管理</p>
    </div>
    <span id="healthStatus" class="status">待连接</span>
  </header>

  <main class="layout">
    <section class="panel">
      <h2>员工预约</h2>
      <form id="reservationForm" class="grid-form">
        <label>姓名<input name="name" required value="Alice"></label>
        <label>工号<input name="employee_id" required value="E001"></label>
        <label>手机号<input name="mobile" required value="13800000000"></label>
        <label>园区<select name="campus" id="campusSelect" required></select></label>
        <label>预约日期<input name="reservation_date" id="reservationDate" type="date" required></label>
        <label>车牌号<input name="plate_no" required placeholder="鲁G12345 / 鲁G12345E"></label>
        <button type="submit">提交预约</button>
      </form>
      <p id="reservationMessage" class="message"></p>
      <div class="hint" id="campusInstruction">请选择园区查看临时车预约说明。</div>
    </section>

    <section class="panel">
      <div class="section-heading">
        <h2>我的预约</h2>
        <button id="refreshReservations" type="button">刷新</button>
      </div>
      <table>
        <thead>
          <tr><th>预约号</th><th>园区</th><th>日期</th><th>车牌</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody id="reservationRows"></tbody>
      </table>
    </section>

    <section class="panel">
      <h2>管理员后台</h2>
      <p class="hint">查看园区开关、工作日/休息日配额与说明文字。</p>
      <table>
        <thead>
          <tr><th>园区</th><th>工作日配额</th><th>休息日配额</th><th>预约开关</th><th>说明</th></tr>
        </thead>
        <tbody id="campusRows"></tbody>
      </table>
    </section>
  </main>

  <script src="./app.js"></script>
</body>
</html>
''',
    "styles.css": r'''
* {
  box-sizing: border-box;
}

body {
  margin: 0;
  background: #f7f8fa;
  color: #1f2937;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

.topbar {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  align-items: center;
  padding: 24px 32px;
  background: #ffffff;
  border-bottom: 1px solid #d7dde5;
}

h1, h2, p {
  margin: 0;
}

h1 {
  font-size: 24px;
}

h2 {
  font-size: 18px;
  margin-bottom: 16px;
}

.status, .message, .hint {
  font-size: 14px;
}

.status {
  padding: 6px 10px;
  border: 1px solid #b7c4d4;
  background: #eef3f8;
  border-radius: 6px;
  white-space: nowrap;
}

.layout {
  display: grid;
  gap: 20px;
  padding: 24px 32px;
}

.panel {
  background: #ffffff;
  border: 1px solid #d7dde5;
  border-radius: 8px;
  padding: 20px;
}

.grid-form {
  display: grid;
  grid-template-columns: repeat(3, minmax(160px, 1fr));
  gap: 14px;
  align-items: end;
}

label {
  display: grid;
  gap: 6px;
  font-size: 14px;
}

input, select, button {
  min-height: 38px;
  border: 1px solid #b7c4d4;
  border-radius: 6px;
  padding: 8px 10px;
  font: inherit;
}

button {
  background: #14532d;
  color: #ffffff;
  border-color: #14532d;
  cursor: pointer;
}

.section-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}

th, td {
  padding: 10px;
  border-bottom: 1px solid #e1e6ed;
  text-align: left;
}

.message {
  min-height: 22px;
  margin-top: 12px;
  font-weight: 600;
}

.hint {
  margin-top: 10px;
  color: #526172;
}

@media (max-width: 820px) {
  .topbar {
    align-items: flex-start;
    flex-direction: column;
    padding: 20px;
  }

  .layout {
    padding: 16px;
  }

  .grid-form {
    grid-template-columns: 1fr;
  }
}
''',
    "app.js": r'''
const apiBase = location.protocol === "file:" ? "http://127.0.0.1:8001" : location.origin;
const escapeHtml = value => String(value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;", "'":"&#39;"}[c]));

const healthStatus = document.querySelector("#healthStatus");
const campusSelect = document.querySelector("#campusSelect");
const campusRows = document.querySelector("#campusRows");
const reservationRows = document.querySelector("#reservationRows");
const reservationForm = document.querySelector("#reservationForm");
const reservationMessage = document.querySelector("#reservationMessage");
const campusInstruction = document.querySelector("#campusInstruction");
const reservationDate = document.querySelector("#reservationDate");

function setMessage(text, ok = true) {
  reservationMessage.textContent = text;
  reservationMessage.style.color = ok ? "#14532d" : "#b42318";
}

async function request(path, options = {}) {
  const response = await fetch(`${apiBase}${path}`, {
    headers: {"Content-Type": "application/json"},
    ...options,
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  }
  return response.json();
}

function setDefaultDate() {
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  reservationDate.value = tomorrow.toISOString().slice(0, 10);
}

async function loadHealth() {
  try {
    const data = await request("/health");
    healthStatus.textContent = data.status === "ok" ? "后端已连接" : "连接异常";
  } catch {
    healthStatus.textContent = "后端未连接";
  }
}

async function loadCampuses() {
  const campuses = await request("/campuses");
  campusSelect.innerHTML = "";
  campusRows.innerHTML = "";
  for (const campus of campuses) {
    const option = document.createElement("option");
    option.value = campus.campus;
    option.textContent = ({Weifang:"潍坊",Qingdao:"青岛",Rongcheng:"荣成",Dongguan:"东莞"})[campus.campus] || campus.campus;
    option.dataset.instruction = campus.instruction || "";
    campusSelect.appendChild(option);

    const row = document.createElement("tr");
    row.innerHTML = `<td>${campus.campus}</td><td>${campus.weekday_quota}</td><td>${campus.rest_day_quota}</td><td>${campus.enabled}</td><td>${escapeHtml(campus.instruction)}</td>`;
    const edit = document.createElement("button");
    edit.textContent = "配置";
    edit.onclick = async () => {
      const weekday = prompt("工作日配额", campus.weekday_quota);
      if (weekday === null) return;
      const rest = prompt("休息日配额", campus.rest_day_quota);
      if (rest === null) return;
      try {
        await request(`/campuses/${campus.campus}`, {method:"PUT", body: JSON.stringify({weekday_quota:Number(weekday), rest_day_quota:Number(rest), enabled:confirm("开启预约？取消表示关闭"), instruction:campus.instruction})});
        await loadCampuses(); await refreshAvailability();
      } catch(error) { setMessage(error.message, false); }
    };
    const editCell = document.createElement("td"); editCell.appendChild(edit); row.appendChild(editCell);
    campusRows.appendChild(row);
  }
  updateInstruction();
}

function updateInstruction() {
  const selected = campusSelect.selectedOptions[0];
  campusInstruction.textContent = selected?.dataset.instruction || "请选择园区查看临时车预约说明。";
}

async function loadReservations() {
  const reservations = await request("/reservations");
  reservationRows.innerHTML = "";
  for (const item of reservations) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${item.reservation_id}</td>
      <td>${item.campus}</td>
      <td>${item.reservation_date}</td>
      <td>${escapeHtml(item.plate_no)}</td>
      <td>${item.status}</td>
      <td>
        <button data-action="pay" data-id="${item.reservation_id}" type="button">提前缴费</button>
        <button data-action="cancel" data-id="${item.reservation_id}" type="button">取消</button>
      </td>`;
    reservationRows.appendChild(row);
  }
}

reservationForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(reservationForm).entries());
  try {
    const result = await request("/reservations", {method: "POST", body: JSON.stringify(payload)});
    setMessage(result.message, result.success);
    await loadReservations();
    await refreshAvailability();
  } catch (error) {
    setMessage(`提交失败：${error.message}`, false);
  }
});

async function refreshAvailability() {
  if (!campusSelect.value || !reservationDate.value) return;
  try {
    const data = await request(`/availability?campus=${encodeURIComponent(campusSelect.value)}&reservation_date=${reservationDate.value}`);
    campusInstruction.textContent = `${data.instruction} · 剩余可预约车位：${data.remaining}`;
  } catch(error) { setMessage(error.message, false); }
}
campusSelect.addEventListener("change", () => {updateInstruction(); refreshAvailability();});
reservationDate.addEventListener("change", refreshAvailability);

document.querySelector("#refreshReservations").addEventListener("click", loadReservations);

reservationRows.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const id = button.dataset.id;
  const action = button.dataset.action;
  const path = action === "pay" ? `/reservations/${id}/pay` : `/reservations/${id}/cancel`;
  try {
    const result = await request(path, {method: "POST"});
    setMessage(result.message, result.success);
    await loadReservations(); await refreshAvailability();
  } catch(error) { setMessage(error.message, false); }
});

setDefaultDate();
loadHealth();
loadCampuses().then(async()=>{await loadReservations(); await refreshAvailability();}).catch((error) => setMessage(`加载失败：${error.message}`, false));
''',
}

BUSINESS_TEST = r'''
from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from src import api as business_api
from src.csv_repository import CSVRepository
from src.models import ReservationCreate
from src.services import DISABLED_MESSAGE, PAYMENT_SUCCESS_MESSAGE, ReservationService


def make_request(day: date, campus: str = "Weifang", plate_no: str = "鲁A12345") -> ReservationCreate:
    return ReservationCreate(
        name="Alice",
        employee_id="E001",
        mobile="13800000000",
        campus=campus,
        reservation_date=day,
        plate_no=plate_no,
    )


def api_payload(day: date, campus: str = "Weifang", plate_no: str = "鲁A12345") -> dict[str, str]:
    return {
        "name": "Alice",
        "employee_id": "E001",
        "mobile": "13800000000",
        "campus": campus,
        "reservation_date": day.isoformat(),
        "plate_no": plate_no,
    }


def test_fastapi_endpoints_use_local_csv_service(tmp_path, monkeypatch):
    monkeypatch.setattr(business_api, "service", ReservationService(tmp_path))
    client = TestClient(business_api.app)
    day = date.today() + timedelta(days=1)

    assert client.get("/health").json()["status"] == "ok"
    assert len(client.get("/campuses").json()) == 4

    created = client.post("/reservations", json=api_payload(day, plate_no="鲁A54321"))
    assert created.status_code == 200
    body = created.json()
    assert body["success"] is True
    reservation_id = body["reservation_id"]

    assert len(client.get("/reservations").json()) == 1
    paid = client.post(f"/reservations/{reservation_id}/pay")
    assert paid.status_code == 200
    assert paid.json()["message"] == PAYMENT_SUCCESS_MESSAGE
    cancelled = client.post(f"/reservations/{reservation_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["success"] is True


def test_csv_repository_initialization_append_and_update(tmp_path):
    repo = CSVRepository(tmp_path)
    repo.initialize()
    repo.append("internal_vehicle_archive.csv", {"plate_no": "鲁A12345", "owner": "Alice", "remark": "demo"})
    assert repo.read_all("internal_vehicle_archive.csv")[0]["owner"] == "Alice"
    count = repo.update("internal_vehicle_archive.csv", lambda row: row["plate_no"] == "鲁A12345", {"owner": "Bob"})
    assert count == 1
    assert repo.read_all("internal_vehicle_archive.csv")[0]["owner"] == "Bob"


def test_successful_reservation_and_advance_payment(tmp_path):
    service = ReservationService(tmp_path)
    result = service.create_reservation(make_request(date.today() + timedelta(days=1)))
    assert result["success"] is True
    payment = service.advance_payment(result["reservation_id"])
    assert payment["message"] == PAYMENT_SUCCESS_MESSAGE
    assert service.repository.read_all("payment_records.csv")[0]["status"] == "success"


def test_quota_exceeded(tmp_path):
    service = ReservationService(tmp_path)
    day = date.today() + timedelta(days=1)
    service.repository.update("campus_configs.csv", lambda row: row["campus"] == "Weifang", {"weekday_quota": 1, "rest_day_quota": 1})
    assert service.create_reservation(make_request(day, plate_no="鲁A12345"))["success"] is True
    result = service.create_reservation(make_request(day, plate_no="鲁A12346"))
    assert result["success"] is False
    assert "名额已满" in result["message"]


def test_reservation_date_outside_next_seven_days_fails(tmp_path):
    service = ReservationService(tmp_path)
    result = service.create_reservation(make_request(date.today() + timedelta(days=8)))
    assert result["success"] is False
    assert "未来7天内" in result["message"]


def test_same_plate_number_cannot_reserve_twice_same_day(tmp_path):
    service = ReservationService(tmp_path)
    day = date.today() + timedelta(days=1)
    assert service.create_reservation(make_request(day, campus="Weifang", plate_no="鲁A12345"))["success"] is True
    result = service.create_reservation(make_request(day, campus="Qingdao", plate_no="鲁A12345"))
    assert result["success"] is False
    assert "同一车牌" in result["message"]


def test_disabled_campus_fails(tmp_path):
    service = ReservationService(tmp_path)
    service.set_campus_enabled("Weifang", False)
    result = service.create_reservation(make_request(date.today() + timedelta(days=1)))
    assert result["success"] is False
    assert result["message"] == DISABLED_MESSAGE


def test_cancellation_releases_quota(tmp_path):
    service = ReservationService(tmp_path)
    day = date.today() + timedelta(days=1)
    service.repository.update("campus_configs.csv", lambda row: row["campus"] == "Weifang", {"weekday_quota": 1, "rest_day_quota": 1})
    first = service.create_reservation(make_request(day, plate_no="鲁A12345"))
    assert first["success"] is True
    cancel = service.cancel_reservation(first["reservation_id"])
    assert cancel["success"] is True
    second = service.create_reservation(make_request(day, plate_no="鲁A12346"))
    assert second["success"] is True
    ketuo = service.repository.read_all("ketuo_reservation_archive.csv")
    assert ketuo[0]["status"] == "cancelled"

def test_internal_archive_plate_normalization_and_payment_idempotency(tmp_path):
    service = ReservationService(tmp_path)
    day = date.today() + timedelta(days=1)
    service.repository.append('internal_vehicle_archive.csv', {'plate_no':'鲁A12345','owner':'Alice'})
    assert not service.create_reservation(make_request(day))['success']
    created = service.create_reservation(make_request(day, plate_no=' 鲁a54321 '))
    assert created['success']
    first = service.advance_payment(created['reservation_id'])
    assert service.advance_payment(created['reservation_id'])['payment_id'] == first['payment_id']
    assert len(service.repository.read_all('payment_records.csv')) == 1
    assert service.repository.read_all('payment_records.csv')[0]['campus'] == 'Weifang'
    assert not service.cancel_reservation('missing')['success']
    assert not service.advance_payment('missing')['success']


def test_availability_and_configuration_api(tmp_path, monkeypatch):
    monkeypatch.setattr(business_api, 'service', ReservationService(tmp_path))
    with TestClient(business_api.app) as client:
        day = (date.today() + timedelta(days=1)).isoformat()
        response = client.put('/campuses/Weifang', json={'weekday_quota':0,'rest_day_quota':0,'enabled':True,'instruction':'Closed quota'})
        assert response.status_code == 200 and response.json()['success']
        assert client.get('/availability', params={'campus':'Weifang','reservation_date':day}).json()['remaining'] == 0
        assert client.get('/availability', params={'campus':'Unknown','reservation_date':day}).status_code == 404
        assert client.put('/campuses/Unknown', json={'weekday_quota':1,'rest_day_quota':1,'enabled':True}).status_code == 404
        assert client.put('/campuses/Weifang', json={'weekday_quota':-1,'rest_day_quota':1,'enabled':True}).status_code == 422
        assert client.post('/reservations', json={}).status_code == 422


def test_repository_invalid_table_and_field(tmp_path):
    import pytest
    repo = CSVRepository(tmp_path)
    with pytest.raises(ValueError):
        repo.read_all('unknown.csv')
    repo.append('internal_vehicle_archive.csv', {'plate_no':'鲁A12345'})
    with pytest.raises(ValueError):
        repo.update('internal_vehicle_archive.csv', lambda row: True, {'unknown':'x'})
    assert repo.update('internal_vehicle_archive.csv', lambda row: False, {'owner':'x'}) == 0


def test_frontend_is_served_and_has_controls(tmp_path, monkeypatch):
    monkeypatch.setattr(business_api, 'service', ReservationService(tmp_path))
    with TestClient(business_api.app) as client:
        response = client.get('/')
        assert response.status_code == 200
        assert '<form' in response.text and '<button' in response.text
        assert client.get('/app.js').status_code == 200


def test_concurrent_reservations_cannot_exceed_quota(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    service = ReservationService(tmp_path)
    day = date.today() + timedelta(days=1)
    service.repository.update('campus_configs.csv', lambda row: row['campus'] == 'Weifang', {'weekday_quota':1,'rest_day_quota':1})
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda plate: service.create_reservation(make_request(day, plate_no=plate)), ['鲁A12345','鲁A12346','鲁A12347']))
    assert sum(result['success'] for result in results) == 1
    assert len(service.repository.read_all('reservations.csv')) == 1

'''

def code_result(system_name: str) -> CodeGenerationResult:
    from app.agents.code_agent import CodeGenerationResult, GeneratedCodeFile
    result = CodeGenerationResult(
        files=[
            GeneratedCodeFile(path=f"src/{relative_path}", content=dedent(content).lstrip("\n"))
            for relative_path, content in BUSINESS_FILES.items()
        ]
        + [
            GeneratedCodeFile(path=f"frontend/{relative_path}", content=dedent(content).lstrip("\n"))
            for relative_path, content in FRONTEND_FILES.items()
        ],
        manifest={
            "system_name": "Employee Temporary Vehicle Reservation System",
            "strategy": "sample-fixture",
            "limitations": ["No employee/admin authentication in offline template", "Calendar uses weekends, not statutory holiday calendars", "CSV is intended for single-process local demos"],
            "storage_contract": {"module": "src.api", "service_attribute": "service", "factory": "src.services.ReservationService", "argument": "data_dir"},
            "api_routes": [{"method": method, "path": path} for method, path in [
                ("GET", "/health"), ("GET", "/campuses"), ("GET", "/availability"), ("PUT", "/campuses/{campus}"),
                ("GET", "/reservations"), ("POST", "/reservations"),
                ("POST", "/reservations/{reservation_id}/cancel"), ("POST", "/reservations/{reservation_id}/pay")]],
            "business_functions": ["campus configuration", "reservation", "cancellation", "advance payment", "query"],
            "csv_tables": [
                "campus_configs.csv",
                "reservations.csv",
                "ketuo_reservation_archive.csv",
                "payment_records.csv",
                "internal_vehicle_archive.csv",
            ],
            "frontend_pages": [
                {
                    "path": "frontend/index.html",
                    "name": "员工临时车辆预约管理页面",
                    "purpose": "员工提交预约、查看预约、取消预约、提前缴费，管理员查看园区配置与配额",
                    "controls": [
                        "reservation form",
                        "campus selector",
                        "reservation table",
                        "cancel button",
                        "prepay button",
                        "campus configuration table",
                    ],
                }
            ],
            "run_instructions": [
                "uvicorn src.api:app --port 8001",
                "http://127.0.0.1:8001",
            ],
        },
    )

    result.manifest["system_name"] = system_name
    result.manifest["sample_fixture"] = SAMPLE_FIXTURE_ID
    for file in result.files:
        if file.path == "src/api.py":
            file.content = file.content.replace('title="Employee Temporary Vehicle Reservation System"', "title=" + repr(system_name))
        elif file.path == "frontend/index.html":
            file.content = file.content.replace("员工临时车辆预约管理系统", escape(system_name))
    return result


def test_result() -> TestGenerationResult:
    from app.agents.test_agent import TestGenerationResult, GeneratedTestFile
    return TestGenerationResult(
        files=[
            GeneratedTestFile(
                path="tests/generated/test_business_reservation.py",
                content=dedent(BUSINESS_TEST).lstrip("\n"),
            )
        ],
        test_plan_markdown=(
            "# Test Plan\n\n"
            "- Validate CSV repository initialization, append, query, and update.\n"
            "- Validate reservation success, quota limits, date window, duplicate plate, disabled campus, cancellation, and payment.\n"
            "- Tests use only local temporary CSV files and never call an LLM.\n"
        ),
    )


def design_details() -> dict:
    details = {}
    details.update(modules=['campus configuration', 'reservation', 'cancellation', 'advance payment', 'CSV integration'],
                      entities=['CampusConfig', 'Reservation', 'PaymentRecord', 'InternalVehicleArchive'],
                      business_rules=['Reserve today through 7 days ahead', 'One plate per day across campuses',
                                      'Reject disabled or full campus', 'Reject internally registered vehicles',
                                      'Cancellation releases quota and updates archive', 'Payment is idempotent'],
                      api_endpoints=['GET /health', 'GET /campuses', 'GET /availability', 'PUT /campuses/{campus}',
                                     'GET /reservations', 'POST /reservations', 'POST /reservations/{reservation_id}/cancel',
                                     'POST /reservations/{reservation_id}/pay'],
                      csv_tables=['campus_configs.csv', 'reservations.csv', 'ketuo_reservation_archive.csv',
                                  'payment_records.csv', 'internal_vehicle_archive.csv'],
                      validation_rules=['date_window', 'daily_quota', 'plate_format', 'duplicate_plate', 'internal_vehicle'],
                      acceptance_criteria=['Persist two reservations', 'Reject duplicate and internal vehicle',
                                           'Cancellation releases quota', 'Persist campus in payment record'])
    return details
