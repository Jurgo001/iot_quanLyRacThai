#pragma once

// Portable sensor-only logic: no Arduino dependency so the math can be
// regression-tested on a desktop before running Wokwi on an ESP32.
#include <math.h>
#include <stdint.h>

// Gravity angle from the original upright position (+Z). Return 0 degrees
// upright, 90 sideways, 180 upside down. Ignore near-zero/high-G transient
// samples rather than inventing a tilt from a moving/invalid accelerometer.
inline bool gravityTiltDegrees(float ax, float ay, float az, float& degrees) {
  const float norm = sqrtf(ax * ax + ay * ay + az * az);
  // Raw MPU6050 +/-2g values in LSB: 16384 LSB/g.
  if (!isfinite(norm) || norm < 16384.0f * 0.55f || norm > 16384.0f * 1.5f)
    return false;
  degrees = atan2f(sqrtf(ax * ax + ay * ay), az) * (180.0f / 3.14159265358979323846f);
  return isfinite(degrees);
}

struct TiltAlarmState {
  static constexpr float TRIGGER_DEGREES = 30.0f;
  static constexpr float CLEAR_DEGREES = 20.0f;
  static constexpr unsigned long STABLE_MS = 600;
  float filteredAngle = 0.0f;
  bool ready = false;
  bool active = false;
  bool pending = false;
  bool pendingTarget = false;
  unsigned long pendingSince = 0;

  void reset() {
    filteredAngle = 0.0f;
    ready = false;
    active = false;
    pending = false;
    pendingTarget = false;
    pendingSince = 0;
  }

  // Returns true ONLY when the stable alarm state changes (not on each sample).
  bool update(float measuredDegrees, unsigned long now) {
    if (!isfinite(measuredDegrees) || measuredDegrees < 0.0f || measuredDegrees > 180.0f)
      return false;
    if (!ready) {
      ready = true;
      filteredAngle = measuredDegrees;
    } else {
      filteredAngle = 0.65f * filteredAngle + 0.35f * measuredDegrees;
    }

    const bool target = active ? filteredAngle >= CLEAR_DEGREES
                               : filteredAngle >= TRIGGER_DEGREES;
    if (target == active) {
      pending = false;
      return false;
    }
    if (!pending || pendingTarget != target) {
      pending = true;
      pendingTarget = target;
      pendingSince = now;
      return false;
    }
    if (now - pendingSince < STABLE_MS) return false;
    active = target;
    pending = false;
    return true;
  }
};
