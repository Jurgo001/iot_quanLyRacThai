# P2 — Giai đoạn 1: Hardware & Embedded (Nhóm 15)

**Nguồn chuẩn:** ZIP người dùng gửi, Git commit `6024a9a` (P0 + P1-1…P1-5).  
**Nền tảng đang chạy:** ESP32 DevKit trên Wokwi, HTTP/Wi-Fi, FastAPI, SQLite và Dashboard.  
**Phạm vi:** Bản nâng cấp tương ứng các yêu cầu Câu 2A trên mô hình ESP32. **Chưa thay bằng STM32 hoặc thiết bị đo cháy thực tế.**

## 1. Đã triển khai được gì?

| Mục | Mã nguồn/đầu ra | Kết quả |
|---|---|---|
| P2-1 | `src/main.cpp` | Giữ Median 5 mẫu của P0, thêm trung bình trượt 3 trung vị; khởi tạo không lệch, xóa mẫu cũ khi cảm biến lỗi |
| P2-2 | `main.cpp`, `diagram.json` | Thêm 2 công tắc Wokwi mô phỏng tín hiệu số từ cảm biến nghiêng và báo cháy; khử dội 80 ms; LED báo trạng thái |
| P2-3 | `backend_server/app.py`, `index.html` | Bổ sung cờ nghiêng/cháy, lịch sử cảnh báo và tự phục hồi; kiểm tra kiểu bool, chống bản tin trùng, bảo vệ BIN_01 |
| P2-4 | Tài liệu này | Phương án RTC/external wakeup/đóng cắt nguồn; **không bật Deep Sleep trong bản demo liên tục** |
| P2-5 | Tài liệu + kiểm thử | Bảng chân, cách vẽ mạch, công thức pin, kiểm thử API trên database tạm và checklist Wokwi |

Tính năng hiện hữu P0–P1 giữ nguyên: 3 ngưỡng LED theo mức đầy, nút khẩn cấp BIN_01, Wi-Fi retry, FIFO 24 bản tin, API key, `message_id`, tuyến Dijkstra/NN/2-opt, xe mô phỏng, ưu tiên và vòng đời sự kiện.

## 2. Sơ đồ kết nối ESP32 (đúng `diagram.json`)

| Chức năng | Chân ESP32 | Nối dây / lưu ý |
|---|---|---|
| HC-SR04 TRIG | GPIO5 | TRIG trực tiếp |
| HC-SR04 ECHO | GPIO18 | ECHO 5 V qua phân áp 1 kΩ nối tiếp và 2 kΩ xuống GND, còn ~3,33 V |
| LED xanh/vàng/đỏ | GPIO25/26/27 | Mỗi LED có điện trở 220 Ω nối tiếp |
| Nút ưu tiên | GPIO14 | Nút nhấn xuống GND, `INPUT_PULLUP` |
| Mô phỏng lỗi HC-SR04 | GPIO33 | Công tắc trượt GND–GPIO–3V3, vị trí nối GND = lỗi |
| Mô phỏng thùng nghiêng | GPIO32 | Công tắc trượt GND–GPIO–3V3, vị trí nối GND = phát hiện nghiêng |
| Mô phỏng nguy cơ cháy | GPIO13 | Công tắc trượt GND–GPIO–3V3, vị trí nối GND = phát hiện cháy |

**Vẽ mạch trên giấy:** đặt khối ESP32 ở giữa; HC-SR04 ở bên phải phía trên; ba LED ở bên trái; nút khẩn cấp và ba công tắc ở bên phải phía dưới. Ghi phân áp tại dây ECHO. Vẽ thêm khối `[Nguồn 5V] → [High-side load switch/MOSFET] → [Nguồn HC-SR04]`, điều khiển bằng GPIO qua mạch driver phù hợp. Đây là **phương án phần cứng**, không phải phần nguồn đã thi công trên Wokwi.

### Về cách ly quang và đóng cắt nguồn

- Cảm biến HC-SR04 và ESP32 có chung GND trong mô phỏng; phân áp ECHO đã xử lý mức 5 V sang 3,3 V.
- Nếu triển khai cảm biến/chấp hành ở dây dài hoặc nguồn riêng, có thể dùng PC817 **khi thiết kế đầy đủ miền nguồn và đường hồi dòng riêng**. Chỉ đặt một PC817 vào đường tín hiệu rồi vẫn nối chung mọi mass thì không được coi là cách ly điện hoàn chỉnh.
- Có thể dùng MOSFET kênh P hoặc load-switch phía nguồn cao để ngắt 5 V cảm biến khi ngủ; phải ngăn cấp nguồn ngược qua TRIG/ECHO. Cần kiểm tra lại mức logic và nguồn bằng phần cứng thật.

