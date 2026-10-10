#!/usr/bin/env python3
"""workspace_manager.py - Git worktree workspace isolation manager for tmux-butler.

Manages temporary git worktrees as sandboxes for parallel agent tasks.
Core philosophy: 保持线性历史，worktree 是临时沙盒，merge 后即清理。

Design:
  - Each task gets an isolated worktree at ~/.tmux-butler-workspaces/<task_id>/
  - Branches always named butler/<task-id> for clear ownership
  - Merge strategy: squash (default) or rebase to keep linear history
  - Conflict handling: abort + mark BLOCKED, never auto-resolve
  - Active workspace tracking in data/workspaces.json

CLI:
  python3 workspace_manager.py create  --task-id T --repo /path
  python3 workspace_manager.py list
  python3 workspace_manager.py status  --task-id T
  python3 workspace_manager.py merge   --task-id T [--strategy squash|rebase] [--message MSG]
  python3 workspace_manager.py cleanup --task-id T | --all
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

CONFIG_DIR = Path.home() / ".gemini/config/skills/tmux-butler"
DATA_DIR = CONFIG_DIR / "data"
WORKSPACES_FILE = DATA_DIR / "workspaces.json"
WORKSPACES_ROOT = Path.home() / ".tmux-butler-workspaces"
CONFIG_FILE = CONFIG_DIR / "config.json"

BRANCH_PREFIX = "butler/"


# ─── Git helpers ────────────────────────────────────────────────────────────

def _run_git(args, cwd=None, check=True):
    """Run a git command, return CompletedProcess. Raises on failure if check=True."""
    cmd = ["git"] + args
    try:
        res = subprocess.run(
            cmd, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git command timed out: {' '.join(cmd)}")

    if check and res.returncode != 0:
        err = res.stderr.strip() or res.stdout.strip() or "Unknown git error"
        raise RuntimeError(f"git command failed ({res.returncode}): {' '.join(cmd)}\n{err}")
    return res


def _get_main_branch(repo_path):
    """Detect the primary branch (main/master) of the repo."""
    for candidate in ("main", "master"):
        res = _run_git(["rev-parse", "--verify", candidate], cwd=repo_path, check=False)
        if res.returncode == 0:
            return candidate
    # 回退到 HEAD 所在分支
    res = _run_git(["symbolic-ref", "--short", "HEAD"], cwd=repo_path, check=False)
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip()
    return "main"


def _detect_repo_path():
    """Auto-detect the repo path from config or cwd."""
    # 1. Check config.json for default_repo
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            repo = cfg.get("default_repo")
            if repo:
                p = Path(repo).expanduser().resolve()
                if (p / ".git").exists():
                    return str(p)
        except Exception:
            pass

    # 2. Try current working directory
    cwd = Path.cwd()
    res = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(cwd), capture_output=True, text=True
    )
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip()

    return None


# ─── Workspace registry (data/workspaces.json) ─────────────────────────────

def _load_workspaces():
    """Load the workspace registry."""
    if WORKSPACES_FILE.exists():
        try:
            with open(WORKSPACES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_workspaces(data):
    """Persist workspace registry."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(WORKSPACES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ─── WorkspaceManager class ────────────────────────────────────────────────

