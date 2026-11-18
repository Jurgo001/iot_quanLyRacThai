#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <esp_system.h>
#include <math.h>
#include <time.h>
#include "local_secrets.h" // P1-1: untracked per-device API key
#include "p2_durable_outbox.h"
#include "p4_sensor_math.h"

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
// P4: Real simulated sensor components, not manual hazard toggle switches.
// MPU6050 measures 3-axis acceleration, MQ-2 measures COMBUSTIBLE GAS risk.
// Gas risk is mapped to the backwards-compatible fire_detected API flag;
// it does NOT prove the presence of actual flames or a certified fire alarm.
const uint8_t MPU_ADDR = 0x68;
const int MPU_SDA_PIN = 21;
const int MPU_SCL_PIN = 22;
const int MQ2_DIGITAL_PIN = 13; // MQ2 DOUT active LOW; 5V module needs a level shifter

const float D_EMPTY = 100.0f;
const float D_FULL = 20.0f;
const unsigned long SENSOR_SAMPLE_INTERVAL_MS = 60;
const unsigned long SEND_INTERVAL_MS = 5000;
const unsigned long WIFI_RETRY_INTERVAL_MS = 5000;
const unsigned long HTTP_NEXT_INTERVAL_MS = 200;
const unsigned long HTTP_RETRY_INITIAL_MS = 1000;
const unsigned long HTTP_RETRY_MAX_MS = 60000;
const unsigned long DEBUG_INTERVAL_MS = 2000;
const unsigned long BUTTON_DEBOUNCE_MS = 40;
const unsigned long HAZARD_DEBOUNCE_MS = 80;
const unsigned long MPU_READ_INTERVAL_MS = 200;
const unsigned long MPU_RECONNECT_INTERVAL_MS = 5000;
const int SENSOR_WINDOW = 5;
const int SMOOTH_WINDOW = 3;
DurableOutbox outbox;

uint32_t bootSessionId = 0;
unsigned long msgCounter = 1;
unsigned long lastWifiRetryMs = 0;
unsigned long lastSampleMs = 0;
unsigned long lastTelemetryAttemptMs = 0;
unsigned long lastHttpAttemptMs = 0;
unsigned long lastDebugMs = 0;
bool lastHttpFailed = false;
unsigned long nextRetryDelayMs = HTTP_NEXT_INTERVAL_MS;
uint8_t consecutiveHttpFailures = 0;
bool ntpStarted = false;
bool haveQueuedTelemetry = false;
float lastQueuedFill = -1.0f;
String lastQueuedSensorStatus = "";

float samples[SENSOR_WINDOW] = { -1, -1, -1, -1, -1 };
int samplePos = 0;
int sampleCount = 0;
float distanceCm = -1.0f;
float lastMedianCm = -1.0f;
float fillPercent = -1.0f;
String sensorStatus = "SENSOR_ERROR";
bool readingReady = false;
float medianHistory[SMOOTH_WINDOW] = { 0, 0, 0 };
int smoothPos = 0;
int smoothCount = 0;

struct DebouncedHazard {
  int pin;
  int raw;
  bool active;
  unsigned long rawChangedMs;
  explicit DebouncedHazard(int inputPin)
      : pin(inputPin), raw(HIGH), active(false), rawChangedMs(0) {}
};
DebouncedHazard gasAlarm(MQ2_DIGITAL_PIN);
TiltAlarmState tiltAlarm;
bool mpuAvailable = false;
unsigned long lastMpuReadMs = 0;
unsigned long lastMpuRetryMs = 0;
float lastTiltAngleDeg = -1.0f;
bool tiltDetected = false;
bool fireDetected = false;

int lastRawButton = HIGH;
int stableButton = HIGH;
unsigned long lastButtonChangeMs = 0;

