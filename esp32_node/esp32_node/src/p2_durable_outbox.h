#pragma once

// P2-B: small durable ring-buffer for ESP32 Arduino / Wokwi.
// Stores critical transitions and offline backlog in one atomic NVS blob.
// Steady online telemetry is *not* written every 5s, to limit flash wear.

#include <Arduino.h>
#include <Preferences.h>
#include <stdint.h>
#include <string.h>

static const uint8_t QF_SENSOR_ERROR = 0x01;
static const uint8_t QF_PRIORITY     = 0x02;
static const uint8_t QF_TILT         = 0x04;
static const uint8_t QF_FIRE         = 0x08;
static const uint8_t QF_URGENT       = 0x10;

#pragma pack(push, 1)
struct EdgeTelemetryRecord {
  uint32_t bootId;
  uint32_t sequence;
  uint32_t uptimeSeconds;
  uint32_t capturedAtUnix;  // 0 = UTC not synchronized yet
  int16_t distanceDeciCm;  // -10 = sensor error
  int16_t fillDeciPercent; // -10 = sensor error
  uint8_t flags;
  uint8_t reserved;
};
#pragma pack(pop)

static_assert(sizeof(EdgeTelemetryRecord) == 22, "P2 edge frame must stay compact");

class DurableOutbox {
 public:
  static const uint8_t CAPACITY = 24;

  DurableOutbox() : head_(0), tail_(0), count_(0), persisted_(false),
                    storageUsable_(false), droppedNormal_(0), droppedCritical_(0) {
    memset(records_, 0, sizeof(records_));
  }

  bool begin() {
    if (!prefs_.begin("iot_edge_q", false)) {
      Serial.println("[NVS] Khong mo duoc Preferences, queue chi o RAM");
      return false;
    }
    storageUsable_ = true;
    size_t storedLength = prefs_.getBytesLength("snapshot");
    if (storedLength == 0) return true;  // clean first boot
    if (storedLength != sizeof(Snapshot)) return rejectCorruptSnapshot();

    Snapshot saved = {};
    if (prefs_.getBytes("snapshot", &saved, sizeof(saved)) != sizeof(saved)) {
      return rejectCorruptSnapshot();
    }
    uint32_t savedCrc = saved.crc;
    saved.crc = 0;
    if (saved.magic != MAGIC || saved.version != VERSION || saved.count > CAPACITY ||
        checksum(saved) != savedCrc) return rejectCorruptSnapshot();

    for (uint8_t i = 0; i < saved.count; ++i) {
      if (saved.records[i].reserved != 0 || (saved.records[i].flags & ~VALID_FLAGS)) {
        return rejectCorruptSnapshot();
      }
      records_[i] = saved.records[i];
    }
    head_ = 0;
    count_ = (uint8_t)saved.count;
    tail_ = count_ % CAPACITY;
    persisted_ = count_ != 0;
    Serial.printf("[NVS] RESTORED %u queued messages after reboot\n", (unsigned)count_);
    return true;
  }

  uint8_t count() const { return count_; }
  bool hasPersistentBacklog() const { return persisted_; }
  bool storageUsable() const { return storageUsable_; }
  uint32_t droppedNormal() const { return droppedNormal_; }
  uint32_t droppedCritical() const { return droppedCritical_; }

  bool peek(EdgeTelemetryRecord& result) const {
    if (count_ == 0) return false;
    result = records_[head_];
    return true;
  }

  bool enqueue(const EdgeTelemetryRecord& record, bool shouldPersist) {
    const bool urgent = (record.flags & QF_URGENT) != 0;
    if (count_ == CAPACITY) {
      if (!urgent) {
        ++droppedNormal_;
        Serial.println("[QUEUE] FULL: dropping new routine measurement");
        return false;
      }
      int replaceIndex = -1;
      for (uint8_t i = 0; i < count_; ++i) {
        if ((records_[at(i)].flags & QF_URGENT) == 0) {
          replaceIndex = i;
          break;
        }
      }
      if (replaceIndex < 0) {
        ++droppedCritical_;
        Serial.println("[QUEUE] CRITICAL OVERFLOW: all 24 slots contain urgent events");
        return false;
      }
      removeLogical((uint8_t)replaceIndex);
      ++droppedNormal_;
      Serial.println("[QUEUE] Replaced oldest routine message with urgent event");
    }

    records_[tail_] = record;
    tail_ = (tail_ + 1) % CAPACITY;
    ++count_;
    // A restored backlog must remain persistent until all items are ACKed.
    if (shouldPersist || persisted_) persist();
    return true;
  }

  void markOffline() {
    if (count_ > 0 && !persisted_) persist();
  }

  bool removeAcknowledgedFront() {
    if (count_ == 0) return false;
    records_[head_] = EdgeTelemetryRecord();
    head_ = (head_ + 1) % CAPACITY;
    --count_;
    if (count_ == 0) head_ = tail_ = 0;
    // If power fails after a valid ACK but before NVS update, a duplicate is
    // safe: the server's message_id PK will ACK it again without side effects.
    if (persisted_) persist();
    return true;
  }

 private:
  static const uint32_t MAGIC = 0x50324251; // P2BQ
  static const uint16_t VERSION = 1;
  static const uint8_t VALID_FLAGS = QF_SENSOR_ERROR | QF_PRIORITY |
                                     QF_TILT | QF_FIRE | QF_URGENT;

#pragma pack(push, 1)
  struct Snapshot {
    uint32_t magic;
    uint16_t version;
    uint16_t count;
    uint32_t crc;
    EdgeTelemetryRecord records[CAPACITY];
  };
#pragma pack(pop)

  uint8_t at(uint8_t logicalIndex) const { return (head_ + logicalIndex) % CAPACITY; }

  void removeLogical(uint8_t index) {
    for (uint8_t i = index; i + 1 < count_; ++i) records_[at(i)] = records_[at(i + 1)];
    --count_;
    tail_ = (head_ + count_) % CAPACITY;
    records_[tail_] = EdgeTelemetryRecord();
  }

  static uint32_t checksum(const Snapshot& state) {
    const uint8_t* bytes = (const uint8_t*)&state;
    uint32_t hash = 2166136261UL;
    for (size_t i = 0; i < sizeof(state); ++i) {
      hash ^= bytes[i];
      hash *= 16777619UL;
    }
    return hash;
  }

  bool persist() {
    if (!storageUsable_) {
      Serial.println("[NVS] WARNING: queue is volatile (cannot persist)");
      return false;
    }
    Snapshot snap = {};
    snap.magic = MAGIC;
    snap.version = VERSION;
    snap.count = count_;
    for (uint8_t i = 0; i < count_; ++i) snap.records[i] = records_[at(i)];
    snap.crc = checksum(snap);
    if (prefs_.putBytes("snapshot", &snap, sizeof(snap)) != sizeof(snap)) {
      Serial.println("[NVS] WRITE FAILED; pending messages may be lost on reset");
      return false;
    }
    persisted_ = count_ > 0;
    return true;
  }

  bool rejectCorruptSnapshot() {
    storageUsable_ = false; // never overwrite a possibly recoverable snapshot
    Serial.println("[NVS] CORRUPT snapshot; retaining bytes for recovery, RAM queue only");
    return false;
  }

  Preferences prefs_;
  EdgeTelemetryRecord records_[CAPACITY];
  uint8_t head_;
  uint8_t tail_;
  uint8_t count_;
  bool persisted_;
  bool storageUsable_;
  uint32_t droppedNormal_;
  uint32_t droppedCritical_;
};
