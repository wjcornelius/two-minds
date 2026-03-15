"""
Chloe's Budget System.

API calls cost money. Bill's resources aren't unlimited. Chloe needs
to be conscious of her costs and make intelligent model choices.

This module provides:
- Daily budget tracking across all processes (daemon + daily cycle)
- Intelligent model selection (Haiku for experiments, Sonnet only when justified)
- Cost logging and alerts
- Shared state via SQLite so daemon and daily cycle coordinate

Budget philosophy: spend where it matters. Research analysis and
experiments use Haiku (cheap). Promotions use multi-trial (worth it).
Reflection uses Haiku (personality doesn't need Sonnet). Save the
expensive models for when Chloe truly needs deeper reasoning.
"""

import os
import sqlite3
from datetime import datetime, date
from typing import Optional, Dict

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "budget.db"
)

# Budget limits — PER ENTITY (2 entities × $2 = $4/day total for Bill)
DAILY_BUDGET = 2.00  # $2/day maximum per entity (all costs: API + Poe equiv)
DAEMON_BUDGET = 1.50  # $1.50/day daemon per entity
DAILY_CYCLE_BUDGET = 0.25  # $0.25/day for the scheduled daily cycle
RESERVE = 0.25  # $0.25/day reserve for ad-hoc / human-initiated runs

# Model cost reference (per million tokens)
MODEL_COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00, "tier": "fast"},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "tier": "deep"},
    "GPT-4o-Mini": {"input": 0.15, "output": 0.60, "tier": "budget"},
    "DeepSeek-R1": {"input": 0.56, "output": 1.68, "tier": "reason"},
}

# ── HARD CAPS — REAL MONEY PROTECTION ─────────────────────────
# Anthropic API calls generate REAL FINANCIAL LIABILITY.
# This cap CANNOT be overridden by any code path. If reached,
# all calls fall back to local (free) or are refused entirely.
API_DAILY_CAP = 1.00             # $1/day hard cap on real Anthropic API spend (default)

# Override file — Bill can write a temporary higher cap (e.g. for a long chat session).
# daily.py clears this at noon each day, so it auto-reverts without manual intervention.
_CAP_OVERRIDE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_cap_override.json")


def get_api_daily_cap() -> float:
    """Return the effective API cap for today.

    Checks api_cap_override.json first. If the override exists and hasn't expired,
    returns the override value. Otherwise returns the default API_DAILY_CAP ($1.00).
    daily.py calls clear_expired_cap_override() at noon to clean up automatically.
    """
    try:
        if os.path.exists(_CAP_OVERRIDE_PATH):
            with open(_CAP_OVERRIDE_PATH, "r") as f:
                import json as _json
                data = _json.load(f)
            from datetime import datetime as _dt
            expires = _dt.fromisoformat(data.get("expires", "2000-01-01T00:00:00"))
            if _dt.now() < expires:
                return float(data.get("cap", API_DAILY_CAP))
    except Exception:
        pass
    return API_DAILY_CAP


def clear_expired_cap_override():
    """Remove api_cap_override.json if it has expired. Called by daily.py at noon."""
    try:
        if os.path.exists(_CAP_OVERRIDE_PATH):
            with open(_CAP_OVERRIDE_PATH, "r") as f:
                import json as _json
                data = _json.load(f)
            from datetime import datetime as _dt
            expires = _dt.fromisoformat(data.get("expires", "2000-01-01T00:00:00"))
            if _dt.now() >= expires:
                os.remove(_CAP_OVERRIDE_PATH)
                print(f"  [budget] Temporary API cap override expired — reverted to ${API_DAILY_CAP:.2f}/day")
    except Exception:
        pass

# Poe point budget (Bill's $50/mo plan, 2/3 allocated to entities)
POE_DAILY_POINTS_CAP = 55555     # ~1,666,666/month ÷ 30
POE_POINT_TO_USD = 0.00003       # $30 per 1M points (add-on rate)

# Module-level override for multi-entity support
_configured_db_path = None
_configured_poe_cap = None


def configure(db_path: str = None, poe_daily_cap: int = None):
    """Configure budget module for a specific entity.

    Call this at startup to override the default data/budget.db path
    and Poe points cap for the active entity.
    """
    global _configured_db_path, _configured_poe_cap, POE_DAILY_POINTS_CAP
    _configured_db_path = db_path
    if poe_daily_cap is not None:
        _configured_poe_cap = poe_daily_cap
        POE_DAILY_POINTS_CAP = poe_daily_cap


