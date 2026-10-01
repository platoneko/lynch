#!/usr/bin/env bash
# Build tsnetlib into libs/tsnetlib.aar, shared by the lynch and tailfwd shells.
set -euo pipefail
cd "$(dirname "$0")/tsnetlib"
: "${ANDROID_HOME:=$HOME/Android/Sdk}"
: "${ANDROID_NDK_HOME:=/opt/android-ndk-r29}"
export ANDROID_HOME ANDROID_NDK_HOME GOTOOLCHAIN=auto
# gomobile builds its generated main package under $TMPDIR; VCS stamping there is
# meaningless and fails outright if a stray .git sits above it (e.g. /tmp/.git).
export GOFLAGS="${GOFLAGS:+$GOFLAGS }-buildvcs=false"
mkdir -p ../libs
gomobile bind -target=android/arm64 -androidapi 26 -o ../libs/tsnetlib.aar .
echo "built $(ls -l ../libs/tsnetlib.aar)"
