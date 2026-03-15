"""
Model Router — Intelligent multi-model routing for Chloe.

Three routing mechanisms:
1. Self-Consistency Gating (ACAR): Run local 3x, escalate if disagreement
2. Bandit Routing (BaRP): UCB1 learns which models work for which tasks
3. Shepherding: Get hint from expensive model, complete locally

All mechanisms are Poe-point-budget-aware. Chloe has 22,222 points/day.
If budget is exhausted, everything falls back to local (free).
"""

import os
import re
import json
import math
import time
import requests
from typing import Dict, Optional
from pathlib import Path

from .brain import Brain

DATA_DIR = Path(__file__).parent.parent / "data"
MODELS_PATH = DATA_DIR / "models.json"
STATS_PATH = DATA_DIR / "model_stats.json"


class ModelRouter:
    """Routes prompts to the best model based on task, budget, and learned performance."""

    def __init__(self, brain: Brain, stats_path: Path = None):
        self.brain = brain
        self._stats_path = stats_path or STATS_PATH
        self.models = self._load_models()
        self.stats = self._load_stats()
        self.poe_budget = self.models.get("poe_budget", {})
        self.default_routing = self.models.get("default_routing", {})

        # Poe balance cache (avoid hitting API every call)
        self._poe_balance_cache = None
        self._poe_balance_ts = 0
        self._poe_calls_since_check = 0

        # Daily point tracking — initialize from budget DB so restarts
        # don't reset the counter and bypass the daily cap.
        from datetime import date as _date
        self._today = _date.today().isoformat()
        try:
            from .budget import get_poe_points_today
            self._today_points = get_poe_points_today()
        except Exception:
            self._today_points = 0

    # ── Public API ───────────────────────────────────────────────

    def route(self, prompt: str, system: str = "", task_type: str = "think",
              max_tokens: int = 1024, temperature: float = 0.3,
              allow_sc: bool = True, allow_shepherd: bool = False,
              force_tier: Optional[str] = None) -> Dict:
        """
        Smart routing. Returns brain.think() dict plus routing metadata.

        Args:
            prompt: The prompt to send
            system: System prompt
            task_type: What kind of task (think, experiment, research, etc.)
            max_tokens: Max response tokens
            temperature: Creativity level
            allow_sc: Allow self-consistency gating (THINK step only)
            allow_shepherd: Allow shepherding (expensive tasks)
            force_tier: Override routing and use this tier directly

        Returns:
            Dict from brain.think() plus: routing_method, tier_used, sc_agreement
        """
        self._check_date_rollover()

        # HARD BUDGET STOP: if daily budget is exhausted, force everything local
        from entity.budget import is_budget_exhausted
        if is_budget_exhausted() and force_tier != "local":
            force_tier = "local"

        # Forced tier (e.g., from existing code that specifies tier directly)
        if force_tier:
            if force_tier != "local" and not self._can_afford_tier(force_tier):
                print(f"  [router] Can't afford tier={force_tier}, falling back to local")
                force_tier = "local"
            result = self.brain.think(
                prompt=prompt, system=system, tier=force_tier,
                max_tokens=max_tokens, temperature=temperature,
                _skip_poe_log=True,  # Router handles Poe logging — prevent double-count
            )
            self._log_poe_usage(force_tier, result)
            result["routing_method"] = "forced"
            result["tier_used"] = force_tier
            return result

        # SC gate disabled — 3 votes always agreed on "research" but the
        # full thinking call picked something else, then UCB1 overrode that too.
        # Net effect: 15s wasted per cycle, zero influence on final action.
        # UCB1 bandit + single thinking call is the actual decision-maker.

        # Bandit-selected tier for this task type
        tier = self._bandit_select_tier(task_type)

        # Shepherding for expensive tasks
        if allow_shepherd and tier in ("fast", "deep") and self.brain.local_available:
            shepherd_result = self._shepherd(
                prompt, system, task_type, tier, max_tokens, temperature
            )
            if shepherd_result:
                return shepherd_result

        # Standard call
        result = self.brain.think(
            prompt=prompt, system=system, tier=tier,
            max_tokens=max_tokens, temperature=temperature,
            _skip_poe_log=True,  # Router handles Poe logging — prevent double-count
        )
        self._log_poe_usage(tier, result)
        result["routing_method"] = "bandit"
        result["tier_used"] = tier
        print(f"  [router] {task_type} -> {tier} ({result.get('model', '?')}) "
              f"[{result.get('duration', 0):.1f}s]")
        return result

    def update_stats(self, tier: str, task_type: str, reward: float):
        """Update model bandit stats after seeing result quality."""
        key = f"{tier}:{task_type}"
        if key not in self.stats:
            self.stats[key] = {"total_reward": 0, "times_chosen": 0}
        self.stats[key]["total_reward"] += reward
        self.stats[key]["times_chosen"] += 1
        self._save_stats()

        n = self.stats[key]["times_chosen"]
        q = self.stats[key]["total_reward"] / n
        print(f"  [router] Stats update: {key} reward={reward:.2f} "
              f"avg={q:.2f} n={n}")

    def get_budget_status(self) -> Dict:
        """Get current Poe point budget status."""
        self._check_date_rollover()
        daily_cap = self.poe_budget.get("daily_points_cap", 55555)
        return {
            "daily_cap": daily_cap,
            "used_today": self._today_points,
            "remaining_today": max(0, daily_cap - self._today_points),
            "poe_balance": self._poe_balance_cache,
        }

    # ── Self-Consistency Gating ──────────────────────────────────

    def _self_consistency_gate(self, prompt: str, system: str,
                                max_tokens: int, temperature: float) -> Optional[Dict]:
        """
        ACAR-inspired: Run local model 3x, check if ACTION agrees.
        If 2/3+ agree on a real action -> use local (free).
        If all disagree or all 'unknown' -> escalate to budget tier.

        SC samples use think=False for speed — we only need the ACTION vote,
        not deep chain-of-thought reasoning. This makes each sample ~5s
        instead of ~60s.

        If local consensus is reached, we then run ONE full thinking call
        to get the complete STAR-format response with chain-of-thought.
        """
        if not self.brain.local_available:
            return None

        # Run 3 quick samples with thinking DISABLED (just need ACTION votes).
        # We append a constraint to the prompt so the model outputs ONLY
        # the action name — Qwen3 without thinking mode won't produce STAR
        # format labels, so we need to force a one-word answer.
        sc_temp = max(temperature, 0.7)  # Need some variance for SC
        actions = []
        results = []

        sc_prompt = (
            prompt + "\n\n"
            "IMPORTANT: Respond with ONLY the action name you choose. "
            "Just one word from this list: experiment, code_experiment, "
            "research, explore_bills_world, self_study, reflect, set_goal\n"
            "ACTION:"
        )

        for i in range(3):
            result = self.brain.think(
                prompt=sc_prompt, system=system, tier="local",
                max_tokens=64,  # Only need one word
                temperature=sc_temp,
                think=False,  # Disable thinking for fast ACTION parsing
            )
            results.append(result)
            action = self._parse_action(result.get("text", ""))
            actions.append(action)

        # Check agreement
        from collections import Counter
        counts = Counter(actions)
        most_common, most_count = counts.most_common(1)[0]

        # Treat 'unknown' consensus as failure — it means parsing failed,
        # not that the model genuinely agrees on something.
        if most_common == "unknown":
            most_count = 0  # Force escalation

        if most_count >= 2:
            # Majority agrees on a real action — trust local consensus.
            # (Previously overrode "reflect" consensus with paid API call,
            #  but reflect is the cheapest action — no reason to second-guess it.)
            print(f"  [router] SC gate: {actions} -> agreement on '{most_common}' "
                  f"({most_count}/3), running full local call with thinking")
            result = self.brain.think(
                prompt=prompt, system=system, tier="local",
                max_tokens=max_tokens, temperature=temperature,
                think=True,  # Full thinking for the real response
            )
            result["routing_method"] = "self_consistency"
            result["tier_used"] = "local"
            result["sc_agreement"] = most_count / 3
            result["sc_actions"] = actions
            return result
        else:
            # Disagreement or all 'unknown' — escalate
            escalate_tier = "budget" if self._can_afford_tier("budget") else "local"
            if escalate_tier == "local":
                # Can't afford escalation — run one full local call with thinking
                print(f"  [router] SC gate: {actions} -> no agreement, "
                      f"can't afford escalation, running full local call")
                result = self.brain.think(
                    prompt=prompt, system=system, tier="local",
                    max_tokens=max_tokens, temperature=temperature,
                    think=True,
                )
                result["routing_method"] = "self_consistency_fallback"
                result["tier_used"] = "local"
                result["sc_agreement"] = 0
                result["sc_actions"] = actions
                return result

            # Escalate to budget tier
            print(f"  [router] SC gate: {actions} -> no agreement, "
                  f"escalating to {escalate_tier}")
            result = self.brain.think(
                prompt=prompt, system=system, tier=escalate_tier,
                max_tokens=max_tokens, temperature=temperature,
                _skip_poe_log=True,  # Router handles logging
            )
            self._log_poe_usage(escalate_tier, result)
            result["routing_method"] = "self_consistency_escalation"
            result["tier_used"] = escalate_tier
            result["sc_agreement"] = 0
            result["sc_actions"] = actions
            return result

        return None  # Shouldn't reach here

    # Valid action names for bare-word matching in SC gate responses
    KNOWN_ACTIONS = {
        "experiment", "code_experiment", "research", "explore_bills_world",
        "self_study", "reflect", "set_goal",
    }

    def _parse_action(self, text: str) -> str:
        """Extract ACTION field from a STAR-format response.
        Handles markdown bold: **ACTION:** research, *ACTION:* research, etc.
        Also matches bare action names (for SC gate's constrained one-word responses).
        """
        # Match ACTION: with optional surrounding markdown * characters
        match = re.search(r'\*{0,2}ACTION:\*{0,2}\s*([a-zA-Z_]+)', text, re.IGNORECASE)
        if match:
            return match.group(1).strip().lower()

        # Fallback: check if the response IS a bare action name
        # (SC gate asks for just one word)
        cleaned = text.strip().strip("*").strip().lower()
        if cleaned in self.KNOWN_ACTIONS:
            return cleaned

        # Last resort: scan for any known action name in the text
        for action in self.KNOWN_ACTIONS:
            if action in text.lower():
                return action

        return "unknown"

    # ── Shepherding ──────────────────────────────────────────────

    def _shepherd(self, prompt: str, system: str, task_type: str,
                  hint_tier: str, max_tokens: int, temperature: float) -> Optional[Dict]:
        """
        Get a short hint from an expensive model, then complete locally.
        Saves points by only requesting ~50 tokens from the paid model.
        """
        if not self._can_afford_tier(hint_tier):
            return None

        # Get hint (first ~50 tokens)
        hint_result = self.brain.think(
            prompt=prompt, system=system, tier=hint_tier,
            max_tokens=60, temperature=temperature,
            _skip_poe_log=True,  # Router handles logging
        )
        self._log_poe_usage(hint_tier, hint_result)
        hint_text = hint_result.get("text", "")

        if not hint_text.strip():
            return None

        # Feed hint to local model for completion
        shepherded_prompt = (
            f"{prompt}\n\n"
            f"Here is a partial expert analysis to build on:\n{hint_text}\n\n"
            f"Continue and complete this analysis thoroughly."
        )
        local_result = self.brain.think(
            prompt=shepherded_prompt, system=system, tier="local",
            max_tokens=max_tokens, temperature=temperature,
        )

        # Combine metadata
        local_result["routing_method"] = "shepherding"
        local_result["tier_used"] = f"shepherd:{hint_tier}+local"
        local_result["hint_text"] = hint_text[:200]
        local_result["hint_cost"] = hint_result.get("cost", 0)
        local_result["cost"] = hint_result.get("cost", 0) + local_result.get("cost", 0)

        print(f"  [router] Shepherd: {hint_tier} hint ({len(hint_text)} chars) -> "
              f"local completion [{local_result.get('duration', 0):.1f}s]")
        return local_result

    # ── Bandit Routing ───────────────────────────────────────────

    def _bandit_select_tier(self, task_type: str) -> str:
        """UCB1 over tiers for this task type. Budget-aware."""
        # Get default tier for this task type
        default = self.default_routing.get(task_type, "local")

        # Get all affordable tiers
        affordable = ["local"]  # Local is always affordable
        tiers = self.models.get("tiers", {})
        for tier_name in tiers:
            if tier_name != "local" and self._can_afford_tier(tier_name):
                affordable.append(tier_name)

        if len(affordable) <= 1:
            return "local"

        # UCB1 selection
        total_n = sum(
            self.stats.get(f"{t}:{task_type}", {}).get("times_chosen", 0)
            for t in affordable
        )
        if total_n == 0:
            # No data yet — use default
            return default if default in affordable else "local"

        best_tier = "local"
        best_score = -1

        for tier in affordable:
            key = f"{tier}:{task_type}"
            s = self.stats.get(key, {"total_reward": 0.5, "times_chosen": 1})
            n = max(s["times_chosen"], 1)
            q = s["total_reward"] / n

            # UCB1 exploration bonus
            exploration = 1.4 * math.sqrt(math.log(max(total_n, 1)) / n)
            score = q + exploration

            # Default tier loyalty bonus (small)
            if tier == default:
                score += 0.1

            if score > best_score:
                best_score = score
                best_tier = tier

        return best_tier

    # ── Poe Budget Management ────────────────────────────────────

    def _can_afford_tier(self, tier: str) -> bool:
        """Check if we can afford a call to this tier today."""
        if tier == "local":
            return True

        tiers = self.models.get("tiers", {})
        config = tiers.get(tier, {})
        points_needed = config.get("points_per_msg", 0)

        if points_needed == 0:
            return True

        daily_cap = self.poe_budget.get("daily_points_cap", 55555)
        if self._today_points + points_needed > daily_cap:
            return False

        # Check Poe balance (periodically)
        reserve_floor = self.poe_budget.get("bill_reserve_floor", 100000)
        balance = self._get_poe_balance()
        if balance is not None and balance < reserve_floor + points_needed:
            print(f"  [router] Poe balance ({balance}) near Bill's reserve "
                  f"floor ({reserve_floor}), blocking {tier}")
            return False

        return True

    def _log_poe_usage(self, tier: str, result: dict):
        """Track Poe point usage for a call. Persists to budget.db."""
        if tier == "local":
            return

        tiers = self.models.get("tiers", {})
        config = tiers.get(tier, {})
        points = config.get("points_per_msg", 0)

        if points > 0:
            self._today_points += points
            self._poe_calls_since_check += 1
            daily_cap = self.poe_budget.get("daily_points_cap", 55555)
            remaining = max(0, daily_cap - self._today_points)
            result["points_used"] = points
            result["points_remaining_today"] = remaining

            # Persist to budget database
            try:
                from .budget import log_poe_spend
                model = result.get("model", config.get("model_id", "unknown"))
                log_poe_spend(points, model=model, description=f"router:{tier}")
            except Exception as e:
                print(f"  [router] Failed to log Poe spend: {e}")

    def _get_poe_balance(self) -> Optional[int]:
        """Get Poe balance, with caching (check every 50 calls or 5 min)."""
        now = time.time()
        if (self._poe_balance_cache is not None
                and self._poe_calls_since_check < 50
                and now - self._poe_balance_ts < 300):
            return self._poe_balance_cache

        self._poe_calls_since_check = 0
        poe_key = os.getenv("POE_API_KEY")
        if not poe_key:
            return None

        try:
            resp = requests.get(
                "https://api.poe.com/usage/current_balance",
                headers={"Authorization": f"Bearer {poe_key}"},
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                self._poe_balance_cache = data.get("current_point_balance")
                self._poe_balance_ts = now
                print(f"  [router] Poe balance: {self._poe_balance_cache} points")
                return self._poe_balance_cache
        except Exception as e:
            print(f"  [router] Poe balance check failed: {e}")

        return self._poe_balance_cache  # Return stale cache if API fails

    def _check_date_rollover(self):
        """Reset daily point counter at midnight."""
        from datetime import date
        today = date.today().isoformat()
        if today != self._today:
            if self._today is not None and self._today_points > 0:
                print(f"  [router] Day rollover: spent {self._today_points} points yesterday")
            self._today = today
            self._today_points = 0

    # ── Persistence ──────────────────────────────────────────────

    def _load_models(self) -> dict:
        """Load model registry."""
        try:
            if MODELS_PATH.exists():
                with open(MODELS_PATH, "r") as f:
                    return json.load(f)
        except Exception as e:
            print(f"  [router] Failed to load models.json: {e}")
        return {}

    def _load_stats(self) -> dict:
        """Load model performance stats."""
        try:
            if self._stats_path.exists():
                with open(self._stats_path, "r") as f:
                    return json.load(f)
        except Exception as e:
            print(f"  [router] Failed to load model_stats.json: {e}")
        return {}

    def _save_stats(self):
        """Persist model performance stats."""
        try:
            with open(self._stats_path, "w") as f:
                json.dump(self.stats, f, indent=2)
        except Exception as e:
            print(f"  [router] Failed to save model_stats.json: {e}")
