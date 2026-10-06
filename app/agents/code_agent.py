from __future__ import annotations

import shutil
import json
from html import escape
from pathlib import Path
from textwrap import dedent

from pydantic import BaseModel, Field

from app.agents.base import BaseAgent


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
            if design.get("generation_profile") != "vehicle":
                from app.agents.fallback import generic_code
                return CodeGenerationResult.model_validate(generic_code(design))
            result = self._template_generation_result()
            result.manifest["system_name"] = design["system_name"]
            for file in result.files:
                if file.path == "src/api.py":
                    file.content = file.content.replace('title="Employee Temporary Vehicle Reservation System"', "title=" + repr(design["system_name"]))
                elif file.path == "frontend/index.html":
                    file.content = file.content.replace("员工临时车辆预约管理系统", escape(design["system_name"]))
            return result

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

    def _template_generation_result(self) -> CodeGenerationResult:
        return CodeGenerationResult(
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
                "strategy": "template-fallback",
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