class WorkspaceManager:
    """Manages git worktree sandboxes for parallel task isolation.

    Each workspace = isolated worktree + butler/<task_id> branch.
    Merge back via squash or rebase to keep linear history.
    """

    def __init__(self, repo_path=None):
        self.repo_path = repo_path or _detect_repo_path()
        if not self.repo_path:
            raise RuntimeError(
                "Cannot detect git repo. Specify --repo or set 'default_repo' in config.json."
            )
        self.repo_path = str(Path(self.repo_path).resolve())
        # 确认是有效的 git repo
        if not (Path(self.repo_path) / ".git").exists():
            # 也可能是 worktree 自身，检查 .git 文件
            git_path = Path(self.repo_path) / ".git"
            if not git_path.exists():
                raise RuntimeError(f"Not a git repository: {self.repo_path}")

    def _branch_name(self, task_id):
        return f"{BRANCH_PREFIX}{task_id}"

    def _workspace_path(self, task_id):
        return WORKSPACES_ROOT / task_id

    def create_workspace(self, task_id, base_branch=None):
        """Create an isolated worktree for a task.

        Args:
            task_id: Unique task identifier (e.g. 'task-042')
            base_branch: Branch to base the worktree on (defaults to main/master)

        Returns:
            str: Absolute path to the new worktree directory
        """
        ws_path = self._workspace_path(task_id)
        branch = self._branch_name(task_id)

        if ws_path.exists():
            raise RuntimeError(f"Workspace already exists: {ws_path}")

        # 确定基础分支
        if not base_branch:
            base_branch = _get_main_branch(self.repo_path)

        # 确保基础分支最新（不强制，失败也继续）
        _run_git(["fetch", "origin", base_branch], cwd=self.repo_path, check=False)

        # 创建 worktree + 新分支
        WORKSPACES_ROOT.mkdir(parents=True, exist_ok=True)
        _run_git(
            ["worktree", "add", str(ws_path), "-b", branch, base_branch],
            cwd=self.repo_path
        )

        # 注册到 workspaces.json
        registry = _load_workspaces()
        registry[task_id] = {
            "path": str(ws_path),
            "branch": branch,
            "base_branch": base_branch,
            "repo": self.repo_path,
            "created_at": datetime.now().isoformat(),
            "status": "active",  # active | merged | blocked | cleaned
        }
        _save_workspaces(registry)

        return str(ws_path)

    def workspace_status(self, task_id):
        """Get status info for a workspace.

        Returns:
            dict with keys: exists, path, branch, has_changes, ahead, status
        """
        registry = _load_workspaces()
        entry = registry.get(task_id)
        ws_path = self._workspace_path(task_id)
        branch = self._branch_name(task_id)

        if not entry and not ws_path.exists():
            return {"exists": False, "task_id": task_id}

        info = {
            "exists": ws_path.exists(),
            "task_id": task_id,
            "path": str(ws_path),
            "branch": branch,
            "has_changes": False,
            "ahead": 0,
            "status": entry.get("status", "unknown") if entry else "unknown",
            "repo": entry.get("repo", self.repo_path) if entry else self.repo_path,
            "base_branch": entry.get("base_branch", "") if entry else "",
            "created_at": entry.get("created_at", "") if entry else "",
        }

        if not ws_path.exists():
            info["status"] = "cleaned"
            return info

        # 检查是否有未提交的更改
        res = _run_git(["status", "--porcelain"], cwd=str(ws_path), check=False)
        if res.returncode == 0:
            info["has_changes"] = bool(res.stdout.strip())

        # 检查领先主分支多少 commits
        base = entry.get("base_branch", _get_main_branch(self.repo_path)) if entry else _get_main_branch(self.repo_path)
        res = _run_git(
            ["rev-list", "--count", f"{base}..{branch}"],
            cwd=str(ws_path), check=False
        )
        if res.returncode == 0 and res.stdout.strip().isdigit():
            info["ahead"] = int(res.stdout.strip())

        return info

    def merge_back(self, task_id, commit_message=None, strategy="squash"):
        """Merge workspace changes back to main branch.

        Args:
            task_id: Task identifier
            commit_message: Commit message for the squash/merge
            strategy: 'squash' (default) or 'rebase'

        Returns:
            tuple: (success: bool, message: str)
        """
        registry = _load_workspaces()
        entry = registry.get(task_id)
        if not entry:
            return False, f"No workspace found for task '{task_id}'"

        ws_path = Path(entry["path"])
        branch = entry["branch"]
        base_branch = entry.get("base_branch", _get_main_branch(self.repo_path))
        repo = entry.get("repo", self.repo_path)

        if not ws_path.exists():
            return False, f"Worktree directory not found: {ws_path}"

        # 检查 worktree 是否有未提交的更改
        res = _run_git(["status", "--porcelain"], cwd=str(ws_path), check=False)
        if res.returncode == 0 and res.stdout.strip():
            return False, f"Workspace has uncommitted changes. Commit or stash first."

        # 检查主仓是否干净：merge 会在主仓执行 checkout + merge，
        # 主仓有未提交改动或未解决的冲突时一律拒绝，避免污染用户工作区
        res = _run_git(["status", "--porcelain"], cwd=repo, check=False)
        if res.returncode == 0 and res.stdout.strip():
            return False, (
                "Main repo has uncommitted changes or unresolved conflicts. "
                "Commit/stash/reset the main repo first — refusing to merge."
            )

        # 检查是否有需要合并的 commits
        res = _run_git(
            ["rev-list", "--count", f"{base_branch}..{branch}"],
            cwd=str(ws_path), check=False
        )
        if res.returncode == 0 and res.stdout.strip() == "0":
            return False, "No commits to merge (branch is not ahead of base)."

        if not commit_message:
            commit_message = f"butler: merge {task_id}"

        try:
            if strategy == "squash":
                return self._squash_merge(task_id, repo, base_branch, branch, commit_message, registry)
            elif strategy == "rebase":
                return self._rebase_merge(task_id, repo, base_branch, branch, ws_path, registry)
            else:
                return False, f"Unknown merge strategy: {strategy}"
        except RuntimeError as e:
            return False, f"Merge failed: {e}"

    def _squash_merge(self, task_id, repo, base_branch, branch, commit_message, registry):
        """Squash merge: condenses all worktree commits into a single commit on base."""
        # 切换到 base 分支（在主 repo 中操作）
        _run_git(["checkout", base_branch], cwd=repo)

        # 尝试 squash merge
        res = _run_git(["merge", "--squash", branch], cwd=repo, check=False)
        if res.returncode != 0:
            # 冲突！回退 merge，标记 BLOCKED
            # 注意：merge --squash 不写 MERGE_HEAD，merge --abort 无效；
            # 必须用 reset --merge 清理冲突 index 与工作区
            _run_git(["reset", "--merge"], cwd=repo, check=False)
            registry[task_id]["status"] = "blocked"
            registry[task_id]["blocked_reason"] = "merge_conflict"
            registry[task_id]["blocked_at"] = datetime.now().isoformat()
            _save_workspaces(registry)
            return False, (
                f"Squash merge conflict detected for '{task_id}'. "
                f"Merge rolled back via reset --merge. Worktree left intact "
                f"for manual resolution. Task marked as BLOCKED."
            )

        # Commit the squash
        res = _run_git(["commit", "-m", commit_message], cwd=repo, check=False)
        if res.returncode != 0:
            err = res.stderr.strip() or res.stdout.strip()
            # 清理 staged 的 squash 状态，避免污染主仓 index
            _run_git(["reset", "--merge"], cwd=repo, check=False)
            registry[task_id]["status"] = "blocked"
            registry[task_id]["blocked_reason"] = "squash_commit_failed"
            registry[task_id]["blocked_at"] = datetime.now().isoformat()
            _save_workspaces(registry)
            return False, f"Failed to commit squash merge (staged state rolled back): {err}"

        registry[task_id]["status"] = "merged"
        registry[task_id]["merged_at"] = datetime.now().isoformat()
        registry[task_id]["merge_strategy"] = "squash"
        _save_workspaces(registry)

        return True, f"Successfully squash-merged '{task_id}' into {base_branch}."

    def _rebase_merge(self, task_id, repo, base_branch, branch, ws_path, registry):
        """Rebase merge: replay worktree commits on top of base."""
        # 在 worktree 中 rebase onto base
        res = _run_git(["rebase", base_branch], cwd=str(ws_path), check=False)
        if res.returncode != 0:
            # 冲突！中止 rebase，标记 BLOCKED
            _run_git(["rebase", "--abort"], cwd=str(ws_path), check=False)
            registry[task_id]["status"] = "blocked"
            registry[task_id]["blocked_reason"] = "rebase_conflict"
            registry[task_id]["blocked_at"] = datetime.now().isoformat()
            _save_workspaces(registry)
            return False, (
                f"Rebase conflict detected for '{task_id}'. "
                f"Rebase aborted. Worktree left intact for manual resolution. "
                f"Task marked as BLOCKED."
            )

        # Fast-forward merge into base (in main repo)
        _run_git(["checkout", base_branch], cwd=repo)
        res = _run_git(["merge", "--ff-only", branch], cwd=repo, check=False)
        if res.returncode != 0:
            err = res.stderr.strip() or res.stdout.strip()
            return False, f"Fast-forward merge failed after rebase: {err}"

        registry[task_id]["status"] = "merged"
        registry[task_id]["merged_at"] = datetime.now().isoformat()
        registry[task_id]["merge_strategy"] = "rebase"
        _save_workspaces(registry)

        return True, f"Successfully rebased and merged '{task_id}' into {base_branch}."

    def cleanup_workspace(self, task_id, force=False):
        """Remove a workspace's worktree and delete its branch.

        Safety: refuses to cleanup unless the branch has been merged or force=True.

        Returns:
            tuple: (success: bool, message: str)
        """
        registry = _load_workspaces()
        entry = registry.get(task_id)
        ws_path = self._workspace_path(task_id)
        branch = self._branch_name(task_id)

        status = entry.get("status", "unknown") if entry else "unknown"

        # 安全检查：只允许已合并的或强制清理
        if status not in ("merged", "cleaned") and not force:
            return False, (
                f"Cannot cleanup task '{task_id}' (status: {status}). "
                f"Branch has not been merged. Use --force to override."
            )

        messages = []

        # 移除 worktree
        if ws_path.exists():
            repo = entry.get("repo", self.repo_path) if entry else self.repo_path
            res = _run_git(["worktree", "remove", str(ws_path), "--force"], cwd=repo, check=False)
            if res.returncode != 0:
                # 手动清理
                import shutil
                try:
                    shutil.rmtree(str(ws_path))
                    messages.append(f"Removed worktree directory (manual): {ws_path}")
                    # 清理 git worktree 索引
                    _run_git(["worktree", "prune"], cwd=repo, check=False)
                except Exception as e:
                    return False, f"Failed to remove worktree: {e}"
            else:
                messages.append(f"Removed worktree: {ws_path}")

        # 删除分支
        repo = entry.get("repo", self.repo_path) if entry else self.repo_path
        res = _run_git(["branch", "-D", branch], cwd=repo, check=False)
        if res.returncode == 0:
            messages.append(f"Deleted branch: {branch}")
        else:
            # 分支可能已不存在
            messages.append(f"Branch {branch} already removed or not found.")

        # 更新注册表
        if entry:
            registry[task_id]["status"] = "cleaned"
            registry[task_id]["cleaned_at"] = datetime.now().isoformat()
            _save_workspaces(registry)

        return True, " ".join(messages)

    def cleanup_all(self):
        """Clean up all workspaces that have been merged.

        Also prunes worktrees whose branches no longer exist (stale GC).

        Returns:
            list of (task_id, success, message) tuples
        """
        registry = _load_workspaces()
        results = []

        for task_id, entry in list(registry.items()):
            status = entry.get("status", "unknown")

            # 已清理的跳过
            if status == "cleaned":
                continue

            should_cleanup = False
            reason = ""

            if status == "merged":
                should_cleanup = True
                reason = "merged"
            else:
                # 检查分支是否已被删除（stale worktree）
                branch = entry.get("branch", self._branch_name(task_id))
                repo = entry.get("repo", self.repo_path)
                res = _run_git(["rev-parse", "--verify", branch], cwd=repo, check=False)
                if res.returncode != 0:
                    should_cleanup = True
                    reason = "stale (branch deleted)"

            if should_cleanup:
                success, msg = self.cleanup_workspace(task_id, force=(reason == "stale (branch deleted)"))
                results.append((task_id, success, f"[{reason}] {msg}"))

        return results

    def list_workspaces(self):
        """List all registered workspaces with their status.

        Returns:
            dict: task_id -> workspace info
        """
        return _load_workspaces()