## 3. P2-1 — Median + Moving Average

1. Mỗi 60 ms, firmware thu thập một phép đo siêu âm (đã có ở P0).
2. Sau 5 lần lấy mẫu, loại giá trị không hợp lệ và chọn **trung vị** của các mẫu hợp lệ (tối thiểu 3).
3. Thêm **trung bình trượt 3 giá trị trung vị gần nhất**.
4. Tính `fill_percent = clamp((100 - distance_cm) × 100 / 80, 0, 100)`.
5. Trạng thái lỗi cảm biến: xóa bộ làm mượt để lần phục hồi không sử dụng dữ liệu cũ.

Ví dụ năm mẫu `40, 41, 150, 39, 40` (cm) có trung vị **40 cm**, vì 150 là nhiễu xung. Nếu ba trung vị gần đây là 40, 42, 41 thì giá trị trung bình trượt là **41 cm**.

**Đánh đổi:** lọc số tạo độ trễ nhỏ khi mức rác thực sự thay đổi đột ngột. Trên Wokwi, hãy đợi vài chu kỳ sau khi kéo thanh cảm biến rồi mới kết luận đèn hoặc Dashboard bị sai.

## 4. P2-2/P2-3 — Cảnh báo độc lập

- Wokwi GPIO32/GPIO13 phát hiện thay đổi trạng thái đã khử dội; hai trường `tilt_detected`, `fire_detected` là **boolean thực** trong JSON.
- Khi bật/tắt, firmware đẩy bản tin chuyển trạng thái vào FIFO với mức giữ ưu tiên; **không tự thay `is_priority` của nút khẩn cấp**.
- SQLite `bins` được bổ sung hai cột `INTEGER DEFAULT 0` bằng `ALTER TABLE` an toàn khi chạy backend lần đầu. Dữ liệu cũ được giữ nguyên.
- Bảng `alert_events` ghi `TILT_DETECTED`, `TILT_CLEARED`, `FIRE_DETECTED`, `FIRE_CLEARED`. Một `message_id` phát lại không được tạo sự kiện trùng.
- Dashboard hiển thị cảnh báo riêng và `CRITICAL`; mức đầy, thời gian đọc cảm biến, nút ưu tiên, vị trí xe không bị chỉnh giả.
- **Quan trọng:** cảnh báo cháy không mặc định đưa xe thu gom rác tới hiện trường. Cần quy trình an toàn và người điều phối; thuật toán chọn thùng của P1 không bị thay đổi.

### Quy tắc đèn khi có nhiều trạng thái

1. Có nguy cơ cháy: **LED đỏ nhấp nháy nhanh**; Web hiển thị nguy cơ cháy.
2. Nếu không cháy nhưng thùng nghiêng: **LED vàng nhấp nháy**.
3. Nếu không nghiêng/cháy nhưng cảm biến lỗi: **LED đỏ nhấp nháy** như P0.
4. Các trường hợp bình thường: xanh khi `<60%`, vàng khi `60–<80%`, đỏ khi `>=80%`.

Các công tắc mô phỏng không đo được nhiệt độ, khói thật, góc nghiêng, và không phải hệ thống báo cháy đạt chứng nhận.

## 5. P2-4 — Phương án quản lý nguồn

### Chế độ demo hiện tại

Giữ firmware chạy liên tục để đèn, Wi-Fi, nút khẩn cấp và FIFO 24 bản tin hoạt động đúng. **Không gọi `esp_deep_sleep_start()` trong bản demo này.** Dữ liệu FIFO đang ở RAM có thể mất khi khởi động lại; việc lưu bền vững sẽ thuộc giai đoạn 2.

### Chế độ triển khai ngoài thực tế (thiết kế, chưa kiểm nghiệm)

`RTC wakeup → đo cảm biến đã cấp nguồn → lọc → gửi bản tin + nhận ACK → ngắt nguồn cảm biến → Sleep`. Tín hiệu khẩn cấp phải có đường đánh thức phù hợp. GPIO32 và GPIO13 là các chân RTC trên ESP32 gốc, nhưng cấu hình `ext0`/`ext1`, điện trở kéo và cực tính ngắt phải được thiết kế và đo lại. Với thiết bị thật, cần lưu bản tin chưa gửi trên Flash/FRAM trước khi ngủ; đồng thời cần giải pháp giám sát cháy độc lập luôn hoạt động nếu muốn cảnh báo 24/7.

### Ví dụ tính pin **minh họa, không phải số đo thực tế**

