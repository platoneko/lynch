#!/usr/bin/env -S just --justfile
# lynch local shortcuts — dedicated neige for browsing frameworks/_INDEX.md
# Run `just` or `just --list` to see recipes.

# Main neige (../neige) already owns :3131; this instance uses a separate port
# so session state under .neige/ stays isolated to this repo.
port := "3141"
host := "0.0.0.0"
pid_file := "_local/tmp/neige-lynch.pid"
log_file := "_local/tmp/neige-lynch.log"
# Dedicated auth (hash only on disk). Do NOT share ~/.config/neige/auth.json
# with the main :3131 server — rotate here won't invalidate that one.
# Auth is a random token (neige default). Plaintext is shown once at first
# boot or via `just auth-rotate` (stop → rotate → start). Optional override:
# `NEIGE_PASSWORD=... just start` (not the default).
auth_file := "_local/neige-lynch-auth.json"
# Reachable URL the user asked for (tailscale1 / neko net).
public_origin := "http://100.93.210.104:" + port

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
    mkdir -p _local
    # Pass NEIGE_PASSWORD through only if the caller set it; never default it.
    extra=()
    if [ -n "${NEIGE_PASSWORD:-}" ]; then
      extra+=(NEIGE_PASSWORD="$NEIGE_PASSWORD")
    fi
    exec env -u CLAUDE_CODE_CHILD_SESSION \
        -u CLAUDE_CODE_SESSION_ID \
        -u CLAUDE_CODE_EXECPATH \
        -u CLAUDE_CODE_ENTRYPOINT \
        -u CLAUDECODE \
        -u NO_COLOR \
        -u CLICOLOR \
        -u CLICOLOR_FORCE \
        -u FORCE_COLOR \
        -u NEIGE_PASSWORD \
      TERM=xterm-256color \
      COLORTERM=truecolor \
      NEIGE_TIE_TO_PARENT="${NEIGE_TIE_TO_PARENT:-1}" \
      "${extra[@]}" \
      "$bin" \
        --port {{port}} \
        --listen {{host}} \
        --auth-file "{{auth_file}}" \
        --allowed-origin "{{public_origin}}" \
        --allowed-cidr 100.64.0.0/10 \
        --allowed-cidr 10.8.0.0/24 \
        --tailscale-socket /run/tailscale-neko/tailscaled.sock \
        --tailscale-socket /home/neko/.config/tailscale-plat/sock \
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
      echo "already running pid=$(cat {{pid_file}}) — {{public_origin}}  (just stop first to restart)"
      exit 0
    fi
    rm -f "{{pid_file}}"
    extra=()
    if [ -n "${NEIGE_PASSWORD:-}" ]; then
      extra+=(NEIGE_PASSWORD="$NEIGE_PASSWORD")
    fi
    # Same env stripping as `just run`; do not use `exec` so we can capture pid.
    # Unset NEIGE_PASSWORD in the outer env when empty so a leftover shell export
    # cannot rewrite the random-token hash on start.
    nohup env -u CLAUDE_CODE_CHILD_SESSION \
        -u CLAUDE_CODE_SESSION_ID \
        -u CLAUDE_CODE_EXECPATH \
        -u CLAUDE_CODE_ENTRYPOINT \
        -u CLAUDECODE \
        -u NO_COLOR \
        -u CLICOLOR \
        -u CLICOLOR_FORCE \
        -u FORCE_COLOR \
        -u NEIGE_PASSWORD \
      TERM=xterm-256color \
      COLORTERM=truecolor \
      NEIGE_TIE_TO_PARENT="${NEIGE_TIE_TO_PARENT:-1}" \
      "${extra[@]}" \
      "$bin" \
        --port {{port}} \
        --listen {{host}} \
        --auth-file "{{auth_file}}" \
        --allowed-origin "{{public_origin}}" \
        --allowed-cidr 100.64.0.0/10 \
        --allowed-cidr 10.8.0.0/24 \
        --tailscale-socket /run/tailscale-neko/tailscaled.sock \
        --tailscale-socket /home/neko/.config/tailscale-plat/sock \
        {{args}} \
      >>"{{log_file}}" 2>&1 </dev/null &
    echo $! >"{{pid_file}}"
    sleep 0.5
    if ! kill -0 "$(cat "{{pid_file}}")" 2>/dev/null; then
      echo "failed to start — tail {{log_file}}" >&2
      exit 1
    fi
    echo "neige-lynch backgrounded pid=$(cat {{pid_file}})"
    echo "  public    {{public_origin}}"
    echo "  local     http://127.0.0.1:{{port}}"
    echo "  auth      {{auth_file}}  (random token; plaintext only via just auth-rotate)"
    echo "  log       tail -f {{log_file}}"
    echo "  open      frameworks/_INDEX.md after login"
    if login=$(rg -o 'https?://[^ ]+/login#token=[^ ]+' "{{log_file}}" 2>/dev/null | tail -1); then
      # Rewrite loopback URL from the log to the public origin.
      token="${login##*#token=}"
      echo "  login     {{public_origin}}/login#token=${token}"
    fi

