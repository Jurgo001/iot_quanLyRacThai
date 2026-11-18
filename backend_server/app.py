import os
import time
import math
import heapq
import json
import secrets
import sqlite3
from contextlib import contextmanager, closing
from typing import List, Optional, Literal
from fastapi import FastAPI, HTTPException, Depends, Header, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict, StrictBool, model_validator
if __package__:
    from .p3_auth import init_auth_tables, authenticate, issue_token, verify_bearer
else:
    # P2 tests load app.py into an isolated temporary directory as a module.
    from p3_auth import init_auth_tables, authenticate, issue_token, verify_bearer

app = FastAPI(title="Hệ Thống Quản Lý Rác Thải Thông Minh - Nhóm 15 HUIT")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key", "Authorization"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get("IOT_DB_PATH", os.path.join(BASE_DIR, "smart_waste.db"))
HTML_FILE = os.path.join(BASE_DIR, "index.html")
LOCK_READS = os.getenv("IOT_P3_LOCK_READS", "0") == "1"
DISABLE_LEGACY_ADMIN = os.getenv("IOT_P3_DISABLE_LEGACY_ADMIN", "0") == "1"

# P1-1: Demo-only keys generated on this PC, never commit this JSON to Git.
LOCAL_KEYS_FILE = os.path.join(BASE_DIR, "p1_local_keys.json")
try:
    with open(LOCAL_KEYS_FILE, "r", encoding="utf-8") as _f:
        _local_keys = json.load(_f)
except (FileNotFoundError, ValueError) as _exc:
    raise RuntimeError("P1-1: Missing/invalid backend_server/p1_local_keys.json. Run apply_p1_1.py first.") from _exc

DEVICE_API_KEY = _local_keys.get("device_api_key", "")
ADMIN_API_KEY = _local_keys.get("admin_api_key", "")
if not isinstance(DEVICE_API_KEY, str) or not isinstance(ADMIN_API_KEY, str) or min(len(DEVICE_API_KEY), len(ADMIN_API_KEY)) < 32:
    raise RuntimeError("P1-1: Invalid local API keys; both must have at least 32 characters.")


def require_device_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
    if not x_api_key or not secrets.compare_digest(x_api_key, DEVICE_API_KEY):
        raise HTTPException(status_code=401, detail="Device API key missing or invalid")


def require_admin_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
                      authorization: Optional[str] = Header(default=None, alias="Authorization")):
    # Backwards-compatible P1 admin key, including all Stage1/Stage2 regression tests.
    # P3 operator/admin users may instead send a short-lived signed JWT.
    if x_api_key is not None:
        if not DISABLE_LEGACY_ADMIN and secrets.compare_digest(x_api_key, ADMIN_API_KEY):
            return
        raise HTTPException(status_code=401, detail="Admin API key missing or invalid")
    if authorization:
        verify_bearer(DB_FILE, authorization, {"operator", "admin"})
        return
    raise HTTPException(status_code=401, detail="Admin API key or operator/admin token required")


def require_reporting_user(authorization: Optional[str] = Header(default=None, alias="Authorization")):
    return verify_bearer(DB_FILE, authorization, {"viewer", "operator", "admin"})


def require_read_access(authorization: Optional[str] = Header(default=None, alias="Authorization")):
    # Stage1/2 demo can keep public reads; deploy with LOCK_READS=1 for JWT RBAC.
    if LOCK_READS:
        return verify_bearer(DB_FILE, authorization, {"viewer", "operator", "admin"})


@app.get("/api/ui/config")
def ui_config():
    return {"require_login_for_reads": LOCK_READS,
            "legacy_admin_enabled": not DISABLE_LEGACY_ADMIN}


# Điểm tập kết xe rác (Depot): Cổng chính ĐH Công Thương TP.HCM (140 Lê Trọng Tấn)
DEPOT = {"id": "DEPOT", "name": "Trạm Xe Thu Gom (ĐH Công Thương)", "lat": 10.8061, "lng": 106.6286}

# Các nút giao lộ trung gian (Intersections) tạo nên mạng lưới đồ thị cục bộ G = (V, E)
GRAPH_NODES = {
    "DEPOT":  {"name": "Trạm Tập Kết (ĐH Công Thương)", "lat": 10.8061, "lng": 106.6286, "type": "DEPOT"},
    "BIN_01": {"name": "Thùng 01 - Khu A HUIT (Wokwi)", "lat": 10.8069, "lng": 106.6295, "type": "BIN"},
    "BIN_02": {"name": "Thùng 02 - KTX Tân Phú",        "lat": 10.8085, "lng": 106.6312, "type": "BIN"},
    "BIN_03": {"name": "Thùng 03 - Công viên D9",       "lat": 10.8048, "lng": 106.6335, "type": "BIN"},
    "BIN_04": {"name": "Thùng 04 - Chợ Sơn Kỳ",         "lat": 10.8025, "lng": 106.6298, "type": "BIN"},
    "BIN_05": {"name": "Thùng 05 - NVH Tây Thạnh",      "lat": 10.8032, "lng": 106.6255, "type": "BIN"},
    "BIN_06": {"name": "Thùng 06 - Trạm Y Tế Phường",   "lat": 10.8075, "lng": 106.6248, "type": "BIN"},
    "INT_01": {"name": "Ngã 4 Lê Trọng Tấn - Nguyễn Hữu Tiến", "lat": 10.8055, "lng": 106.6305, "type": "INTERSECTION"},
    "INT_02": {"name": "Ngã 3 Chế Lan Viên - Dương Đức Hiền",  "lat": 10.8078, "lng": 106.6320, "type": "INTERSECTION"},
    "INT_03": {"name": "Giao lộ Sơn Kỳ - Tân Kỳ Tân Quý",      "lat": 10.8030, "lng": 106.6280, "type": "INTERSECTION"},
    "INT_04": {"name": "Ngã 3 Tây Thạnh - Lê Trọng Tấn",       "lat": 10.8068, "lng": 106.6265, "type": "INTERSECTION"}
}