void updateLocalLeds(float fill, const String& status) {
  // Safety alarms take visual precedence over normal fill-level LEDs.
  if (fireDetected) {
    digitalWrite(LED_GREEN_PIN, LOW);
    digitalWrite(LED_YELLOW_PIN, LOW);
    digitalWrite(LED_RED_PIN, (millis() / 180) % 2 ? HIGH : LOW);
    return;
  }
  if (tiltDetected) {
    digitalWrite(LED_GREEN_PIN, LOW);
    digitalWrite(LED_RED_PIN, LOW);
    digitalWrite(LED_YELLOW_PIN, (millis() / 300) % 2 ? HIGH : LOW);
    return;
  }
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

// P2-1: Use a moving average of the *median* samples, not the raw echo pulses.
// Until the window warms up, average only available entries (no startup bias).
// Reset it on confirmed SENSOR_ERROR so recovery cannot reuse stale readings.
void resetMedianSmoother() {
  smoothPos = 0;
  smoothCount = 0;
  lastMedianCm = -1.0f;
}

float smoothMedian(float medianCm) {
  medianHistory[smoothPos] = medianCm;
  smoothPos = (smoothPos + 1) % SMOOTH_WINDOW;
  if (smoothCount < SMOOTH_WINDOW) smoothCount++;
  float total = 0.0f;
  for (int i = 0; i < smoothCount; i++) total += medianHistory[i];
  lastMedianCm = medianCm;
  return total / smoothCount;
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
    resetMedianSmoother();
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
  // P0 Median Filter retained; P2 adds a three-median moving average.
  distanceCm = smoothMedian(valid[n / 2]);
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

bool updateHazardSwitch(DebouncedHazard& sensor, unsigned long now) {
  int current = digitalRead(sensor.pin);
  if (current != sensor.raw) {
    sensor.raw = current;
    sensor.rawChangedMs = now;
  }
  const bool newActive = (current == LOW);
  if (now - sensor.rawChangedMs >= HAZARD_DEBOUNCE_MS && newActive != sensor.active) {
    sensor.active = newActive;
    return true;
  }
  return false;
}

bool initMpu6050() {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x6B); // PWR_MGMT_1: exit sleep mode
  Wire.write(0x00);
  if (Wire.endTransmission() != 0) return false;
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x1C); // ACCEL_CONFIG: +/-2g = 16384 LSB/g
  Wire.write(0x00);
  return Wire.endTransmission() == 0;
}

bool readMpuAngleDegrees(float& degrees) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x3B); // ACCEL_XOUT_H..ACCEL_ZOUT_L
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(MPU_ADDR, (uint8_t)6, (uint8_t)true) != 6) return false;
  int16_t rawX = (int16_t)((Wire.read() << 8) | Wire.read());
  int16_t rawY = (int16_t)((Wire.read() << 8) | Wire.read());
  int16_t rawZ = (int16_t)((Wire.read() << 8) | Wire.read());
  return gravityTiltDegrees((float)rawX, (float)rawY, (float)rawZ, degrees);
}

bool updateMpuTilt(unsigned long now) {
  if (!mpuAvailable) {
    if (lastMpuRetryMs != 0 && now - lastMpuRetryMs < MPU_RECONNECT_INTERVAL_MS) return false;
    lastMpuRetryMs = now;
    mpuAvailable = initMpu6050();
    Serial.println(mpuAvailable ? "[MPU] MPU6050 OK (I2C 0x68)" : "[MPU] Not found; retry after 5s");
    if (!mpuAvailable) return false;
    // A newly attached sensor must first provide stable gravity samples.
    tiltAlarm.reset();
    tiltDetected = false;
  }
  if (now - lastMpuReadMs < MPU_READ_INTERVAL_MS) return false;
  lastMpuReadMs = now;
  float angle = 0.0f;
  if (!readMpuAngleDegrees(angle)) {
    mpuAvailable = false;
    lastMpuRetryMs = now;
    Serial.println("[MPU] Read failed; hold last state until sensor reconnect");
    return false;
  }
  lastTiltAngleDeg = angle;
  bool changed = tiltAlarm.update(angle, now);
  tiltDetected = tiltAlarm.active;
  return changed;
}

void maintainWifi(unsigned long now) {
  if (WiFi.status() == WL_CONNECTED) {
    // NTP is optional: captured_at_unix=0 is used until valid UTC is known.
    if (!ntpStarted) {
      configTime(0, 0, "pool.ntp.org", "time.google.com");
      ntpStarted = true;
    }
    return;
  }
  if (outbox.count() > 0) outbox.markOffline();
  if (now - lastWifiRetryMs >= WIFI_RETRY_INTERVAL_MS) {
    lastWifiRetryMs = now;
    Serial.println("[WIFI] Chua ket noi, thu reconnect khong chan loop...");
    WiFi.reconnect();
  }
}

String messageIdFor(const EdgeTelemetryRecord& record) {
  return String(BIN_ID) + "_MSG_" + String((unsigned long)record.bootId, HEX) + "_" + String((unsigned long)record.sequence);
}

