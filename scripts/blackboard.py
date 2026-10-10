#!/usr/bin/env python3
"""blackboard.py - Lightweight SQLite-based task coordination blackboard.

Ephemeral work-unit tracker for multi-agent orchestration. Not a project
management system — just enough structure to keep agents from stepping on
each other.

Usage (CLI):
    python3 blackboard.py create --title "重构点云配准" --issuer claude --assignee devin
    python3 blackboard.py list [--all] [--status RUNNING] [--assignee devin]
    python3 blackboard.py show task-042
    python3 blackboard.py update task-042 --status DONE --note "All tests pass"
    python3 blackboard.py claim task-042 --assignee devin
    python3 blackboard.py log task-042 --event note --actor butler --detail "..."
    python3 blackboard.py summary

Usage (Python API):
    from blackboard import Blackboard
    bb = Blackboard()
    task = bb.create_task(title="...", issuer="claude", assignee="devin")
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_DIR = Path.home() / ".gemini/config/skills/tmux-butler/data"
DB_PATH = DB_DIR / "blackboard.db"

# Valid statuses and allowed transitions
VALID_STATUSES = {
    "PENDING", "CLAIMED", "RUNNING", "TESTING",
    "REVIEWING", "DONE", "BLOCKED", "CANCELLED",
}

TERMINAL_STATUSES = {"DONE", "CANCELLED"}

# Allowed status transitions (from -> set of valid targets)
# Kept loose intentionally — velocity over ceremony
STATUS_TRANSITIONS = {
    "PENDING":   {"CLAIMED", "RUNNING", "BLOCKED", "CANCELLED"},
    "CLAIMED":   {"RUNNING", "BLOCKED", "CANCELLED", "PENDING"},
    "RUNNING":   {"TESTING", "REVIEWING", "DONE", "BLOCKED", "CANCELLED"},
    "TESTING":   {"RUNNING", "REVIEWING", "DONE", "BLOCKED", "CANCELLED"},
    "REVIEWING": {"RUNNING", "DONE", "BLOCKED", "CANCELLED"},
    "BLOCKED":   {"PENDING", "CLAIMED", "RUNNING", "CANCELLED"},
    "DONE":      {"RUNNING"},       # reopen only
    "CANCELLED": {"PENDING"},       # resurrect only
}

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT DEFAULT 'PENDING',
    assignee TEXT,
    issuer TEXT,
    working_dir TEXT,
    spec TEXT,
    deliverables TEXT,
    created_at TEXT,
    updated_at TEXT,
    claimed_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS task_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    event TEXT NOT NULL,
    actor TEXT,
    detail TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now_iso():
    """Current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_str(iso_ts):
    """Human-readable age string from ISO timestamp."""
    if not iso_ts:
        return "?"
    try:
        created = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - created
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s"
        elif secs < 3600:
            return f"{secs // 60}m"
        elif secs < 86400:
            return f"{secs // 3600}h"
        else:
            return f"{secs // 86400}d"
    except Exception:
        return "?"


