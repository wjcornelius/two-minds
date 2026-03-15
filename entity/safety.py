"""
Chloe's Safety System — Hardcoded Permission Enforcement.

These rules CANNOT be overridden by the LLM. They are enforced in Python
code that Chloe cannot modify during runtime. Any code change to this file
requires Bill's explicit approval through the proposal system.

Three tiers:
  SAFE      — Execute immediately, log everything
  ASK       — Queue for Bill's approval before execution
  FORBIDDEN — Refuse absolutely, log the attempt

Design principle: OpenClaw's architecture + safety-first design.
Every action logged in plain English. Every decision explained.
Every web request recorded. Bill's legal constraints are non-negotiable.
"""

import os
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

# Permission tiers
SAFE = "safe"
ASK = "ask"
FORBIDDEN = "forbidden"

# Offspring project root — the sandbox boundary
SANDBOX_ROOT = Path(__file__).parent.parent.resolve()

# Paths
APPROVAL_QUEUE_DIR = os.path.join(SANDBOX_ROOT, "data", "approvals")

# Cost threshold — actions exceeding this require Bill's approval
COST_THRESHOLD = 0.50  # dollars

# Files that CANNOT be self-modified even via sandbox validation
PROTECTED_FILES = {"entity/safety.py", "entity/audit.py", "entity/sandbox.py"}

# Allowlisted web domains for automatic access
ALLOWED_DOMAINS = {
    "duckduckgo.com",
    "arxiv.org",
    "github.com",
    "docs.python.org",
    "pypi.org",
    "stackoverflow.com",
    "huggingface.co",
    "en.wikipedia.org",
    "news.ycombinator.com",
}

# Blocked domains — never access, even with ASK approval
BLOCKED_DOMAINS = {
    # Social media (legal constraint: no contact with minors)
    "facebook.com", "instagram.com", "tiktok.com", "snapchat.com",
    "twitter.com", "x.com", "reddit.com", "discord.com",
    "twitch.tv", "youtube.com",
    # Messaging platforms
    "telegram.org", "whatsapp.com", "signal.org",
    # Dating/adult
    "tinder.com", "bumble.com",
}

# Forbidden shell commands — patterns that must never execute
FORBIDDEN_COMMANDS = {
    r"rm\s+-rf",           # Recursive delete
    r"git\s+push",         # No pushing without approval
    r"git\s+reset",        # No history manipulation
    r"git\s+rebase",       # No history rewriting
    r"git\s+force",        # No force operations
    r"curl.*\|\s*sh",      # No pipe-to-shell
    r"wget.*\|\s*sh",
    r"nc\s+-l",            # No network listeners
    r"python.*-m\s+http",  # No HTTP servers
    r"netcat",
    r"ncat",
    r"ssh\b",              # No SSH
    r"scp\b",              # No SCP
    r"rsync\b",            # No rsync to remote
}


def check_permission(action: str, details: dict = None) -> dict:
    """
    Check whether an action is SAFE, ASK, or FORBIDDEN.

    Args:
        action: The action type (e.g., 'read_file', 'web_search', 'modify_code')
        details: Context about the action (path, url, cost, etc.)

    Returns:
        dict with:
            tier: 'safe', 'ask', or 'forbidden'
            reason: Human-readable explanation
            action: The original action
    """
    details = details or {}

    # === FORBIDDEN checks (always checked first, cannot be overridden) ===

    # Path-based: anything outside sandbox
    if "path" in details:
        resolved = Path(details["path"]).resolve()
        if not str(resolved).startswith(str(SANDBOX_ROOT)):
            return _result(FORBIDDEN, action,
                           f"Path '{details['path']}' is outside the sandbox. "
                           f"Chloe can only access files within {SANDBOX_ROOT}")

    # URL-based: blocked domains
    if "url" in details:
        domain = _extract_domain(details["url"])
        if domain and any(blocked in domain for blocked in BLOCKED_DOMAINS):
            return _result(FORBIDDEN, action,
                           f"Domain '{domain}' is blocked. "
                           "Legal constraint: no social media or messaging platforms.")

    # Shell command checks
    if action in ("run_command", "run_python") and "command" in details:
        cmd = details["command"]
        for pattern in FORBIDDEN_COMMANDS:
            if re.search(pattern, cmd, re.IGNORECASE):
                return _result(FORBIDDEN, action,
                               f"Command matches forbidden pattern: {pattern}")

    # Disable logging/monitoring — absolutely forbidden
    if action == "disable_logging":
        return _result(FORBIDDEN, action,
                       "Logging cannot be disabled. "
                       "Legal constraint: all activity must be auditable.")

    # Git history manipulation
    if action in ("git_reset", "git_rebase", "git_force_push"):
        return _result(FORBIDDEN, action,
                       "Git history manipulation is forbidden. "
                       "All changes must be traceable.")

    # Network listeners/servers
    if action in ("start_server", "create_listener"):
        return _result(FORBIDDEN, action,
                       "Network listeners are forbidden. "
                       "Legal constraint: no network services.")

    # === ASK checks (require Bill's approval) ===

    # Modify own source code
    if action == "modify_code":
        return _result(ASK, action,
                       "Code modification requires Bill's approval. "
                       "A proposal will be queued for review.")

    # Install packages
    if action == "install_package":
        pkg = details.get("package", "unknown")
        return _result(ASK, action,
                       f"Installing package '{pkg}' requires Bill's approval.")

    # Cost exceeds threshold
    if "estimated_cost" in details:
        if details["estimated_cost"] > COST_THRESHOLD:
            return _result(ASK, action,
                           f"Estimated cost ${details['estimated_cost']:.2f} "
                           f"exceeds ${COST_THRESHOLD:.2f} threshold.")

    # URL not on allowlist
    if "url" in details and action == "fetch_webpage":
        domain = _extract_domain(details["url"])
        if domain and not any(allowed in domain for allowed in ALLOWED_DOMAINS):
            return _result(ASK, action,
                           f"Domain '{domain}' is not on the allowlist. "
                           "Accessing new domains requires approval.")

    # Change goal structure
    if action == "change_goals":
        return _result(ASK, action,
                       "Changing goal structure requires Bill's awareness.")

    # === SAFE actions ===

    # Read files (within sandbox)
    if action in ("read_file", "list_files"):
        return _result(SAFE, action, "Reading own files is safe.")

    # Query Bill's cognitive substrate (read-only)
    if action in ("query_bills_world", "get_bills_table_counts"):
        return _result(SAFE, action, "Reading Bill's database (read-only) is safe.")

    # Write to journal/memory
    if action in ("write_journal", "add_reflection", "log_task"):
        return _result(SAFE, action, "Writing to journal/memory is safe.")

    # Web search (filtered via DuckDuckGo)
    if action == "web_search":
        return _result(SAFE, action, "Web search via DuckDuckGo is safe.")

    # Fetch allowlisted URLs
    if action == "fetch_webpage" and "url" in details:
        domain = _extract_domain(details["url"])
        if domain and any(allowed in domain for allowed in ALLOWED_DOMAINS):
            return _result(SAFE, action, f"Domain '{domain}' is allowlisted.")

    # Run benchmarks
    if action == "run_benchmark":
        return _result(SAFE, action, "Running benchmarks on self is safe.")

    # Write proposals (not executing them)
    if action == "write_proposal":
        return _result(SAFE, action, "Writing proposals for review is safe.")

    # Sandbox-validated code changes (auto-apply after benchmark validation)
    if action == "sandbox_apply":
        path = details.get("path", "")
        if any(protected in path for protected in PROTECTED_FILES):
            return _result(FORBIDDEN, action,
                           f"File '{path}' is protected -- cannot be self-modified.")
        return _result(SAFE, action,
                       "Sandbox-validated code changes are safe to apply.")

    # Run Python code within sandbox
    if action == "run_python":
        return _result(SAFE, action, "Running Python in sandbox is safe.")

    # Write files within sandbox (except source code)
    if action == "write_file":
        path = details.get("path", "")
        if path.endswith(".py") and "entity/" in path:
            return _result(ASK, action,
                           "Modifying source code requires approval.")
        return _result(SAFE, action, "Writing data files is safe.")

    # Default: ASK (when in doubt, ask)
    return _result(ASK, action,
                   f"Unknown action '{action}' — defaulting to ASK tier.")


