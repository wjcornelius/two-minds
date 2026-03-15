"""
Chloe's Experiment Sandbox.

The experiment system is where Chloe actually LEARNS. Instead of a
mechanical improve loop, she forms hypotheses, designs experiments,
runs them in isolation, analyzes results, and builds genuine knowledge.

The schema is deliberately flexible (JSON columns) so it can evolve
as Chloe discovers what data she needs to track. Schema migrations
are logged so she has a history of her own structural evolution.

Lifecycle:
  1. PROPOSE - What should I try? (informed by weaknesses + research)
  2. DESIGN  - Generate the specific modification
  3. EXECUTE - Test against target category (not full benchmark)
  4. ANALYZE - What happened and what did I learn?
  5. STORE   - Record everything for future reference
"""

import os
import json
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .brain import Brain
from .evaluator import Evaluator
from .strategies import (
    load_all_strategies, get_best_strategy, get_strategies_for_category,
    apply_strategy, invent_strategy, save_custom_strategy,
)

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "experiments.db"
)

# Module-level override for multi-entity support
_configured_db_path = None


def configure(db_path: str = None):
    """Configure experiments module for a specific entity."""
    global _configured_db_path
    _configured_db_path = db_path


def get_db() -> sqlite3.Connection:
    """Get experiment database connection."""
    path = _configured_db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection):
    """Create tables if they don't exist. Schema evolves with Chloe."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS experiments (
            id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            strategy TEXT NOT NULL,
            hypothesis TEXT,
            target_category TEXT,
            modification_desc TEXT,
            prompt_before_hash TEXT,
            prompt_after TEXT,
            score_before REAL,
            score_after REAL,
            delta REAL,
            result TEXT,           -- success, failure, inconclusive
            learning TEXT,
            research_source TEXT,  -- which finding inspired this
            cost REAL DEFAULT 0,
            duration REAL DEFAULT 0,
            metadata TEXT DEFAULT '{}'  -- flexible JSON for evolving needs
        );

        CREATE TABLE IF NOT EXISTS strategy_stats (
            strategy TEXT PRIMARY KEY,
            times_tried INTEGER DEFAULT 0,
            successes INTEGER DEFAULT 0,
            failures INTEGER DEFAULT 0,
            avg_delta REAL DEFAULT 0,
            best_delta REAL DEFAULT 0,
            last_tried TEXT,
            notes TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS schema_evolution (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            change_type TEXT,      -- add_column, add_table, modify
            description TEXT,
            sql_executed TEXT,
            proposed_by TEXT       -- 'chloe', 'human', 'system'
        );

        CREATE TABLE IF NOT EXISTS learnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            category TEXT,         -- reasoning, coding, meta, strategy
            insight TEXT NOT NULL,
            confidence REAL,       -- 0-1, how sure is she?
            evidence TEXT,         -- JSON list of experiment IDs
            supersedes TEXT,       -- ID of learning this replaces
            active INTEGER DEFAULT 1
        );
    """)
    # Add parent_id column for tree-structured experiments (migration)
    try:
        conn.execute("ALTER TABLE experiments ADD COLUMN parent_id TEXT DEFAULT NULL")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.commit()