# Fresh random token: stop → rotate → start (process must reload hash).
[doc('轮换随机 token（停→rotate→启）并打印登录 URL')]
auth-rotate:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p _local _local/tmp
    bin="{{neige_bin}}"
    if [ ! -x "$bin" ]; then
      echo "missing $bin — build with: (cd {{neige_root}} && just build)" >&2
      exit 1
    fi
    just stop
    # Ensure start does not re-apply a leftover NEIGE_PASSWORD over the new hash.
    unset NEIGE_PASSWORD || true
    out=$("$bin" --auth-file "{{auth_file}}" auth rotate)
    token=$(printf '%s\n' "$out" | awk '/^  /{print $1; exit}')
    if [ -z "${token:-}" ]; then
      echo "failed to rotate — check auth-file {{auth_file}}" >&2
      echo "$out" >&2
      exit 1
    fi
    printf '%s\n' "$token" >_local/tmp/neige-lynch-token.txt
    chmod 600 _local/tmp/neige-lynch-token.txt
    just start
    # Verify the running process accepts this token before printing it.
    code=$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' \
      -X POST "http://127.0.0.1:{{port}}/login/submit" \
      -H 'Content-Type: application/json' \
      -H 'Origin: http://127.0.0.1:{{port}}' \
      -d "{\"token\":\"${token}\"}" || true)
    if [ "$code" != "204" ]; then
      echo "token minted but login probe got HTTP ${code:-?} — not printing URL" >&2
      exit 1
    fi
    echo "Token rotated + server reloaded (probe 204). Old URLs are dead."
    echo "Copy the whole line (token may contain - and _):"
    echo "{{public_origin}}/login#token=${token}"
    echo "http://127.0.0.1:{{port}}/login#token=${token}"

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
    echo "auth={{auth_file}}"
    if [ -f "{{pid_file}}" ] && kill -0 "$(cat "{{pid_file}}")" 2>/dev/null; then
      echo "pid=$(cat {{pid_file}}) (from {{pid_file}})"
    else
      echo "pid_file: not running"
    fi
    ss -ltnp 2>/dev/null | rg ":{{port}}\b" || echo "ss: nothing on :{{port}}"
    echo "public    {{public_origin}}"
    echo "local     http://127.0.0.1:{{port}}"
    ts_ip="{{tailscale_ip}}"
    ts_host="{{tailscale_host}}"
    if [ -n "$ts_host" ]; then
      echo "magicdns  http://${ts_host}:{{port}}  (default tailscale0 — may differ from public)"
    elif [ -n "$ts_ip" ]; then
      echo "ts0-ip    http://${ts_ip}:{{port}}"
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

# ── mobile reader (app/) ────────────────────────────────────────────────
# Separate from the neige instance above: its own port, pid and log.
app_host := "100.93.210.104"
app_port := "3151"
app_pid := "_local/tmp/lynch-app.pid"
app_log := "_local/tmp/lynch-app.log"

[doc('构建手机阅读 app 前端（app/web/dist）')]
app-web:
    cd app/web && npm ci && npm run build

