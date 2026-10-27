# Chay trong mot cua so VS Code

Mo file `QuanLyRacThai_IoT.code-workspace` o thu muc goc. Workspace gom hai thu muc Backend va ESP32 trong cung mot cua so.

1. Ctrl+Shift+B -> `IoT: Khoi dong he thong`: build ESP32 roi chay web trong Terminal.
2. F1 -> `Wokwi: Start Simulator`: mo mach ESP32 ngay trong cua so nay.
3. F1 -> `Browser: Open Integrated Browser` -> `http://localhost:8000`: mo web trong VS Code.
4. Keo tab web sang ben canh tab Wokwi de xem dong thoi.
5. Bam cam bien HC-SR04 va keo Distance de chinh du lieu BIN_01.

Dung backend: Terminal `IoT: Chay web` -> Ctrl+C.
Dung mo phong: bam Stop trong Wokwi.
Sau khi sua main.cpp: chay task `IoT: Build ESP32` roi restart Wokwi.
Neu web dang chay, khong chay them backend o Terminal khac vi trung cong 8000.

Backend dung `backend_server/.venv/Scripts/python.exe`; PlatformIO dung ban cai trong `.platformio` cua tai khoan Windows hien tai.
Neu mo ca thu muc goc thay vi workspace: F1 -> `Wokwi: Select Config File` -> chon `esp32_node/esp32_node/wokwi.toml` truoc khi chay mo phong.