#!/bin/zsh
set -eu
cd "$(dirname "$0")"
staging=$(mktemp -d /private/tmp/gpsroute-build.XXXXXX)
trap 'rm -rf "$staging"' EXIT
app="$staging/GPS Route Studio.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp -X Studio.swift RouteMap.swift Diagnostics.swift "$staging/"
xcrun swiftc -parse-as-library -module-cache-path "$staging/cache" -target arm64-apple-macos14.0 "$staging/Studio.swift" "$staging/RouteMap.swift" "$staging/Diagnostics.swift" -o "$app/Contents/MacOS/GPSRouteStudio"
cp worker.py motion_worker.py diagnostic_worker.py android_worker.py gpsroute.py "$app/Contents/Resources/"
mkdir -p "$app/Contents/Resources/Android"
cp Android/GPSRouteBridge.apk "$app/Contents/Resources/Android/"
mkdir -p "$app/Contents/Resources/Motion"
cp Receiver.swift GPSMotionUITests.swift "$app/Contents/Resources/Motion/"
ditto --noextattr GPSReceiver.xcodeproj "$app/Contents/Resources/Motion/GPSReceiver.xcodeproj"
cp Info.plist "$app/Contents/"
codesign --force --sign - "$app"
ditto --noextattr "$app" 'GPS Route Studio.app'