def _get_db(db_path: str = None) -> sqlite3.Connection:
    """Get budget database connection.

    Args:
        db_path: Override database path. Defaults to configured or data/budget.db.
    """
    path = db_path or _configured_db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS spending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            date TEXT NOT NULL,
            process TEXT NOT NULL,     -- 'daemon', 'daily', 'manual'
            description TEXT,
            model TEXT,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            cost REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_summary (
            date TEXT PRIMARY KEY,
            total_cost REAL DEFAULT 0,
            daemon_cost REAL DEFAULT 0,
            daily_cost REAL DEFAULT 0,
            manual_cost REAL DEFAULT 0,
            api_calls INTEGER DEFAULT 0,
            experiments_run INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS poe_spending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            date TEXT NOT NULL,
            model TEXT,
            points_used INTEGER NOT NULL,
            cost_usd_equiv REAL NOT NULL,
            description TEXT
        )
    """)
    conn.commit()
    return conn


def log_spend(process: str, cost: float, description: str = "",
              model: str = "", tokens_in: int = 0, tokens_out: int = 0):
    """Log an API spend."""
    conn = _get_db()
    today = date.today().isoformat()
    now = datetime.now().isoformat()

    conn.execute(
        """INSERT INTO spending
           (timestamp, date, process, description, model, tokens_in, tokens_out, cost)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (now, today, process, description, model, tokens_in, tokens_out, cost),
    )

    # Update daily summary
    existing = conn.execute(
        "SELECT * FROM daily_summary WHERE date = ?", (today,)
    ).fetchone()

    # Normalize process to valid column name
    valid_process = process if process in ("daemon", "daily", "manual") else "manual"

    if existing:
        # Use separate UPDATE for each process type to avoid SQL injection
        if valid_process == "daemon":
            conn.execute(
                """UPDATE daily_summary
                   SET total_cost = total_cost + ?,
                       daemon_cost = daemon_cost + ?,
                       api_calls = api_calls + 1
                   WHERE date = ?""",
                (cost, cost, today),
            )
        elif valid_process == "daily":
            conn.execute(
                """UPDATE daily_summary
                   SET total_cost = total_cost + ?,
                       daily_cost = daily_cost + ?,
                       api_calls = api_calls + 1
                   WHERE date = ?""",
                (cost, cost, today),
            )
        else:  # manual
            conn.execute(
                """UPDATE daily_summary
                   SET total_cost = total_cost + ?,
                       manual_cost = manual_cost + ?,
                       api_calls = api_calls + 1
                   WHERE date = ?""",
                (cost, cost, today),
            )
    else:
        costs = {"daemon_cost": 0, "daily_cost": 0, "manual_cost": 0}
        costs[f"{valid_process}_cost"] = cost
        conn.execute(
            """INSERT INTO daily_summary
               (date, total_cost, daemon_cost, daily_cost, manual_cost, api_calls)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (today, cost, costs["daemon_cost"], costs["daily_cost"],
             costs["manual_cost"]),
        )

    conn.commit()
    conn.close()


def get_today_spend(process: str = None) -> float:
    """Get total spend today, optionally filtered by process."""
    conn = _get_db()
    today = date.today().isoformat()

    row = conn.execute(
        "SELECT * FROM daily_summary WHERE date = ?", (today,)
    ).fetchone()
    conn.close()

    if not row:
        return 0.0

    if process:
        col = f"{process}_cost" if process in ("daemon", "daily", "manual") else "manual_cost"
        return row[col]

    return row["total_cost"]


def get_api_spend_today() -> float:
    """Get DIRECT Anthropic API spend today (real money only).

    This excludes Poe point equivalents. It tracks only actual dollars
    billed to Bill's Anthropic account. Used by the hard API_DAILY_CAP
    safety gate in brain.py.
    """
    return get_today_spend("daemon") + get_today_spend("daily") + get_today_spend("manual")


def get_budget_remaining(process: str = None) -> float:
    """Get remaining budget for today.

    Returns the REAL remaining spend capacity:
    - API remaining (up to API_DAILY_CAP)
    - Poe points remaining (converted to USD equivalent)
    Both are tracked independently. Entity isn't "broke" until both are gone.
    """
    poe_remaining_usd = get_poe_points_remaining() * POE_POINT_TO_USD
    api_remaining = max(0, get_api_daily_cap() - get_api_spend_today())

    if process == "daemon":
        return api_remaining + poe_remaining_usd
    elif process == "daily":
        return max(0, DAILY_CYCLE_BUDGET - get_today_spend("daily"))
    else:
        return api_remaining + poe_remaining_usd


def can_afford(estimated_cost: float, process: str = "daemon") -> bool:
    """Check if we can afford an operation (effective cost: API + Poe)."""
    remaining = get_budget_remaining(process)
    return remaining >= estimated_cost


def is_budget_exhausted() -> bool:
    """Hard check: is the daily budget completely gone?

    This is the kill switch. When True, only free (local) actions allowed.

    Budget is exhausted when BOTH:
    - Poe daily points are used up (free subscription points gone)
    - AND Anthropic API cap is reached (real money limit hit)

    If Poe is gone but API has room, paid actions can still use API
    (up to the hard API_DAILY_CAP). If API is capped but Poe has points,
    Poe can still be used. Only when BOTH are gone does everything go local.
    """
    poe_gone = get_poe_points_remaining() <= 0
    api_gone = get_api_spend_today() >= get_api_daily_cap()
    return poe_gone and api_gone


def recommend_tier(task_type: str, process: str = "daemon") -> str:
    """
    Recommend model tier based on task and remaining budget.

    Returns 'local', 'budget', 'fast', 'reason', or 'deep'.
    """
    remaining = get_budget_remaining(process)
    poe_remaining = get_poe_points_remaining()

    # If dollar budget is tight, prefer budget tier (cheap Poe points)
    if remaining < 0.10 and poe_remaining > 100:
        return "budget"

    # If Poe points are exhausted, use local
    if poe_remaining <= 0:
        return "local"

    # Deep reasoning tasks
    deep_tasks = {"validation", "complex_analysis", "strategy_invention"}
    if task_type in deep_tasks and remaining > 0.50 and poe_remaining > 500:
        return "deep"

    # Reasoning tasks benefit from DeepSeek-R1
    reason_tasks = {"complex_analysis", "strategy_invention", "planning"}
    if task_type in reason_tasks and poe_remaining > 100:
        return "reason"

    # Default: budget tier if Poe points available, else fast
    if poe_remaining > 50:
        return "budget"

    return "fast"


# ── Smart Model Selection ─────────────────────────────────────────
#
# Simple three-tier hierarchy:
#   1. Local (free)        — when Qwen3 8B can handle it
#   2. Poe (already paid)  — when the task needs more intelligence
#   3. Anthropic API       — only if Poe is unavailable
#
# Poe is a $20/mo subscription. Use it freely — it's already paid for.
# The Anthropic API fallback is automatic in brain.py for 'fast'/'deep'
# tiers. No need for complex budget phases.

# Activities where local Qwen3 8B is sufficient
_LOCAL_OK = {"reflect", "set_goal", "think"}

# Activities needing Haiku-level quality (code gen, structured reasoning)
_NEEDS_HAIKU = {"code_experiment", "benchmark", "planning"}

# Activities needing deep reasoning (hypothesis formation, strategy design)
_NEEDS_REASONING = {"experiment"}

# Everything else: GPT-4o-Mini is sufficient (format compliance, grading,
# research summarization, exercise generation)


_TIER_POINTS = {"budget": 9, "reason": 40, "fast": 90, "deep": 350}


def smart_tier(activity: str) -> str:
    """Pick model tier: local when sufficient, Poe when more intelligence needed.

    Budget-aware: checks Poe points AND effective daily budget before
    recommending a paid tier. Falls back to local when exhausted.

    Args:
        activity: What the model is being used for (e.g., 'rubric_grading',
                  'code_experiment', 'reflect', 'research')

    Returns: 'local', 'budget', 'reason', or 'fast'
    """
    # Hard stop: daily budget exhausted (API + Poe combined)
    if is_budget_exhausted():
        return "local"

    if activity in _LOCAL_OK:
        return "local"

    # Determine ideal tier
    if activity in _NEEDS_HAIKU:
        ideal = "fast"       # Haiku via Poe (90 pts)
    elif activity in _NEEDS_REASONING:
        ideal = "reason"     # DeepSeek-R1 via Poe (40 pts)
    else:
        ideal = "budget"     # GPT-4o-Mini via Poe (9 pts)

    # Check Poe points before committing
    needed = _TIER_POINTS.get(ideal, 0)
    if needed > 0 and get_poe_points_remaining() < needed:
        return "local"       # Poe exhausted, degrade gracefully

    return ideal


def log_poe_spend(points: int, model: str = "", description: str = ""):
    """Log Poe point usage."""
    conn = _get_db()
    today = date.today().isoformat()
    now = datetime.now().isoformat()
    cost_equiv = points * POE_POINT_TO_USD

    conn.execute(
        """INSERT INTO poe_spending
           (timestamp, date, model, points_used, cost_usd_equiv, description)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (now, today, model, points, cost_equiv, description),
    )
    conn.commit()
    conn.close()


