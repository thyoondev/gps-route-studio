# GPS Route Studio

A macOS development tool for creating routes on a map and sending simulated locations to an iPhone or Android device over USB. The application interface is currently in Korean.

## Features

- Map-based waypoint editing and pin clustering
- Adjustable speed and location jitter, pause/resume, looping, and round-trip playback
- XCTest-based location and speed delivery to iPhone, with receiver diagnostics
- An Android helper using standard mock locations, with restoration status reporting
- GPX route playback through a separate Python CLI

**This is an experimental development tool.** Reported transmission or receiver speed does not mean another app has accepted an activity record. Android locations remain marked as mock locations. This project does not include mock-location concealment, sensor spoofing, or rooting tools, and does not guarantee acceptance of ride, game, or reward records by third-party apps.

## Requirements

The current build scripts target **Apple Silicon Macs running macOS 14 or later**.

- A full Xcode installation with Command Line Tools configured
- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- For Android builds: Java 17, Android SDK Platform 35, Build Tools 36.0.0, and Platform Tools
- Default Android SDK location: `~/Library/Android/sdk`
- For iPhone: Developer Mode, trust established with the Mac, and your own Apple development signing configuration
- For Android: Android 12 or later, USB debugging enabled, and the Mac authorized for debugging

Install the Python environment used by the GUI:

```sh
uv tool install --python 3.13 'pymobiledevice3==9.31.0'
```

The GUI expects Python at `~/.local/share/uv/tools/pymobiledevice3/bin/python3`. If you use a custom uv storage location, update the paths in `Studio.swift` and `Diagnostics.swift`. The tested version of `pmd-pytcp` is 0.0.4. Installing dependencies requires network access.

## Build

1. If you plan to use an iPhone, open `GPSReceiver.xcodeproj` in Xcode and set your signing team for **both the GPSReceiver and GPSMotionUITests targets**. No Team ID is included in the public source. If you change the receiver's Bundle Identifier, also update the `local.gpsroute.receiver` launch target in `diagnostic_worker.py`.
2. Build the Android helper, then the Mac application. The current Mac packaging script requires the Android APK, even if you only plan to use an iPhone.

```sh
./Android/build.sh
./build-mac.sh
open 'GPS Route Studio.app'
```

The Android development signing key is generated in `Android/.local/`, which is excluded from Git. The Mac application uses local ad hoc signing and is not a notarized distribution binary. Rebuild the Mac application after changing the iPhone signing configuration.

## Usage and GPS restoration

1. Close other location simulators. If GeoShift is running, use its **Restore GPS** action before quitting it.
2. Connect your device over USB and select its platform and device in the Mac application.
3. Set the route, speed, and jitter, then start playback. If prompted on Android, select **GPS Route Bridge** as the mock location app in Developer options.
4. When finished, click **Stop / Restore GPS** (the button labeled **중지 · GPS 복구**) and wait for confirmation that restoration has completed before disconnecting the cable.

GPS restoration may not complete immediately after a disconnection or forced application exit. Reconnect the device and follow the application's recovery procedure. The Android helper includes cleanup on connection loss, but you should still verify restoration. If the device does not return to its real location, stop all location simulators and restart the device.

CLI example using public landmarks, from Seoul City Hall toward Namsan. This interpolates between coordinates; it does not follow a road network.

```sh
~/.local/share/uv/tools/pymobiledevice3/bin/python3 gpsroute.py \
  -p 37.5665,126.9780 -p 37.5512,126.9882 --speed 40 --jitter 3
~/.local/share/uv/tools/pymobiledevice3/bin/python3 gpsroute.py \
  --gpx example.gpx --speed 40
```

Supply your own `example.gpx` file. The CLI attempts to restore GPS when stopped with Ctrl-C. Verify that the device has returned to its real location.

## Testing

Run the unit tests without connecting a device or transmitting locations:

```sh
~/.local/share/uv/tools/pymobiledevice3/bin/python3 -m unittest discover -v
```

These tests cover route calculations, protocol handling, and recovery paths. They do not validate every iOS or Android version, physical device connectivity, or third-party acceptance of activity records. On a new device, separately verify start, pause, and restoration using a short test route.

## Privacy and license

Settings, routes, and device recovery information may be stored locally during use. The map uses Apple MapKit, which may make network requests to display map content. Do not include personal routes, device identifiers, development Team IDs, logs, screenshots, or signing keys in issues or commits.

Project code is distributed under **GPL-3.0-or-later**. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
