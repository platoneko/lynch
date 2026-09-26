#!/usr/bin/env bash
# Build tsnetlib into app/libs/tsnetlib.aar for the Android shell.
set -euo pipefail
cd "$(dirname "$0")/tsnetlib"
: "${ANDROID_HOME:=$HOME/Android/Sdk}"
: "${ANDROID_NDK_HOME:=/opt/android-ndk-r29}"
export ANDROID_HOME ANDROID_NDK_HOME GOTOOLCHAIN=auto
mkdir -p ../app/libs
gomobile bind -target=android/arm64 -androidapi 26 -o ../app/libs/tsnetlib.aar .
echo "built $(ls -l ../app/libs/tsnetlib.aar)"
