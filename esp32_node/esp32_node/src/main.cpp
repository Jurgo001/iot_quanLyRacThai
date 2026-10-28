#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <esp_system.h>
#include <math.h>
#include "local_secrets.h" // P1-1: untracked per-device API key

// P0 patch for Jurgo001/iot_quanLyRacThai main (original blob: 0191b888...)
// Keep hardware pins, HTTP endpoint and bin fullness thresholds unchanged.
const char* WIFI_SSID = "Wokwi-GUEST";
const char* WIFI_PASS = "";
const char* BACKEND_URL = "http://host.wokwi.internal:8000/api/telemetry";
const char* DEVICE_ID = "ESP32_NODE_01";
const char* BIN_ID = "BIN_01";

const int TRIG_PIN = 5;
const int ECHO_PIN = 18;
const int LED_GREEN_PIN = 25;
const int LED_YELLOW_PIN = 26;
const int LED_RED_PIN = 27;
const int BTN_PRIO_PIN = 14;
const int SW_FAULT_PIN = 33;

const float D_EMPTY = 100.0f;
const float D_FULL = 20.0f;
const unsigned long SENSOR_SAMPLE_INTERVAL_MS = 60;
const unsigned long SEND_INTERVAL_MS = 5000;
const unsigned long WIFI_RETRY_INTERVAL_MS = 5000;
const unsigned long HTTP_RETRY_INTERVAL_MS = 3000;
const unsigned long HTTP_NEXT_INTERVAL_MS = 200;
const unsigned long DEBUG_INTERVAL_MS = 2000;
const unsigned long BUTTON_DEBOUNCE_MS = 40;
const int SENSOR_WINDOW = 5;
const int MAX_QUEUE = 24;

struct TelemetryMessage {
  String messageId;
  String payload;
  bool priority;
};
TelemetryMessage outbox[MAX_QUEUE];
int outboxSize = 0;

uint32_t bootSessionId = 0;
unsigned long msgCounter = 1;
unsigned long lastWifiRetryMs = 0;
unsigned long lastSampleMs = 0;
unsigned long lastTelemetryAttemptMs = 0;
unsigned long lastHttpAttemptMs = 0;
unsigned long lastDebugMs = 0;
bool lastHttpFailed = false;
bool haveQueuedTelemetry = false;
float lastQueuedFill = -1.0f;
String lastQueuedSensorStatus = "";

float samples[SENSOR_WINDOW] = { -1, -1, -1, -1, -1 };
int samplePos = 0;
int sampleCount = 0;
float distanceCm = -1.0f;
float fillPercent = -1.0f;
String sensorStatus = "SENSOR_ERROR";
bool readingReady = false;

int lastRawButton = HIGH;
int stableButton = HIGH;
unsigned long lastButtonChangeMs = 0;

void updateLocalLeds(float fill, const String& status) {
  if (status != "OK") {
    digitalWrite(LED_GREEN_PIN, LOW);
    digitalWrite(LED_YELLOW_PIN, LOW);
    digitalWrite(LED_RED_PIN, (millis() / 300) % 2 ? HIGH : LOW);
    return;
  }
  digitalWrite(LED_GREEN_PIN, fill < 60.0f ? HIGH : LOW);
  digitalWrite(LED_YELLOW_PIN, fill >= 60.0f && fill < 80.0f ? HIGH : LOW);
  digitalWrite(LED_RED_PIN, fill >= 80.0f ? HIGH : LOW);
}

float readSingleDistanceCm() {
  if (digitalRead(SW_FAULT_PIN) == LOW) return -1.0f;
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  // pulseIn has a bounded timeout; the main loop is otherwise event/time driven.
  unsigned long duration = pulseIn(ECHO_PIN, HIGH, 30000);
  if (duration == 0) return -1.0f;
  float distance = (duration * 0.0343f) / 2.0f;
  return (distance >= 2.0f && distance <= 400.0f) ? distance : -1.0f;
}