[doc('后台启动手机阅读 app 服务端（只绑 tailnet 地址）')]
app-start:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p _local/tmp
    if [ ! -f app/web/dist/index.html ]; then
      echo "missing app/web/dist — run: just app-web" >&2
      exit 1
    fi
    if [ -f "{{app_pid}}" ] && kill -0 "$(cat "{{app_pid}}")" 2>/dev/null; then
      echo "already running pid=$(cat {{app_pid}}) — http://{{app_host}}:{{app_port}}"
      exit 0
    fi
    (cd app/server && uv sync --quiet)
    # Run the venv python directly so the pid file holds the server, not a wrapper.
    PYTHONPATH=app/server nohup app/server/.venv/bin/python -m lynch_reader.web \
        --host {{app_host}} --port {{app_port}} >>"{{app_log}}" 2>&1 </dev/null &
    echo $! >"{{app_pid}}"
    sleep 1
    if ! kill -0 "$(cat "{{app_pid}}")" 2>/dev/null; then
      echo "failed to start — tail {{app_log}}" >&2
      exit 1
    fi
    echo "lynch-app pid=$(cat {{app_pid}})  http://{{app_host}}:{{app_port}}  log={{app_log}}"

[doc('停止手机阅读 app 服务端')]
app-stop:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -f "{{app_pid}}" ] && kill -0 "$(cat "{{app_pid}}")" 2>/dev/null; then
      pid=$(cat "{{app_pid}}")
      kill "$pid"
      # The server gives open chat streams 5 s, then stops its claude processes; wait for that.
      for _ in $(seq 30); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
      done
      if kill -0 "$pid" 2>/dev/null; then
        # Keep the pid file so app-start still sees it and does not start a second server.
        echo "pid=$pid still running after 15 s — check {{app_log}}, or kill -9 $pid" >&2
        exit 1
      fi
      echo "stopped pid=$pid"
    else
      echo "not running"
    fi
    rm -f "{{app_pid}}"

[doc('查看手机阅读 app 服务端状态')]
app-status:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -f "{{app_pid}}" ] && kill -0 "$(cat "{{app_pid}}")" 2>/dev/null; then
      echo "pid=$(cat {{app_pid}})"
    else
      echo "pid: not running"
    fi
    code=$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' "http://{{app_host}}:{{app_port}}/pair" || true)
    echo "GET /pair -> ${code:-unreachable}"

[doc('生成一次性配对码（10 分钟有效），新设备在 /pair 输入；name 省略时用当前时间')]
app-pair name="":
    @(cd app/server && uv sync --quiet)
    @PYTHONPATH=app/server app/server/.venv/bin/python -m lynch_reader.auth pair -- {{quote(name)}}

[doc('列出已配对设备（id、配对时间、名字）')]
app-devices:
    @(cd app/server && uv sync --quiet)
    @PYTHONPATH=app/server app/server/.venv/bin/python -m lynch_reader.auth devices

[doc('吊销一台设备，立即生效')]
app-revoke id:
    @(cd app/server && uv sync --quiet)
    @PYTHONPATH=app/server app/server/.venv/bin/python -m lynch_reader.auth revoke {{quote(id)}}

[doc('发现新票：读 invest 的 KOL 的 X 列表与回看范围（skill discover-kol 用）')]
kol-sources days="2":
    @(cd app/server && uv sync --quiet)
    @PYTHONPATH=app/server app/server/.venv/bin/python .agents/skills/discover-kol/scripts/kol_sources.py --days {{days}}

[doc('发现新票：把回答文件里点名的候选存进候选区，如 just candidates-save _local/tmp/discover.md AXTI.US 0700.HK')]
candidates-save file +tickers:
    @(cd app/server && uv sync --quiet)
    @PYTHONPATH=app/server app/server/.venv/bin/python .agents/skills/discover-kol/scripts/save_candidates.py {{quote(file)}} {{tickers}}

[doc('构建手机阅读 app 的 debug APK')]
app-apk:
    app/android/build-apk.sh
