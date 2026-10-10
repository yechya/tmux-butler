#!/usr/bin/env python3
"""protocol.py - ACOM (Agent Collaboration Object Message) structured messaging.

Lightweight cross-window agent communication protocol for tmux-butler.
Renders messages as terminal-friendly text, parses from captured pane output,
and provides a CLI for sending structured messages via tmux_bridge.

设计原则:
  - 最小可用元数据，不搞官僚协议
  - 渲染结果对 agent 友好、紧凑
  - CJK 字符宽度正确处理
  - 向下兼容 tmux_bridge.send_keys 纯文本发送
"""

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add current script dir to path for tmux_bridge import
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmux_bridge

# ──────────────────────────────────────────────
# Message Types
# ──────────────────────────────────────────────

VALID_MSG_TYPES = frozenset({
    'TASK_ASSIGN',
    'TASK_UPDATE',
    'TASK_COMPLETE',
    'RELAY',
    'NUDGE',
    'BROADCAST',
})


# ──────────────────────────────────────────────
# Data Model
# ──────────────────────────────────────────────

@dataclass
class ACOMMessage:
    """Agent Collaboration Object Message.

    Minimal structured message for inter-agent communication.
    Most fields are optional — only msg_type, sender, and recipient are required.
    """
    msg_type: str               # TASK_ASSIGN | TASK_UPDATE | TASK_COMPLETE | RELAY | NUDGE | BROADCAST
    sender: str                 # e.g. 'claude', 'butler', 'devin'
    recipient: str              # e.g. 'devin', 'all'
    task_id: Optional[str] = None       # optional task reference
    content: str = ''                   # main message body
    metadata: Optional[dict] = field(default_factory=lambda: None)
    timestamp: Optional[str] = None     # auto-filled ISO timestamp
    trace_id: Optional[str] = None      # optional, for tracking message chains

    def __post_init__(self):
        if self.msg_type not in VALID_MSG_TYPES:
            raise ValueError(f"Invalid msg_type '{self.msg_type}'. Must be one of: {', '.join(sorted(VALID_MSG_TYPES))}")
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat(timespec='seconds')


# ──────────────────────────────────────────────
# CJK Width Utilities
# ──────────────────────────────────────────────

def _display_width(text: str) -> int:
    """Calculate terminal display width, accounting for CJK double-width characters."""
    width = 0
    for ch in text:
        eaw = unicodedata.east_asian_width(ch)
        if eaw in ('W', 'F'):
            width += 2
        else:
            width += 1
    return width


def _pad_to_width(text: str, target_width: int, fill_char: str = '═') -> str:
    """Pad text line with fill_char to reach target terminal width."""
    current = _display_width(text)
    deficit = target_width - current
    if deficit <= 0:
        return text
    return text + fill_char * deficit


# ──────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────

# 渲染目标宽度 (终端列数)
RENDER_WIDTH = 50


def render_message(msg: ACOMMessage) -> str:
    """Render an ACOMMessage to terminal-friendly text for pane injection.

    Returns a compact, bordered string that agents can parse from their context.
    """
    if msg.msg_type == 'NUDGE':
        return _render_nudge(msg)
    elif msg.msg_type == 'TASK_ASSIGN':
        return _render_task_assign(msg)
    elif msg.msg_type == 'TASK_COMPLETE':
        return _render_task_complete(msg)
    elif msg.msg_type == 'TASK_UPDATE':
        return _render_task_update(msg)
    elif msg.msg_type == 'RELAY':
        return _render_relay(msg)
    elif msg.msg_type == 'BROADCAST':
        return _render_broadcast(msg)
    # Fallback (should not reach here due to __post_init__ validation)
    return _render_generic(msg)


def _header_line(label: str) -> str:
    """Build a ═══ [...] ═══ header line."""
    inner = f" {label} "
    prefix = "═══"
    raw = f"{prefix}{inner}"
    return _pad_to_width(raw, RENDER_WIDTH, '═')


def _footer_line() -> str:
    return '═' * RENDER_WIDTH


