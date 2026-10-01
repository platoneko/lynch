#!/usr/bin/env bash
# Build tsnetlib.aar, then the debug APK. Gradle ignores http(s)_proxy, so pass it explicitly.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Android/Sdk}"
export PATH="$HOME/go/bin:$PATH"
"$here/build-aar.sh"
proxy_opts=()
if [ -n "${https_proxy:-}" ]; then
  hp="${https_proxy#*://}"; hp="${hp%/}"
  proxy_opts=(-Dhttps.proxyHost="${hp%:*}" -Dhttps.proxyPort="${hp##*:}" -Dhttp.proxyHost="${hp%:*}" -Dhttp.proxyPort="${hp##*:}")
fi
cd "$here"
# Gradle 8.10.2 cannot run on the user's default JDK 25; build with a full JDK 17.
export JAVA_HOME="${ANDROID_JAVA_HOME:-$HOME/.local/opt/jdk-17}"
echo "sdk.dir=$ANDROID_HOME" > local.properties
./gradlew "${proxy_opts[@]}" --no-daemon assembleDebug
ls -l app/build/outputs/apk/debug/app-debug.apk tailfwd/build/outputs/apk/debug/tailfwd-debug.apk
