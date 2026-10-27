#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <esp_system.h> // esp_random(): tao ID phien khoi dong

const char* WIFI_SSID   = "Wokwi-GUEST";
const char* WIFI_PASS   = "";
const char* BACKEND_URL = "http://host.wokwi.internal:8000/api/telemetry";

const char* DEVICE_ID = "ESP32_NODE_01";
const char* BIN_ID    = "BIN_01";

// Chân kết nối HC-SR04 (Mục 3.1.3)
const int TRIG_PIN = 5;
const int ECHO_PIN = 18;

// Chân cụm đèn LED trạng thái tại thùng & Nút nhấn / Công tắc lỗi
const int LED_GREEN_PIN  = 25;
const int LED_YELLOW_PIN = 26;
const int LED_RED_PIN    = 27;
const int BTN_PRIO_PIN   = 14;
const int SW_FAULT_PIN   = 33;

// Thông số hiệu chuẩn (Mục 2.1.1)
const float D_EMPTY = 100.0;
const float D_FULL  = 20.0;

const unsigned long SEND_INTERVAL_MS = 5000;
unsigned long lastSendTime = 0;
float lastReportedFill = -1.0;

bool hasPendingMessage = false;
String pendingPayload = "";
String pendingMessageId = "";
unsigned long msgCounter = 1;
uint32_t bootSessionId = 0;
unsigned long lastDebugTime = 0;

// Cập nhật 3 đèn LED báo hiệu tại thùng rác
void updateLocalLeds(float fillPercent, const String& sensorStatus) {
  if (sensorStatus != "OK") {
    digitalWrite(LED_GREEN_PIN, LOW);
    digitalWrite(LED_YELLOW_PIN, LOW);
    // Nhấp nháy LED Đỏ khi cảm biến lỗi
    digitalWrite(LED_RED_PIN, (millis() / 300) % 2);
    return;
  }
  digitalWrite(LED_GREEN_PIN,  (fillPercent < 60.0) ? HIGH : LOW);
  digitalWrite(LED_YELLOW_PIN, (fillPercent >= 60.0 && fillPercent < 80.0) ? HIGH : LOW);
  digitalWrite(LED_RED_PIN,    (fillPercent >= 80.0) ? HIGH : LOW);
}

// Đọc 1 mẫu khoảng cách từ HC-SR04
float readSingleDistanceCm() {
  // Nếu gạt công tắc giả lập lỗi cảm biến (rác dính sát mặt cảm biến / đứt dây)
  if (digitalRead(SW_FAULT_PIN) == LOW) {
    return -1.0;
  }
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);

  long duration = pulseIn(ECHO_PIN, HIGH, 30000);
  if (duration == 0) return -1.0;
  float distance = (duration * 0.0343) / 2.0;
  if (distance < 2.0 || distance > 400.0) return -1.0;
  return distance;
}

// Bộ lọc trung vị 5 mẫu (Median Filter - Mục 2.1.1 & 3.3.2)
float getFilteredDistanceCm(int numSamples = 5) {
  float validSamples[10];
  int validCount = 0;
  for (int i = 0; i < numSamples; i++) {
    float d = readSingleDistanceCm();
    if (d > 0) validSamples[validCount++] = d;
    delay(30);
  }
  if (validCount < (numSamples / 2 + 1)) return -1.0;

  for (int i = 0; i < validCount - 1; i++) {
    for (int j = i + 1; j < validCount; j++) {
      if (validSamples[i] > validSamples[j]) {
        float tmp = validSamples[i];
        validSamples[i] = validSamples[j];
        validSamples[j] = tmp;
      }
    }
  }
  return validSamples[validCount / 2];
}

float calculateFillPercent(float distanceCm) {
  float percent = ((D_EMPTY - distanceCm) / (D_EMPTY - D_FULL)) * 100.0;
  if (percent < 0.0) percent = 0.0;
  if (percent > 100.0) percent = 100.0;
  return round(percent * 10.0) / 10.0;
}

