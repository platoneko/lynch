#!/usr/bin/env -S just --justfile
# lynch local shortcuts — dedicated neige for browsing frameworks/_INDEX.md
# Run `just` or `just --list` to see recipes.

# Main neige (../neige) already owns :3131; this instance uses a separate port
# so session state under .neige/ stays isolated to this repo.
port := "3141"
host := "0.0.0.0"
pid_file := "_local/tmp/neige-lynch.pid"
log_file := "_local/tmp/neige-lynch.log"

# Binary + static assets live in the sibling neige checkout (not this repo).
neige_root := absolute_path("../neige")
neige_bin := neige_root / "target/release/neige-server"

tailscale_ip := `tailscale ip -4 2>/dev/null || true`
tailscale_host := `tailscale status --json 2>/dev/null | python3 -c "import sys,json; n=(json.load(sys.stdin).get('Self') or {}).get('DNSName') or ''; print(n.rstrip('.').split('.')[0] if n else '')" 2>/dev/null || true`

[private]
default:
    @just --list --unsorted

# ── neige (lynch-dedicated) ──────────────────────────────────────────────

# Foreground — Ctrl-C stops. cwd = this repo so .neige/ lands here.
[doc('前台启动 lynch 专用 neige（{{host}}:{{port}}）')]
run *args:
    #!/usr/bin/env bash
    set -euo pipefail
    bin="{{neige_bin}}"
    if [ ! -x "$bin" ]; then
      echo "missing $bin — build with: (cd {{neige_root}} && just build)" >&2
      exit 1
    fi
    # Strip agent/CI nesting markers so child claude sessions get their own
    # transcripts (same as ../neige/justfile). NEIGE_TIE_TO_PARENT so daemons
    # die with this server.
    exec env -u CLAUDE_CODE_CHILD_SESSION \
        -u CLAUDE_CODE_SESSION_ID \
        -u CLAUDE_CODE_EXECPATH \
        -u CLAUDE_CODE_ENTRYPOINT \
        -u CLAUDECODE \
        -u NO_COLOR \
        -u CLICOLOR \
        -u CLICOLOR_FORCE \
        -u FORCE_COLOR \
      TERM=xterm-256color \
      COLORTERM=truecolor \
      NEIGE_TIE_TO_PARENT="${NEIGE_TIE_TO_PARENT:-1}" \
      "$bin" \
        --port {{port}} \
        --listen {{host}} \
        --allowed-cidr 100.64.0.0/10 \
        {{args}}

# Background — nohup + pid file under _local/tmp/
[doc('后台启动 lynch 专用 neige（{{host}}:{{port}}）')]
start *args:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p _local/tmp
    bin="{{neige_bin}}"
    if [ ! -x "$bin" ]; then
      echo "missing $bin — build with: (cd {{neige_root}} && just build)" >&2
      exit 1
    fi
    if [ -f "{{pid_file}}" ] && kill -0 "$(cat "{{pid_file}}")" 2>/dev/null; then
      echo "already running pid=$(cat {{pid_file}}) — http://127.0.0.1:{{port}}  (just stop first to restart)"
      exit 0
    fi
    rm -f "{{pid_file}}"
    # Same env stripping as `just run`; do not use `exec` so we can capture pid.
    nohup env -u CLAUDE_CODE_CHILD_SESSION \
        -u CLAUDE_CODE_SESSION_ID \
        -u CLAUDE_CODE_EXECPATH \
        -u CLAUDE_CODE_ENTRYPOINT \
        -u CLAUDECODE \
        -u NO_COLOR \
        -u CLICOLOR \
        -u CLICOLOR_FORCE \
        -u FORCE_COLOR \
      TERM=xterm-256color \
      COLORTERM=truecolor \
      NEIGE_TIE_TO_PARENT="${NEIGE_TIE_TO_PARENT:-1}" \
      "$bin" \
        --port {{port}} \
        --listen {{host}} \
        --allowed-cidr 100.64.0.0/10 \
        {{args}} \
      >>"{{log_file}}" 2>&1 </dev/null &
    echo $! >"{{pid_file}}"
    sleep 0.5
    if ! kill -0 "$(cat "{{pid_file}}")" 2>/dev/null; then
      echo "failed to start — tail {{log_file}}" >&2
      exit 1
    fi
    echo "neige-lynch backgrounded pid=$(cat {{pid_file}})"
    echo "  local     http://127.0.0.1:{{port}}"
    ts_host="{{tailscale_host}}"
    ts_ip="{{tailscale_ip}}"
    if [ -n "$ts_host" ]; then
      echo "  tailscale http://${ts_host}:{{port}}"
    fi
    if [ -n "$ts_ip" ]; then
      echo "  ts-ip     http://${ts_ip}:{{port}}  (peers; same-host hairpin may fail)"
    fi
    echo "  log       tail -f {{log_file}}"
    echo "  open      frameworks/_INDEX.md after login"

# Alias used in the design note
[doc('同 just start')]
neige *args:
    @just start {{args}}

[doc('停止 lynch 专用 neige（:{{port}}）')]
stop:
    #!/usr/bin/env bash
    set -euo pipefail
    stopped=0
    if [ -f "{{pid_file}}" ]; then
      pid=$(cat "{{pid_file}}")
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        # Give NEIGE_TIE_TO_PARENT daemons a moment to exit with the server.
        sleep 0.4
        kill -9 "$pid" 2>/dev/null || true
        echo "stopped pid=$pid"
        stopped=1
      fi
      rm -f "{{pid_file}}"
    fi
    if command -v fuser >/dev/null 2>&1; then
      if fuser "{{port}}/tcp" >/dev/null 2>&1; then
        fuser -k "{{port}}/tcp" >/dev/null 2>&1 || true
        echo "cleared listeners on :{{port}}"
        stopped=1
      fi
    fi
    if [ "$stopped" -eq 0 ]; then
      echo "nothing listening on :{{port}}"
    fi

[doc('重启 lynch 专用 neige')]
restart *args:
    @just stop
    @just start {{args}}

[doc('查看 lynch neige 状态')]
status:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "port={{port}} host={{host}}"
    echo "bin={{neige_bin}}"
    if [ -f "{{pid_file}}" ] && kill -0 "$(cat "{{pid_file}}")" 2>/dev/null; then
      echo "pid=$(cat {{pid_file}}) (from {{pid_file}})"
    else
      echo "pid_file: not running"
    fi
    ss -ltnp 2>/dev/null | rg ":{{port}}\b" || echo "ss: nothing on :{{port}}"
    echo "local     http://127.0.0.1:{{port}}"
    ts_ip="{{tailscale_ip}}"
    ts_host="{{tailscale_host}}"
    if [ -n "$ts_host" ]; then
      echo "tailscale http://${ts_host}:{{port}}"
    elif [ -n "$ts_ip" ]; then
      echo "tailscale http://${ts_ip}:{{port}}"
    else
      echo "tailscale (unavailable — is tailscale up?)"
    fi

[doc('跟踪 neige 日志')]
logs:
    @mkdir -p _local/tmp
    @touch {{log_file}}
    tail -f {{log_file}}

# ── index ────────────────────────────────────────────────────────────────

[doc('从 frameworks/*.md 题头重生 _INDEX.md')]
gen-index:
    python3 scripts/gen_index.py