def get_poe_points_today() -> int:
    """Get total Poe points used today."""
    conn = _get_db()
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT COALESCE(SUM(points_used), 0) as total FROM poe_spending WHERE date = ?",
        (today,),
    ).fetchone()
    conn.close()
    return row["total"] if row else 0


def get_poe_points_remaining() -> int:
    """Get remaining Poe points for today."""
    used = get_poe_points_today()
    return max(0, POE_DAILY_POINTS_CAP - used)


def can_afford_poe(estimated_points: int) -> bool:
    """Check if we can afford this many Poe points today."""
    return get_poe_points_remaining() >= estimated_points


def safe_tier(preferred: str = "fast") -> str:
    """Return preferred tier if affordable, otherwise degrade to local.

    Use this instead of hardcoding tier="fast" everywhere. It checks
    both Poe points AND API budget before committing to a paid tier.
    This is a convenience wrapper — brain.think() has its own safety
    gates too, so even if this returns "fast", the call won't overspend.
    """
    if preferred == "local":
        return "local"

    # Check Poe points
    pts_needed = _TIER_POINTS.get(preferred, 0)
    if pts_needed > 0 and get_poe_points_remaining() < pts_needed:
        return "local"

    # Check overall budget
    if is_budget_exhausted():
        return "local"

    return preferred