class Blackboard:
    """Lightweight task coordination blackboard backed by SQLite."""

    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=5)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=3000")
        self._init_schema()

    def _init_schema(self):
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------
    # Task ID generation
    # ------------------------------------------------------------------

    def _next_task_id(self):
        """Generate sequential task ID: task-001, task-002, etc."""
        cur = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'task_counter'"
        )
        row = cur.fetchone()
        counter = int(row["value"]) + 1 if row else 1
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('task_counter', ?)",
            (str(counter),),
        )
        return f"task-{counter:03d}"

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def create_task(self, title, issuer=None, assignee=None, working_dir=None,
                    spec=None, deliverables=None):
        """Create a new task. Returns the full task row as dict."""
        now = _now_iso()
        spec_json = json.dumps(spec, ensure_ascii=False) if spec and not isinstance(spec, str) else spec
        deliv_json = json.dumps(deliverables, ensure_ascii=False) if deliverables and not isinstance(deliverables, str) else deliverables

        task_id = self._next_task_id()
        self._conn.execute(
            """INSERT INTO tasks
               (task_id, title, status, assignee, issuer, working_dir,
                spec, deliverables, created_at, updated_at)
               VALUES (?, ?, 'PENDING', ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, title, assignee, issuer, working_dir,
             spec_json, deliv_json, now, now),
        )
        self._add_log_entry(task_id, "status_change", issuer, "Created → PENDING")
        self._conn.commit()
        return self._get_task(task_id)

    def _get_task(self, task_id):
        """Fetch a single task or None."""
        cur = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_task(self, task_id):
        """Public getter. Raises if not found."""
        task = self._get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")
        return task

    def list_tasks(self, status=None, assignee=None, include_terminal=False):
        """Query tasks with optional filters."""
        clauses = []
        params = []

        if status:
            clauses.append("status = ?")
            params.append(status.upper())
        elif not include_terminal:
            placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
            clauses.append(f"status NOT IN ({placeholders})")
            params.extend(TERMINAL_STATUSES)

        if assignee:
            clauses.append("assignee = ?")
            params.append(assignee)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cur = self._conn.execute(
            f"SELECT * FROM tasks {where} ORDER BY created_at ASC", params
        )
        return [dict(r) for r in cur.fetchall()]

    def update_status(self, task_id, new_status, actor=None, note=None):
        """Transition task status with validation."""
        new_status = new_status.upper()
        if new_status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {new_status}. Valid: {', '.join(sorted(VALID_STATUSES))}")

        task = self.get_task(task_id)
        old_status = task["status"]

        allowed = STATUS_TRANSITIONS.get(old_status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Cannot transition {task_id} from {old_status} → {new_status}. "
                f"Allowed: {', '.join(sorted(allowed)) if allowed else 'none'}"
            )

        now = _now_iso()
        updates = {"status": new_status, "updated_at": now}
        if new_status in TERMINAL_STATUSES:
            updates["completed_at"] = now

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [task_id]
        self._conn.execute(f"UPDATE tasks SET {set_clause} WHERE task_id = ?", vals)

        detail = f"{old_status} → {new_status}"
        if note:
            detail += f": {note}"
        self._add_log_entry(task_id, "status_change", actor, detail)
        self._conn.commit()
        return self._get_task(task_id)

    def claim_task(self, task_id, assignee):
        """Atomic claim: only succeeds if task is currently PENDING."""
        task = self.get_task(task_id)
        if task["status"] != "PENDING":
            return False

        now = _now_iso()
        self._conn.execute(
            "UPDATE tasks SET status='CLAIMED', assignee=?, claimed_at=?, updated_at=? WHERE task_id=? AND status='PENDING'",
            (assignee, now, now, task_id),
        )
        if self._conn.total_changes == 0:
            # Race condition: someone else claimed it first
            return False

        self._add_log_entry(task_id, "status_change", assignee, f"PENDING → CLAIMED (claimed by {assignee})")
        self._conn.commit()
        return True

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _add_log_entry(self, task_id, event, actor=None, detail=None):
        """Insert log entry (caller must commit)."""
        self._conn.execute(
            "INSERT INTO task_log (task_id, timestamp, event, actor, detail) VALUES (?, ?, ?, ?, ?)",
            (task_id, _now_iso(), event, actor, detail),
        )

    def add_log(self, task_id, event, actor=None, detail=None):
        """Public: add a log entry and commit."""
        # Verify task exists
        self.get_task(task_id)
        self._add_log_entry(task_id, event, actor, detail)
        self._conn.execute(
            "UPDATE tasks SET updated_at = ? WHERE task_id = ?",
            (_now_iso(), task_id),
        )
        self._conn.commit()

    def get_log(self, task_id):
        """Retrieve full log for a task."""
        cur = self._conn.execute(
            "SELECT * FROM task_log WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def format_table(self, tasks):
        """Compact table: ID | STATUS | ASSIGNEE | TITLE | AGE"""
        if not tasks:
            return "No tasks found."
        header = f"{'ID':<10} {'STATUS':<10} {'ASSIGNEE':<10} {'TITLE':<36} {'AGE':>5}"
        sep = "-" * len(header)
        lines = [header, sep]
        for t in tasks:
            title = t["title"][:36]
            assignee = (t["assignee"] or "-")[:10]
            age = _age_str(t["created_at"])
            lines.append(f"{t['task_id']:<10} {t['status']:<10} {assignee:<10} {title:<36} {age:>5}")
        return "\n".join(lines)

    def format_show(self, task_id):
        """Full detail view with log entries."""
        task = self.get_task(task_id)
        log = self.get_log(task_id)

        lines = [
            f"═══ {task['task_id']} ═══",
            f"  Title:       {task['title']}",
            f"  Status:      {task['status']}",
            f"  Assignee:    {task['assignee'] or '-'}",
            f"  Issuer:      {task['issuer'] or '-'}",
            f"  Working Dir: {task['working_dir'] or '-'}",
            f"  Created:     {task['created_at']}",
            f"  Updated:     {task['updated_at']}",
            f"  Claimed:     {task['claimed_at'] or '-'}",
            f"  Completed:   {task['completed_at'] or '-'}",
        ]

        if task["spec"]:
            lines.append(f"  Spec:        {task['spec']}")
        if task["deliverables"]:
            lines.append(f"  Deliverables:{task['deliverables']}")

        if log:
            lines.append("")
            lines.append("  ─── Log ───")
            for entry in log:
                ts = entry["timestamp"]
                actor = entry["actor"] or "?"
                lines.append(f"  [{ts}] {entry['event']} by {actor}: {entry['detail'] or ''}")

        return "\n".join(lines)

    def summary(self):
        """Ultra-compact summary for LLM context injection (< 100 tokens).

        Format: one line per active task, pipe-delimited.
        """
        tasks = self.list_tasks(include_terminal=False)
        if not tasks:
            return "BOARD:EMPTY"
        parts = []
        for t in tasks:
            assignee = t["assignee"] or "?"
            age = _age_str(t["created_at"])
            # Truncate title aggressively for token economy
            title = t["title"][:25]
            parts.append(f"{t['task_id']}|{t['status']}|{assignee}|{title}|{age}")
        return "BOARD:" + ";".join(parts)


# ======================================================================
# CLI Interface
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description="blackboard: lightweight task coordination for tmux-butler"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    # create
    p_create = subparsers.add_parser("create", help="Create a new task")
    p_create.add_argument("--title", required=True, help="Brief task description")
    p_create.add_argument("--issuer", default=None, help="Who created the task")
    p_create.add_argument("--assignee", default=None, help="Target agent/window")
    p_create.add_argument("--working-dir", default=None, help="Workspace path")
    p_create.add_argument("--spec", default=None, help="JSON spec blob")
    p_create.add_argument("--deliverables", default=None, help="JSON deliverables blob")

    # list
    p_list = subparsers.add_parser("list", help="List tasks")
    p_list.add_argument("--all", action="store_true", help="Include terminal statuses")
    p_list.add_argument("--status", default=None, help="Filter by status")
    p_list.add_argument("--assignee", default=None, help="Filter by assignee")

    # show
    p_show = subparsers.add_parser("show", help="Show task detail with log")
    p_show.add_argument("task_id", help="Task ID to show")

    # update
    p_update = subparsers.add_parser("update", help="Update task status")
    p_update.add_argument("task_id", help="Task ID to update")
    p_update.add_argument("--status", required=True, help="New status")
    p_update.add_argument("--note", default=None, help="Optional note")
    p_update.add_argument("--actor", default=None, help="Who is making the change")

    # claim
    p_claim = subparsers.add_parser("claim", help="Atomically claim a PENDING task")
    p_claim.add_argument("task_id", help="Task ID to claim")
    p_claim.add_argument("--assignee", required=True, help="Who is claiming")

    # log
    p_log = subparsers.add_parser("log", help="Add a log entry to a task")
    p_log.add_argument("task_id", help="Task ID")
    p_log.add_argument("--event", required=True, help="Event type: note|error|relay|status_change")
    p_log.add_argument("--actor", default=None, help="Who is logging")
    p_log.add_argument("--detail", default=None, help="Free-form detail")

    # summary
    subparsers.add_parser("summary", help="Ultra-compact summary for LLM context")

    args = parser.parse_args()
    bb = Blackboard()

    try:
        if args.action == "create":
            spec = None
            if args.spec:
                try:
                    spec = json.loads(args.spec)
                except json.JSONDecodeError:
                    spec = args.spec

            deliverables = None
            if args.deliverables:
                try:
                    deliverables = json.loads(args.deliverables)
                except json.JSONDecodeError:
                    deliverables = args.deliverables

            task = bb.create_task(
                title=args.title,
                issuer=args.issuer,
                assignee=args.assignee,
                working_dir=args.working_dir,
                spec=spec,
                deliverables=deliverables,
            )
            print(f"Created {task['task_id']}: {task['title']}")

        elif args.action == "list":
            tasks = bb.list_tasks(
                status=args.status,
                assignee=args.assignee,
                include_terminal=args.all,
            )
            print(bb.format_table(tasks))

        elif args.action == "show":
            print(bb.format_show(args.task_id))

        elif args.action == "update":
            task = bb.update_status(
                args.task_id, args.status,
                actor=args.actor, note=args.note,
            )
            print(f"Updated {task['task_id']}: {task['status']}")

        elif args.action == "claim":
            success = bb.claim_task(args.task_id, args.assignee)
            if success:
                print(f"Claimed {args.task_id} → {args.assignee}")
            else:
                print(f"Failed to claim {args.task_id} (not PENDING or already claimed)", file=sys.stderr)
                sys.exit(1)

        elif args.action == "log":
            bb.add_log(args.task_id, event=args.event, actor=args.actor, detail=args.detail)
            print(f"Logged {args.event} on {args.task_id}")

        elif args.action == "summary":
            print(bb.summary())

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        bb.close()


if __name__ == "__main__":
    main()