# Trọng số cạnh đồ thị cục bộ (mét) dùng cho thuật toán Dijkstra (Mục 2.1.3 & 2.1.6)
LOCAL_GRAPH = {
    "DEPOT":  {"BIN_01": 220, "INT_01": 260, "INT_04": 280},
    "BIN_01": {"DEPOT": 220, "BIN_02": 320, "INT_01": 210},
    "BIN_02": {"BIN_01": 320, "INT_02": 180},
    "INT_02": {"BIN_02": 180, "BIN_03": 390, "INT_01": 310},
    "INT_01": {"DEPOT": 260, "BIN_01": 210, "INT_02": 310, "BIN_03": 360, "BIN_04": 370},
    "BIN_03": {"INT_02": 390, "INT_01": 360, "BIN_04": 420},
    "BIN_04": {"INT_01": 370, "BIN_03": 420, "INT_03": 240},
    "INT_03": {"BIN_04": 240, "BIN_05": 290, "DEPOT": 410},
    "BIN_05": {"INT_03": 290, "INT_04": 430, "BIN_06": 510},
    "INT_04": {"DEPOT": 280, "BIN_05": 430, "BIN_06": 230},
    "BIN_06": {"INT_04": 230, "BIN_05": 510, "BIN_01": 540}
}

# P1-4: This is a HAND-MODELED DIRECTED weighted graph, not OSRM road data.
# Do not silently add reverse edges: one-way vs missing edges needs field review.
def audit_local_graph(graph):
    missing_reverse = []
    different_weights = []
    for source, neighbors in graph.items():
        if source not in GRAPH_NODES:
            raise ValueError(f"Unknown graph node: {source}")
        for target, weight in neighbors.items():
            if target not in GRAPH_NODES or target not in graph:
                raise ValueError(f"Unknown graph target: {source}->{target}")
            if not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight <= 0:
                raise ValueError(f"Invalid positive Dijkstra weight: {source}->{target}: {weight}")
            reverse = graph[target].get(source)
            if reverse is None:
                missing_reverse.append(f"{source}->{target}")
            elif reverse != weight:
                different_weights.append(f"{source}<->{target}")
    return {
        "missing_reverse_edges": sorted(missing_reverse),
        "different_reverse_weights": sorted(set(different_weights)),
    }


def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS bins (
        bin_id TEXT PRIMARY KEY,
        device_id TEXT,
        name TEXT,
        lat REAL,
        lng REAL,
        distance_cm REAL,
        fill_percent REAL,
        status TEXT,
        sensor_status TEXT,
        pending_status TEXT,
        consecutive_count INTEGER,
        is_priority INTEGER,
        last_updated REAL,
        tilt_detected INTEGER NOT NULL DEFAULT 0,
        fire_detected INTEGER NOT NULL DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS telemetry_logs (
        message_id TEXT PRIMARY KEY,
        bin_id TEXT,
        device_id TEXT,
        distance_cm REAL,
        fill_percent REAL,
        sensor_status TEXT,
        received_at REAL
    )''')
    # P3: time-series oriented access on the existing append-only readings.
    c.execute('''CREATE INDEX IF NOT EXISTS idx_telemetry_bin_time
        ON telemetry_logs(bin_id, received_at DESC)''')
    # P2-B: additive delivery audit; preserve the legacy telemetry_logs schema.
    # A message can be received long after it was measured during an outage.
    c.execute('''CREATE TABLE IF NOT EXISTS telemetry_delivery_audit (
        message_id TEXT PRIMARY KEY,
        captured_at_unix INTEGER,
        received_at REAL NOT NULL,
        delivery_delay_seconds REAL,
        transport TEXT NOT NULL DEFAULT 'WIFI_HTTP_JSON',
        FOREIGN KEY(message_id) REFERENCES telemetry_logs(message_id)
    )''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_delivery_received
        ON telemetry_delivery_audit(received_at DESC)''')
    c.execute('''CREATE TABLE IF NOT EXISTS trip_stops (
        stop_order INTEGER,
        bin_id TEXT PRIMARY KEY,
        action_status TEXT,
        skip_reason TEXT,
        updated_at REAL
    )''')

    # P2-Stage1 additive migration. Preserve every existing sensor reading,
    # trip, priority flag and event in the real project database.
    existing_columns = {row[1] for row in c.execute("PRAGMA table_info(bins)")}
    for field_name in ("tilt_detected", "fire_detected"):
        if field_name not in existing_columns:
            c.execute(f"ALTER TABLE bins ADD COLUMN {field_name} INTEGER NOT NULL DEFAULT 0")

    c.execute("SELECT COUNT(*) FROM bins")
    if c.fetchone()[0] == 0:
        now = time.time()
        sample_bins = [
            ("BIN_01", "ESP32_NODE_01", "Thùng 01 - Khu A HUIT (Wokwi ESP32)", 10.8069, 106.6295, 85.0, 18.8, "NORMAL", "OK", "NORMAL", 2, 0, now),
            ("BIN_02", "SIM_NODE_02",   "Thùng 02 - Ký túc xá Tân Phú",       10.8085, 106.6312, 28.0, 90.0, "NEED_COLLECTION", "OK", "NEED_COLLECTION", 2, 0, now),
            ("BIN_03", "SIM_NODE_03",   "Thùng 03 - Công viên D9",            10.8048, 106.6335, 32.0, 85.0, "NEED_COLLECTION", "OK", "NEED_COLLECTION", 2, 0, now),
            ("BIN_04", "SIM_NODE_04",   "Thùng 04 - Chợ Sơn Kỳ",              10.8025, 106.6298, 45.0, 68.8, "NEAR_FULL", "OK", "NEAR_FULL", 2, 0, now),
            ("BIN_05", "SIM_NODE_05",   "Thùng 05 - Nhà Văn Hóa Tây Thạnh",   10.8032, 106.6255, 24.0, 95.0, "NEED_COLLECTION", "OK", "NEED_COLLECTION", 2, 1, now),
            ("BIN_06", "SIM_NODE_06",   "Thùng 06 - Trạm Y Tế Phường",        10.8075, 106.6248, 30.0, 87.5, "NEED_COLLECTION", "OK", "NEED_COLLECTION", 2, 0, now),
        ]
        c.executemany('''INSERT INTO bins
            (bin_id, device_id, name, lat, lng, distance_cm, fill_percent,
             status, sensor_status, pending_status, consecutive_count, is_priority, last_updated)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', sample_bins)
    c.execute('''CREATE TABLE IF NOT EXISTS alert_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bin_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        description TEXT NOT NULL,
        created_at REAL NOT NULL
    )''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_alert_events_time
        ON alert_events(created_at DESC)''')
    conn.commit()
    conn.close()
    init_auth_tables(DB_FILE)

init_db()

# P1-1: Reject malformed or inconsistent telemetry BEFORE writing to SQLite.
class TelemetryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    device_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_]+$")
    bin_id: str = Field(pattern=r"^BIN_0[1-6]$")
    message_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")
    measured_at: int = Field(ge=0)  # ESP32 uptime seconds, NOT Unix timestamp.
    # P2-B: only a synchronized real UTC timestamp; absent for devices without NTP/RTC.
    captured_at_unix: Optional[int] = Field(default=None, ge=1609459200, le=4294967295)
    distance_cm: float = Field(allow_inf_nan=False)
    fill_percent: float = Field(allow_inf_nan=False)
    sensor_status: Literal["OK", "SENSOR_ERROR"]
    is_priority: StrictBool = False
    # Optional for old Wokwi firmware; strict bool rejects 0/1/string guesses.
    tilt_detected: StrictBool = False
    fire_detected: StrictBool = False

    @model_validator(mode="after")
    def ensure_consistency(self):
        if self.sensor_status == "SENSOR_ERROR":
            if self.distance_cm != -1.0 or self.fill_percent != -1.0:
                raise ValueError("SENSOR_ERROR requires distance_cm=-1 and fill_percent=-1")
        else:
            if not 2.0 <= self.distance_cm <= 400.0 or not 0.0 <= self.fill_percent <= 100.0:
                raise ValueError("OK requires distance 2..400 cm and fill 0..100%")
            expected = round(max(0.0, min(100.0, (100.0-self.distance_cm)*100.0/80.0)), 1)
            if abs(self.fill_percent-expected) > 0.25:
                raise ValueError(f"fill_percent inconsistent with distance_cm; expected ~{expected}%")
        return self


class SimulateBinInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    bin_id: str = Field(pattern=r"^BIN_0[1-6]$")
    distance_cm: float = Field(allow_inf_nan=False)
    sensor_status: Literal["OK", "SENSOR_ERROR"] = "OK"
    is_priority: StrictBool = False

    @model_validator(mode="after")
    def validate_sim(self):
        if self.sensor_status == "SENSOR_ERROR":
            if self.distance_cm != -1.0:
                raise ValueError("SENSOR_ERROR requires distance_cm=-1")
        elif not 2.0 <= self.distance_cm <= 400.0:
            raise ValueError("distance_cm must be 2..400 when sensor is OK")
        return self


class PriorityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    bin_id: str = Field(pattern=r"^BIN_0[2-6]$")
    is_priority: StrictBool
    reason: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def validate_reason(self):
        if self.is_priority and not self.reason.strip():
            raise ValueError("Bật ưu tiên cần nhập lý do")
        return self


class ConfirmInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    bin_id: str = Field(pattern=r"^BIN_0[1-6]$")
    action: Literal["COLLECTED", "SKIPPED"]
    reason: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def validate_reason(self):
        if self.action == "SKIPPED" and not self.reason.strip():
            raise ValueError("SKIPPED requires a reason")
        return self

def classify_fill(fill: float) -> str:
    if fill >= 80.0:
        return "NEED_COLLECTION"
    elif fill >= 60.0:
        return "NEAR_FULL"
    return "NORMAL"

def calc_fill_from_dist(dist: float) -> float:
    pct = ((100.0 - dist) / (100.0 - 20.0)) * 100.0
    return round(max(0.0, min(100.0, pct)), 1)

# P1-5: independent priority management & alert lifecycle.
@contextmanager
def p1_5_db_transaction():
    conn = sqlite3.connect(DB_FILE)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def log_alert_event(cursor, bin_id: str, event_type: str, description: str, now: float):
    cursor.execute(
        "INSERT INTO alert_events(bin_id, event_type, description, created_at) VALUES (?,?,?,?)",
        (bin_id, event_type, description, now),
    )


def classify_alerts(status: str, sensor_status: str, is_priority: bool,
                    tilt_detected: bool = False, fire_detected: bool = False):
    reasons = []
    if fire_detected:
        reasons.append("Nguy cơ cháy (đầu vào Wokwi mô phỏng)")
    if tilt_detected:
        reasons.append("Thùng bị nghiêng/đổ (đầu vào Wokwi mô phỏng)")
    if sensor_status == "OFFLINE":
        reasons.append("Thiết bị mất kết nối")
    elif sensor_status == "SENSOR_ERROR":
        reasons.append("Lỗi cảm biến")
    if is_priority:
        reasons.append("Yêu cầu thu gom ưu tiên")
    if status == "NEED_COLLECTION":
        reasons.append("Thùng đầy từ 80%")
    elif status == "NEAR_FULL":
        reasons.append("Thùng gần đầy 60–79%")
    severity = ("CRITICAL" if fire_detected or tilt_detected or sensor_status != "OK" or is_priority or status == "NEED_COLLECTION"
                else "WARNING" if status == "NEAR_FULL" else "NONE")
    return {"alert_severity": severity, "alert_reasons": reasons}