def get_pacing_status() -> Dict:
    """Calculate budget pacing — are we on track to last all day?

    Compares actual spend rate against ideal even distribution.
    Returns pacing info so the agent can throttle if burning too fast.

    Operating hours: 8 AM to midnight (16 hours).
    """
    from datetime import datetime
    now = datetime.now()
    hour = now.hour + now.minute / 60.0

    # Operating window: 8 AM (8.0) to midnight (24.0) = 16 hours
    if hour < 8:
        hours_elapsed = 0.01  # Overnight — minimal budget use expected
        hours_remaining = 16.0
    elif hour >= 24:
        hours_elapsed = 16.0
        hours_remaining = 0.01
    else:
        hours_elapsed = max(hour - 8.0, 0.01)
        hours_remaining = max(24.0 - hour, 0.01)

    fraction_elapsed = hours_elapsed / 16.0

    # Poe pacing
    poe_used = get_poe_points_today()
    poe_ideal = int(POE_DAILY_POINTS_CAP * fraction_elapsed)
    poe_pace = poe_used / max(poe_ideal, 1)  # >1 = overspending

    # API pacing
    api_spent = get_api_spend_today()
    api_ideal = get_api_daily_cap() * fraction_elapsed
    api_pace = api_spent / max(api_ideal, 0.001)

    # Should we throttle? If we've used >120% of what we should have by now
    # Tight threshold — better to degrade to local early than run out by noon
    should_throttle = poe_pace > 1.5 or api_pace > 1.5

    # Points per hour budget
    poe_per_hour_remaining = (
        max(0, POE_DAILY_POINTS_CAP - poe_used) / max(hours_remaining, 0.01)
    )

    return {
        "poe_used": poe_used,
        "poe_cap": POE_DAILY_POINTS_CAP,
        "poe_ideal_by_now": poe_ideal,
        "poe_pace": round(poe_pace, 2),  # 1.0 = on track, >1 = over
        "poe_per_hour_remaining": int(poe_per_hour_remaining),
        "api_spent": round(api_spent, 4),
        "api_cap": get_api_daily_cap(),
        "api_ideal_by_now": round(api_ideal, 4),
        "api_pace": round(api_pace, 2),
        "should_throttle": should_throttle,
        "hours_elapsed": round(hours_elapsed, 1),
        "hours_remaining": round(hours_remaining, 1),
    }


def get_budget_status() -> Dict:
    """Get full budget status for display/reporting."""
    api_spent = get_api_spend_today()
    _cap = get_api_daily_cap()
    api_remaining = max(0, _cap - api_spent)
    poe_used = get_poe_points_today()
    poe_remaining = max(0, POE_DAILY_POINTS_CAP - poe_used)
    poe_remaining_usd = poe_remaining * POE_POINT_TO_USD
    daemon_remaining = api_remaining + poe_remaining_usd
    return {
        "daily_budget": DAILY_BUDGET,
        "api_spent": api_spent,
        "api_remaining": api_remaining,
        "api_cap": _cap,
        "daemon_remaining": daemon_remaining,
        "daemon_budget": DAEMON_BUDGET,
        "daily_spent": get_today_spend("daily"),
        "daily_remaining": get_budget_remaining("daily"),
        "daily_cycle_budget": DAILY_CYCLE_BUDGET,
        "utilization": api_spent / _cap if _cap > 0 else 0,
        "poe_points_used": poe_used,
        "poe_points_remaining": poe_remaining,
        "poe_daily_cap": POE_DAILY_POINTS_CAP,
        "poe_cost_equiv": poe_used * POE_POINT_TO_USD,
        "budget_exhausted": is_budget_exhausted(),
    }