bool sendTelemetryToBackend(const String& jsonPayload) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WARN] Mat ket noi Wi-Fi! Dang ket noi lai...");
    WiFi.reconnect();
    return false;
  }
  HTTPClient http;
  http.begin(BACKEND_URL);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(3000);

  int httpResponseCode = http.POST(jsonPayload);
  if (httpResponseCode >= 200 && httpResponseCode < 300) {
    String response = http.getString();
    Serial.printf("[OK] HTTP %d: %s\n", httpResponseCode, response.c_str());
    http.end();
    return true;
  } else {
    Serial.printf("[ERROR] HTTP %d. Luu tam ban tin de gui lai!\n", httpResponseCode);
    http.end();
    return false;
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  pinMode(LED_GREEN_PIN, OUTPUT);
  pinMode(LED_YELLOW_PIN, OUTPUT);
  pinMode(LED_RED_PIN, OUTPUT);
  pinMode(BTN_PRIO_PIN, INPUT_PULLUP);
  pinMode(SW_FAULT_PIN, INPUT_PULLUP);

  Serial.println("==================================================");
  Serial.println("HE THONG QUAN LY RAC THAI THONG MINH - NHOM 15 HUIT");
  Serial.println("==================================================");

  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Dang ket noi Wi-Fi Wokwi-GUEST");
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
  }
  Serial.println("\n[WIFI] Da ket noi! IP: " + WiFi.localIP().toString());

  // Moi lan khoi dong phai co session ID rieng, tranh trung message_id trong DB.
  bootSessionId = esp_random();
  Serial.printf("[BOOT] Session=%08lx, SW_FAULT=%d (1=OK, 0=LOI)\n",
                (unsigned long)bootSessionId, digitalRead(SW_FAULT_PIN));
}

void loop() {
  unsigned long now = millis();
  bool emergencyButtonPressed = (digitalRead(BTN_PRIO_PIN) == LOW);

  if (hasPendingMessage && (now - lastSendTime >= SEND_INTERVAL_MS)) {
    lastSendTime = now;
    Serial.println("[RETRY] Gui lai ban tin luu tam: " + pendingMessageId);
    if (sendTelemetryToBackend(pendingPayload)) {
      hasPendingMessage = false;
      pendingPayload = "";
    }
    return;
  }

  float filteredDist = getFilteredDistanceCm(5);
  String sensorStatus = "OK";
  float fillPercent = -1.0;

  if (filteredDist < 0) {
    sensorStatus = "SENSOR_ERROR";
  } else {
    fillPercent = calculateFillPercent(filteredDist);
  }

  updateLocalLeds(fillPercent, sensorStatus);

  // In chan doan 2 giay/lan de kiem tra sensor/switch khi demo Wokwi.
  if (now - lastDebugTime >= 2000) {
    lastDebugTime = now;
    Serial.printf("[SENSOR] sw_fault=%d | dist=%.1f cm | fill=%.1f%% | status=%s\n",
                  digitalRead(SW_FAULT_PIN), filteredDist, fillPercent, sensorStatus.c_str());
  }

  bool stateChanged = (fillPercent >= 0 && lastReportedFill >= 0 && abs(fillPercent - lastReportedFill) >= 10.0);
  if (emergencyButtonPressed || stateChanged || (now - lastSendTime >= SEND_INTERVAL_MS)) {
    lastSendTime = now;
    if (fillPercent >= 0) lastReportedFill = fillPercent;

    // ID giu nguyen qua cac lan retry, nhung khac nhau giua cac lan khoi dong.
    pendingMessageId = String(BIN_ID) + "_MSG_" + String(bootSessionId, HEX) + "_" + String(msgCounter++);

    String payload = "{";
    payload += "\"device_id\":\"" + String(DEVICE_ID) + "\",";
    payload += "\"bin_id\":\"" + String(BIN_ID) + "\",";
    payload += "\"message_id\":\"" + pendingMessageId + "\",";
    payload += "\"measured_at\":" + String(now / 1000) + ",";
    payload += "\"distance_cm\":" + String(filteredDist, 1) + ",";
    payload += "\"fill_percent\":" + String(fillPercent, 1) + ",";
    payload += "\"sensor_status\":\"" + sensorStatus + "\",";
    payload += "\"is_priority\":" + String(emergencyButtonPressed ? "true" : "false");
    payload += "}";

    if (emergencyButtonPressed) {
      Serial.println("[PRIORITY] Nhan nut khan cap tai thung! Gui ban tin uu tien...");
    }
    Serial.println("[DATA] " + payload);

    if (!sendTelemetryToBackend(payload)) {
      hasPendingMessage = true;
      pendingPayload = payload;
    }
    if (emergencyButtonPressed) delay(500);
  }
  delay(200);
}