@app.post("/api/bins/priority")
def set_bin_priority(req: PriorityInput, _authorized: None = Depends(require_admin_key)):
    """Only SIM_NODE_02..06. Does NOT change measurement or last_updated."""
    with p1_5_db_transaction() as conn:
        c = conn.cursor()
        c.execute("SELECT device_id, is_priority FROM bins WHERE bin_id=?", (req.bin_id,))
        found = c.fetchone()
        if not found:
            raise HTTPException(status_code=404, detail="Bin not found")
        if not found[0].startswith("SIM_NODE_"):
            raise HTTPException(status_code=409, detail="BIN_01 priority is controlled by Wokwi")
        previous = bool(found[1])
        if previous == req.is_priority:
            return {"status": "unchanged", "bin_id": req.bin_id, "is_priority": previous}
        if req.is_priority:
            c.execute("SELECT action_status FROM trip_stops WHERE bin_id=?", (req.bin_id,))
            completed = c.fetchone()
            if completed:
                raise HTTPException(status_code=409, detail="Thùng đã xử lý trong chuyến; Reset chuyến trước khi yêu cầu mới")
        now = time.time()
        c.execute("UPDATE bins SET is_priority=? WHERE bin_id=?", (int(req.is_priority), req.bin_id))
        log_alert_event(c, req.bin_id,
                        "PRIORITY_ON" if req.is_priority else "PRIORITY_OFF",
                        req.reason.strip() or "Người điều phối hủy ưu tiên", now)
        conn.commit()
        return {"status": "updated", "bin_id": req.bin_id, "is_priority": req.is_priority}


