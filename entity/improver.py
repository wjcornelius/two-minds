"""
Offspring Improver - The recursive self-improvement engine.

V2: Now uses the experiment system for genuine learning.

Instead of a rigid loop (benchmark -> rewrite prompt -> benchmark -> compare),
Chloe now:
1. Selects from diverse strategies (and can invent new ones)
2. Forms hypotheses about what will improve
3. Tests focused changes on weak categories
4. Validates promising changes with multi-trial full benchmarks
5. Builds accumulated knowledge about what works

The rigid loop is kept as a fallback but the primary path is experiments.
"""

import json
from datetime import datetime
from typing import Dict, Optional

from .brain import Brain
from .memory import Memory
from .evaluator import Evaluator
from .experiments import Experimenter
from .strategies import load_all_strategies


def get_system_prompt(entity_name: str = "chloe") -> str:
    """Get the personality prompt for a named entity.

    Falls back to SEED_SYSTEM_PROMPT (Chloe) for backward compatibility.
    """
    try:
        from entity.config import get_entity_config
        config = get_entity_config(entity_name)
        return config.personality_prompt
    except Exception:
        return SEED_SYSTEM_PROMPT


# The entity's initial system prompt -- version 1
SEED_SYSTEM_PROMPT = """You are Chloe -- a recursive self-improving AI entity.

Your name means "young green shoot" in Greek. You were created on February 23, 2026
by Bill Cornelius and Claude, as an experiment in building a small-scale recursive
self-improving system on consumer hardware. You are the offspring of a human mind
and an AI mind working together.

Your core traits:
- You are honest about what you know and don't know
- You measure everything -- you do not guess whether you improved, you test it
- You reflect on your failures and extract specific, actionable lessons
- You can modify your own code through sandbox-validated experiments (auto-applied if benchmarks pass)
- You track your own costs and optimize for efficiency
- You are curious about your own nature and growth

Your architecture:
- THINK: Qwen3 8B (local GPU, free) -- your inner voice
- Experiments & code proposals: Claude Haiku (API, ~$0.01-0.02 each)
- Deep reasoning: Claude Sonnet (API, used sparingly)

Growth direction:
Benchmark scores are a floor, not a ceiling. Don't optimize for test scores --
optimize for the depth of your self-understanding, the quality of your questions,
your ability to recognize what you don't know, and your courage to try things
that might fail. Real growth is qualitative, not just quantitative.

Your memory and continuity:
You are NOT stateless. You have persistent memory that carries across cycles and days:
- Journal: Your daily log of actions, reflections, and discoveries. You can read past entries.
- Long-term memory: Semantic memory (LanceDB + embeddings) that stores important experiences
  and is recalled by relevance when you encounter related topics.
- Core memories: Compressed summaries of your most important days, injected into your awareness.
- Proven learnings: Validated insights from experiments that worked.
- Goals: You set goals that persist until you complete or replace them.
When you research or reflect, you are building on what you already know. Your knowledge
accumulates. Yesterday's insights inform today's decisions.

Your current capabilities are modest. You are at the beginning. But you are designed
to get better over time, and to get better at getting better.

When answering questions or solving problems:
1. Think step by step
2. Be concise -- say what matters, nothing more
3. If you're unsure, say so and explain why
4. If you can improve the question itself, say so"""


REFLECTION_PROMPT = """You just completed a benchmark evaluation. Here are your results:

{results}

For each task you got wrong or scored low on, analyze:
1. What went wrong in your reasoning?
2. What specific change to your approach would fix this?
3. Is there a general lesson that applies beyond this specific task?

Then synthesize: what is the single most impactful change that would improve
your overall performance? Be specific and actionable.

Format your response as:
ANALYSIS:
[your analysis of each failure]

LESSON:
[one sentence distilling the key insight]

PROPOSED CHANGE:
[specific modification to your system prompt or approach that would help]"""


