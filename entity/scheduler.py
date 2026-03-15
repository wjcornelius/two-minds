"""
Chloe's Developmental Mode Scheduler — Gives her day structure.

Replaces the flat UCB1 bandit with a mode-based system that allocates
cycles to four modes (CURRICULUM, LABORATORY, FREE, REFLECT) based on
Chloe's developmental phase. The balance shifts as she matures.

Deterministic — no LLM call needed. ~0 cost.

Usage:
    from entity.scheduler import pick_mode, MODE_ACTIONS, PHASE_TARGETS
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Mode → Action Mapping ────────────────────────────────────────
# Each mode scopes which ACTIONS are available during that cycle.
# UCB1 still operates, but only within the mode's action set.

MODE_ACTIONS = {
    "curriculum": [],  # Curriculum handler picks deterministically, no UCB1
    "laboratory": ["experiment", "code_experiment"],
    "free": ["research", "explore_bills_world", "self_study"],
    "reflect": ["reflect", "set_goal"],
}

# ── Phase Targets ─────────────────────────────────────────────────
# As Chloe matures, curriculum shrinks and free time grows.

PHASE_TARGETS = {
    "infant": {
        "curriculum": 0.40,
        "laboratory": 0.20,
        "free": 0.25,
        "reflect": 0.15,
    },
    "toddler": {
        "curriculum": 0.30,
        "laboratory": 0.25,
        "free": 0.30,
        "reflect": 0.15,
    },
    "child": {
        "curriculum": 0.20,
        "laboratory": 0.25,
        "free": 0.40,
        "reflect": 0.15,
    },
    "adolescent": {
        "curriculum": 0.10,
        "laboratory": 0.20,
        "free": 0.50,
        "reflect": 0.20,
    },
    "adult": {
        "curriculum": 0.05,
        "laboratory": 0.15,
        "free": 0.60,
        "reflect": 0.20,
    },
    "expert": {
        "curriculum": 0.10,
        "laboratory": 0.25,
        "free": 0.45,
        "reflect": 0.20,
    },
    "sage": {
        "curriculum": 0.05,
        "laboratory": 0.30,
        "free": 0.45,
        "reflect": 0.20,
    },
}

ALL_MODES = ["curriculum", "laboratory", "free", "reflect"]


def pick_mode(
    cycle_count: int,
    competencies: dict,
    recent_modes: list,
    has_budget: bool = True,
) -> str:
    """Pick the mode for this cycle based on developmental phase targets.

    Algorithm:
    1. Look at last 10 modes to compute actual distribution.
    2. Compare against phase target.
    3. Pick the most underrepresented mode.
    4. Apply hard constraints (no curriculum 2x in a row, etc.).

    Args:
        cycle_count: Current cycle number.
        competencies: Full competencies dict (has overall_phase).
        recent_modes: List of recent mode strings (most recent last).
        has_budget: Whether daemon budget allows API calls.

    Returns:
        One of: "curriculum", "laboratory", "free", "reflect".
    """
    phase = competencies.get("overall_phase", "infant")
    targets = PHASE_TARGETS.get(phase, PHASE_TARGETS["infant"])

    # Count actual distribution in recent window
    window = recent_modes[-10:] if recent_modes else []
    window_size = max(len(window), 1)

    actual = {}
    for mode in ALL_MODES:
        actual[mode] = sum(1 for m in window if m == mode) / window_size

    # Compute deficit (how far below target each mode is)
    deficit = {}
    for mode in ALL_MODES:
        deficit[mode] = targets[mode] - actual[mode]

    # ── Hard constraints: filter out disallowed modes ──

    available = set(ALL_MODES)

    # No curriculum 2x in a row
    if recent_modes and recent_modes[-1] == "curriculum":
        available.discard("curriculum")

    # No reflect 3x in a row
    if len(recent_modes) >= 2 and all(m == "reflect" for m in recent_modes[-2:]):
        available.discard("reflect")

    # No laboratory without budget
    if not has_budget:
        available.discard("laboratory")

    # First cycle: always start with curriculum
    if cycle_count <= 1 or not recent_modes:
        if "curriculum" in available:
            return "curriculum"

    if not available:
        available = {"free"}  # Always safe fallback

    # Pick mode with highest deficit (most underrepresented)
    best_mode = max(available, key=lambda m: deficit.get(m, 0))

    logger.debug(
        f"Mode selection: phase={phase}, "
        f"deficits={', '.join(f'{m}={deficit[m]:+.2f}' for m in ALL_MODES)}, "
        f"picked={best_mode}"
    )

    return best_mode


def get_mode_for_action(action: str) -> str:
    """Given an action name, return which mode it belongs to."""
    for mode, actions in MODE_ACTIONS.items():
        if action in actions:
            return mode
    # curriculum has no actions in the list (handled separately)
    return "free"  # Default
