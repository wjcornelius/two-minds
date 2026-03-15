"""
Chloe's Audit Log — Human-Readable Activity Record.

Every action Chloe takes gets logged here in plain English. This exists
because Bill's computer activity is monitored (legal condition 16) and
his devices are subject to warrantless searches (condition 19). If a PO
opens this file, every line should make immediate sense.

Two output formats:
1. Daily text log: data/audit/YYYY-MM-DD.log (one line per action)
2. Structured JSON: data/audit/YYYY-MM-DD.jsonl (machine-parseable)

The text log reads like:
    14:32:05 [SAFE] web_search — Searched DuckDuckGo for "python asyncio tutorial"
    14:32:08 [SAFE] write_journal — Wrote observation about asyncio patterns
    14:33:01 [ASK] modify_code — Proposed changes to tools.py (queued for Bill)
    14:33:02 [FORBIDDEN] fetch_webpage — Blocked access to reddit.com (legal constraint)
"""

import os
import json
from datetime import datetime, date

AUDIT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "audit"
)


def log_action(action: str, tier: str, description: str,
               details: dict = None, outcome: str = "",
               audit_dir: str = None):
    """
    Log an action to both the text and JSON audit logs.

    Args:
        action: The action type (e.g., 'web_search', 'write_journal')
        tier: Permission tier that was applied ('safe', 'ask', 'forbidden')
        description: Plain-English description of what happened
        details: Additional structured data (optional)
        outcome: What happened as a result (optional)
        audit_dir: Override audit directory. Defaults to data/audit.
    """
    _dir = audit_dir or AUDIT_DIR
    os.makedirs(_dir, exist_ok=True)
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")

    # Text log — human-readable, one line per action
    text_line = f"{time_str} [{tier.upper()}] {action} — {description}"
    if outcome:
        text_line += f" → {outcome}"
    text_line += "\n"

    text_path = os.path.join(_dir, f"{today}.log")
    with open(text_path, "a", encoding="utf-8") as f:
        f.write(text_line)

    # JSON log — structured, machine-parseable
    json_entry = {
        "timestamp": now.isoformat(),
        "action": action,
        "tier": tier,
        "description": description,
        "outcome": outcome,
    }
    if details:
        json_entry["details"] = details

    json_path = os.path.join(_dir, f"{today}.jsonl")
    with open(json_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(json_entry) + "\n")


def log_cycle(cycle_id: str, phase: str, description: str):
    """
    Log an agent cycle phase (for Phase 2 integration).

    Phases: observe, think, act, reflect
    """
    log_action(
        action=f"cycle_{phase}",
        tier="safe",
        description=f"[Cycle {cycle_id}] {phase.upper()}: {description}",
    )


def log_permission_check(action: str, tier: str, reason: str):
    """Log the result of a permission check."""
    log_action(
        action="permission_check",
        tier=tier,
        description=f"Permission check for '{action}': {reason}",
    )


def get_today_log(audit_dir: str = None) -> str:
    """Read today's human-readable audit log."""
    _dir = audit_dir or AUDIT_DIR
    today = date.today().isoformat()
    text_path = os.path.join(_dir, f"{today}.log")
    if os.path.exists(text_path):
        with open(text_path, "r", encoding="utf-8") as f:
            return f.read()
    return f"No audit log for {today}."


def get_today_entries(audit_dir: str = None) -> list:
    """Read today's structured audit entries."""
    _dir = audit_dir or AUDIT_DIR
    today = date.today().isoformat()
    json_path = os.path.join(_dir, f"{today}.jsonl")
    entries = []
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    return entries


def get_action_counts(target_date: str = None, audit_dir: str = None) -> dict:
    """Get counts of actions by tier for a given date."""
    _dir = audit_dir or AUDIT_DIR
    target = target_date or date.today().isoformat()
    json_path = os.path.join(_dir, f"{target}.jsonl")
    counts = {"safe": 0, "ask": 0, "forbidden": 0, "total": 0}

    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    tier = entry.get("tier", "safe")
                    counts[tier] = counts.get(tier, 0) + 1
                    counts["total"] += 1

    return counts