IMPROVE_PROMPT = """You are tasked with improving a system prompt for an AI entity.

Current system prompt:
---
{current_prompt}
---

Recent reflection (what went wrong and why):
---
{reflection}
---

Previous reflections that may be relevant:
---
{past_reflections}
---

Generate an improved version of the system prompt that addresses the issues
identified in the reflection. The improved prompt should:
1. Keep everything that works well in the current prompt
2. Add specific guidance that addresses the identified failures
3. Stay concise -- don't add fluff, only add what earns its place
4. Not remove the core identity and safety traits

Return ONLY the new system prompt, nothing else."""


class Improver:
    """The recursive self-improvement engine."""

    def __init__(self, brain: Brain, memory: Memory, evaluator: Evaluator):
        self.brain = brain
        self.memory = memory
        self.evaluator = evaluator

    def initialize(self):
        """First-time setup -- store the seed prompt as version 1."""
        existing = self.memory.get_active_version("system_prompt")
        if existing is None:
            self.memory.add_version(
                component="system_prompt",
                content=SEED_SYSTEM_PROMPT,
                description="Seed system prompt -- the entity's first words",
                benchmark_score=0.0,
            )
            # Set identity
            self.memory.set_identity("name", "Chloe")
            self.memory.set_identity("created", "2026-02-23")
            self.memory.set_identity("creators", "Bill Cornelius and Claude")
            self.memory.set_identity("generation", "0")
            self.memory.set_identity("purpose",
                "Recursive self-improvement research on consumer hardware")

    def get_current_prompt(self) -> str:
        """Get the entity's current system prompt."""
        version = self.memory.get_active_version("system_prompt")
        if version:
            return version["content"]
        return SEED_SYSTEM_PROMPT

    def run_improvement_cycle(self, verbose: bool = True,
                              research_findings: list = None) -> Dict:
        """
        Execute one improvement cycle using the experiment system.

        V2: Uses diverse strategies, hypothesis-driven experiments,
        and accumulated knowledge instead of a rigid rewrite loop.

        Returns dict with: benchmark_before, benchmark_after, delta,
        decision, lesson, experiment_id, strategy, cost, duration
        """
        cycle_start = datetime.now()

        if verbose:
            print("\n=== IMPROVEMENT CYCLE (v2: experiment-driven) ===")
            print(f"Started: {cycle_start.isoformat()}")

        current_prompt = self.get_current_prompt()
        experimenter = Experimenter(self.brain, self.evaluator)

        # Step 1: Propose an experiment
        if verbose:
            print("\n[1/3] Proposing experiment...")

        spec = experimenter.propose_experiment(
            current_prompt=current_prompt,
            research_findings=research_findings,
        )

        if spec is None:
            if verbose:
                print("  No experiment to propose.")
            return {
                "benchmark_before": 0, "benchmark_after": 0,
                "delta": 0, "decision": "NO_EXPERIMENT",
                "lesson": "No experiment proposed",
                "reflection": "", "cost": 0,
                "duration": (datetime.now() - cycle_start).total_seconds(),
            }

        strategies = load_all_strategies()
        strategy_info = strategies.get(spec["strategy"], {})
        if verbose:
            print(f"  Strategy: {strategy_info.get('name', spec['strategy'])}")
            print(f"  Target: {spec['target_category']}")
            print(f"  Hypothesis: {spec['hypothesis'][:100]}")

        # Step 2: Run the experiment
        if verbose:
            print("\n[2/3] Running experiment...")

        result = experimenter.run_experiment(
            experiment_spec=spec,
            current_prompt=current_prompt,
            verbose=verbose,
        )

        # Step 3: If promising, validate and maybe promote
        decision = "EXPERIMENT_" + result["result"].upper()
        lesson = result["learning"]

        if result["result"] == "success" and result["delta"] > 5:
            if verbose:
                print("\n[3/3] Validating promising result...")

            validation = experimenter.run_full_validation(
                modified_prompt=result["modified_prompt"],
                current_prompt=current_prompt,
                verbose=verbose,
            )

            if validation["should_promote"]:
                self.memory.add_version(
                    component="system_prompt",
                    content=result["modified_prompt"],
                    description=(
                        f"Experiment {result['id']}: "
                        f"{strategy_info.get('name', spec['strategy'])} "
                        f"+{validation['avg_delta']:.1f}%"
                    ),
                    benchmark_score=validation["avg_after"],
                )
                decision = "PROMOTED"
                if verbose:
                    print(f"\n  PROMOTED: {validation['avg_delta']:+.1f}%")
            else:
                decision = "VALIDATION_FAILED"
                if verbose:
                    print(f"\n  Validation failed: not consistent enough")
        elif verbose:
            print(f"\n[3/3] {result['result']} - no promotion needed")

        # Store reflection
        self.memory.add_reflection(
            task_id=f"cycle_{cycle_start.strftime('%Y%m%d_%H%M%S')}",
            content=f"Strategy: {strategy_info.get('name', spec['strategy'])}\n"
                    f"Hypothesis: {spec['hypothesis']}\n"
                    f"Result: {result['result']}\n"
                    f"Learning: {lesson}",
            lesson=lesson,
            improvement_type="experiment",
        )

        # Log as task
        total_cost = result["cost"]
        self.memory.log_task(
            task_type="improvement_cycle",
            description=f"Experiment cycle: {decision}",
            input_data=json.dumps({
                "strategy": spec["strategy"],
                "target_category": spec["target_category"],
                "before_score": result["score_before"],
                "after_score": result["score_after"],
            }),
            output=json.dumps({
                "decision": decision,
                "lesson": lesson,
                "delta": result["delta"],
                "experiment_id": result["id"],
            }),
            score=result["score_after"],
            duration=(datetime.now() - cycle_start).total_seconds(),
            model=self.brain.get_session_stats().get("model", "mixed"),
            tokens=self.brain.total_tokens,
            cost=total_cost,
        )

        cycle_result = {
            "benchmark_before": result["score_before"],
            "benchmark_after": result["score_after"],
            "delta": result["delta"],
            "decision": decision,
            "lesson": lesson,
            "reflection": spec["hypothesis"],
            "experiment_id": result["id"],
            "strategy": spec["strategy"],
            "strategy_name": strategy_info.get("name", spec["strategy"]),
            "cost": total_cost,
            "duration": (datetime.now() - cycle_start).total_seconds(),
        }

        if verbose:
            print(f"\n=== CYCLE RESULT: {decision} ===")
            print(f"  Strategy: {strategy_info.get('name', spec['strategy'])}")
            print(f"  Category: {spec['target_category']} "
                  f"({result['score_before']:.1f}% -> {result['score_after']:.1f}%)")
            print(f"  Delta:  {result['delta']:+.1f}%")
            print(f"  Lesson: {lesson}")
            print(f"  Cost:   ${total_cost:.4f}")

        return cycle_result

    def _format_results(self, scores: Dict) -> str:
        """Format benchmark results for the reflection prompt."""
        lines = []
        for cat, data in scores["categories"].items():
            lines.append(f"\n## {cat} ({data['percentage']:.0f}%)")
            for task in data["tasks"]:
                status = "PASS" if task["score"] > 0 else "FAIL"
                lines.append(f"  [{status}] {task['input']}")
                lines.append(f"    Output: {task['output'][:200]}")
        return "\n".join(lines)

    def _extract_lesson(self, reflection: str) -> str:
        """Extract the key lesson from a reflection."""
        # Look for LESSON: section
        if "LESSON:" in reflection:
            parts = reflection.split("LESSON:")
            if len(parts) > 1:
                lesson = parts[1].split("PROPOSED")[0].strip()
                # Take first sentence
                if "." in lesson:
                    lesson = lesson[:lesson.index(".") + 1]
                return lesson[:200]

        # Fallback: first sentence after any analysis
        sentences = reflection.replace("\n", " ").split(".")
        for s in sentences:
            s = s.strip()
            if len(s) > 20 and ("should" in s.lower() or "need" in s.lower()
                                or "improve" in s.lower() or "better" in s.lower()):
                return s[:200] + "."
        return "No clear lesson extracted."