String buildJsonPayload(const EdgeTelemetryRecord& rec) {
  String messageId = messageIdFor(rec);
  bool sensorError = (rec.flags & QF_SENSOR_ERROR) != 0;
  String payload = "{";
  payload += "\"device_id\":\"" + String(DEVICE_ID) + "\",";
  payload += "\"bin_id\":\"" + String(BIN_ID) + "\",";
  payload += "\"message_id\":\"" + messageId + "\",";
  payload += "\"measured_at\":" + String((unsigned long)rec.uptimeSeconds) + ",";
  if (rec.capturedAtUnix != 0) {
    payload += "\"captured_at_unix\":" + String((unsigned long)rec.capturedAtUnix) + ",";
  }
  payload += "\"distance_cm\":" + String(rec.distanceDeciCm / 10.0f, 1) + ",";
  payload += "\"fill_percent\":" + String(rec.fillDeciPercent / 10.0f, 1) + ",";
  payload += "\"sensor_status\":\"" + String(sensorError ? "SENSOR_ERROR" : "OK") + "\",";
  payload += "\"is_priority\":" + String((rec.flags & QF_PRIORITY) ? "true" : "false") + ",";
  payload += "\"tilt_detected\":" + String((rec.flags & QF_TILT) ? "true" : "false") + ",";
  payload += "\"fire_detected\":" + String((rec.flags & QF_FIRE) ? "true" : "false");
  payload += "}";
  return payload;
}

bool enqueueTelemetry(bool priority, bool hazardTransition, unsigned long now) {
  // Hardware emergency priority remains manually latched in backend; alarms
  // never silently override is_priority or the P1 route business rules.
  const bool urgent = priority || hazardTransition;
  EdgeTelemetryRecord record = {};
  record.bootId = bootSessionId;
  record.sequence = (uint32_t)msgCounter++;
  record.uptimeSeconds = (uint32_t)(now / 1000);
  time_t utc = time(nullptr);
  record.capturedAtUnix = (utc >= (time_t)1609459200 && (uint64_t)utc <= UINT32_MAX)
                              ? (uint32_t)utc : 0;
  record.distanceDeciCm = (int16_t)lroundf(distanceCm * 10.0f);
  record.fillDeciPercent = (int16_t)lroundf(fillPercent * 10.0f);
  record.flags = (sensorStatus == "SENSOR_ERROR" ? QF_SENSOR_ERROR : 0) |
                 (priority ? QF_PRIORITY : 0) |
                 (tiltDetected ? QF_TILT : 0) |
                 (fireDetected ? QF_FIRE : 0) |
                 (urgent ? QF_URGENT : 0);
  // When offline, every accepted record goes to NVS. When online, only
  // transitions/priority persist immediately, avoiding frequent flash writes.
  const bool durable = urgent || lastHttpFailed || WiFi.status() != WL_CONNECTED;
  if (!outbox.enqueue(record, durable)) return false;
  if (priority) Serial.println("[PRIORITY] Nhan nut khan cap tai thung! Da xep hang ban tin uu tien.");
  Serial.printf("[QUEUE] %s | waiting=%u NVS=%d\n", messageIdFor(record).c_str(),
                (unsigned)outbox.count(), outbox.hasPersistentBacklog());
  return true;
}

// Minimal JSON string-field extractor for the exact server ACK contract.
// JSON-escaped chars cannot occur inside generated message_id/status values.
String readAckString(const String& body, const char* field) {
  String needle = String("\"") + field + "\"";
  int p = body.indexOf(needle);
  if (p < 0) return "";
  p = body.indexOf(':', p + needle.length());
  if (p < 0) return "";
  ++p;
  while (p < body.length() && (body[p] == ' ' || body[p] == '\t' || body[p] == '\n')) ++p;
  if (p >= body.length() || body[p++] != '"') return "";
  int stop = body.indexOf('"', p);
  if (stop < 0) return "";
  return body.substring(p, stop);
}

bool sendTelemetryToBackend(const String& payload, const String& expectedId) {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  if (!http.begin(BACKEND_URL)) return false;
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", DEVICE_API_KEY);
  http.setTimeout(1500);
  int code = http.POST(payload);
  if (code >= 200 && code < 300) {
    String response = http.getString();
    String responseId = readAckString(response, "message_id");
    String status = readAckString(response, "status");
    bool acknowledged = responseId == expectedId &&
      (status == "accepted" || status == "duplicate_ignored" || status == "sensor_error_logged");
    if (acknowledged) {
      Serial.printf("[ACK] HTTP %d id=%s status=%s\n", code, responseId.c_str(), status.c_str());
    } else {
      Serial.printf("[HTTP] HTTP %d but wrong/missing ACK; keep %s\n", code, expectedId.c_str());
    }
    http.end();
    return acknowledged;
  }
  Serial.printf("[HTTP] loi code=%d; giu nguyen message_id de retry\n", code);
  http.end();
  return false;
}

