# P4 - Thay công tắc giả lập bằng cảm biến Wokwi thực

## Phạm vi thay đổi

- **Độ nghiêng:** MPU6050 I2C (SDA GPIO21, SCL GPIO22, VCC 3V3, GND), đọc gia tốc 3 trục. Tính góc so với hướng dựng thẳng +Z. Báo khi `>=30 độ` ổn định `>=600ms`, chỉ hết báo khi `<20 độ` ổn định `>=600ms`, lọc mượt EMA. Trạng thái đảo ngược gần 180 độ vẫn được nhận là nghiêng.
- **Nguy cơ cháy:** MQ-2 gas sensor (VCC 5V/VIN, GND, DOUT GPIO13 **qua cầu chia áp 1k/2k** vì ESP32 là logic 3.3V). MQ-2 phát hiện **khí dễ cháy**, không phát hiện ngọn lửa trực tiếp. DOUT xuống LOW khi vượt ngưỡng của mô hình cảm biến. Giữ chống rung 80ms và mã cảnh báo `fire_detected`/`FIRE_DETECTED` của backend để tương thích P1/P2/P3. Trong báo cáo phải gọi đó là cảnh báo *nguy cơ cháy do khí*, không khẳng định đã phát hiện ngọn lửa.
- **Không thay đổi:** HC-SR04, 3 LED, nút ưu tiên GPIO14, công tắc **lỗi cảm biến siêu âm** GPIO33, định dạng JSON, `message_id`, NVS outbox, ACK/retry, backend, SQLite, JWT/RBAC, trang Dashboard.
- **Sự cố MPU6050:** ghi log và thử kết nối lại sau 5s; không tự suy diễn nguy hiểm từ dữ liệu lỗi. Cần xử lý thiết bị lỗi cảm biến nghiêng riêng trước khi triển khai thực tế.

## Cài bản nâng cấp (PowerShell Windows)

