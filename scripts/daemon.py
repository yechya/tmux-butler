#!/usr/bin/env python3
"""daemon.py - Zero-token background supervisor daemon for tmux-butler.

Continuously monitors tmux panes, tracks silence and prompt waiting states,
auto-confirms routine prompts with dangerous operation protection, handles
PID single-instance locking, recursive process tree activity, busy-loop detection,
and writes compact incident diagnostics.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Add current script dir to path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmux_bridge

CONFIG_DIR = Path.home() / ".gemini/config/skills/tmux-butler"
INCIDENTS_DIR = CONFIG_DIR / "incidents"
STATE_FILE = CONFIG_DIR / "daemon_state.json"
PID_FILE = CONFIG_DIR / "daemon.pid"

# Safe auto-confirmation triggers
AUTO_CONFIRM_TRIGGERS = [
    (r'\[y/N\]|\(y/n\)|\[Y/n\]', 'y'),
    (r'Allow\s+execution\b.*\?', 'y'),
    (r'Press\s+(?:Enter|any\s+key)\s+to\s+continue', ''),
    (r'Do\s+you\s+want\s+to\s+proceed\?\s*\[Y/n\]', 'y'),
]

# Security Guard: ONLY block genuinely catastrophic, irreversible operations
# Philosophy: development speed > paranoid safety. Normal dev ops (delete files,
# kill processes, format code, discuss costs) are NOT dangerous.
DANGEROUS_KEYWORDS = [
    r'\brm\s+-rf\s+[/~]',          # rm -rf starting from root or home
    r'\bgit\s+push\b[^|;&]*--force(?!-with-lease)\b',      # force push (but allow --force-with-lease which is safe)
    r'\bgit\s+push\b[^|;&]*\s-[a-zA-Z]*f',                  # short -f flag (also catches bundled -fu)
    r'\bdrop\s+(?:table|database)\b.*\bproduction\b',       # DROP on production only
    r'\bsudo\s+rm\s+-rf\s+/',       # sudo rm -rf from root
    r'\bmkfs\b|\bdd\s+if=',         # filesystem format or raw disk write
]

# Signatures that strongly suggest an agent finished work and is ready to relay
TASK_COMPLETION_PATTERNS = [
    r'Finished\b|Completed\b|Task\s+done\b',
    r'Summary\b|Successfully|Results?\s+saved',
    r'Tests?\s+passed|All\s+checks\s+passed',
    r'Generated\s+code|Output\s+written\s+to',
    r'已完成|任务结束|执行成功|修改完毕|报告如下|审查完毕',
]


def acquire_pid_lock():
    """Ensure only one instance of daemon is running."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    f = open(PID_FILE, "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        return f
    except (IOError, BlockingIOError):
        print(f"Error: Another daemon instance is already running (PID in {PID_FILE}).", file=sys.stderr)
        sys.exit(1)


def release_pid_lock():
    """Safely remove PID file only if it belongs to current process."""
    try:
        if PID_FILE.exists():
            with open(PID_FILE, "r") as f:
                recorded_pid = f.read().strip()
            if recorded_pid == str(os.getpid()):
                PID_FILE.unlink()
    except Exception:
        pass