def _render_task_assign(msg: ACOMMessage) -> str:
    meta = msg.metadata or {}
    header_label = f"TASK_ASSIGN · from {msg.sender}"
    if msg.task_id:
        header_label += f" · {msg.task_id}"

    lines = [_header_line(header_label)]
    if msg.content:
        lines.append(f"目标: {msg.content}")
    if meta.get('working_dir'):
        lines.append(f"工作目录: {meta['working_dir']}")
    if meta.get('test_cmd'):
        lines.append(f"验证命令: {meta['test_cmd']}")
    if meta.get('expected_files'):
        files = meta['expected_files']
        if isinstance(files, list):
            files = ', '.join(files)
        lines.append(f"交付文件: {files}")
    # Emit remaining metadata keys not already rendered
    _rendered_keys = {'working_dir', 'test_cmd', 'expected_files'}
    for k, v in meta.items():
        if k not in _rendered_keys:
            lines.append(f"{k}: {v}")
    lines.append(_footer_line())
    return '\n'.join(lines)


def _render_task_complete(msg: ACOMMessage) -> str:
    meta = msg.metadata or {}
    header_label = f"TASK_COMPLETE · from {msg.sender}"
    if msg.task_id:
        header_label += f" · {msg.task_id}"

    lines = [_header_line(header_label)]
    lines.append(f"状态: {meta.get('status', 'DONE')}")
    if msg.content:
        lines.append(f"摘要: {msg.content}")
    if meta.get('files_modified'):
        fm = meta['files_modified']
        if isinstance(fm, list):
            fm = ', '.join(fm)
        lines.append(f"修改文件: {fm}")
    if meta.get('test_results'):
        lines.append(f"测试结果: {meta['test_results']}")
    # Remaining metadata
    _rendered_keys = {'status', 'files_modified', 'test_results'}
    for k, v in meta.items():
        if k not in _rendered_keys:
            lines.append(f"{k}: {v}")
    lines.append(_footer_line())
    return '\n'.join(lines)


def _render_task_update(msg: ACOMMessage) -> str:
    meta = msg.metadata or {}
    header_label = f"TASK_UPDATE · from {msg.sender}"
    if msg.task_id:
        header_label += f" · {msg.task_id}"

    lines = [_header_line(header_label)]
    if msg.content:
        lines.append(f"进展: {msg.content}")
    if meta.get('progress'):
        lines.append(f"进度: {meta['progress']}")
    if meta.get('blockers'):
        lines.append(f"阻塞: {meta['blockers']}")
    _rendered_keys = {'progress', 'blockers'}
    for k, v in meta.items():
        if k not in _rendered_keys:
            lines.append(f"{k}: {v}")
    lines.append(_footer_line())
    return '\n'.join(lines)


def _render_relay(msg: ACOMMessage) -> str:
    header_label = f"RELAY · {msg.sender} → {msg.recipient}"
    if msg.task_id:
        header_label += f" · {msg.task_id}"

    lines = [_header_line(header_label)]
    if msg.content:
        lines.append(msg.content)
    lines.append(_footer_line())
    return '\n'.join(lines)


def _render_nudge(msg: ACOMMessage) -> str:
    lines = [_header_line("管家介入 · NUDGE")]
    if msg.content:
        # Content may be multi-line nudge text
        lines.extend(msg.content.splitlines())
    else:
        # Default nudge template
        lines.append("检测到你已静默且 CPU 空闲。")
        lines.append("如已完成当前任务，请输出结论摘要。")
        lines.append("如遇到阻塞，请描述具体问题。")
    lines.append(_footer_line())
    return '\n'.join(lines)


def _render_broadcast(msg: ACOMMessage) -> str:
    header_label = f"BROADCAST · from {msg.sender}"
    lines = [_header_line(header_label)]
    if msg.content:
        lines.extend(msg.content.splitlines())
    lines.append(_footer_line())
    return '\n'.join(lines)


def _render_generic(msg: ACOMMessage) -> str:
    """Fallback renderer for unknown types (shouldn't normally be called)."""
    header_label = f"{msg.msg_type} · from {msg.sender} → {msg.recipient}"
    if msg.task_id:
        header_label += f" · {msg.task_id}"

    lines = [_header_line(header_label)]
    if msg.content:
        lines.extend(msg.content.splitlines())
    if msg.metadata:
        for k, v in msg.metadata.items():
            lines.append(f"{k}: {v}")
    lines.append(_footer_line())
    return '\n'.join(lines)


