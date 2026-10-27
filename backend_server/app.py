import os
import time
import math
import heapq
import sqlite3
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Hệ Thống Quản Lý Rác Thải Thông Minh - Nhóm 15 HUIT")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "smart_waste.db")
HTML_FILE = os.path.join(BASE_DIR, "index.html")

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
        last_updated REAL
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
    c.execute('''CREATE TABLE IF NOT EXISTS trip_stops (
        stop_order INTEGER,
        bin_id TEXT PRIMARY KEY,
        action_status TEXT,
        skip_reason TEXT,
        updated_at REAL
    )''')

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
        c.executemany("INSERT INTO bins VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", sample_bins)
    conn.commit()
    conn.close()

init_db()

class TelemetryInput(BaseModel):
    device_id: str
    bin_id: str
    message_id: str
    measured_at: int
    distance_cm: float
    fill_percent: float
    sensor_status: str
    is_priority: Optional[bool] = False

class SimulateBinInput(BaseModel):
    bin_id: str
    distance_cm: float
    sensor_status: Optional[str] = "OK"
    is_priority: Optional[bool] = False

class ConfirmInput(BaseModel):
    bin_id: str
    action: str
    reason: Optional[str] = ""

def classify_fill(fill: float) -> str:
    if fill >= 80.0:
        return "NEED_COLLECTION"
    elif fill >= 60.0:
        return "NEAR_FULL"
    return "NORMAL"

def calc_fill_from_dist(dist: float) -> float:
    pct = ((100.0 - dist) / (100.0 - 20.0)) * 100.0
    return round(max(0.0, min(100.0, pct)), 1)