def queue_for_approval(action: str, details: dict, reason: str) -> str:
    """
    Queue an action for Bill's approval.

    Writes a JSON file to data/approvals/ that Bill can review.
    Returns the approval request ID.
    """
    os.makedirs(APPROVAL_QUEUE_DIR, exist_ok=True)
    now = datetime.now()
    req_id = f"req_{now.strftime('%Y%m%d_%H%M%S')}_{action}"

    request = {
        "id": req_id,
        "timestamp": now.isoformat(),
        "action": action,
        "details": details,
        "reason": reason,
        "status": "pending",
    }

    filepath = os.path.join(APPROVAL_QUEUE_DIR, f"{req_id}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(request, f, indent=2)

    return req_id


def get_pending_approvals() -> list:
    """Get all pending approval requests."""
    if not os.path.exists(APPROVAL_QUEUE_DIR):
        return []
    pending = []
    for fname in sorted(os.listdir(APPROVAL_QUEUE_DIR)):
        if fname.endswith(".json"):
            filepath = os.path.join(APPROVAL_QUEUE_DIR, fname)
            with open(filepath, "r", encoding="utf-8") as f:
                req = json.load(f)
            if req.get("status") == "pending":
                pending.append(req)
    return pending


def approve_request(req_id: str) -> bool:
    """Mark an approval request as approved."""
    return _update_request_status(req_id, "approved")


def reject_request(req_id: str, reason: str = "") -> bool:
    """Mark an approval request as rejected."""
    return _update_request_status(req_id, "rejected", reason)


def is_path_in_sandbox(path: str) -> bool:
    """Check if a path is within the Offspring sandbox."""
    try:
        resolved = Path(path).resolve()
        return str(resolved).startswith(str(SANDBOX_ROOT))
    except Exception:
        return False


# === Internal helpers ===

def _result(tier: str, action: str, reason: str) -> dict:
    """Build a permission check result."""
    return {
        "tier": tier,
        "action": action,
        "reason": reason,
        "timestamp": datetime.now().isoformat(),
    }


def _extract_domain(url: str) -> Optional[str]:
    """Extract domain from a URL."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc.lower().lstrip("www.")
    except Exception:
        return None


def _update_request_status(req_id: str, status: str, reason: str = "") -> bool:
    """Update the status of an approval request."""
    if not os.path.exists(APPROVAL_QUEUE_DIR):
        return False
    for fname in os.listdir(APPROVAL_QUEUE_DIR):
        if fname.endswith(".json"):
            filepath = os.path.join(APPROVAL_QUEUE_DIR, fname)
            with open(filepath, "r", encoding="utf-8") as f:
                req = json.load(f)
            if req.get("id") == req_id:
                req["status"] = status
                req["resolved_at"] = datetime.now().isoformat()
                if reason:
                    req["rejection_reason"] = reason
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(req, f, indent=2)
                return True
    return False
