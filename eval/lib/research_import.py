"""Extract one completed tool response, never a model's reconstruction of it."""
from __future__ import annotations

import json
from pathlib import Path
import re

from eval.lib.research_store import ResearchError, sensitive

_MAX_LINE = 16 * 1024 * 1024


def _check_native_completion(output, payload):
    if payload.get('is_error') or payload.get('status') in ('failed', 'error', 'running', 'inProgress', 'interrupted', 'cancelled'):
        raise ResearchError('tool_failed', '工具结果失败或尚未完成，不能归档为成功资料')
    blocks = output if isinstance(output, list) else [output]
    for block in blocks:
        text = block.get('text', '') if isinstance(block, dict) and 'type' in block else block
        envelope = text
        if isinstance(text, str):
            exits = re.findall(r'(?i)(?:process exited with code|exit code:)\s*(-?\d+)', text)
            if any(int(code) != 0 for code in exits) or re.search(r'(?i)(script running with cell ID|process running with session ID)', text):
                raise ResearchError('tool_failed', '命令失败或仍在运行，不能归档未完成的资料')
            try:
                envelope = json.loads(text)
            except ValueError:
                continue
        if isinstance(envelope, dict) and (envelope.get('isError') or envelope.get('is_error') or
                envelope.get('exit_code', 0) not in (None, 0) or
                (envelope.get('session_id') is not None and envelope.get('exit_code') is None)):
            raise ResearchError('tool_failed', '工具返回失败或运行中的结果，不能归档')


def _select_content(content, content_index):
    if isinstance(content, str):
        if content_index not in (None, 0):
            raise ResearchError('input', '工具响应没有指定的内容块')
        return content.encode(), 'text/plain'
    if not isinstance(content, list):
        # Preserve actual structured results, never evaluate stringified objects.
        if isinstance(content, dict) and content_index is None:
            return json.dumps(content, ensure_ascii=False, separators=(',', ':')).encode(), 'application/json'
        raise ResearchError('unsupported', '工具结果格式不支持直接导入，请取得原文件')
    if content_index is None:
        if len(content) != 1:
            raise ResearchError('ambiguous', '工具响应有多个内容块，需要明确 --content-index')
        content_index = 0
    if not 0 <= content_index < len(content):
        raise ResearchError('input', '工具响应内容块编号无效')
    block = content[content_index]
    if not isinstance(block, dict) or block.get('type') not in ('text', 'input_text', 'output_text') or not isinstance(block.get('text'), str):
        raise ResearchError('unsupported', '非文本工具内容需要原文件，不能伪造正文')
    return block['text'].encode(), 'text/plain'


def extract_tool_result(transcript: Path, *, format: str, call_id: str, thread_id: str | None = None,
                        content_index: int | None = None) -> dict:
    if format not in ('claude-jsonl', 'codex-jsonl'):
        raise ResearchError('unsupported', '会话格式未经验证，不能导入工具消息；可改用原文件')
    if not call_id:
        raise ResearchError('input', '需要精确的工具调用编号')
    calls, results, session_id = set(), [], None
    try:
        with transcript.open('rb') as stream:
            number = 0
            while True:
                raw = stream.readline(_MAX_LINE + 1)
                if not raw:
                    break
                number += 1
                if len(raw) > _MAX_LINE:
                    raise ResearchError('record_too_large', '会话单行超过导入上限，未完整核对结果')
                if not raw.endswith(b'\n'):
                    # A live writer may have left a partial record, even valid JSON.
                    raise ResearchError('incomplete', '会话末行尚未完整写入，请稍后重试')
                try:
                    entry = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ResearchError('record_invalid', '会话包含无法解析的记录，不能确定结果') from None
                if not isinstance(entry, dict):
                    raise ResearchError('record_invalid', '会话记录不是对象')
                if format == 'codex-jsonl':
                    payload = entry.get('payload', {})
                    if entry.get('type') == 'session_meta':
                        session_id = payload.get('id')
                    if entry.get('type') != 'response_item' or not isinstance(payload, dict):
                        continue
                    owner = entry.get('thread_id') or session_id
                    if payload.get('call_id') != call_id:
                        continue
                    if payload.get('type') in ('function_call', 'custom_tool_call'):
                        calls.add(owner)
                    elif payload.get('type') in ('function_call_output', 'custom_tool_call_output'):
                        if thread_id is None or owner == thread_id:
                            _check_native_completion(payload.get('output'), payload)
                        results.append((owner, number, payload.get('output'), False))
                else:
                    message = entry.get('message') or {}
                    content = message.get('content', [])
                    if not isinstance(content, list):
                        continue
                    owner = entry.get('thread_id') or entry.get('parent_tool_use_id') or entry.get('sessionId')
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get('type') == 'tool_use' and block.get('id') == call_id:
                            calls.add(owner)
                        elif block.get('type') == 'tool_result' and block.get('tool_use_id') == call_id:
                            results.append((owner, number, block.get('content'), bool(block.get('is_error'))))
    except OSError:
        raise ResearchError('io', '指定会话记录无法读取') from None
    matching = [r for r in results if r[0] in calls and (thread_id is None or r[0] == thread_id)]
    if len(matching) != 1:
        raise ResearchError('ambiguous' if matching else 'incomplete', '未找到唯一、已完成且归属明确的工具结果')
    owner, number, content, failed = matching[0]
    if failed:
        raise ResearchError('tool_failed', '工具调用失败，不能登记为成功取得的原始资料')
    body, media_type = _select_content(content, content_index)
    if not body.strip():
        raise ResearchError('empty', '工具未返回可保存的资料')
    text = body.decode('utf-8')
    if sensitive(text):
        raise ResearchError('sensitive', '工具结果含疑似身份信息，未导入')
    truncated = bool(re.search(r'(?i)(output truncated|truncated output|original token count|内容已截断|输出已截断)', text))
    return {'bytes': body, 'media_type': media_type, 'representation': 'tool_response',
            'completeness': 'excerpt' if truncated else 'unknown',
            'provenance': {'format': format, 'call_id': call_id, 'thread_id': owner,
                           'record_line': number, 'content_index': content_index,
                           'source': str(transcript.resolve()), 'source_metadata': 'model_supplied'}}
