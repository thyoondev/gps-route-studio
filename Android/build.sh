#!/bin/zsh
set -eu
cd "$(dirname "$0")"
root="$PWD"
sdk="${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}"
build_tools="$sdk/build-tools/36.0.0"
java_root=$(/usr/libexec/java_home -v 17)
export JAVA_HOME="$java_root"
build=$(mktemp -d /private/tmp/gpsroute-android.XXXXXX)
trap 'rm -rf "$build"' EXIT
mkdir -p "$build/classes" "$build/dex"
"$build_tools/aapt2" link -I "$sdk/platforms/android-35/android.jar" --manifest AndroidManifest.xml -o "$build/base.apk"
"$java_root/bin/javac" --release 8 -cp "$sdk/platforms/android-35/android.jar" -d "$build/classes" src/local/gpsroute/bridge/*.java
"$java_root/bin/jar" cf "$build/classes.jar" -C "$build/classes" .
"$build_tools/d8" --min-api 31 --lib "$sdk/platforms/android-35/android.jar" --output "$build/dex" "$build/classes.jar"
(cd "$build/dex"; /usr/bin/zip -q "$build/base.apk" classes.dex)
"$build_tools/zipalign" -p -f 4 "$build/base.apk" "$build/aligned.apk"
key="$root/.local/android-signing.keystore"
if [ ! -f "$key" ]; then
  mkdir -p "$(dirname "$key")"
  "$java_root/bin/keytool" -genkeypair -keystore "$key" -storepass android -keypass android -alias gpsroute-debug -keyalg RSA -keysize 2048 -validity 3650 -dname 'CN=GPS Route Studio Development'
  chmod 600 "$key"
fi
"$build_tools/apksigner" sign --ks "$key" --ks-key-alias gpsroute-debug --ks-pass pass:android --key-pass pass:android --out "$root/GPSRouteBridge.apk" "$build/aligned.apk"
"$build_tools/apksigner" verify "$root/GPSRouteBridge.apk"
