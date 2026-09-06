"""Feishu custom-bot webhook notify (text + optional @所有人)."""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_WEBHOOK_ENV = "FEISHU_ALERT_WEBHOOK"
_HTTP_TIMEOUT_S = 10
_AT_ALL = '<at user_id="all">所有人</at>'

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_toml_webhook(path: Path, *key_path: str) -> str:
    if not path.is_file():
        return ""
    try:
        import tomllib

        data: Any = tomllib.loads(path.read_text(encoding="utf-8"))
        cur: Any = data
        for k in key_path:
            if not isinstance(cur, dict):
                return ""
            cur = cur.get(k)
        return str(cur or "").strip()
    except Exception:
        return ""


def _load_config_webhook() -> str:
    """Optional local config/daily.toml [feishu].webhook — never commit secrets."""
    return _load_toml_webhook(REPO_ROOT / "config" / "daily.toml", "feishu", "webhook")


def _load_invest_webhook() -> str:
    """Same source as ../invest alerts: config/policies.toml [alerts].feishu_webhook."""
    invest_policies = REPO_ROOT.parent / "invest" / "config" / "policies.toml"
    return _load_toml_webhook(invest_policies, "alerts", "feishu_webhook")


def resolve_webhook() -> str:
    """Env FEISHU_ALERT_WEBHOOK → config/daily.toml → ../invest policies.toml."""
    env = (os.environ.get(_WEBHOOK_ENV) or "").strip()
    if env:
        return env
    local = _load_config_webhook()
    if local:
        return local
    return _load_invest_webhook()


def _http_post_json(url: str, payload: dict) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
            ok = 200 <= int(status) < 300
            # Feishu returns HTTP 200 with code != 0 on logical errors
            try:
                parsed = json.loads(body) if body else {}
                if isinstance(parsed, dict) and parsed.get("code") not in (None, 0):
                    ok = False
                    log.warning("feishu logical error: %s", body)
            except json.JSONDecodeError:
                pass
            return {"ok": ok, "status": int(status), "body": body}
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except Exception:
            pass
        log.warning("feishu webhook HTTPError status=%s: %s", e.code, e)
        return {"ok": False, "status": int(e.code), "body": ""}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("feishu webhook request failed: %s", e)
        return {"ok": False, "status": None, "body": ""}


def _dispatch(payload: dict[str, Any], *, at_all: bool, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        log.info("feishu dry_run at_all=%s payload=%s", at_all, payload)
        return {
            "ok": True,
            "status": None,
            "dry_run": True,
            "skipped_reason": None,
            "at_all": at_all,
        }

    webhook = resolve_webhook()
    if not webhook:
        return {
            "ok": False,
            "status": None,
            "dry_run": False,
            "skipped_reason": "missing_webhook",
            "at_all": at_all,
        }

    result = _http_post_json(webhook, payload)
    return {
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "dry_run": False,
        "skipped_reason": None,
        "at_all": at_all,
    }


def send_text(
    title: str,
    body: str,
    *,
    at_all: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Send Feishu plain text. at_all prefixes official @所有人 tag for custom bots."""
    parts: list[str] = []
    if at_all:
        parts.append(_AT_ALL)
    if title:
        parts.append(title)
    if body:
        parts.append(body)
    text = "\n".join(parts)
    payload = {"msg_type": "text", "content": {"text": text}}
    return _dispatch(payload, at_all=at_all, dry_run=dry_run)


def send_post(
    title: str,
    lines: list[str],
    *,
    at_all: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Send Feishu rich-text post (zh_cn). Each line is one paragraph of text tags."""
    content: list[list[dict[str, str]]] = []
    if at_all:
        content.append(
            [
                {"tag": "at", "user_id": "all"},
                {"tag": "text", "text": " "},
            ]
        )
    for line in lines:
        content.append([{"tag": "text", "text": line if line.endswith("\n") else line + "\n"}])
    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": content,
                }
            }
        },
    }
    return _dispatch(payload, at_all=at_all, dry_run=dry_run)
