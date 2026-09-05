# MacBook Pro Power Tuning — Headless Frigate Server

## Hardware

- **Model**: MacBook Pro 14" (MacBookPro18,3)
- **Chip**: Apple M1 Pro (6 Performance + 2 Efficiency cores)
- **RAM**: 16 GB
- **Adapter**: 67W USB-C
- **OS**: macOS 25D2128

## Baseline (Pre-Tuning)

- Wall draw: ~4.67W (20.3V × 0.23A)
- Frigate Docker container: ~17% CPU
- `zmq_onnx_client.py` (ONNX detector): ~28% CPU
- `OrbStack Helper` (vmgr): ~30–49% CPU
- Both cameras detecting at 10 fps

## Changes Applied

### 1. Frigate Config — Detect FPS reduced (10 → 5)

`config/config.yaml` — both `cam1` and `cam2` cameras:

```yaml
detect:
  enabled: true
  fps: 5  # was 10
```

5 fps is sufficient for person detection; people don't move fast enough to require 10 fps.
This roughly halved the detector and ffmpeg decode workload.

### 2. Frigate Config — LPR disabled

```yaml
lpr:
  enabled: false  # was true — not in use
```

### 3. Low Power Mode enabled

Enables scheduler preference for E-cores and caps P-core boost frequency.
Appropriate for sustained background workloads where latency is not critical.

```bash
sudo pmset -a lowpowermode 1
```

**Persistent across reboots**: yes — stored in power management preferences.

### 4. Wake on LAN disabled

The machine uses `caffeinate -s` to stay awake regardless; WoL polling just wastes NIC power.

```bash
sudo pmset -a womp 0
```

**Note**: This resets on reboot. Re-apply after each restart, or add to a login script.

### 5. Spotlight indexing disabled on Frigate data directory

Prevents `mds`/`mds_stores` from waking up on every new MP4 recording segment written by Frigate.

```bash
sudo mdutil -i off ~/Projects/frigate
```

### 6. WiFi — already off ✓

WiFi was already disabled (`Wi-Fi Power (en0): Off`). Ethernet (AX88179A USB adapter) is the primary interface at <mac-ip>.

### 7. AlDente — already configured ✓

AlDente Pro is running and holding battery charge at ~84% rather than 100%.
This reduces charging inefficiency and long-term battery degradation.
No changes needed.

### 8. caffeinate launch agent — already in place ✓

`~/Library/LaunchAgents/com.user.caffeinate.plist` runs `caffeinate -s` at login,
preventing system sleep while on AC power.

## Not Applied / Out of Scope

### Bluetooth
Bluetooth is on but unused. Disabling it persistently across reboots without disabling SIP
requires workarounds (e.g. a login item calling `blueutil --power 0`).
Estimated saving: ~0.2–0.4W. Low priority given overall draw is already very low.

### iCloud / Apple Intelligence daemons
Services such as `iCloudHelper`, `intelligenceplatformd`, `mediaanalysisd`,
`textunderstandingd`, `remindd`, `geoanalyticsd` can be disabled via:

```bash
launchctl disable gui/$(id -u)/com.apple.iCloudHelper
launchctl disable gui/$(id -u)/com.apple.intelligenceplatformd
launchctl disable gui/$(id -u)/com.apple.mediaanalysisd
launchctl disable gui/$(id -u)/com.apple.remindd
launchctl disable gui/$(id -u)/com.apple.geoanalyticsd
launchctl disable gui/$(id -u)/com.apple.textunderstandingd
```

**Warning**: these re-enable after macOS updates. Combined saving is minor (~0.1–0.2W).
Not applied to avoid maintenance burden.

### Semantic Search / Face Recognition
These were considered for disabling but the user actively uses them. Left enabled.

```yaml
semantic_search:
  enabled: true
  model_size: large
face_recognition:
  enabled: true
  model_size: large
```

## Result

| Metric | Before | After |
|---|---|---|
| Wall draw | ~4.67W | ~3.21W |
| Reduction | — | **1.46W (31% lower)** |
| Frigate container CPU | ~17% | ~6.7% |
| Detect FPS | 10/camera | 5/camera |
| Low Power Mode | Off | On |
| LPR | Enabled | Disabled |

## Frigate Service Architecture (Reference)

| Component | Where |
|---|---|
| Frigate (NVR, go2rtc, web UI) | OrbStack Docker container |
| ONNX detector (`zmq_onnx_client.py`) | Host process (macOS), connected via ZMQ TCP |
| ffmpeg instances | Host processes, HW decode via `videotoolbox` |
| `frigate-coordinator` | Host launchd agent (`com.frigate.coordinator`) |
| `frigate-detector` | Host launchd agent (`com.frigate.detector`) |
| caffeinate | Host launchd agent (`com.user.caffeinate`) |

## Post-Reboot Checklist

After a reboot, verify the following:

```bash
# Power settings
pmset -g | grep -E "lowpowermode|womp|sleep"

# Re-apply womp if needed (resets on reboot)
sudo pmset -a womp 0

# Frigate container
docker ps

# Frigate API
docker exec frigate wget -qO- http://localhost:5000/api/version

# Camera FPS (should show ~5.0)
docker exec frigate wget -qO- http://localhost:5000/api/stats | python3 -m json.tool | grep -E "camera_fps|detection_fps"

# Current wall draw
sudo ioreg -rn AppleSmartBattery | grep SystemPowerIn
```