Giả sử hệ thống tối ưu ngoài thực tế: mỗi 15 phút thức 10 giây với `I_active=120 mA`, ngủ 890 giây với `I_sleep=0,2 mA`; thêm đầu vào báo động luôn bật `I_alarm=0,05 mA` (giả định dùng cảm biến công suất thấp, **không phải MQ-2**).

`I_avg = (120×10 + 0,2×890)/900 + 0,05 = 1,581 mA`.

Dung lượng pin cho 365 ngày, hệ số dự phòng `K=1,3`:

`C_battery = 1,581 × 24 × 365 × 1,3 ≈ 18.005 mAh`.

Vậy chọn nguồn danh định khoảng **18 Ah trở lên trong kịch bản giả định**, còn phải xét hiệu suất DC-DC, dòng đỉnh Wi-Fi, hóa học pin, nhiệt độ và mức suy hao. Con số trên **không chứng minh DevKit thực tế sẽ chạy được một năm**.

MQ-2 dùng bộ gia nhiệt 5 V; nếu giả định heater tiêu thụ liên tục 150 mA thì riêng heater đã cần `150 × 24 × 365 = 1.314.000 mAh` trong một năm (chưa tính ESP32). Không nên hứa dùng pin nhỏ cho MQ-2 giám sát liên tục.

## 6. Kiểm thử tự động và bằng tay

**Test backend:** chạy `test_p2_stage1.py` trong package; script tự tạo thư mục tạm và database tạm, không sửa `backend_server/smart_waste.db`.

**Test thủ công Wokwi + Dashboard (theo đúng thứ tự):**

1. Backend chạy và Wokwi bắt đầu, 2 công tắc mới ở trạng thái bình thường. Xác nhận BIN_01 online, không có cảnh báo nghiêng/cháy.
2. Chỉnh HC-SR04 lần lượt 80 cm (xanh), 45 cm (vàng), 20 cm (đỏ); chờ bộ lọc ổn định; Serial có `median` và `dist_sma`.
3. Gạt **sw_tilt** sang chân GND: LED vàng nhấp nháy nếu không có cháy, Web hiển thị `THÙNG NGHIÊNG`, sự kiện `TILT_DETECTED`.
4. Gạt **sw_fire** sang chân GND: LED đỏ nhấp nháy nhanh, Web hiển thị `NGUY CƠ CHÁY`, sự kiện `FIRE_DETECTED`.
5. Gạt công tắc trở về trạng thái bình thường: Web xóa báo động và log `TILT_CLEARED`, `FIRE_CLEARED`.
6. Bật lại công tắc lỗi cảm biến P0: `SENSOR_ERROR`; đưa lại bình thường: `SENSOR_RECOVERED`. Giá trị đo hồi phục không bị kéo theo trung bình cũ.
7. Nhấn nút khẩn cấp BIN_01: ưu tiên vẫn hoạt động như P1; tắt công tắc cháy/nghiêng **không tự hủy ưu tiên bằng nút**.
8. Dừng backend bằng `Ctrl+C`, thay đổi công tắc, khởi động lại backend: bản tin còn trong FIFO được gửi lại theo P0. Không tắt Wokwi/mất nguồn trong thử nghiệm này vì FIFO vẫn là RAM.
9. Lập tuyến và xác nhận thu gom: kiểm tra thuật toán đường đi, vị trí xe và quy tắc nguồn dữ liệu P1 vẫn hoạt động.

## 7. Giới hạn cần trình bày trung thực

- **Đã mã hóa và test API:** Median + Moving Average, công tắc cảnh báo mô phỏng, DB migration, alert lifecycle, Web hiển thị.
- **Cần người dùng build/test Wokwi trên Windows:** firmware và wiring mới; kết quả test thực tế chưa được xác nhận cho tới khi bạn chạy.
- **Chỉ mới thiết kế trên giấy:** STM32 Low-power, pin hoạt động 1 năm, Deep Sleep/RTC/external wakeup thực, MOSFET power gating/PC817 phần cứng, cảm biến nghiêng/cháy vật lý. Muốn tuyên bố các chức năng này đã triển khai thật cần có board, mạch và phép đo dòng.

Tham khảo kỹ thuật: [ESP32 Sleep Modes — Espressif](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/system/sleep_modes.html), [Đo dòng tiêu thụ ESP32 — Espressif](https://docs.espressif.com/projects/esp-idf/en/release-v5.4/esp32/api-guides/current-consumption-measurement-modules.html), [MQ-2 Hanwei datasheet](https://www.digikey.com/htmldatasheets/production/849192/0/0/1/mq-2.pdf).