float calculateFillPercent(float distance) {
  float percent = (D_EMPTY - distance) * 100.0f / (D_EMPTY - D_FULL);
  if (percent < 0.0f) percent = 0.0f;
  if (percent > 100.0f) percent = 100.0f;
  return roundf(percent * 10.0f) / 10.0f;
}

void sampleSensor(unsigned long now) {
  if (now - lastSampleMs < SENSOR_SAMPLE_INTERVAL_MS) return;
  lastSampleMs = now;
  samples[samplePos] = readSingleDistanceCm();
  samplePos = (samplePos + 1) % SENSOR_WINDOW;
  if (sampleCount < SENSOR_WINDOW) sampleCount++;
  if (sampleCount < SENSOR_WINDOW) return;

  float valid[SENSOR_WINDOW];
  int n = 0;
  for (int i = 0; i < SENSOR_WINDOW; i++) {
    if (samples[i] > 0.0f) valid[n++] = samples[i];
  }
  readingReady = true;
  if (n < 3) {
    distanceCm = -1.0f;
    fillPercent = -1.0f;
    sensorStatus = "SENSOR_ERROR";
    return;
  }
  for (int i = 0; i < n - 1; i++) {
    for (int j = i + 1; j < n; j++) {
      if (valid[j] < valid[i]) {
        float t = valid[i]; valid[i] = valid[j]; valid[j] = t;
      }
    }
  }
  distanceCm = valid[n / 2];
  fillPercent = calculateFillPercent(distanceCm);
  sensorStatus = "OK";
}

bool buttonPressedOnce(unsigned long now) {
  int raw = digitalRead(BTN_PRIO_PIN);
  if (raw != lastRawButton) {
    lastRawButton = raw;
    lastButtonChangeMs = now;
  }
  if (now - lastButtonChangeMs >= BUTTON_DEBOUNCE_MS && raw != stableButton) {
    stableButton = raw;
    return stableButton == LOW;
  }
  return false;
}

void maintainWifi(unsigned long now) {
  if (WiFi.status() == WL_CONNECTED) return;
  if (now - lastWifiRetryMs >= WIFI_RETRY_INTERVAL_MS) {
    lastWifiRetryMs = now;
    Serial.println("[WIFI] Chua ket noi, thu reconnect khong chan loop...");
    WiFi.reconnect();
  }
}

void removeOutboxAt(int index) {
  if (index < 0 || index >= outboxSize) return;
  for (int i = index; i < outboxSize - 1; ++i) outbox[i] = outbox[i + 1];
  outbox[--outboxSize] = TelemetryMessage();
}

bool enqueueTelemetry(bool priority, unsigned long now) {
  if (outboxSize == MAX_QUEUE) {
    if (!priority) {
      Serial.println("[QUEUE] FULL - bo qua 1 ban tin thong thuong; can bo nho ben vung neu can offline lau.");
      return false;
    }
    int discardIndex = -1;
    for (int i = 0; i < outboxSize; ++i) {
      if (!outbox[i].priority) { discardIndex = i; break; }
    }
    if (discardIndex < 0) {
      Serial.println("[QUEUE] FULL - khong the ghi them su kien uu tien; can bo nho ben vung.");
      return false;
    }
    Serial.println("[QUEUE] FULL - thay 1 ban tin thuong bang ban tin uu tien.");
    removeOutboxAt(discardIndex);
  }

  String messageId = String(BIN_ID) + "_MSG_" + String(bootSessionId, HEX) + "_" + String(msgCounter++);
  String payload = "{";
  payload += "\"device_id\":\"" + String(DEVICE_ID) + "\",";
  payload += "\"bin_id\":\"" + String(BIN_ID) + "\",";
  payload += "\"message_id\":\"" + messageId + "\",";
  // As in original firmware, measured_at is uptime in seconds, NOT Unix/NTP time.
  payload += "\"measured_at\":" + String(now / 1000) + ",";
  payload += "\"distance_cm\":" + String(distanceCm, 1) + ",";
  payload += "\"fill_percent\":" + String(fillPercent, 1) + ",";
  payload += "\"sensor_status\":\"" + sensorStatus + "\",";
  payload += "\"is_priority\":" + String(priority ? "true" : "false");
  payload += "}";

  outbox[outboxSize].messageId = messageId;
  outbox[outboxSize].payload = payload;
  outbox[outboxSize].priority = priority;
  outboxSize++;
  if (priority) Serial.println("[PRIORITY] Nhan nut khan cap tai thung! Da xep hang ban tin uu tien.");
  Serial.printf("[QUEUE] %s | waiting=%d\n", messageId.c_str(), outboxSize);
  return true;
}