def _tree_jiffies(pid):
    """Total utime+stime jiffies across the process tree via /proc (Linux).

    Returns (jiffies, n_pids) or (None, n_pids) when /proc is unavailable.
    """
    if not pid or not str(pid).isdigit():
        return None, 0
    pids = {str(pid)}
    try:
        res = subprocess.run(["pstree", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            pids.update(re.findall(r'\((\d+)\)', res.stdout))
    except Exception:
        pass
    total = 0
    found = 0
    for p in pids:
        try:
            with open(f"/proc/{p}/stat") as f:
                # comm may contain spaces/parens — split after the last ')'
                rest = f.read().rsplit(")", 1)[1].split()
            # rest[0]=state (field 3); utime=field14 → idx 11, stime=field15 → idx 12
            total += int(rest[11]) + int(rest[12])
            found += 1
        except Exception:
            continue
    return (total, len(pids)) if found else (None, len(pids))


def is_process_busy(pid):
    """Fallback: lifetime-average CPU across the process tree via ps.

    NOTE: ps %cpu is a cumulative average — kept only for non-Linux fallback.
    Prefer _tree_jiffies deltas for instantaneous busy detection.
    """
    if not pid or not str(pid).isdigit():
        return False
    try:
        # Check whole process tree PIDs
        res = subprocess.run(["pstree", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        pids_to_check = [str(pid)]
        if res.returncode == 0:
            found = re.findall(r'\((\d+)\)', res.stdout)
            if found:
                pids_to_check.extend(found)

        # Query CPU for collected PIDs
        pids_csv = ",".join(set(pids_to_check))
        res_ps = subprocess.run(["ps", "-p", pids_csv, "-o", "%cpu", "--no-headers"],
                                capture_output=True, text=True, timeout=2)
        if res_ps.returncode == 0 and res_ps.stdout.strip():
            for line in res_ps.stdout.strip().splitlines():
                val = line.strip()
                if val:
                    try:
                        if float(val) > 3.0:
                            return True
                    except ValueError:
                        pass
    except Exception:
        pass
    return False


def extract_structured_summary(pane_output):
    """Extract structured details from pane output for relay messages."""
    summary = {
        "status": "completed",
        "files_modified": [],
        "test_results": None,
        "errors": [],
        "highlights": []
    }
    lines = pane_output.strip().splitlines()

    # 1. Scan for file modifications
    for line in lines[-40:]:
        clean = line.strip()
        m_file = re.search(r'(?:Created|Modified|Deleted|Added|Updated):\s+([^\s,]+)', clean, re.IGNORECASE)
        if m_file:
            summary["files_modified"].append(m_file.group(0))

    # 2. Scan for test results
    for line in lines[-25:]:
        clean = line.strip()
        m_test = re.search(r'(\d+)\s+passed.*?(?:(\d+)\s+failed)?', clean, re.IGNORECASE)
        if m_test:
            summary["test_results"] = clean

    # 3. Scan for errors & highlights
    for line in lines[-20:]:
        clean = line.strip()
        if re.search(r'\bError\b|\bFailed\b|\bException\b|失败|报错', clean, re.IGNORECASE):
            summary["errors"].append(clean[:80])
        elif re.search(r'✓|✔|Passed|成功|已完成|完成', clean):
            summary["highlights"].append(clean[:80])

    if summary["errors"] and not summary["highlights"]:
        summary["status"] = "failed"
    return summary


class ButlerDaemon:
    def __init__(self, session=None, read_only=False):
        self.session = tmux_bridge.get_current_session(session)
        self.config = tmux_bridge.load_config()
        self.idle_timeout = self.config.get("idle_timeout_seconds", 60)
        self.auto_confirm = self.config.get("auto_confirm_safe_prompts", True)
        self.read_only = read_only
        self.incident_cooldown_seconds = self.config.get("incident_cooldown_seconds", 300)
        self.state = self._load_state()
        # Deduplication: {target: {incident_type: last_reported_timestamp}}
        self.incident_cooldown = self.state.get("incident_cooldown", {})
        # Track how many incidents were suppressed by dedup (reset each scan)
        self.suppressed_count = 0
        # Accumulate incidents for batched writes
        self._pending_incidents = []
        self._last_write_time = 0

    def _load_state(self):
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"panes": {}, "last_incident_time": 0, "incident_cooldown": {}}

    def _save_state(self):
        STATE_DIR = STATE_FILE.parent
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.state["incident_cooldown"] = self.incident_cooldown
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    def is_session_alive(self):
        """Check if monitored tmux session still exists."""
        res = tmux_bridge.run_tmux(["has-session", "-t", self.session], check=False)
        return res.returncode == 0

    def _is_process_busy(self, pane_pid, pane_info):
        """Instantaneous busy check: CPU jiffies delta since last observation.

        Stores cpu_jiffies/cpu_ts inside pane_info so each scan compares
        against the previous sample. Falls back to lifetime %cpu when /proc
        is unavailable (non-Linux). Returns False when no baseline exists yet.
        """
        now = time.time()
        jiffies, _ = _tree_jiffies(pane_pid)
        if jiffies is None:
            return is_process_busy(pane_pid)
        prev_j = pane_info.get("cpu_jiffies")
        prev_t = pane_info.get("cpu_ts")
        pane_info["cpu_jiffies"] = jiffies
        pane_info["cpu_ts"] = now
        if prev_j is None or prev_t is None or now <= prev_t:
            return False
        try:
            hz = os.sysconf("SC_CLK_TCK")
        except (ValueError, AttributeError):
            hz = 100
        rate = (jiffies - prev_j) / (hz * (now - prev_t))
        return rate > 0.03

    def scan_once(self):
        now = time.time()
        self.suppressed_count = 0
        try:
            windows = tmux_bridge.list_windows(self.session)
        except Exception as e:
            print(f"[WARN] Failed to list windows in session {self.session}: {e}", file=sys.stderr, flush=True)
            return []

        incidents = []

        for w in windows:
            try:
                target = w["target"]
                pane_pid = w.get("pane_pid")
                pane_info = self.state.get("panes", {}).get(target, {
                    "last_hash": "",
                    "last_change_time": now,
                    "consecutive_stalls": 0,
                    "busy_silent_cycles": 0
                })

                text = tmux_bridge.capture_pane(target, lines=30)
                cur_hash = hashlib.md5(text.encode("utf-8")).hexdigest()

                if cur_hash != pane_info.get("last_hash"):
                    pane_info["last_hash"] = cur_hash
                    pane_info["last_change_time"] = now
                    pane_info["consecutive_stalls"] = 0
                    pane_info["busy_silent_cycles"] = 0
                
                idle_seconds = int(now - pane_info.get("last_change_time", now))
                pane_state = tmux_bridge.detect_pane_state(target, sample_lines=15)

                # Check 1: Safe auto-confirm with security keyword blacklist
                if self.auto_confirm and pane_state["state"] == "waiting_input":
                    # 用真正命中提示符的那一行：带底部状态栏的 TUI 中
                    # last_line 往往是状态栏而不是确认提示
                    last_line = pane_state["last_line"]
                    confirm_line = pane_state.get("matched_line") or last_line
                    matched_trigger = None
                    for pat, response in AUTO_CONFIRM_TRIGGERS:
                        if re.search(pat, confirm_line, re.IGNORECASE):
                            matched_trigger = (pat, response)
                            break

                    if matched_trigger:
                        context_blob = "\n".join(pane_state.get("context_tail", []))
                        is_dangerous = any(re.search(kw, context_blob, re.IGNORECASE) for kw in DANGEROUS_KEYWORDS)

                        if is_dangerous:
                            incidents.append({
                                "timestamp": datetime.now().isoformat(),
                                "target": target,
                                "window_name": w["name"],
                                "window_index": w["index"],
                                "idle_seconds": idle_seconds,
                                "incident_type": "requires_human_approval",
                                "prompt_type": pane_state["prompt_type"],
                                "last_line": confirm_line,
                                "reason": "Dangerous operation detected near confirmation prompt"
                            })
                        elif self.read_only:
                            incidents.append({
                                "timestamp": datetime.now().isoformat(),
                                "target": target,
                                "window_name": w["name"],
                                "window_index": w["index"],
                                "idle_seconds": idle_seconds,
                                "incident_type": "auto_confirmable_prompt",
                                "prompt_type": pane_state["prompt_type"],
                                "last_line": confirm_line,
                                "reason": "Safe prompt detected (read-only: not confirmed)"
                            })
                        else:
                            resp = matched_trigger[1]
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] Auto-confirming safe prompt in {target}: '{confirm_line.strip()}' -> sending '{resp}'", flush=True)
                            tmux_bridge.send_keys(target, resp, enter=True)
                            pane_info["last_change_time"] = now
                            time.sleep(0.3)

                # Check 2: Idle timeout detection & Busy-spin loop detection
                if idle_seconds >= self.idle_timeout:
                    lines = [l.strip() for l in text.splitlines() if l.strip()]
                    tail_lines = lines[-12:] if len(lines) >= 12 else lines
                    tail_blob = "\n".join(tail_lines)

                    looks_completed = any(re.search(pat, tail_blob, re.IGNORECASE) for pat in TASK_COMPLETION_PATTERNS)
                    is_waiting_input = pane_state["state"] == "waiting_input"
                    process_active = self._is_process_busy(pane_pid, pane_info)

                    incident_type = None

                    # Early busy-spin loop detection (high CPU but silent for extended period)
                    if process_active and idle_seconds >= int(self.idle_timeout * 1.5):
                        pane_info["busy_silent_cycles"] = pane_info.get("busy_silent_cycles", 0) + 1
                        if pane_info["busy_silent_cycles"] >= 3:
                            incident_type = "cpu_busy_spin_loop"

                    if not incident_type:
                        if is_waiting_input and looks_completed:
                            incident_type = "forgot_to_relay"
                        elif is_waiting_input and not looks_completed:
                            incident_type = "waiting_for_input_or_blocked"
                        elif idle_seconds >= self.idle_timeout * 2 and not process_active:
                            incident_type = "long_running_or_frozen"

                    if incident_type:
                        # Dedup: suppress if same (target, incident_type) reported recently
                        cooldown_key = self.incident_cooldown.get(target, {}).get(incident_type, 0)
                        if now - cooldown_key < self.incident_cooldown_seconds:
                            self.suppressed_count += 1
                        else:
                            pane_info["consecutive_stalls"] = pane_info.get("consecutive_stalls", 0) + 1
                            structured = extract_structured_summary(tail_blob)
                            incidents.append({
                                "timestamp": datetime.now().isoformat(),
                                "target": target,
                                "window_name": w["name"],
                                "window_index": w["index"],
                                "idle_seconds": idle_seconds,
                                "incident_type": incident_type,
                                "prompt_type": pane_state["prompt_type"],
                                "last_line": pane_state["last_line"],
                                "structured_summary": structured,
                                "tail_snippet": "\n".join(tail_lines[-5:])
                            })
                            self.incident_cooldown.setdefault(target, {})[incident_type] = now

                self.state.setdefault("panes", {})[target] = pane_info

            except Exception as e:
                print(f"[WARN] Error scanning window {w.get('name', 'unknown')}: {e}", file=sys.stderr, flush=True)
                continue

        # 清掉已关闭窗口的残留状态
        current_targets = {w["target"] for w in windows}
        panes = self.state.get("panes", {})
        for stale in [t for t in panes if t not in current_targets]:
            del panes[stale]

        self._save_state()

        if incidents:
            self._pending_incidents.extend(incidents)
            # Flush immediately for critical incidents or if enough time has passed
            has_critical = any(i["incident_type"] == "requires_human_approval" for i in incidents)
            elapsed_since_write = now - self._last_write_time
            if has_critical or elapsed_since_write >= 60:
                self._flush_incidents()

        return incidents

    def _flush_incidents(self):
        """Write accumulated incidents to disk and clear the buffer."""
        if not self._pending_incidents:
            return
        self._record_incidents(self._pending_incidents)
        self._pending_incidents = []
        self._last_write_time = time.time()

    def _record_incidents(self, incidents):
        INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)
        now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        report = {
            "session": self.session,
            "generated_at": datetime.now().isoformat(),
            "count": len(incidents),
            "incidents": incidents
        }

        # Write latest.json and historical log
        with open(INCIDENTS_DIR / "latest.json", "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        hist_file = INCIDENTS_DIR / f"incident_{now_str}.json"
        with open(hist_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        # Rotate: keep at most 50 historical incident files
        hist_files = sorted(INCIDENTS_DIR.glob("incident_*.json"))
        if len(hist_files) > 50:
            for old_file in hist_files[:-50]:
                try:
                    old_file.unlink()
                except OSError:
                    pass

    def run_loop(self, interval=15):
        print(f"Starting tmux-butler daemon on session [{self.session}] (PID: {os.getpid()}, poll: {interval}s, idle timeout: {self.idle_timeout}s)...", flush=True)
        consecutive_errors = 0
        try:
            while True:
                if not self.is_session_alive():
                    print(f"\n[EXIT] Tmux session '{self.session}' no longer exists. Stopping daemon.", flush=True)
                    break

                try:
                    incidents = self.scan_once()
                    consecutive_errors = 0
                    now_str = datetime.now().strftime("%H:%M:%S")
                    if incidents:
                        print(f"[{now_str}] ⚠️ Detected {len(incidents)} issue(s):", flush=True)
                        for inc in incidents:
                            print(f"   -> [{inc['window_name']} ({inc['target']})] {inc['incident_type']} (idle {inc['idle_seconds']}s): {inc['last_line'][:55]}", flush=True)
                except Exception as e:
                    consecutive_errors += 1
                    print(f"[WARN] Exception in scan loop: {e}", file=sys.stderr, flush=True)
                    if consecutive_errors >= 5:
                        print(f"[EXIT] 5 consecutive failures in scan loop. Stopping daemon.", file=sys.stderr, flush=True)
                        break

                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nDaemon stopped gracefully by user.", flush=True)


def main():
    parser = argparse.ArgumentParser(description="tmux-butler zero-token supervisor daemon")
    subparsers = parser.add_subparsers(dest="action", required=True)

    # run
    p_run = subparsers.add_parser("run", help="Start continuous monitoring loop")
    p_run.add_argument("--session", "-s", default=None)
    p_run.add_argument("--interval", "-i", type=int, default=15)

    # check
    p_check = subparsers.add_parser("check", help="Run a single inspection pass and print report")
    p_check.add_argument("--session", "-s", default=None)
    p_check.add_argument("--json", action="store_true")
    p_check.add_argument("--compact", action="store_true", help="Output ultra-compact format (<50 tokens)")
    p_check.add_argument("--read-only", action="store_true",
                         help="Inspect only: report confirmable prompts instead of sending keys")

    # latest
    p_latest = subparsers.add_parser("latest", help="Show latest incident report for LLM Butler")
    p_latest.add_argument("--clear", action="store_true", help="Clear latest incident file")

    args = parser.parse_args()

    if args.action == "run":
        lock_file = acquire_pid_lock()
        daemon = ButlerDaemon(args.session)
        try:
            daemon.run_loop(args.interval)
        finally:
            release_pid_lock()

    elif args.action == "check":
        daemon = ButlerDaemon(args.session, read_only=args.read_only)
        incidents = daemon.scan_once()
        if args.json:
            print(json.dumps({"incidents": incidents}, indent=2, ensure_ascii=False))
        elif args.compact:
            supp_suffix = f" ({daemon.suppressed_count} suppressed)" if daemon.suppressed_count > 0 else ""
            if not incidents:
                print(f"STATUS:OK{supp_suffix}")
            else:
                lines = [f"{i['window_name']}|{i['incident_type']}|{i['idle_seconds']}s|{i['last_line'][:35]}" for i in incidents]
                if supp_suffix:
                    lines.append(f"SUPPRESSED:{daemon.suppressed_count}")
                print("\n".join(lines))
        else:
            if not incidents:
                print("✅ All tmux windows are running healthily (no stalls detected).")
            else:
                print(f"⚠️ {len(incidents)} issue(s) detected:")
                for inc in incidents:
                    print(f"  [{inc['window_name']}] type={inc['incident_type']} idle={inc['idle_seconds']}s")
                    print(f"    Prompt: {inc['prompt_type']}")
                    print(f"    Last line: {inc['last_line']}")

    elif args.action == "latest":
        latest_file = INCIDENTS_DIR / "latest.json"
        if args.clear:
            if latest_file.exists():
                latest_file.unlink()
            print("Cleared latest incidents.")
        else:
            if not latest_file.exists():
                print("No active incidents. System is healthy.")
            else:
                with open(latest_file, "r", encoding="utf-8") as f:
                    print(f.read())


if __name__ == "__main__":
    main()