# 1. API Nhận bản tin từ mạch Wokwi ESP32
@app.post("/api/telemetry")
def receive_telemetry(data: TelemetryInput):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("SELECT message_id FROM telemetry_logs WHERE message_id = ?", (data.message_id,))
    if c.fetchone():
        conn.close()
        return {"status": "duplicate_ignored", "message_id": data.message_id}

    now = time.time()
    c.execute("INSERT INTO telemetry_logs VALUES (?,?,?,?,?,?,?)",
              (data.message_id, data.bin_id, data.device_id, data.distance_cm, data.fill_percent, data.sensor_status, now))

    c.execute("SELECT status, pending_status, consecutive_count, fill_percent, is_priority FROM bins WHERE bin_id = ?", (data.bin_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Bin not found")

    current_status, pending_status, consecutive_count, old_fill, old_prio = row
    new_prio = 1 if data.is_priority else old_prio

    if data.sensor_status != "OK" or data.distance_cm < 0:
        c.execute("UPDATE bins SET sensor_status = 'SENSOR_ERROR', is_priority = ?, last_updated = ? WHERE bin_id = ?",
                  (new_prio, now, data.bin_id))
        conn.commit()
        conn.close()
        return {"status": "sensor_error_logged", "bin_id": data.bin_id}

    candidate_status = classify_fill(data.fill_percent)
    if candidate_status == pending_status:
        consecutive_count += 1
    else:
        pending_status = candidate_status
        consecutive_count = 1

    new_status = candidate_status if consecutive_count >= 2 else current_status

    c.execute('''UPDATE bins SET distance_cm=?, fill_percent=?, status=?, sensor_status='OK',
                 pending_status=?, consecutive_count=?, is_priority=?, last_updated=? WHERE bin_id=?''',
              (data.distance_cm, data.fill_percent, new_status, pending_status, consecutive_count, new_prio, now, data.bin_id))
    conn.commit()
    conn.close()
    return {"status": "accepted", "bin_id": data.bin_id, "confirmed_status": new_status, "consecutive": consecutive_count}

# 2. API Giả lập trực tiếp các nút IoT (BIN_01 .. BIN_06) từ bảng điều khiển Web
@app.post("/api/simulate/bin")
def simulate_bin_node(req: SimulateBinInput):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = time.time()
    msg_id = f"SIM_{req.bin_id}_{int(now * 1000)}"

    if req.sensor_status != "OK" or req.distance_cm < 0:
        c.execute("UPDATE bins SET sensor_status = ?, is_priority = ?, last_updated = ? WHERE bin_id = ?",
                  (req.sensor_status, 1 if req.is_priority else 0, now, req.bin_id))
        c.execute("INSERT INTO telemetry_logs VALUES (?,?,?,?,?,?,?)",
                  (msg_id, req.bin_id, f"SIM_{req.bin_id}", -1.0, -1.0, req.sensor_status, now))
    else:
        fill = calc_fill_from_dist(req.distance_cm)
        st = classify_fill(fill)
        c.execute('''UPDATE bins SET distance_cm=?, fill_percent=?, status=?, sensor_status='OK',
                     pending_status=?, consecutive_count=2, is_priority=?, last_updated=? WHERE bin_id=?''',
                  (req.distance_cm, fill, st, st, 1 if req.is_priority else 0, now, req.bin_id))
        c.execute("INSERT INTO telemetry_logs VALUES (?,?,?,?,?,?,?)",
                  (msg_id, req.bin_id, f"SIM_{req.bin_id}", req.distance_cm, fill, "OK", now))
    conn.commit()
    conn.close()
    return {"status": "simulated", "bin_id": req.bin_id}

# 3. API Truy vấn danh sách thùng rác + Đồ thị cục bộ
@app.get("/api/bins")
def get_bins():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT bin_id, device_id, name, lat, lng, distance_cm, fill_percent, status, sensor_status, is_priority, last_updated, consecutive_count FROM bins ORDER BY bin_id ASC")
    rows = c.fetchall()
    c.execute("SELECT bin_id, action_status, skip_reason FROM trip_stops")
    stops = {r[0]: {"action_status": r[1], "skip_reason": r[2]} for r in c.fetchall()}
    conn.close()

    now = time.time()
    result = []
    for r in rows:
        is_offline = (r[0] == "BIN_01") and ((now - r[10]) > 60.0)
        seconds_ago = int(now - r[10]) if r[0] == "BIN_01" else int(now % 12) + 1
        result.append({
            "bin_id": r[0], "device_id": r[1], "name": r[2], "lat": r[3], "lng": r[4],
            "distance_cm": r[5], "fill_percent": r[6], "status": r[7],
            "sensor_status": "OFFLINE" if is_offline else r[8],
            "is_priority": bool(r[9]),
            "seconds_ago": seconds_ago,
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
def get_system_logs():
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
    conn.close()
    return {"telemetry_logs": logs}

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
    n = len(nodes)
    unvisited = set(range(1, n))
    nn_tour = [0]
    curr = 0
    while unvisited:
        next_node = min(unvisited, key=lambda x: cost_matrix[curr][x])
        nn_tour.append(next_node)
        unvisited.remove(next_node)
        curr = next_node
    nn_tour.append(0)

    def run_2opt(tour):
        best = tour[:]
        improved = True
        while improved:
            improved = False
            for i in range(1, n - 1):
                for j in range(i + 1, n):
                    new_tour = best[:i] + best[i:j + 1][::-1] + best[j + 1:]
                    if calc_tour_cost(new_tour, cost_matrix) < calc_tour_cost(best, cost_matrix):
                        best = new_tour
                        improved = True
        return best

    best_2opt = run_2opt(nn_tour)
    seq_2opt = run_2opt(list(range(n)) + [0])
    if calc_tour_cost(seq_2opt, cost_matrix) < calc_tour_cost(best_2opt, cost_matrix):
        best_2opt = seq_2opt

    return nn_tour, best_2opt

# 5. API Lập tuyến thu gom tối ưu
@app.post("/api/routes/optimize")
def optimize_route():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT bin_id, name, lat, lng, fill_percent, status, is_priority FROM bins ORDER BY lat DESC")
    all_bins = c.fetchall()
    c.execute("SELECT bin_id, action_status FROM trip_stops")
    completed_or_skipped = {r[0]: r[1] for r in c.fetchall()}

    selected = []
    for b in all_bins:
        bid, name, lat, lng, fill, status, prio = b
        if bid in completed_or_skipped:
            continue
        if status == "NEED_COLLECTION" or prio == 1:
            selected.append({"id": bid, "name": name, "lat": lat, "lng": lng, "fill": fill, "is_priority": bool(prio)})

    if not selected:
        conn.close()
        return {"message": "Không có thùng rác nào vượt ngưỡng cần thu gom!", "ordered_stops": []}

    nodes = [DEPOT] + selected
    n = len(nodes)

    cost_matrix = [[0.0] * n for _ in range(n)]
    path_details = {}
    for i in range(n):
        for j in range(n):
            if i != j:
                cost, sub_path = dijkstra_shortest_path(LOCAL_GRAPH, nodes[i]["id"], nodes[j]["id"])
                cost_matrix[i][j] = cost
                path_details[(nodes[i]["id"], nodes[j]["id"])] = sub_path

    # Quãng đường tuần tự chưa tối ưu
    unoptimized_order = list(range(n)) + [0]
    unoptimized_cost = calc_tour_cost(unoptimized_order, cost_matrix)

    # Quãng đường Nearest Neighbor và Nearest Neighbor + 2-opt
    nn_tour, best_tour_indices = solve_tsp_nn_and_2opt(nodes, cost_matrix)
    nn_cost = calc_tour_cost(nn_tour, cost_matrix)
    optimized_cost = calc_tour_cost(best_tour_indices, cost_matrix)

    ordered_nodes = []
    full_graph_polyline = []
    legs = []

    for k in range(len(best_tour_indices)):
        idx = best_tour_indices[k]
        node_info = dict(nodes[idx])
        if k > 0:
            prev_id = nodes[best_tour_indices[k - 1]]["id"]
            curr_id = node_info["id"]
            leg_cost = cost_matrix[best_tour_indices[k - 1]][idx]
            sub_nodes = path_details.get((prev_id, curr_id), [prev_id, curr_id])
            legs.append({
                "step": k,
                "from_id": prev_id,
                "to_id": curr_id,
                "to_name": node_info["name"],
                "distance_m": leg_cost,
                "via_nodes": " ➔ ".join(sub_nodes)
            })
            for sn in sub_nodes:
                coord = [GRAPH_NODES[sn]["lat"], GRAPH_NODES[sn]["lng"]]
                if not full_graph_polyline or full_graph_polyline[-1] != coord:
                    full_graph_polyline.append(coord)
        ordered_nodes.append(node_info)

    conn.close()
    return {
        "unoptimized_distance_m": unoptimized_cost,
        "nn_distance_m": nn_cost,
        "optimized_distance_m": optimized_cost,
        "saved_percent": round((1 - optimized_cost / unoptimized_cost) * 100, 1) if unoptimized_cost > 0 else 0,
        "ordered_stops": ordered_nodes,
        "legs": legs,
        "graph_polyline": full_graph_polyline
    }

@app.post("/api/routes/confirm")
def confirm_collection(req: ConfirmInput):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO trip_stops VALUES (?,?,?,?,?)",
              (0, req.bin_id, req.action, req.reason, time.time()))
    conn.commit()
    conn.close()
    return {"status": "confirmed", "bin_id": req.bin_id, "action": req.action}

@app.post("/api/routes/reset")
def reset_trip():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM trip_stops")
    c.execute("UPDATE bins SET last_updated = ?", (time.time(),))
    conn.commit()
    conn.close()
    return {"status": "trip_reset"}

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        return f.read()