# ──────────────────────────────────────────────
# Parsing (best-effort)
# ──────────────────────────────────────────────

# Header regex: ═══ [TYPE · from SENDER ...] ═══  or  ═══ [管家介入 · NUDGE] ═══
_HEADER_RE = re.compile(
    r'═{3}\s*\[(.+?)\]\s*═*'
)

# Sub-patterns within header content
_TYPE_SENDER_RE = re.compile(
    r'(TASK_ASSIGN|TASK_COMPLETE|TASK_UPDATE|RELAY|BROADCAST|NUDGE|管家介入)'
    r'(?:\s*·\s*(?:from\s+)?(\S+?))?'
    r'(?:\s*(?:→\s*(\S+?))?)?'
    r'(?:\s*·\s*(\S+?))?$'
)


def parse_message(text: str) -> Optional[ACOMMessage]:
    """Best-effort parse of an ACOM message from captured pane text.

    Scans for the ═══ [...] ═══ header pattern and extracts fields.
    Returns ACOMMessage if parsing succeeds, None otherwise.
    """
    if not text or '═' not in text:
        return None

    lines = text.strip().splitlines()

    # Find the header line
    header_line = None
    header_idx = None
    for i, line in enumerate(lines):
        m = _HEADER_RE.search(line)
        if m:
            header_line = m.group(1).strip()
            header_idx = i
            break

    if header_line is None:
        return None

    # Find footer (next line of solid ═ or end of text)
    body_lines = []
    for line in lines[header_idx + 1:]:
        stripped = line.strip()
        if stripped and all(ch == '═' for ch in stripped):
            break
        body_lines.append(line)

    # Parse header content
    # Handle NUDGE special case: "管家介入 · NUDGE"
    msg_type = None
    sender = 'butler'
    recipient = 'all'
    task_id = None

    if '管家介入' in header_line or header_line.strip() == 'NUDGE':
        msg_type = 'NUDGE'
    else:
        m = _TYPE_SENDER_RE.search(header_line)
        if m:
            type_str = m.group(1)
            if type_str in VALID_MSG_TYPES:
                msg_type = type_str
            sender = m.group(2) or sender
            recipient = m.group(3) or recipient
            task_id = m.group(4) or None
        else:
            # 无法识别消息类型
            return None

    if msg_type is None:
        return None

    # Extract known fields from body
    content_parts = []
    metadata = {}
    # 已知中文 label -> metadata key 映射
    label_map = {
        '目标': 'content',
        '摘要': 'content',
        '进展': 'content',
        '状态': 'status',
        '工作目录': 'working_dir',
        '验证命令': 'test_cmd',
        '交付文件': 'expected_files',
        '修改文件': 'files_modified',
        '测试结果': 'test_results',
        '进度': 'progress',
        '阻塞': 'blockers',
    }

    content_text = ''
    for line in body_lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Try to match "label: value" pattern
        kv_match = re.match(r'^(\S+?):\s+(.+)$', stripped)
        if kv_match:
            label = kv_match.group(1)
            value = kv_match.group(2)
            if label in label_map:
                mapped = label_map[label]
                if mapped == 'content':
                    content_text = value
                else:
                    metadata[mapped] = value
            else:
                metadata[label] = value
        else:
            content_parts.append(stripped)

    # If no content from labeled fields, use accumulated body lines
    if not content_text and content_parts:
        content_text = '\n'.join(content_parts)

    try:
        return ACOMMessage(
            msg_type=msg_type,
            sender=sender,
            recipient=recipient,
            task_id=task_id,
            content=content_text,
            metadata=metadata if metadata else None,
        )
    except (ValueError, TypeError):
        return None


# ──────────────────────────────────────────────
# Sending via tmux_bridge
# ──────────────────────────────────────────────

def send_acom(target: str, msg: ACOMMessage, enter: bool = True):
    """Render and send an ACOM message to a tmux pane target."""
    text = render_message(msg)
    tmux_bridge.send_keys(target, text, enter=enter)
    return text