# 1. API Nhận bản tin từ mạch Wokwi ESP32
@app.post("/api/telemetry")
def receive_telemetry(data: TelemetryInput, _authorized: None = Depends(require_device_key)):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Atomically serialize check+insert+state transitions. A retry arriving at
    # the same moment cannot produce two events or crash on duplicate PK.
    c.execute("BEGIN IMMEDIATE")

    # Verify registered device/bin pairing before inserting any telemetry row.
    c.execute("SELECT device_id, status, pending_status, consecutive_count, fill_percent, is_priority, sensor_status, last_updated, tilt_detected, fire_detected FROM bins WHERE bin_id = ?", (data.bin_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Bin not found")
    registered_device, current_status, pending_status, consecutive_count, old_fill, old_prio, old_sensor, old_updated, old_tilt, old_fire = row
    if data.device_id != registered_device:
        conn.close()
        raise HTTPException(status_code=403, detail="Device does not own this bin")
    # P1-2: ESP32 telemetry may update only bins registered as actual ESP32 devices.
    if not registered_device.startswith("ESP32_"):
        conn.close()
        raise HTTPException(status_code=403, detail="This bin is controlled by the Web simulator")

    c.execute("SELECT message_id FROM telemetry_logs WHERE message_id = ?", (data.message_id,))
    if c.fetchone():
        conn.close()
        return {"status": "duplicate_ignored", "message_id": data.message_id}

    now = time.time()
    c.execute("INSERT INTO telemetry_logs VALUES (?,?,?,?,?,?,?)",
              (data.message_id, data.bin_id, data.device_id, data.distance_cm, data.fill_percent, data.sensor_status, now))
    delay = max(0.0, now - data.captured_at_unix) if data.captured_at_unix is not None else None
    c.execute('''INSERT INTO telemetry_delivery_audit
        (message_id, captured_at_unix, received_at, delivery_delay_seconds)
        VALUES (?,?,?,?)''', (data.message_id, data.captured_at_unix, now, delay))
    new_prio = 1 if data.is_priority else old_prio
    if data.is_priority and not old_prio:
        log_alert_event(c, data.bin_id, "PRIORITY_ON", "Nút khẩn cấp trên Wokwi", now)
    if old_updated is not None and now - old_updated > 60:
        log_alert_event(c, data.bin_id, "DEVICE_ONLINE", "Thiết bị kết nối và gửi dữ liệu lại", now)
    if data.sensor_status != "OK" and old_sensor != "SENSOR_ERROR":
        log_alert_event(c, data.bin_id, "SENSOR_ERROR", "Cảm biến báo lỗi", now)
    if data.sensor_status == "OK" and old_sensor == "SENSOR_ERROR":
        log_alert_event(c, data.bin_id, "SENSOR_RECOVERED", "Cảm biến hoạt động trở lại", now)

    # P2-Stage1: alarm lifecycle is independent of fill, manual priority,
    # and sensor fault. A repeated packet does not log duplicate transitions.
    if bool(old_tilt) != data.tilt_detected:
        log_alert_event(c, data.bin_id, "TILT_DETECTED" if data.tilt_detected else "TILT_CLEARED",
                        "Thùng bị nghiêng/đổ (Wokwi mô phỏng)" if data.tilt_detected else "Thùng trở về vị trí bình thường", now)
    if bool(old_fire) != data.fire_detected:
        log_alert_event(c, data.bin_id, "FIRE_DETECTED" if data.fire_detected else "FIRE_CLEARED",
                        "Nguy cơ cháy (Wokwi mô phỏng)" if data.fire_detected else "Đầu vào báo cháy trở về bình thường", now)
    c.execute("UPDATE bins SET tilt_detected=?, fire_detected=? WHERE bin_id=?",
              (int(data.tilt_detected), int(data.fire_detected), data.bin_id))

    if data.sensor_status != "OK" or data.distance_cm < 0:
        c.execute("UPDATE bins SET sensor_status = 'SENSOR_ERROR', is_priority = ?, last_updated = ? WHERE bin_id = ?",
                  (new_prio, now, data.bin_id))
        conn.commit()
        conn.close()
        return {"status": "sensor_error_logged", "bin_id": data.bin_id, "message_id": data.message_id}

    candidate_status = classify_fill(data.fill_percent)
    if candidate_status == pending_status:
        consecutive_count += 1
    else:
        pending_status = candidate_status
        consecutive_count = 1

    new_status = candidate_status if consecutive_count >= 2 else current_status
    if new_status != current_status:
        log_alert_event(c, data.bin_id, "FILL_STATUS_CHANGED",
                        f"{current_status} → {new_status} ({data.fill_percent:.1f}%)", now)

    c.execute('''UPDATE bins SET distance_cm=?, fill_percent=?, status=?, sensor_status='OK',
                 pending_status=?, consecutive_count=?, is_priority=?, last_updated=? WHERE bin_id=?''',
              (data.distance_cm, data.fill_percent, new_status, pending_status, consecutive_count, new_prio, now, data.bin_id))
    conn.commit()
    conn.close()
    # ACK must echo the exact message_id. Only then may the device dequeue.
    return {"status": "accepted", "bin_id": data.bin_id, "message_id": data.message_id,
            "confirmed_status": new_status, "consecutive": consecutive_count}


@app.get("/api/network/stats")
def get_network_stats(_authorized: None = Depends(require_admin_key)):
    """Delivery observability for the edge-offline demo; no device API key leaks."""
    with closing(sqlite3.connect(DB_FILE)) as conn:
        count, delayed, worst = conn.execute('''SELECT COUNT(*),
            COALESCE(SUM(CASE WHEN delivery_delay_seconds >= 30 THEN 1 ELSE 0 END), 0),
            MAX(delivery_delay_seconds) FROM telemetry_delivery_audit''').fetchone()
        latest = conn.execute('''SELECT message_id, captured_at_unix, received_at,
            delivery_delay_seconds FROM telemetry_delivery_audit
            ORDER BY received_at DESC LIMIT 10''').fetchall()
    return {"transport": "WIFI_HTTP_JSON", "total_received": count,
            "delayed_at_least_30s": delayed, "max_delivery_delay_seconds": worst,
            "recent": [{"message_id": r[0], "captured_at_unix": r[1],
                        "received_at": r[2], "delivery_delay_seconds": r[3]} for r in latest]}


# P3 / Câu 2C: short-lived JWT accounts are separate from the device API key.
# Account creation is intentionally offline via p3_setup.py, with no shipped passwords.
class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=256)


@app.post("/api/auth/token")
def login(req: LoginInput):
    identity = authenticate(DB_FILE, req.username, req.password)
    token = issue_token(identity)
    return JSONResponse({"access_token": token, "token_type": "bearer",
                         "expires_in": 1800, "role": identity["role"],
                         "username": identity["username"]}, headers={"Cache-Control": "no-store"})


@app.get("/api/auth/me")
def who_am_i(identity: dict = Depends(require_reporting_user)):
    return identity


@app.get("/api/analytics/overview")
def analytics_overview(hours: int = Query(default=24, ge=1, le=168),
                       _identity: dict = Depends(require_reporting_user)):
    """Live operational snapshot and strictly windowed telemetry/alert metrics."""
    now = time.time()
    since = now - hours * 3600
    with closing(sqlite3.connect(DB_FILE)) as conn:
        bins = conn.execute('''SELECT COUNT(*),
            SUM(CASE WHEN sensor_status='SENSOR_ERROR' THEN 1 ELSE 0 END),
            SUM(CASE WHEN fire_detected=1 THEN 1 ELSE 0 END),
            SUM(CASE WHEN tilt_detected=1 THEN 1 ELSE 0 END),
            SUM(CASE WHEN is_priority=1 THEN 1 ELSE 0 END),
            AVG(CASE WHEN sensor_status='OK' AND fill_percent>=0 THEN fill_percent END)
            FROM bins''').fetchone()
        samples = conn.execute("SELECT COUNT(*) FROM telemetry_logs WHERE received_at>=?", (since,)).fetchone()[0]
        alerts = conn.execute("SELECT COUNT(*) FROM alert_events WHERE created_at>=?", (since,)).fetchone()[0]
        delays = conn.execute('''SELECT COUNT(*),
            SUM(CASE WHEN delivery_delay_seconds>=30 THEN 1 ELSE 0 END),
            AVG(delivery_delay_seconds)
            FROM telemetry_delivery_audit WHERE received_at>=?''', (since,)).fetchone()
    return {
        "as_of_unix": now, "window_hours": hours, "bin_count": bins[0],
        "sensor_error_count": bins[1] or 0, "fire_active": bins[2] or 0,
        "tilt_active": bins[3] or 0, "priority_count": bins[4] or 0,
        "avg_valid_fill_percent": round(bins[5], 1) if bins[5] is not None else None,
        "telemetry_samples": samples, "alert_events": alerts,
        "delivery_samples": delays[0], "delayed_at_least_30s": delays[1] or 0,
        "avg_delivery_delay_seconds": round(delays[2], 2) if delays[2] is not None else None,
        "source": "local_sqlite_real_records",
    }


@app.get("/api/analytics/history")
def analytics_history(bin_id: str = Query(default="BIN_01", pattern=r"^BIN_0[1-6]$"),
                      hours: int = Query(default=24, ge=1, le=168),
                      bucket_minutes: int = Query(default=15, ge=5, le=60),
                      _identity: dict = Depends(require_reporting_user)):
    """Aggregate actual received readings; no invented interpolation or demo series."""
    if bucket_minutes not in (5, 15, 60):
        raise HTTPException(status_code=422, detail="bucket_minutes must be 5, 15 or 60")
    now = time.time()
    since = now - hours * 3600
    bucket_seconds = bucket_minutes * 60
    with closing(sqlite3.connect(DB_FILE)) as conn:
        found = conn.execute("SELECT 1 FROM bins WHERE bin_id=?", (bin_id,)).fetchone()
        if not found:
            raise HTTPException(status_code=404, detail="Bin not found")
        points = conn.execute('''SELECT CAST(received_at / ? AS INTEGER) * ? AS bucket_unix,
            COUNT(*) AS sample_count, ROUND(AVG(fill_percent), 2),
            ROUND(MIN(fill_percent), 2), ROUND(MAX(fill_percent), 2)
            FROM telemetry_logs
            WHERE bin_id=? AND received_at>=? AND received_at<=?
                AND sensor_status='OK' AND fill_percent BETWEEN 0 AND 100
            GROUP BY CAST(received_at / ? AS INTEGER)
            ORDER BY bucket_unix ASC''',
            (bucket_seconds, bucket_seconds, bin_id, since, now, bucket_seconds)).fetchall()
    return {"bin_id": bin_id, "window_hours": hours, "bucket_minutes": bucket_minutes,
            "as_of_unix": now, "source": "telemetry_logs.received_at",
            "points": [{"bucket_unix": int(row[0]), "sample_count": row[1],
                        "avg_fill_percent": row[2], "min_fill_percent": row[3],
                        "max_fill_percent": row[4]} for row in points]}

# 2. API Giả lập trực tiếp các nút IoT (BIN_01 .. BIN_06) từ bảng điều khiển Web
@app.post("/api/simulate/bin")
def simulate_bin_node(req: SimulateBinInput, _authorized: None = Depends(require_admin_key)):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = time.time()
    # P1-2: Enforce source ownership in Backend, not just in the Web UI.
    # BIN_01 belongs to ESP32_NODE_01; the simulator cannot overwrite it even with an admin key.
    c.execute("SELECT device_id, sensor_status, status FROM bins WHERE bin_id = ?", (req.bin_id,))
    registered = c.fetchone()
    if not registered:
        conn.close()
        raise HTTPException(status_code=404, detail="Bin not found")
    if not registered[0].startswith("SIM_NODE_"):
        conn.close()
        raise HTTPException(status_code=409, detail="This bin is controlled by Wokwi/ESP32; use the sensor")
    old_sensor, old_status = registered[1], registered[2]
    new_sensor = req.sensor_status
    if old_sensor != new_sensor:
        log_alert_event(c, req.bin_id,
                        "SENSOR_ERROR" if new_sensor == "SENSOR_ERROR" else "SENSOR_RECOVERED",
                        "Mô phỏng cảm biến lỗi" if new_sensor == "SENSOR_ERROR" else "Mô phỏng cảm biến phục hồi", now)
    msg_id = f"SIM_{req.bin_id}_{int(now * 1000)}_{secrets.token_hex(4)}"

    if req.sensor_status != "OK" or req.distance_cm < 0:
        c.execute("UPDATE bins SET sensor_status = ?, last_updated = ? WHERE bin_id = ?",
                  (req.sensor_status, now, req.bin_id))
        c.execute("INSERT INTO telemetry_logs VALUES (?,?,?,?,?,?,?)",
                  (msg_id, req.bin_id, f"SIM_{req.bin_id}", -1.0, -1.0, req.sensor_status, now))
    else:
        fill = calc_fill_from_dist(req.distance_cm)
        st = classify_fill(fill)
        if st != old_status:
            log_alert_event(c, req.bin_id, "FILL_STATUS_CHANGED",
                            f"{old_status} → {st} ({fill:.1f}%)", now)
        c.execute('''UPDATE bins SET distance_cm=?, fill_percent=?, status=?, sensor_status='OK',
                     pending_status=?, consecutive_count=2, last_updated=? WHERE bin_id=?''',
                  (req.distance_cm, fill, st, st, now, req.bin_id))
        c.execute("INSERT INTO telemetry_logs VALUES (?,?,?,?,?,?,?)",
                  (msg_id, req.bin_id, f"SIM_{req.bin_id}", req.distance_cm, fill, "OK", now))
    conn.commit()
    conn.close()
    return {"status": "simulated", "bin_id": req.bin_id}

# 3. API Truy vấn danh sách thùng rác + Đồ thị cục bộ
@app.get("/api/bins")
def get_bins(_authorized: Optional[dict] = Depends(require_read_access)):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT bin_id, device_id, name, lat, lng, distance_cm, fill_percent, status, sensor_status, is_priority, last_updated, consecutive_count, tilt_detected, fire_detected FROM bins ORDER BY bin_id ASC")
    rows = c.fetchall()
    c.execute("SELECT bin_id, action_status, skip_reason FROM trip_stops")
    stops = {r[0]: {"action_status": r[1], "skip_reason": r[2]} for r in c.fetchall()}
    conn.close()

    now = time.time()
    result = []
    for r in rows:
        # Khong tao thoi gian ao cho cac thung mo phong.
        # BIN_01 la nut Wokwi; BIN_02..BIN_06 la cac nut do Web gia lap.
        last_updated = r[10]
        seconds_ago = max(0, int(now - last_updated)) if last_updated is not None else None
        is_offline = (r[0] == "BIN_01") and (last_updated is None or seconds_ago > 60)
        alerts = classify_alerts(r[7], "OFFLINE" if is_offline else r[8], bool(r[9]), bool(r[12]), bool(r[13]))
        result.append({
            "bin_id": r[0], "device_id": r[1], "name": r[2], "lat": r[3], "lng": r[4],
            "distance_cm": r[5], "fill_percent": r[6], "status": r[7],
            "sensor_status": "OFFLINE" if is_offline else r[8],
            "is_priority": bool(r[9]),
            "tilt_detected": bool(r[12]),
            "fire_detected": bool(r[13]),
            "alert_severity": alerts["alert_severity"],
            "alert_reasons": alerts["alert_reasons"],
            "seconds_ago": seconds_ago,
            "data_source": "WOKWI" if r[0] == "BIN_01" else "SIMULATED",
            "consecutive_count": r[11],
            "trip_info": stops.get(r[0], None)
        })

    # Chuẩn bị danh sách cạnh đồ thị để vẽ lớp mạng lưới đường nội bộ trên bản đồ
    edges = []
    seen = set()
    for u, nbrs in LOCAL_GRAPH.items():
        for v, w in nbrs.items():
            pair = tuple(sorted([u, v]))
            if pair not in seen:
                seen.add(pair)
                edges.append({
                    "from": u, "to": v, "weight": w,
                    "coords": [[GRAPH_NODES[u]["lat"], GRAPH_NODES[u]["lng"]],
                               [GRAPH_NODES[v]["lat"], GRAPH_NODES[v]["lng"]]]
                })

    return {"depot": DEPOT, "bins": result, "graph_nodes": GRAPH_NODES, "graph_edges": edges}

# 4. API Lấy nhật ký bản tin IoT & lịch sử chuyến đi
@app.get("/api/logs")
def get_system_logs(_authorized: Optional[dict] = Depends(require_read_access)):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT message_id, bin_id, device_id, distance_cm, fill_percent, sensor_status, received_at FROM telemetry_logs ORDER BY received_at DESC LIMIT 25")
    logs = [
        {
            "message_id": r[0], "bin_id": r[1], "device_id": r[2],
            "distance_cm": r[3], "fill_percent": r[4], "sensor_status": r[5],
            "time_str": time.strftime("%H:%M:%S", time.localtime(r[6]))
        }
        for r in c.fetchall()
    ]
    c.execute("SELECT bin_id, event_type, description, created_at FROM alert_events ORDER BY id DESC LIMIT 40")
    events = [
        {"bin_id": row[0], "event_type": row[1], "description": row[2],
         "time_str": time.strftime("%H:%M:%S", time.localtime(row[3]))}
        for row in c.fetchall()
    ]
    conn.close()
    return {"telemetry_logs": logs, "alert_events": events}

# Thuật toán Dijkstra tìm đường ngắn nhất qua các nút giao lộ cục bộ
def dijkstra_shortest_path(graph, start, target):
    pq = [(0, start, [start])]
    visited = set()
    while pq:
        cost, node, path = heapq.heappop(pq)
        if node == target:
            return cost, path
        if node in visited:
            continue
        visited.add(node)
        for neighbor, weight in graph.get(node, {}).items():
            if neighbor not in visited:
                heapq.heappush(pq, (cost + weight, neighbor, path + [neighbor]))
    return float("inf"), []

def calc_tour_cost(tour, cost_matrix):
    return sum(cost_matrix[tour[k]][tour[k + 1]] for k in range(len(tour) - 1))

# Thuật toán TSP: Nearest Neighbor + 2-opt
def solve_tsp_nn_and_2opt(nodes, cost_matrix):
    """Fixed start (index 0), fixed DEPOT finish (last index).

    Optimize only unvisited bins. 2-opt never moves either endpoint.
    Cost is recomputed on the full directed Dijkstra matrix after reversal.
    """
    n = len(nodes)
    if n < 2:
        raise ValueError("Route needs both start and DEPOT finish")
    finish = n - 1
    unvisited = set(range(1, finish))
    nn_tour = [0]
    curr = 0
    while unvisited:
        next_node = min(unvisited, key=lambda j: (cost_matrix[curr][j], nodes[j]["id"]))
        nn_tour.append(next_node)
        unvisited.remove(next_node)
        curr = next_node
    nn_tour.append(finish)

    def run_2opt(tour):
        best = tour[:]
        improved = True
        while improved:
            improved = False
            for i in range(1, n - 2):
                for j in range(i + 1, n - 1):
                    candidate = best[:i] + best[i:j + 1][::-1] + best[j + 1:]
                    if calc_tour_cost(candidate, cost_matrix) < calc_tour_cost(best, cost_matrix):
                        best = candidate
                        improved = True
        return best

    best_2opt = run_2opt(nn_tour)
    sequential_2opt = run_2opt(list(range(n)))
    if calc_tour_cost(sequential_2opt, cost_matrix) < calc_tour_cost(best_2opt, cost_matrix):
        best_2opt = sequential_2opt
    return nn_tour, best_2opt

# 5. API Lập tuyến thu gom tối ưu
@app.post("/api/routes/optimize")
def optimize_route(_authorized: Optional[dict] = Depends(require_read_access)):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT bin_id, name, lat, lng, fill_percent, status, is_priority FROM bins ORDER BY lat DESC")
    all_bins = c.fetchall()
    c.execute("SELECT bin_id, action_status FROM trip_stops")
    completed_or_skipped = {r[0]: r[1] for r in c.fetchall()}

    # P1-3: No live GPS exists in this mock app. Use the most recently CONFIRMED
    # COLLECTED location as a simulated vehicle position. A skipped stop does NOT
    # move the truck, because it may have been skipped without driving there.
    c.execute("""SELECT bin_id FROM trip_stops WHERE action_status = 'COLLECTED'
                 ORDER BY updated_at DESC, rowid DESC LIMIT 1""")
    last_collected = c.fetchone()
    start_id = last_collected[0] if last_collected else "DEPOT"
    if start_id not in GRAPH_NODES:
        conn.close()
        raise HTTPException(status_code=409, detail="Unknown last collected location; reset trip")
    start_node = {"id": start_id, "name": GRAPH_NODES[start_id]["name"],
                  "lat": GRAPH_NODES[start_id]["lat"], "lng": GRAPH_NODES[start_id]["lng"]}

    selected = []
    for b in all_bins:
        bid, name, lat, lng, fill, status, prio = b
        if bid in completed_or_skipped:
            continue
        if status == "NEED_COLLECTION" or prio == 1:
            selected.append({"id": bid, "name": name, "lat": lat, "lng": lng,
                             "fill": fill, "is_priority": bool(prio)})

    if not selected and start_id == "DEPOT":
        conn.close()
        return {"message": "Không còn thùng cần thu gom; xe đang ở DEPOT.",
                "ordered_stops": [], "legs": [], "current_location_id": "DEPOT",
                "current_location_name": start_node["name"], "remaining_bins": 0,
                "return_only": False, "location_source": "SIMULATED_FROM_CONFIRMATIONS",
                "distance_source": "LOCAL_DIRECTED_WEIGHTED_GRAPH",
                "comparison_baseline": "SEQUENTIAL_SELECTED_ORDER",
                "graph_audit": audit_local_graph(LOCAL_GRAPH)}

    # Validate the hand-entered graph before reporting model-distance metrics.
    try:
        graph_audit = audit_local_graph(LOCAL_GRAPH)
    except ValueError as exc:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Invalid local graph: {exc}") from exc

    # Start at the current confirmed position, visit all remaining bins,
    # and FINISH at DEPOT. Never include a trip back to the start bin.
    nodes = [start_node] + selected + [dict(DEPOT)]
    n = len(nodes)
    cost_matrix = [[0.0] * n for _ in range(n)]
    path_details = {}
    for i in range(n):
        for j in range(n):
            if i != j:
                cost, sub_path = dijkstra_shortest_path(LOCAL_GRAPH, nodes[i]["id"], nodes[j]["id"])
                if not math.isfinite(cost) or not sub_path:
                    conn.close()
                    raise HTTPException(status_code=409,
                                        detail=f"No graph path: {nodes[i]['id']} to {nodes[j]['id']}")
                cost_matrix[i][j] = cost
                path_details[(nodes[i]["id"], nodes[j]["id"])] = sub_path

    unoptimized_order = list(range(n))
    unoptimized_cost = calc_tour_cost(unoptimized_order, cost_matrix)
    nn_tour, best_tour_indices = solve_tsp_nn_and_2opt(nodes, cost_matrix)
    nn_cost = calc_tour_cost(nn_tour, cost_matrix)
    optimized_cost = calc_tour_cost(best_tour_indices, cost_matrix)

    ordered_nodes = []
    full_graph_polyline = []
    legs = []
    for k, idx in enumerate(best_tour_indices):
        node_info = dict(nodes[idx])
        if k > 0:
            prev_id = nodes[best_tour_indices[k - 1]]["id"]
            curr_id = node_info["id"]
            leg_cost = cost_matrix[best_tour_indices[k - 1]][idx]
            sub_nodes = path_details[(prev_id, curr_id)]
            legs.append({"step": k, "from_id": prev_id, "to_id": curr_id,
                         "to_name": node_info["name"], "distance_m": leg_cost,
                         "via_nodes": " ➔ ".join(sub_nodes)})
            for sn in sub_nodes:
                coord = [GRAPH_NODES[sn]["lat"], GRAPH_NODES[sn]["lng"]]
                if not full_graph_polyline or full_graph_polyline[-1] != coord:
                    full_graph_polyline.append(coord)
        ordered_nodes.append(node_info)
    conn.close()
    return {
        "current_location_id": start_id,
        "current_location_name": start_node["name"],
        "location_source": "SIMULATED_FROM_CONFIRMATIONS",
        "distance_source": "LOCAL_DIRECTED_WEIGHTED_GRAPH",
        "comparison_baseline": "SEQUENTIAL_SELECTED_ORDER",
        "graph_audit": graph_audit,
        "remaining_bins": len(selected),
        "return_only": len(selected) == 0,
        "message": "Đã thu gom tất cả thùng trong chuyến; xe cần về DEPOT." if not selected else "",
        "unoptimized_distance_m": unoptimized_cost,
        "nn_distance_m": nn_cost,
        "optimized_distance_m": optimized_cost,
        # This percent compares ONE local-graph sequential baseline,
        # not OSRM and not a proven globally optimal real-world route.
        "saved_percent": round((1 - optimized_cost / unoptimized_cost) * 100, 1) if unoptimized_cost > 0 else 0,
        "ordered_stops": ordered_nodes,
        "legs": legs,
        "graph_polyline": full_graph_polyline
    }

@app.post("/api/routes/confirm")
def confirm_collection(req: ConfirmInput, _authorized: None = Depends(require_admin_key)):
    with p1_5_db_transaction() as conn:
        c = conn.cursor()
        c.execute("SELECT is_priority FROM bins WHERE bin_id = ?", (req.bin_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Bin not found")
        c.execute("SELECT action_status, skip_reason FROM trip_stops WHERE bin_id = ?", (req.bin_id,))
        prior = c.fetchone()
        if prior and prior[0] == req.action and (prior[1] or "") == req.reason:
            return {"status": "duplicate_ignored", "bin_id": req.bin_id, "action": req.action}
        now = time.time()
        c.execute("INSERT OR REPLACE INTO trip_stops VALUES (?,?,?,?,?)",
                  (0, req.bin_id, req.action, req.reason, now))
        if req.action == "COLLECTED":
            if row[0]:
                c.execute("UPDATE bins SET is_priority=0 WHERE bin_id=?", (req.bin_id,))
                log_alert_event(c, req.bin_id, "PRIORITY_CLEARED",
                                "Đã thu gom; đóng yêu cầu ưu tiên", now)
            log_alert_event(c, req.bin_id, "COLLECTED",
                            "Tài xế xác nhận đã thu gom; chưa xác nhận thùng rỗng bằng cảm biến", now)
        else:
            log_alert_event(c, req.bin_id, "SKIPPED", req.reason, now)
        conn.commit()
        return {"status": "confirmed", "bin_id": req.bin_id, "action": req.action}


@app.post("/api/routes/reset")
def reset_trip(_authorized: None = Depends(require_admin_key)):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM trip_stops")
    # Reset chuyen khong duoc gia tao thoi gian cap nhat cam bien.
    conn.commit()
    conn.close()
    return {"status": "trip_reset"}

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        return f.read()