# ─── CLI interface ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="workspace_manager: Git worktree isolation for tmux-butler tasks"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    # create
    p_create = subparsers.add_parser("create", help="Create an isolated workspace for a task")
    p_create.add_argument("--task-id", required=True, help="Unique task identifier")
    p_create.add_argument("--repo", default=None, help="Path to the git repository")
    p_create.add_argument("--base", default=None, help="Base branch (defaults to main/master)")

    # list
    subparsers.add_parser("list", help="List all active workspaces")

    # status
    p_status = subparsers.add_parser("status", help="Show status of a workspace")
    p_status.add_argument("--task-id", required=True)
    p_status.add_argument("--repo", default=None)

    # merge
    p_merge = subparsers.add_parser("merge", help="Merge completed work back to main branch")
    p_merge.add_argument("--task-id", required=True)
    p_merge.add_argument("--strategy", choices=["squash", "rebase"], default="squash",
                         help="Merge strategy (default: squash)")
    p_merge.add_argument("--message", "-m", default=None, help="Commit message for squash merge")
    p_merge.add_argument("--repo", default=None)

    # cleanup
    p_cleanup = subparsers.add_parser("cleanup", help="Clean up workspace(s)")
    p_cleanup.add_argument("--task-id", default=None, help="Task to clean up")
    p_cleanup.add_argument("--all", action="store_true", dest="cleanup_all",
                           help="Clean up ALL merged/stale workspaces")
    p_cleanup.add_argument("--force", action="store_true", help="Force cleanup even if not merged")
    p_cleanup.add_argument("--repo", default=None)

    args = parser.parse_args()

    # ── create ──
    if args.action == "create":
        wm = WorkspaceManager(repo_path=args.repo)
        try:
            path = wm.create_workspace(task_id=args.task_id, base_branch=args.base)
            print(path)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── list ──
    elif args.action == "list":
        registry = _load_workspaces()
        if not registry:
            print("No active workspaces.")
        else:
            print(f"{'Task ID':<20} {'Status':<10} {'Branch':<30} {'Path'}")
            print("-" * 100)
            for tid, info in registry.items():
                status = info.get("status", "?")
                branch = info.get("branch", "?")
                path = info.get("path", "?")
                print(f"{tid:<20} {status:<10} {branch:<30} {path}")

    # ── status ──
    elif args.action == "status":
        wm = WorkspaceManager(repo_path=args.repo)
        info = wm.workspace_status(args.task_id)
        print(json.dumps(info, indent=2, ensure_ascii=False))

    # ── merge ──
    elif args.action == "merge":
        wm = WorkspaceManager(repo_path=args.repo)
        success, msg = wm.merge_back(
            task_id=args.task_id,
            commit_message=args.message,
            strategy=args.strategy
        )
        print(msg)
        if not success:
            sys.exit(1)

    # ── cleanup ──
    elif args.action == "cleanup":
        if not args.task_id and not args.cleanup_all:
            print("Error: specify --task-id or --all", file=sys.stderr)
            sys.exit(1)

        if args.cleanup_all:
            wm = WorkspaceManager(repo_path=args.repo)
            results = wm.cleanup_all()
            if not results:
                print("No workspaces to clean up.")
            else:
                for tid, success, msg in results:
                    mark = "✓" if success else "✗"
                    print(f"  {mark} {tid}: {msg}")
        else:
            wm = WorkspaceManager(repo_path=args.repo)
            success, msg = wm.cleanup_workspace(args.task_id, force=args.force)
            print(msg)
            if not success:
                sys.exit(1)


if __name__ == "__main__":
    main()