1. Giải nén `P4_Sensors_Upgrade_Apply.zip` bên cạnh dự án, ví dụ `D:\Internet of things\P4_Sensors_Upgrade_Apply\`.
2. Mở PowerShell trong thư mục repo `D:\Internet of things\iot_quanLyRacThai`.
3. Kiểm tra trạng thái và tạo nhánh mới nếu muốn: `git status -sb` rồi `git switch -c feat/p4-real-sensors` (những thay đổi P2/P3 chưa commit vẫn đi theo nhánh).
4. Chạy:

```powershell
$script = Get-ChildItem "D:\Internet of things\P4_Sensors_Upgrade_Apply" -Filter "apply_p4_sensors.py" -Recurse -File | Select-Object -First 1 -ExpandProperty FullName
python $script (Get-Location).Path
git status -sb
```

**Bộ cài chỉ ghi firmware, sơ đồ Wokwi, header math, tài liệu và scenario mới**. Mọi file firmware/sơ đồ đang có chỉnh sửa khác bản chuẩn đều bị từ chối nhằm tránh mất code; khi đó gửi nội dung để ghép thủ công. Backup đặt ngoài repo tại `D:\Internet of things\iot_p4_backup\P4_SENSORS_*`. Không đụng DB hay credentials.

## Test tự động

Chạy trong repo sau khi apply (đã cài `.venv` của backend):

```powershell
$env:IOT_P2_PROJECT = (Get-Location).Path
$env:IOT_P3_PROJECT = (Get-Location).Path
$testdir = (Get-ChildItem "D:\Internet of things\P4_Sensors_Upgrade_Apply" -Filter "test_p4_upgrade.py" -Recurse -File | Select-Object -First 1).Directory.FullName
python (Join-Path $testdir "test_p4_upgrade.py")
python (Join-Path $testdir "test_p2_stage1_updated.py")
$p3testdir = (Get-ChildItem "D:\Internet of things\P3_Stage3_Apply" -Filter "test_p3_cloud_security.py" -Recurse -File | Select-Object -First 1).Directory.FullName
python (Join-Path $p3testdir "test_p2_stage2.py")
python (Join-Path $p3testdir "test_p3_cloud_security.py")
Remove-Item Env:\IOT_P2_PROJECT
Remove-Item Env:\IOT_P3_PROJECT
```

Chú ý: test Stage1 cũ trong gói P3 đã *cố ý* kiểm tra hai công tắc `sw_tilt`/`sw_fire`. Nó không còn phù hợp sau khi thay phần cứng, nên dùng `test_p2_stage1_updated.py` trong gói P4. Phần API/backend Stage1 vẫn được kiểm tra; chỉ cập nhật kỳ vọng về sơ đồ thiết bị.

Muốn biên dịch firmware: trong `esp32_node\esp32_node`, mở PlatformIO VS Code → Build, hoặc `pio run`. `Wire.h` có sẵn trong Arduino ESP32; bản P4 không cài thêm thư viện.

## Test Wokwi thủ công

1. Chạy backend hiện có với P3 (`uvicorn backend_server.app:app --host 0.0.0.0 --port 8000`). Mở Dashboard tại `http://127.0.0.1:8000`. Không mở cổng 8000 ra Internet.
2. Mở dự án `esp32_node\esp32_node` với Wokwi for VS Code và Start Simulation, xem Serial Monitor. Cần thấy `[MPU] MPU6050 OK (I2C 0x68)`; nếu không thấy, kiểm tra dây SDA21/SCL22 và 3V3/GND.
3. **Nghiêng:** MPU6050 mặc định `accelX=0`, `accelY=0`, `accelZ=1g`: không cảnh báo. Dùng automation scenario `p4_tilt.scenario.yaml` (Wokwi CLI có token) để đổi `accelX=0.8`, `accelZ=0.6` → góc khoảng 53°, sau khoảng 0.6–1.4s thấy `[HAZARD] TILT=1`, LED vàng nhấp nháy và Dashboard nhận `TILT_DETECTED`. Trả về (0,0,1) → `TILT_CLEARED`. Nếu không có Wokwi CLI, sửa `attrs` của `imu_tilt` trong `diagram.json` rồi **khởi động lại** mô phỏng để kiểm tra từng trạng thái.
4. **Khí dễ cháy:** mặc định `mq2_gas` với `ppm=400`, `threshold=4.4` không báo. Trong simulator bấm linh kiện MQ2, tăng nồng độ khí hoặc chỉnh `ppm` trong `diagram.json` rồi restart; xác nhận DOUT xuống LOW và Serial có `GAS_FIRE_RISK=1`, LED đỏ nhấp nháy, backend có `FIRE_DETECTED`. Giảm về mức bình thường → `FIRE_CLEARED`. Ngưỡng thực tế phụ thuộc model MQ2, không chọn ngẫu nhiên một ppm để kết luận mức cháy.
5. **Thử đồng thời:** nghiêng + MQ2 vượt ngưỡng → cả `tilt_detected` và `fire_detected` đều true, ưu tiên hiển thị LED đỏ. Nút khẩn cấp và phần tính % đầy không thay đổi.
6. **Mất mạng:** giữ Wokwi chạy, tắt backend; tạo cảnh báo → `[QUEUE]` lưu NVS. Bật lại backend, đợi `[ACK]`, đối chiếu nhật ký trên Dashboard; không được có bản ghi trùng `message_id`.

## Những điều CHƯA triển khai

- KY-026 cảm biến hồng ngoại ngọn lửa không nằm trong danh sách linh kiện Wokwi chuẩn (gói này sử dụng MQ2 *khí dễ cháy*). Muốn nhận biết **ngọn lửa thật**, cần thêm KY-026 hoặc mô hình cảm biến tương đương trên phần cứng/custom chip và xác minh thực tế; MQTT, NB-IoT, LoRaWAN và STM32 cũng chưa triển khai trên thiết bị thật.
- Không thể coi mô phỏng Wokwi hoặc ngưỡng `FIRE_DETECTED` hiện tại là thiết bị an toàn phòng cháy chữa cháy đạt chứng nhận. Cần nguồn, tản nhiệt, hiệu chỉnh, bao che, kiểm định môi trường và kiểm tra thực tế.

Nguồn chuẩn: https://docs.wokwi.com/parts/wokwi-mpu6050 và https://docs.wokwi.com/parts/wokwi-gas-sensor