void flushOutbox(unsigned long now) {
  EdgeTelemetryRecord oldest = {};
  if (!outbox.peek(oldest) || WiFi.status() != WL_CONNECTED) return;
  if (now - lastHttpAttemptMs < nextRetryDelayMs) return;
  lastHttpAttemptMs = now;
  String firstId = messageIdFor(oldest);
  bool sent = sendTelemetryToBackend(buildJsonPayload(oldest), firstId);
  lastHttpFailed = !sent;
  if (sent) {
    Serial.println("[QUEUE] Da nhan ACK: " + firstId);
    outbox.removeAcknowledgedFront();
    consecutiveHttpFailures = 0;
    nextRetryDelayMs = HTTP_NEXT_INTERVAL_MS;
  } else {
    outbox.markOffline();
    if (consecutiveHttpFailures < 7) ++consecutiveHttpFailures;
    unsigned long exponentialMs = HTTP_RETRY_INITIAL_MS << (consecutiveHttpFailures - 1);
    if (exponentialMs > HTTP_RETRY_MAX_MS) exponentialMs = HTTP_RETRY_MAX_MS;
    nextRetryDelayMs = exponentialMs + (esp_random() % 400);
    Serial.printf("[RETRY] after %lu ms; pending=%u\n", nextRetryDelayMs, (unsigned)outbox.count());
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
  pinMode(MQ2_DIGITAL_PIN, INPUT); // MQ-2 DOUT drives logic; LOW = gas exceeded threshold
  Wire.begin(MPU_SDA_PIN, MPU_SCL_PIN);
  outbox.begin();  // Restore power-loss-safe backlog before networking.
  bootSessionId = esp_random();
  Serial.println("=== SMART WASTE IoT - P4: MPU6050 + MQ2, P2 outbox, P3 backend ===");
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
  bool hazardChanged = updateMpuTilt(now);
  hazardChanged = updateHazardSwitch(gasAlarm, now) || hazardChanged;
  fireDetected = gasAlarm.active;
  if (hazardChanged) {
    Serial.printf("[HAZARD] TILT=%d (angle=%.1f deg) GAS_FIRE_RISK=%d (MQ2_DOUT=%d)\n",
                  tiltDetected, lastTiltAngleDeg, fireDetected, digitalRead(MQ2_DIGITAL_PIN));
  }
  sampleSensor(now);
  updateLocalLeds(fillPercent, sensorStatus);

  bool periodic = now - lastTelemetryAttemptMs >= SEND_INTERVAL_MS;
  bool fillChanged = readingReady && sensorStatus == "OK" &&
       (lastQueuedFill < 0.0f || fabsf(fillPercent - lastQueuedFill) >= 10.0f);
  bool statusChanged = readingReady && sensorStatus != lastQueuedSensorStatus;
  if (priorityEvent || hazardChanged || (readingReady && (!haveQueuedTelemetry || periodic || fillChanged || statusChanged))) {
    // Avoid tight-loop retries when queue is full and network is down.
    lastTelemetryAttemptMs = now;
    bool added = enqueueTelemetry(priorityEvent, hazardChanged, now);
    if (added) {
      haveQueuedTelemetry = true;
      lastQueuedSensorStatus = sensorStatus;
      if (sensorStatus == "OK") lastQueuedFill = fillPercent;
    }
  }

  if (now - lastDebugMs >= DEBUG_INTERVAL_MS) {
    lastDebugMs = now;
    Serial.printf("[SENSOR] sw_fault=%d median=%.1f dist_sma=%.1f cm fill=%.1f%% status=%s tilt=%d angle=%.1f deg gas_risk=%d queue=%d WiFi=%d\n",
                  digitalRead(SW_FAULT_PIN), lastMedianCm, distanceCm, fillPercent,
                  sensorStatus.c_str(), tiltDetected, lastTiltAngleDeg, fireDetected,
                  (int)outbox.count(), WiFi.status() == WL_CONNECTED);
  }
  flushOutbox(millis());
  // HTTPClient POST can still block for up to its timeout (1.5 s); not hard real-time.
}