# ──────────────────────────────────────────────
# CLI Interface
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ACOM Protocol: structured agent messaging via tmux-butler"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    # ── send ──
    p_send = subparsers.add_parser("send", help="Send a structured ACOM message to a tmux pane")
    p_send.add_argument("--type", required=True, dest="msg_type",
                        choices=sorted(VALID_MSG_TYPES),
                        help="Message type")
    p_send.add_argument("--sender", required=True, help="Sender identifier")
    p_send.add_argument("--recipient", default="all", help="Recipient identifier")
    p_send.add_argument("--task-id", default=None, help="Task reference ID")
    p_send.add_argument("--content", default="", help="Message body text")
    p_send.add_argument("--metadata", default=None,
                        help="JSON string of metadata dict")
    p_send.add_argument("--trace-id", default=None, help="Trace ID for message chain tracking")
    p_send.add_argument("--target", "-t", required=True,
                        help="tmux target pane (e.g. 0:devin)")
    p_send.add_argument("--dry-run", action="store_true",
                        help="Print rendered message without sending")

    # ── nudge ──
    p_nudge = subparsers.add_parser("nudge", help="Send a butler nudge to an agent")
    p_nudge.add_argument("--target", "-t", required=True,
                         help="tmux target pane")
    p_nudge.add_argument("--reason", default=None,
                         help="Custom nudge reason text")
    p_nudge.add_argument("--dry-run", action="store_true")

    # ── broadcast ──
    p_bcast = subparsers.add_parser("broadcast", help="Broadcast a message to all active windows")
    p_bcast.add_argument("--sender", default="butler", help="Sender identifier")
    p_bcast.add_argument("--content", required=True, help="Broadcast message body")
    p_bcast.add_argument("--session", "-s", default=None, help="tmux session name")
    p_bcast.add_argument("--dry-run", action="store_true")

    # ── render ──
    p_render = subparsers.add_parser("render", help="Render a message from JSON to terminal text (no send)")
    p_render.add_argument("--json", required=True, dest="json_str",
                          help="JSON string of ACOMMessage fields")

    args = parser.parse_args()

    if args.action == "send":
        meta = json.loads(args.metadata) if args.metadata else None
        msg = ACOMMessage(
            msg_type=args.msg_type,
            sender=args.sender,
            recipient=args.recipient,
            task_id=args.task_id,
            content=args.content,
            metadata=meta,
            trace_id=args.trace_id,
        )
        rendered = render_message(msg)
        if args.dry_run:
            print(rendered)
        else:
            send_acom(args.target, msg)
            print(f"[ACOM] Sent {args.msg_type} to {args.target} ({len(rendered)} chars)")

    elif args.action == "nudge":
        content = args.reason
        if not content:
            content = (
                "检测到你已静默且 CPU 空闲。\n"
                "如已完成当前任务，请输出结论摘要。\n"
                "如遇到阻塞，请描述具体问题。"
            )
        msg = ACOMMessage(
            msg_type='NUDGE',
            sender='butler',
            recipient='agent',
            content=content,
        )
        rendered = render_message(msg)
        if args.dry_run:
            print(rendered)
        else:
            send_acom(args.target, msg)
            print(f"[ACOM] Nudge sent to {args.target}")

    elif args.action == "broadcast":
        msg = ACOMMessage(
            msg_type='BROADCAST',
            sender=args.sender,
            recipient='all',
            content=args.content,
        )
        rendered = render_message(msg)
        if args.dry_run:
            print(rendered)
        else:
            session = tmux_bridge.get_current_session(args.session)
            windows = tmux_bridge.list_windows(session)
            sent_count = 0
            for w in windows:
                target = w["target"]
                try:
                    send_acom(target, msg)
                    sent_count += 1
                except Exception as e:
                    print(f"[WARN] Failed to send to {target}: {e}", file=sys.stderr)
            print(f"[ACOM] Broadcast sent to {sent_count}/{len(windows)} windows")

    elif args.action == "render":
        data = json.loads(args.json_str)
        msg = ACOMMessage(**data)
        print(render_message(msg))


if __name__ == "__main__":
    main()
