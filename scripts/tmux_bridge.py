#!/usr/bin/env python3
"""tmux_bridge.py - Low-overhead CLI bridge for tmux multi-agent orchestration.

Zero LLM tokens required for operations. Strips ANSI, detects prompts,
manages window roles, and safely relays messages with collision prevention.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

CONFIG_DIR = Path.home() / ".gemini/config/skills/tmux-butler"
CONFIG_FILE = CONFIG_DIR / "config.json"
INCIDENTS_DIR = CONFIG_DIR / "incidents"

# ANSI escape sequence regex
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# Anchored & comprehensive prompt patterns for heterogeneous agents
PROMPT_PATTERNS = [
    # Claude Code
    (r'^❯\s*$', 'claude_code'),
    # Codex / Aider
    (r'^›\s+(?:Ask\s+)?Codex', 'codex_input'),
    (r'^>\s*$', 'codex_or_aider'),
    # Devin (U+27E9 or >)
    (r'^[⟩〉>]\s*$', 'devin'),
    # Pi Agent
    (r'^π>\s*$', 'pi_agent'),
    # AGY / Antigravity CLI
    (r'^>\s+Accept-edits|^>\s*$', 'agy_prompt'),
    # Shell prompts
    (r'^[\$#%]\s*$', 'shell_prompt'),
    (r'^[➜→⇒]\s+', 'shell_custom'),
    # Interactive REPLs
    (r'^>>>\s*', 'python_repl'),
    (r'^\.\.\.\s*', 'python_continuation'),
    (r'^In\s+\[\d+\]:\s*', 'ipython'),
    # Confirmation choices
    (r'\[y/N\]\s*$|\(y/n\)\s*$|\[Y/n\]\s*$', 'confirmation_choice'),
    (r'Allow\s+execution\b.*\?\s*$', 'permission_request'),
    (r'Press\s+(?:Enter|any\s+key)\s+to\s+continue', 'press_enter'),
    (r'Do\s+you\s+want\s+to\s+proceed\?', 'confirm_proceed'),
]

DEFAULT_CONFIG = {
    "session_name": "0",
    "planner_window": "planner",
    "worker_prefix": "worker-",
    "idle_timeout_seconds": 60,
    "worker_launch_cmd": "agy",
    "auto_confirm_safe_prompts": True,
    "role_mappings": {
        "0": "planner",
        "1": "worker-1",
        "2": "butler",
        "3": "worker-2",
        "4": "claude"
    }
}


def load_config():
    if not CONFIG_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            merged = DEFAULT_CONFIG.copy()
            merged.update(cfg)
            return merged
    except Exception:
        return DEFAULT_CONFIG.copy()


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def run_tmux(args, capture_output=True, check=True):
    cmd = ["tmux"] + args
    res = subprocess.run(cmd, stdout=subprocess.PIPE if capture_output else None,
                         stderr=subprocess.PIPE if capture_output else None,
                         text=True)
    if check and res.returncode != 0:
        err = res.stderr.strip() if res.stderr else "Unknown tmux error"
        raise RuntimeError(f"tmux command failed: {' '.join(cmd)}: {err}")
    return res


def strip_ansi(text):
    return ANSI_ESCAPE.sub('', text)


def get_current_session(specified_session=None):
    if specified_session:
        return specified_session
    cfg = load_config()
    default_session = cfg.get("session_name", "0")
    # Verify session exists
    res = run_tmux(["has-session", "-t", default_session], check=False)
    if res.returncode == 0:
        return default_session
    # Fallback to first available session
    res = run_tmux(["list-sessions", "-F", "#{session_name}"], check=False)
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip().splitlines()[0]
    return "0"


def list_windows(session=None):
    sess = get_current_session(session)
    fmt = "#{window_index}\t#{window_name}\t#{window_active}\t#{pane_pid}\t#{pane_current_command}"
    res = run_tmux(["list-windows", "-t", sess, "-F", fmt], check=False)
    if res.returncode != 0:
        return []
    windows = []
    for line in res.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 5:
            windows.append({
                "index": parts[0],
                "name": parts[1],
                "active": parts[2] == "1",
                "pane_pid": parts[3],
                "command": parts[4],
                "target": f"{sess}:{parts[0]}"
            })
    return windows


def capture_pane(target, lines=35, raw=False):
    """Capture trailing lines from target pane."""
    res = run_tmux(["capture-pane", "-pt", target, "-S", f"-{lines}"], check=False)
    if res.returncode != 0:
        return ""
    text = res.stdout
    if not raw:
        text = strip_ansi(text)
    return text


def detect_pane_state(target, sample_lines=25):
    """Detect if pane is waiting for user prompt, idle, or running."""
    text = capture_pane(target, lines=sample_lines)
    stripped_lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not stripped_lines:
        return {"state": "empty", "prompt_type": None, "last_line": ""}

    last_line = stripped_lines[-1]
    second_last = stripped_lines[-2] if len(stripped_lines) >= 2 else ""

    # Check prompts across last 4 lines to support TUIs with bottom status bars
    tail_candidates = stripped_lines[-4:]
    for pat, p_type in PROMPT_PATTERNS:
        for candidate in tail_candidates:
            if re.search(pat, candidate, re.IGNORECASE):
                return {
                    "state": "waiting_input",
                    "prompt_type": p_type,
                    "last_line": last_line,
                    "matched_line": candidate,
                    "context_tail": stripped_lines[-8:]
                }

    return {
        "state": "active_or_silent",
        "prompt_type": None,
        "last_line": last_line,
        "context_tail": stripped_lines[-8:]
    }


def send_keys(target, text, enter=True, literal=True):
    """Send text to target pane with isolated buffer and race-condition prevention."""
    if "\n" in text or len(text) > 150:
        # 1. Use isolated unique buffer name
        buf_name = f"butler_buf_{os.getpid()}_{int(time.time()*1000)}"
        p = subprocess.Popen(["tmux", "load-buffer", "-b", buf_name, "-"],
                             stdin=subprocess.PIPE, text=True)
        p.communicate(input=text)
        
        # 2. Paste into target
        run_tmux(["paste-buffer", "-b", buf_name, "-t", target])
        
        # 3. Synchronize before Enter to prevent truncation
        time.sleep(0.12)
        if enter:
            run_tmux(["send-keys", "-t", target, "Enter"])
            
        # 4. Clean up buffer
        run_tmux(["delete-buffer", "-b", buf_name], check=False)
    else:
        args = ["send-keys", "-t", target]
        if literal:
            args.append("-l")
        args.append(text)
        run_tmux(args)
        if enter:
            time.sleep(0.06)
            run_tmux(["send-keys", "-t", target, "Enter"])


def auto_rename_windows(session=None, mapping=None):
    """Rename windows in session to standardized roles."""
    sess = get_current_session(session)
    cfg = load_config()
    role_map = mapping or cfg.get("role_mappings", {})

    windows = list_windows(sess)
    renamed = []

    for w in windows:
        idx = w["index"]
        current_name = w["name"]
        target_name = role_map.get(idx)
        if not target_name:
            if idx == "0":
                target_name = "planner"
            else:
                target_name = f"worker-{idx}"

        if current_name != target_name:
            run_tmux(["rename-window", "-t", f"{sess}:{idx}", target_name])
            renamed.append({"index": idx, "old_name": current_name, "new_name": target_name})
            role_map[idx] = target_name

    cfg["role_mappings"] = role_map
    cfg["session_name"] = sess
    save_config(cfg)
    return renamed


def scale_worker(session=None, name=None, cmd=None):
    """Dynamically spawn a new worker window."""
    sess = get_current_session(session)
    cfg = load_config()
    windows = list_windows(sess)
    indices = [int(w["index"]) for w in windows if w["index"].isdigit()]
    next_idx = max(indices) + 1 if indices else 1

    worker_name = name or f"worker-{next_idx}"
    worker_cmd = cmd or cfg.get("worker_launch_cmd", "bash")

    # Create window (use sess: to append to session instead of referencing window index 0)
    target_sess = f"{sess}:" if not str(sess).endswith(":") else str(sess)
    run_tmux(["new-window", "-t", target_sess, "-n", worker_name])
    new_target = f"{sess}:{worker_name}"

    # Update config mapping
    cfg["role_mappings"][str(next_idx)] = worker_name
    save_config(cfg)

    # Launch agent command inside if specified
    if worker_cmd and worker_cmd != "bash":
        time.sleep(0.3)
        send_keys(new_target, worker_cmd, enter=True)

    return {"index": next_idx, "name": worker_name, "target": new_target, "cmd": worker_cmd}


def main():
    parser = argparse.ArgumentParser(description="tmux_bridge: zero-token tmux agent tool")
    subparsers = parser.add_subparsers(dest="action", required=True)

    # status
    p_status = subparsers.add_parser("status", help="Get summary status of all managed windows")
    p_status.add_argument("--session", "-s", default=None)
    p_status.add_argument("--json", action="store_true")

    # rename
    p_rename = subparsers.add_parser("rename", help="Standardize window names to planner/worker roles")
    p_rename.add_argument("--session", "-s", default=None)
    p_rename.add_argument("--map", help="JSON string mapping index to name (optional)")

    # capture
    p_capture = subparsers.add_parser("capture", help="Capture trailing output from target pane")
    p_capture.add_argument("--target", "-t", required=True)
    p_capture.add_argument("--lines", "-n", type=int, default=30)
    p_capture.add_argument("--raw", action="store_true")

    # send
    p_send = subparsers.add_parser("send", help="Send command or text to target pane")
    p_send.add_argument("--target", "-t", required=True)
    p_send.add_argument("--text", help="Text to send")
    p_send.add_argument("--file", help="File containing text to send")
    p_send.add_argument("--no-enter", action="store_true")

    # scale
    p_scale = subparsers.add_parser("scale", help="Spawn a new worker window dynamically")
    p_scale.add_argument("--session", "-s", default=None)
    p_scale.add_argument("--name", default=None)
    p_scale.add_argument("--cmd", default=None)

    # interrupt
    p_int = subparsers.add_parser("interrupt", help="Send Ctrl+C to target pane")
    p_int.add_argument("--target", "-t", required=True)

    args = parser.parse_args()

    if args.action == "status":
        sess = get_current_session(args.session)
        windows = list_windows(sess)
        statuses = []
        for w in windows:
            p_state = detect_pane_state(w["target"])
            statuses.append({
                "index": w["index"],
                "name": w["name"],
                "target": w["target"],
                "active": w["active"],
                "command": w["command"],
                "state": p_state["state"],
                "prompt_type": p_state["prompt_type"],
                "last_line": p_state["last_line"]
            })
        if args.json:
            print(json.dumps({"session": sess, "windows": statuses}, indent=2, ensure_ascii=False))
        else:
            print(f"Tmux Session [{sess}] Status:")
            for s in statuses:
                mark = "*" if s["active"] else " "
                print(f" {mark} [{s['index']}] {s['name']:<12} | {s['state']:<14} | prompt: {str(s['prompt_type']):<15} | last: {s['last_line'][:60]}")

    elif args.action == "rename":
        mapping = json.loads(args.map) if args.map else None
        renamed = auto_rename_windows(args.session, mapping)
        print(f"Renamed {len(renamed)} windows:")
        for r in renamed:
            print(f"  Window {r['index']}: '{r['old_name']}' -> '{r['new_name']}'")

    elif args.action == "capture":
        out = capture_pane(args.target, lines=args.lines, raw=args.raw)
        print(out)

    elif args.action == "send":
        content = args.text
        if args.file:
            with open(args.file, "r", encoding="utf-8") as f:
                content = f.read()
        if content is None:
            print("Error: either --text or --file must be specified.", file=sys.stderr)
            sys.exit(1)
        send_keys(args.target, content, enter=not args.no_enter)
        print(f"Successfully sent message to {args.target} ({len(content)} chars)")

    elif args.action == "scale":
        res = scale_worker(args.session, args.name, args.cmd)
        print(f"Created new worker: {res['name']} ({res['target']}) running '{res['cmd']}'")

    elif args.action == "interrupt":
        run_tmux(["send-keys", "-t", args.target, "C-c"])
        print(f"Sent SIGINT (Ctrl+C) to {args.target}")


if __name__ == "__main__":
    main()
