"""Read the shared repository MCP configuration without persisting credentials."""
from __future__ import annotations

import json
from pathlib import Path


def load_mcp_servers(repo_root: Path) -> dict:
    path = repo_root / ".mcp.json"
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("无法读取仓库 MCP 配置，请检查 .mcp.json") from None
    if not isinstance(document, dict):
        raise ValueError("仓库 MCP 配置必须是 JSON 对象")
    servers = document.get("mcpServers", {})
    if servers is None:
        return {}
    if not isinstance(servers, dict):
        raise ValueError("仓库 MCP 配置的 mcpServers 必须是对象")
    return servers


def codex_wisburg_config(repo_root: Path, env: dict[str, str]) -> dict:
    """Only Wisburg is bridged; Longbridge stays on the CLI's existing login.

    Header values go into this child's environment, never JSON-RPC config, argv or app logs.
    The caller supplies a private environment so simultaneous conversations cannot overwrite credentials.
    """
    servers = load_mcp_servers(repo_root)
    if "wisburg" not in servers:
        return {}
    server = servers["wisburg"]
    if not isinstance(server, dict) or server.get("type") != "http":
        raise ValueError("Codex 的 Wisburg MCP 配置必须使用 http 类型")
    url, headers = server.get("url"), server.get("headers", {})
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ValueError("Wisburg MCP 配置缺少有效的 HTTP 地址")
    if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                            for k, v in headers.items()):
        raise ValueError("Wisburg MCP 请求头名称和值必须是字符串")
    header_env = {header: f"LYNCH_CODEX_WISBURG_HEADER_{n}" for n, header in enumerate(headers)}
    env.update({variable: headers[header] for header, variable in header_env.items()})
    return {"mcp_servers.wisburg": {"url": url, "enabled": True, "env_http_headers": header_env}}