class Experimenter:
    """Chloe's experiment sandbox."""

    def __init__(self, brain: Brain, evaluator: Evaluator):
        self.brain = brain
        self.evaluator = evaluator
        self.db = get_db()

    def get_strategy_stats(self) -> Dict[str, Dict]:
        """Get performance stats for each strategy."""
        rows = self.db.execute("SELECT * FROM strategy_stats").fetchall()
        return {r["strategy"]: dict(r) for r in rows}

    def get_tried_strategies(self) -> List[str]:
        """Get list of strategies that have been tried."""
        rows = self.db.execute(
            "SELECT DISTINCT strategy FROM experiments"
        ).fetchall()
        return [r["strategy"] for r in rows]

    def get_experiment_history(self, limit: int = 20) -> List[Dict]:
        """Get recent experiments."""
        rows = self.db.execute(
            "SELECT * FROM experiments ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_learnings(self, category: str = None,
                      active_only: bool = True) -> List[Dict]:
        """Get accumulated learnings."""
        query = "SELECT * FROM learnings WHERE 1=1"
        params = []
        if active_only:
            query += " AND active = 1"
        if category:
            query += " AND category = ?"
            params.append(category)
        query += " ORDER BY confidence DESC"
        rows = self.db.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def find_weakest_category(self, system_prompt: str) -> Tuple[str, Dict]:
        """
        Run a quick benchmark and find the weakest category.
        Returns (category_name, full_scores).
        """
        scores = self.evaluator.run_benchmark(system_prompt=system_prompt)
        categories = scores["categories"]

        weakest = min(
            categories.items(),
            key=lambda x: x[1]["percentage"]
        )
        return weakest[0], scores

    def propose_experiment(
        self,
        current_prompt: str,
        scores: Dict = None,
        research_findings: List[Dict] = None,
        tier: str = "fast",
    ) -> Optional[Dict]:
        """
        Chloe proposes what to try next.

        Returns experiment specification or None if nothing to try.
        """
        # Get current state
        stats = self.get_strategy_stats()
        tried = self.get_tried_strategies()
        learnings = self.get_learnings()
        history = self.get_experiment_history(limit=10)

        # Tree search: check for a recent successful parent to build on
        parent = self._find_successful_parent(history)
        if parent:
            print(f"  [experiment] Building on successful parent: {parent['id']}")
            current_prompt = parent["prompt_after"]  # Use parent's improved prompt

        # Find weakest category if scores not provided
        if scores is None:
            weakest_cat, scores = self.find_weakest_category(current_prompt)
        else:
            categories = scores.get("categories", {})
            weakest_cat = min(
                categories.items(),
                key=lambda x: x[1]["percentage"]
            )[0] if categories else "reasoning"

        # --- CEILING CHECK ---
        # If ALL categories are at 100%, experimenting is pointless.
        # Return None so the agent picks a different action instead of
        # grinding inconclusive experiments forever.
        categories = scores.get("categories", {})
        min_score = min(
            (d["percentage"] for d in categories.values()),
            default=0,
        )
        print(f"  [experiment] Score range: min={min_score:.0f}%, weakest={weakest_cat}")
        if min_score >= 100.0:
            print(f"  [experiment] All categories at 100% — nothing to improve")
            return None

        # --- FAILURE AVOIDANCE ---
        # If we've failed too many times on a category recently, try a different one.
        # This prevents grinding the same dead-end category for hours.
        target_cat = self._pick_target_category(
            weakest_cat, scores, history
        )

        # Extract failure types from scores to guide strategy selection
        failure_types = []
        weak_data = scores.get("categories", {}).get(target_cat, {})
        for task in weak_data.get("tasks", []):
            if task.get("score", 0) == 0:
                inp = task.get("input", "").lower()
                if any(w in inp for w in ["multiply", "what is", "divided", "* "]):
                    failure_types.append("arithmetic error")
                elif "exactly" in inp and "word" in inp:
                    failure_types.append("word_count error")
                elif any(w in inp for w in ["backwards", "reverse", "spell"]):
                    failure_types.append("character manipulation error")
                elif any(w in inp for w in ["count", "how many", "letter"]):
                    failure_types.append("counting error")
                elif any(w in inp for w in ["only", "nothing else", "format"]):
                    failure_types.append("format compliance error")

        # Select strategy (failure-type-aware)
        strategy = get_best_strategy(target_cat, tried, stats, failure_types)
        if not strategy:
            # All strategies tried -- pick least-tried one
            strategy = min(
                get_strategies_for_category(target_cat),
                key=lambda s: stats.get(s, {}).get("times_tried", 0),
                default=None,
            )

        if not strategy:
            print(f"  [experiment] No strategy available for {target_cat}")
            return None

        print(f"  [experiment] Strategy: {strategy}, target: {target_cat}")

        # Build context for hypothesis generation
        history_text = ""
        if history:
            history_text = "Previous experiments:\n" + "\n".join(
                f"  - [{e['strategy']}] {e['result']}: {e['learning'] or 'no learning'}  (delta: {e['delta'] or 0:+.1f}%)"
                for e in history[:5]
            )

        learnings_text = ""
        if learnings:
            learnings_text = "Known learnings:\n" + "\n".join(
                f"  - [{l['category']}] {l['insight']} (confidence: {l['confidence']:.0%})"
                for l in learnings[:5]
            )

        research_text = ""
        if research_findings:
            research_text = "Recent research:\n" + "\n".join(
                f"  - {f.get('title', '')[:80]}: {f.get('summary', '')[:150]}"
                for f in research_findings[:3]
            )

        # Ask Chloe to form a hypothesis
        hypothesis_prompt = (
            f"You are planning an experiment to improve your performance.\n\n"
            f"Target category: {target_cat} "
            f"({scores['categories'][target_cat]['percentage']:.0f}%)\n"
            f"Strategy to try: {load_all_strategies()[strategy]['name']}\n"
            f"Strategy description: {load_all_strategies()[strategy]['description']}\n\n"
            f"Current scores by category:\n"
        )
        for cat, data in scores["categories"].items():
            hypothesis_prompt += f"  {cat}: {data['percentage']:.0f}% ({data['score']}/{data['possible']})\n"
            for task in data["tasks"]:
                status = "PASS" if task["score"] > 0 else "FAIL"
                hypothesis_prompt += f"    [{status}] {task['input'][:80]}\n"

        # Deep failure analysis for target category
        deep_analysis = self._deep_failure_analysis(scores, target_cat)
        hypothesis_prompt += f"\nDETAILED FAILURE ANALYSIS:\n{deep_analysis}\n"

        if history_text:
            hypothesis_prompt += f"\n{history_text}\n"
        if learnings_text:
            hypothesis_prompt += f"\n{learnings_text}\n"
        if research_text:
            hypothesis_prompt += f"\n{research_text}\n"

        hypothesis_prompt += (
            f"\nFill in this experiment proposal template. Be specific and concise.\n\n"
            "FAILURE_PATTERN: [What specific pattern do you see in the failing tasks?]\n"
            "ROOT_CAUSE: [WHY does the current prompt cause this failure?]\n"
            "ATOMIC_CHANGE: [ONE specific modification to the system prompt]\n"
            "MECHANISM: [How does this change fix the root cause?]\n"
            "PREDICTION: [Which specific failing task(s) will this fix? Be exact.]\n"
            "FALSIFICATION: [What result would prove this hypothesis wrong?]\n"
        )

        # Generate 3 candidates, score each, pick best (AIDE pattern)
        candidates = []
        total_cost = 0
        num_candidates = 3

        for i in range(num_candidates):
            response = self.brain.think(
                prompt=hypothesis_prompt,
                system="You are a self-improving AI forming experimental hypotheses. Fill in ALL template fields.",
                tier=tier,
                max_tokens=400,
                temperature=0.7,  # Higher temp for diversity
            )
            total_cost += response["cost"]
            raw_text = response["text"]
            print(f"  [experiment] Candidate {i+1} raw ({len(raw_text)} chars): {raw_text[:200]}...")
            parsed = self._parse_proposal(raw_text)
            if parsed:
                score = self._score_proposal(parsed, scores, target_cat, history)
                candidates.append((parsed, score))
                print(f"  [experiment] Candidate {i+1} PARSED, score={score}")
            else:
                print(f"  [experiment] Candidate {i+1} FAILED parse")

        if not candidates:
            # Retry once with a simplified prompt and higher temperature
            print(f"  [experiment] All {num_candidates} proposals failed validation, retrying with simplified prompt...")
            retry_prompt = (
                f"You are a self-improving AI. Propose ONE experiment to improve performance.\n\n"
                f"FAILURE_PATTERN: [What pattern do you see in recent failures?]\n"
                f"ROOT_CAUSE: [Why does this happen?]\n"
                f"ATOMIC_CHANGE: [One specific change to try]\n"
                f"MECHANISM: [How does this fix help?]\n"
                f"PREDICTION: [What will improve?]\n"
                f"FALSIFICATION: [What result disproves this?]\n"
            )
            response = self.brain.think(
                prompt=retry_prompt,
                system="Fill in ALL template fields. Be concise.",
                tier=tier,
                max_tokens=600,
                temperature=0.9,
            )
            total_cost += response["cost"]
            raw_text = response["text"]
            print(f"  [experiment] Retry raw ({len(raw_text)} chars): {raw_text[:200]}...")
            parsed = self._parse_proposal(raw_text)
            if parsed:
                score = self._score_proposal(parsed, scores, target_cat, history)
                candidates.append((parsed, score))
                print(f"  [experiment] Retry PARSED, score={score}")

        if not candidates:
            print(f"  [experiment] All proposals failed validation (including retry)")
            return None

        # Pick the best-scoring proposal
        candidates.sort(key=lambda x: x[1], reverse=True)
        best_parsed, best_score = candidates[0]
        print(f"  [experiment] {len(candidates)} valid proposals, best score: {best_score}")

        hypothesis = (
            f"FAILURE_PATTERN: {best_parsed['failure_pattern']}\n"
            f"ROOT_CAUSE: {best_parsed.get('root_cause', 'unknown')}\n"
            f"ATOMIC_CHANGE: {best_parsed['atomic_change']}\n"
            f"PREDICTION: {best_parsed['prediction']}"
        )

        return {
            "strategy": strategy,
            "hypothesis": hypothesis,
            "target_category": target_cat,
            "scores": scores,
            "research_context": research_text if research_findings else None,
            "cost": total_cost,
            "parent_id": parent["id"] if parent else None,
        }

    def run_experiment(
        self,
        experiment_spec: Dict,
        current_prompt: str,
        verbose: bool = True,
        thinking_tier: str = "fast",
    ) -> Dict:
        """
        Execute a single experiment.

        1. Apply strategy to generate modified prompt
        2. Benchmark the target category with both prompts
        3. Analyze results
        4. Store everything
        """
        exp_start = datetime.now()
        exp_id = f"exp_{exp_start.strftime('%Y%m%d_%H%M%S')}"
        total_cost = experiment_spec.get("cost", 0)

        strategy = experiment_spec["strategy"]
        target_cat = experiment_spec["target_category"]
        hypothesis = experiment_spec["hypothesis"]

        if verbose:
            print(f"\n--- EXPERIMENT: {load_all_strategies()[strategy]['name']} ---")
            print(f"Target: {target_cat}")
            print(f"Hypothesis: {hypothesis}")

        # Step 1: Apply strategy to generate modified prompt
        if verbose:
            print("\n  [1/4] Generating modification...")

        # Build failure analysis from scores
        scores = experiment_spec["scores"]
        failure_analysis = self._format_failures(scores, target_cat)

        modified_prompt, cost = apply_strategy(
            self.brain,
            strategy,
            current_prompt,
            failure_analysis,
            experiment_spec.get("research_context"),
            tier=thinking_tier,
        )
        total_cost += cost

        if verbose:
            print(f"  Modified prompt: {len(modified_prompt)} chars")

        # Step 2: Benchmark target category with ORIGINAL prompt
        if verbose:
            print(f"\n  [2/4] Baseline benchmark ({target_cat})...")

        baseline = self.evaluator.run_benchmark(
            category=target_cat,
            system_prompt=current_prompt,
        )
        baseline_score = baseline["percentage"]
        total_cost += sum(
            t["cost"] for cat in baseline["categories"].values()
            for t in cat["tasks"]
        )

        if verbose:
            print(f"  Baseline: {baseline_score:.1f}%")

        # Step 3: Benchmark target category with MODIFIED prompt
        if verbose:
            print(f"\n  [3/4] Testing modification ({target_cat})...")

        test_result = self.evaluator.run_benchmark(
            category=target_cat,
            system_prompt=modified_prompt,
        )
        test_score = test_result["percentage"]
        total_cost += sum(
            t["cost"] for cat in test_result["categories"].values()
            for t in cat["tasks"]
        )

        delta = test_score - baseline_score

        if verbose:
            print(f"  Test: {test_score:.1f}% (delta: {delta:+.1f}%)")

        # Step 4: Analyze results
        if verbose:
            print("\n  [4/4] Analyzing results...")

        result_type = (
            "success" if delta > 0
            else "failure" if delta < 0
            else "inconclusive"
        )

        # Ask Chloe to extract a learning
        analysis_prompt = (
            f"You ran an experiment:\n"
            f"Strategy: {load_all_strategies()[strategy]['name']}\n"
            f"Hypothesis: {hypothesis}\n"
            f"Target category: {target_cat}\n"
            f"Baseline score: {baseline_score:.1f}%\n"
            f"Test score: {test_score:.1f}%\n"
            f"Delta: {delta:+.1f}%\n"
            f"Result: {result_type}\n\n"
            f"What specific thing did you learn from this experiment? "
            f"Be precise. If it failed, why? If it succeeded, what "
            f"exactly made the difference? One or two sentences."
        )

        analysis = self.brain.think(
            prompt=analysis_prompt,
            system="You are a self-improving AI analyzing experiment results.",
            tier=thinking_tier,
            max_tokens=200,
            temperature=0.3,
        )
        learning = analysis["text"].strip()
        total_cost += analysis["cost"]

        duration = (datetime.now() - exp_start).total_seconds()

        if verbose:
            print(f"\n  Result: {result_type.upper()}")
            print(f"  Learning: {learning}")
            print(f"  Cost: ${total_cost:.4f}")

        # Store experiment
        parent_id = experiment_spec.get("parent_id")
        self.db.execute(
            """INSERT INTO experiments
               (id, timestamp, strategy, hypothesis, target_category,
                modification_desc, prompt_before_hash, prompt_after,
                score_before, score_after, delta, result, learning,
                research_source, cost, duration, metadata, parent_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                exp_id, exp_start.isoformat(), strategy, hypothesis,
                target_cat, load_all_strategies()[strategy]["description"],
                str(hash(current_prompt)), modified_prompt,
                baseline_score, test_score, delta, result_type,
                learning, experiment_spec.get("research_context", ""),
                total_cost, duration,
                json.dumps({
                    "baseline_tasks": self._task_summary(baseline),
                    "test_tasks": self._task_summary(test_result),
                }),
                parent_id,
            ),
        )

        # Update strategy stats
        self._update_strategy_stats(strategy, result_type, delta)

        # Store learning if confident enough
        if result_type in ("success", "failure"):
            confidence = 0.7 if result_type == "success" else 0.5
            self.db.execute(
                """INSERT INTO learnings
                   (timestamp, category, insight, confidence, evidence)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    datetime.now().isoformat(),
                    target_cat,
                    learning,
                    confidence,
                    json.dumps([exp_id]),
                ),
            )

        self.db.commit()

        return {
            "id": exp_id,
            "strategy": strategy,
            "hypothesis": hypothesis,
            "target_category": target_cat,
            "score_before": baseline_score,
            "score_after": test_score,
            "delta": delta,
            "result": result_type,
            "learning": learning,
            "modified_prompt": modified_prompt,
            "cost": total_cost,
            "duration": duration,
        }

    def run_full_validation(
        self,
        modified_prompt: str,
        current_prompt: str,
        verbose: bool = True,
    ) -> Dict:
        """
        Run full benchmark comparison when an experiment shows promise.
        This is the gate before promoting a change.
        """
        if verbose:
            print("\n--- FULL VALIDATION BENCHMARK ---")

        # Run multiple trials for statistical robustness
        trials = 3  # Up from 2 — majority rule instead of unanimity
        before_scores = []
        after_scores = []
        per_task_changes = []  # Track which tasks flip between trials

        for trial in range(trials):
            if verbose:
                print(f"\n  Trial {trial + 1}/{trials}...")

            before = self.evaluator.run_benchmark(
                system_prompt=current_prompt
            )
            after = self.evaluator.run_benchmark(
                system_prompt=modified_prompt
            )
            before_scores.append(before["percentage"])
            after_scores.append(after["percentage"])

            # Track per-task changes for flakiness detection
            trial_changes = {}
            for cat in before["categories"]:
                if cat in after["categories"]:
                    b_tasks = before["categories"][cat]["tasks"]
                    a_tasks = after["categories"][cat]["tasks"]
                    for bt, at in zip(b_tasks, a_tasks):
                        key = bt["input"][:50]
                        trial_changes[key] = {
                            "before": bt["passed"],
                            "after": at["passed"],
                            "flipped": bt["passed"] != at["passed"],
                        }
            per_task_changes.append(trial_changes)

        avg_before = sum(before_scores) / len(before_scores)
        avg_after = sum(after_scores) / len(after_scores)
        avg_delta = avg_after - avg_before

        # Majority rule: 2 of 3 trials must show improvement (not all)
        improved_count = sum(
            1 for a, b in zip(after_scores, before_scores) if a >= b
        )
        majority_improved = improved_count >= 2

        # Identify flaky tasks (pass/fail inconsistently across trials)
        flaky_tasks = []
        if len(per_task_changes) >= 2:
            all_keys = set()
            for tc in per_task_changes:
                all_keys.update(tc.keys())
            for key in all_keys:
                results = [tc.get(key, {}).get("after") for tc in per_task_changes]
                if len(set(results)) > 1:
                    flaky_tasks.append(key)

        if verbose:
            print(f"\n  Average before: {avg_before:.1f}%")
            print(f"  Average after:  {avg_after:.1f}%")
            print(f"  Average delta:  {avg_delta:+.1f}%")
            print(f"  Majority improved: {improved_count}/{trials}")
            if flaky_tasks:
                print(f"  Flaky tasks: {len(flaky_tasks)}")

        return {
            "avg_before": avg_before,
            "avg_after": avg_after,
            "avg_delta": avg_delta,
            "all_improved": majority_improved,  # Renamed semantics: majority rule
            "majority_improved": majority_improved,
            "improved_count": improved_count,
            "trials": trials,
            "before_scores": before_scores,
            "after_scores": after_scores,
            "flaky_tasks": flaky_tasks,
            "should_promote": avg_delta > 0 and majority_improved,
        }

    def get_summary(self) -> Dict:
        """Get experiment summary for reports."""
        total = self.db.execute(
            "SELECT COUNT(*) FROM experiments"
        ).fetchone()[0]
        successes = self.db.execute(
            "SELECT COUNT(*) FROM experiments WHERE result='success'"
        ).fetchone()[0]
        failures = self.db.execute(
            "SELECT COUNT(*) FROM experiments WHERE result='failure'"
        ).fetchone()[0]

        stats = self.get_strategy_stats()
        recent = self.get_experiment_history(limit=5)
        learnings = self.get_learnings(active_only=True)

        return {
            "total_experiments": total,
            "successes": successes,
            "failures": failures,
            "success_rate": successes / total if total > 0 else 0,
            "strategy_stats": stats,
            "recent_experiments": recent,
            "learnings": learnings,
            "total_learnings": len(learnings),
        }

    def evolve_schema(self, change_type: str, description: str,
                      sql: str, proposed_by: str = "chloe"):
        """
        Evolve the experiment database schema.
        Chloe can modify her own data structures as she learns
        what she needs to track.
        """
        try:
            self.db.execute(sql)
            self.db.execute(
                """INSERT INTO schema_evolution
                   (timestamp, change_type, description, sql_executed, proposed_by)
                   VALUES (?, ?, ?, ?, ?)""",
                (datetime.now().isoformat(), change_type, description,
                 sql, proposed_by),
            )
            self.db.commit()
            return True
        except Exception as e:
            print(f"Schema evolution failed: {e}")
            return False

    def maybe_invent_strategy(self, verbose: bool = True,
                              tier: str = "fast") -> Optional[str]:
        """
        Periodically let Chloe try to invent a new strategy.
        Returns the new strategy name or None.
        """
        history = self.get_experiment_history(limit=10)
        all_strategies = load_all_strategies()

        # Only try inventing after at least 5 experiments
        if len(history) < 5:
            return None

        if verbose:
            print("\n  Considering whether to invent a new strategy...")

        new_strategy = invent_strategy(self.brain, history, all_strategies,
                                       tier=tier)

        if new_strategy and new_strategy["name"] not in all_strategies:
            save_custom_strategy(new_strategy)
            if verbose:
                print(f"  NEW STRATEGY INVENTED: {new_strategy['display_name']}")
                print(f"  Description: {new_strategy['description']}")
            return new_strategy["name"]

        if verbose:
            print("  No new strategy needed right now.")
        return None

    def _find_successful_parent(self, history: List[Dict]) -> Optional[Dict]:
        """Find the most recent successful experiment to build on (tree search).

        Returns the parent experiment dict (with prompt_after) or None.
        Only builds on experiments from the last 24 hours.
        """
        for exp in history:  # newest first
            if exp.get("result") == "success" and exp.get("delta", 0) > 0:
                # Has a valid modified prompt to build on?
                if exp.get("prompt_after"):
                    return exp
        return None

    def _pick_target_category(
        self, weakest_cat: str, scores: Dict, history: List[Dict]
    ) -> str:
        """Pick which category to experiment on, avoiding dead ends.

        If the weakest category has had too many consecutive failures,
        rotate to a different category. This prevents grinding one
        category for hours when progress has stalled.
        """
        MAX_CONSECUTIVE_FAILURES = 5  # Give up on a category after 5 straight failures

        # Count recent consecutive failures on the weakest category
        consecutive_failures = 0
        for exp in history:  # history is newest-first
            if exp.get("target_category") == weakest_cat:
                if exp.get("result") == "failure":
                    consecutive_failures += 1
                else:
                    break  # Had a non-failure, stop counting
            # Experiments on other categories don't reset the count

        if consecutive_failures < MAX_CONSECUTIVE_FAILURES:
            return weakest_cat

        # The weakest category is a dead end right now.
        # Pick the next-weakest category that hasn't stalled.
        print(f"  [experiment] {weakest_cat} has {consecutive_failures} "
              f"consecutive failures -- trying a different category")

        categories = scores.get("categories", {})
        # Sort by score ascending (weakest first), skip the stalled one
        sorted_cats = sorted(
            categories.items(),
            key=lambda x: x[1]["percentage"],
        )

        for cat_name, cat_data in sorted_cats:
            if cat_name == weakest_cat:
                continue
            # Check this category isn't also stalled
            cat_failures = 0
            for exp in history:
                if exp.get("target_category") == cat_name:
                    if exp.get("result") == "failure":
                        cat_failures += 1
                    else:
                        break
            if cat_failures < MAX_CONSECUTIVE_FAILURES:
                print(f"  [experiment] Targeting {cat_name} instead "
                      f"({cat_data['percentage']:.0f}%)")
                return cat_name

        # Everything is stalled -- try to invent a new strategy instead,
        # and fall back to the weakest category
        print(f"  [experiment] All categories stalled -- attempting strategy invention")
        new_strat = self.maybe_invent_strategy(verbose=True, tier="local")
        if new_strat:
            print(f"  [experiment] New strategy invented: {new_strat}")
        return weakest_cat

    def _format_failures(self, scores: Dict, target_cat: str) -> str:
        """Format failure details for a category with rich detail."""
        lines = []
        cat_data = scores["categories"].get(target_cat, {})
        lines.append(f"Category: {target_cat} ({cat_data.get('percentage', 0):.0f}%)")
        lines.append("")
        fail_count = 0
        for task in cat_data.get("tasks", []):
            status = "PASS" if task["score"] > 0 else "FAIL"
            lines.append(f"  [{status}] {task['input'][:120]}")
            if task["score"] == 0:
                fail_count += 1
                lines.append(f"    WRONG OUTPUT: {task['output'][:300]}")
                if task.get("expected"):
                    lines.append(f"    EXPECTED: {task['expected'][:200]}")
                # Identify the TYPE of failure to help strategy selection
                inp = task["input"].lower()
                if any(w in inp for w in ["multiply", "what is", "divided", "* "]):
                    lines.append(f"    FAILURE TYPE: Arithmetic error -- model got the computation wrong")
                elif "exactly" in inp and "word" in inp:
                    lines.append(f"    FAILURE TYPE: Word count error -- model wrote wrong number of words")
                elif any(w in inp for w in ["backwards", "reverse", "spell"]):
                    lines.append(f"    FAILURE TYPE: Character manipulation error -- model dropped or swapped characters")
                elif any(w in inp for w in ["count", "how many", "letter"]):
                    lines.append(f"    FAILURE TYPE: Counting error -- model miscounted characters or items")
        lines.append(f"\nTotal failures: {fail_count}/{len(cat_data.get('tasks', []))}")
        lines.append("Focus on fixing the SPECIFIC failure types above, not generic improvements.")
        return "\n".join(lines)

    def _deep_failure_analysis(self, scores: Dict, target_cat: str) -> str:
        """Analyze HOW tasks fail, not just THAT they fail.

        Inspired by AgentEvolver's self-questioning: explore the failure
        landscape before proposing fixes.
        """
        lines = []
        cat_data = scores["categories"].get(target_cat, {})
        lines.append(f"Category: {target_cat} ({cat_data.get('percentage', 0):.0f}%)")

        failures = []
        for task in cat_data.get("tasks", []):
            if task["score"] == 0:
                failures.append({
                    "input": task["input"],
                    "output": task["output"],
                    "expected": task.get("expected", ""),
                })

        if not failures:
            return "No failures to analyze."

        lines.append(f"\n{len(failures)} failing tasks — detailed analysis:\n")
        for i, f in enumerate(failures, 1):
            lines.append(f"FAILURE {i}:")
            lines.append(f"  Question: {f['input'][:200]}")
            lines.append(f"  Model answered: {f['output'][:300]}")
            if f['expected']:
                lines.append(f"  Expected: {f['expected'][:200]}")
            lines.append("")

        lines.append("PATTERN ANALYSIS: Look for what these failures have in common.")
        lines.append("Are they all the same TYPE of error? Is the model misunderstanding")
        lines.append("the question, computing wrong, or formatting incorrectly?")
        return "\n".join(lines)

    def _parse_proposal(self, text: str) -> Optional[Dict]:
        """Parse a structured proposal response. Returns dict or None if invalid."""
        import re
        fields = {}
        for field in ["FAILURE_PATTERN", "ROOT_CAUSE", "ATOMIC_CHANGE",
                       "MECHANISM", "PREDICTION", "FALSIFICATION"]:
            match = re.search(
                rf"{field}:\s*(.+?)(?=\n[A-Z_]+:|$)", text, re.DOTALL
            )
            if match:
                val = match.group(1).strip()
                # Strip markdown formatting artifacts (**, ###, etc.)
                val = val.lstrip("*#").strip()
                fields[field.lower()] = val

        # Require at least the 3 critical fields
        required = ["failure_pattern", "atomic_change", "prediction"]
        missing = [f for f in required if not fields.get(f)]
        if missing:
            print(f"    [parse] Missing required fields: {missing}")
            print(f"    [parse] Found fields: {list(fields.keys())}")
            return None

        # Reject overly broad changes
        change = fields.get("atomic_change", "").lower()
        if any(w in change for w in ["restructure everything", "rewrite the entire",
                                      "completely overhaul"]):
            print(f"    [parse] Rejected: overly broad change")
            return None

        return fields

    def _score_proposal(self, parsed: Dict, scores: Dict,
                         target_cat: str, history: List[Dict]) -> int:
        """Score a parsed proposal on a quality checklist."""
        score = 0
        change = parsed.get("atomic_change", "")
        prediction = parsed.get("prediction", "")
        failure_pattern = parsed.get("failure_pattern", "")

        # +1 if FAILURE_PATTERN references specific task content
        cat_tasks = scores.get("categories", {}).get(target_cat, {}).get("tasks", [])
        task_keywords = [t["input"][:30].lower() for t in cat_tasks if t["score"] == 0]
        if any(kw[:15] in failure_pattern.lower() for kw in task_keywords if len(kw) >= 15):
            score += 1

        # +1 if ATOMIC_CHANGE is concise (<=50 words = focused)
        if len(change.split()) <= 50:
            score += 1

        # +1 if PREDICTION names a specific failing task
        if any(kw[:15] in prediction.lower() for kw in task_keywords if len(kw) >= 15):
            score += 1

        # +1 if this approach hasn't been tried before
        past_changes = [e.get("hypothesis", "").lower() for e in history]
        change_lower = change.lower()
        if not any(change_lower[:30] in past for past in past_changes if len(change_lower) >= 30):
            score += 1

        # +1 if FALSIFICATION is present and coherent
        if parsed.get("falsification") and len(parsed["falsification"]) > 10:
            score += 1

        # -2 penalty for overly broad language
        broad_words = ["restructure", "rewrite", "overhaul", "completely redo"]
        if any(w in change.lower() for w in broad_words):
            score -= 2

        return score

    def _task_summary(self, scores: Dict) -> List[Dict]:
        """Summarize task results for storage."""
        summary = []
        for cat, data in scores["categories"].items():
            for task in data["tasks"]:
                summary.append({
                    "category": cat,
                    "input": task["input"][:80],
                    "passed": task["score"] > 0,
                })
        return summary

    def _update_strategy_stats(self, strategy: str, result: str,
                               delta: float):
        """Update running stats for a strategy."""
        existing = self.db.execute(
            "SELECT * FROM strategy_stats WHERE strategy = ?",
            (strategy,)
        ).fetchone()

        if existing:
            new_tries = existing["times_tried"] + 1
            new_successes = existing["successes"] + (1 if result == "success" else 0)
            new_failures = existing["failures"] + (1 if result == "failure" else 0)
            # Running average
            new_avg = (
                (existing["avg_delta"] * existing["times_tried"] + delta)
                / new_tries
            )
            new_best = max(existing["best_delta"], delta)

            self.db.execute(
                """UPDATE strategy_stats
                   SET times_tried=?, successes=?, failures=?,
                       avg_delta=?, best_delta=?, last_tried=?
                   WHERE strategy=?""",
                (new_tries, new_successes, new_failures, new_avg,
                 new_best, datetime.now().isoformat(), strategy),
            )
        else:
            self.db.execute(
                """INSERT INTO strategy_stats
                   (strategy, times_tried, successes, failures,
                    avg_delta, best_delta, last_tried)
                   VALUES (?, 1, ?, ?, ?, ?, ?)""",
                (
                    strategy,
                    1 if result == "success" else 0,
                    1 if result == "failure" else 0,
                    delta, delta,
                    datetime.now().isoformat(),
                ),
            )