bool sendTelemetryToBackend(const String& payload) {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  if (!http.begin(BACKEND_URL)) return false;
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", DEVICE_API_KEY);
  http.setTimeout(1500);
  int code = http.POST(payload);
  if (code >= 200 && code < 300) {
    String response = http.getString();
    Serial.printf("[ACK] HTTP %d: %s\n", code, response.c_str());
    http.end();
    return true;
  }
  Serial.printf("[HTTP] loi code=%d; giu nguyen message_id de retry\n", code);
  http.end();
  return false;
}

void flushOutbox(unsigned long now) {
  if (outboxSize == 0 || WiFi.status() != WL_CONNECTED) return;
  unsigned long interval = lastHttpFailed ? HTTP_RETRY_INTERVAL_MS : HTTP_NEXT_INTERVAL_MS;
  if (now - lastHttpAttemptMs < interval) return;
  lastHttpAttemptMs = now;
  String firstId = outbox[0].messageId;
  bool sent = sendTelemetryToBackend(outbox[0].payload);
  lastHttpFailed = !sent;
  if (sent) {
    Serial.println("[QUEUE] Da nhan ACK: " + firstId);
    removeOutboxAt(0);
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
  bootSessionId = esp_random();
  Serial.println("=== SMART WASTE IoT - P0: nonblocking startup, debounce, FIFO ===");
  Serial.printf("[BOOT] session=%08lx sw_fault=%d\n", (unsigned long)bootSessionId, digitalRead(SW_FAULT_PIN));
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  // No blocking while(WiFi.status()!=WL_CONNECTED) here.
}

void loop() {
  unsigned long now = millis();
  maintainWifi(now);
  bool priorityEvent = buttonPressedOnce(now);
  sampleSensor(now);
  updateLocalLeds(fillPercent, sensorStatus);

  bool periodic = now - lastTelemetryAttemptMs >= SEND_INTERVAL_MS;
  bool fillChanged = readingReady && sensorStatus == "OK" &&
       (lastQueuedFill < 0.0f || fabsf(fillPercent - lastQueuedFill) >= 10.0f);
  bool statusChanged = readingReady && sensorStatus != lastQueuedSensorStatus;
  if (priorityEvent || (readingReady && (!haveQueuedTelemetry || periodic || fillChanged || statusChanged))) {
    // Avoid tight-loop retries when queue is full and network is down.
    lastTelemetryAttemptMs = now;
    bool added = enqueueTelemetry(priorityEvent, now);
    if (added) {
      haveQueuedTelemetry = true;
      lastQueuedSensorStatus = sensorStatus;
      if (sensorStatus == "OK") lastQueuedFill = fillPercent;
    }
  }

  if (now - lastDebugMs >= DEBUG_INTERVAL_MS) {
    lastDebugMs = now;
    Serial.printf("[SENSOR] sw_fault=%d dist=%.1f cm fill=%.1f%% status=%s queue=%d WiFi=%d\n",
                  digitalRead(SW_FAULT_PIN), distanceCm, fillPercent, sensorStatus.c_str(),
                  outboxSize, WiFi.status() == WL_CONNECTED);
  }
  flushOutbox(millis());
  // HTTPClient POST can still block for up to its timeout (1.5 s); not hard real-time.
}
