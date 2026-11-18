# P5 — Triển khai Render Free + HTTPS

## Nguyên tắc

- Giữ nguyên ESP32/Wokwi, mạch MPU6050/MQ2, cơ chế hàng đợi P2, SQLite hiện tại và JWT/RBAC P3.
- Render tự cấp HTTPS cho tên miền `*.onrender.com`. Không cần mua tên miền hoặc cài Caddy.
- Render Free có thể ngủ khi không có lượt truy cập; khi khởi động lại, file SQLite cục bộ **không bền vững**.
- Bước này triển khai **Dashboard/API HTTPS**. Chưa đổi firmware Wokwi từ HTTP cục bộ sang HTTPS Cloud.

## Trên máy Windows, trước khi push

1. Đứng tại thư mục gốc `iot_quanLyRacThai`, tạo nhánh `feat/p5-render-cloud` từ `feat/p3-cloud-security`.
2. Chạy `python <duong_dan_giai_nen>/apply_p5_render_cloud.py (Get-Location).Path`.
3. Chạy `python -m pip install -r backend_server/requirements_cloud.txt`.
4. Kiểm tra `git status -sb`. Chỉ commit 4 file P5, không commit file JSON chứa secrets hoặc `.db`.
5. Push nhánh để Render chọn được.

## Render — New > Web Service

- Connect GitHub: `Jurgo001/iot_quanLyRacThai`.
- Branch: `feat/p5-render-cloud`.
- Language: Python 3.
- Root Directory: để trống (repo root).
- Build Command: `pip install -r backend_server/requirements_cloud.txt`.
- Start Command: `uvicorn backend_server.app:app --host 0.0.0.0 --port $PORT`.
- Instance: Free (nếu có).

### Environment Variables — tất cả nhập trên Render, không viết vào Git

| Name | Value |
| --- | --- |
| `IOT_DEVICE_API_KEY` | Chuỗi bí mật ngẫu nhiên dài >=32 ký tự. Muốn ESP32 gửi Cloud, key này phải trùng key của ESP32. |
| `IOT_ADMIN_API_KEY` | Chuỗi bí mật khác dài >=32 ký tự. API admin cũ bị tắt dưới đây. |
| `IOT_P3_JWT_SECRET` | Chuỗi bí mật khác dài >=48 ký tự. |
| `IOT_BOOTSTRAP_ADMIN_USERNAME` | Tên admin riêng (3–32 ký tự, chữ/số/`_`/`.`/`-`). |
| `IOT_BOOTSTRAP_ADMIN_PASSWORD` | Mật khẩu mạnh riêng >=12 ký tự. |
| `IOT_P3_LOCK_READS` | `1` |
| `IOT_P3_DISABLE_LEGACY_ADMIN` | `1` |

Tạo mỗi secret trên máy bằng PowerShell: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
Không gửi giá trị secret/mật khẩu vào chat hoặc ảnh chụp màn hình.

## Kiểm tra

1. Deploy thành công -> `https://<ten-service>.onrender.com/health` trả JSON `{"status":"ok"}`.
2. Mở `https://<ten-service>.onrender.com/`, đăng nhập tài khoản bootstrap.
3. Đăng nhập xem được Dashboard; mở API `/api/bins` không có token bị `401`.
4. Giữ backend cục bộ và Wokwi chạy HTTP như cũ để test; HTTPS từ Wokwi sẽ thực hiện ở giai đoạn kế.

## Quan trọng

- Demo trên Free không cam kết 24/7 và lịch sử SQLite có thể mất khi redeploy/khởi động lại.
- Nếu muốn lưu vĩnh viễn, cần PostgreSQL được hỗ trợ bởi ứng dụng sau khi sửa DAO/SQL, hoặc ổ đĩa bền vững (thường phải trả phí). Tệp `deployment/postgresql_p3_reference.sql` chỉ là tham khảo, chưa đổi backend SQLite sang PostgreSQL.
- Nếu biến môi trường bị lộ, xoay vòng giá trị trên Render; không push các secret lên GitHub.
