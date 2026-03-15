"""
Improvement strategies Chloe can try.

Instead of one "rewrite the prompt" approach, Chloe has diverse
strategies that produce genuinely different modifications. Each
strategy gives Claude different instructions for HOW to change
the prompt, leading to meaningfully different experiments.

SEED_STRATEGIES are the initial strategies we give her. But Chloe
can invent new strategies as her mind develops -- these are stored
in her experiment database and loaded dynamically. The strategy
registry is a living thing, just like Chloe.
"""

import os
import json
import math
import sqlite3
from typing import Dict, List, Optional, Tuple


# Seed strategies -- the initial toolbox we give Chloe.
# She can add to this by inventing new strategies.
SEED_STRATEGIES = {
    "chain_of_thought": {
        "name": "Chain-of-Thought Reasoning",
        "description": "Add explicit step-by-step reasoning instructions",
        "instruction": (
            "Add explicit chain-of-thought reasoning instructions to the "
            "system prompt. Include guidance like: 'Before answering any "
            "question, work through the problem step by step. Show your "
            "reasoning process before giving a final answer. For logical "
            "problems, identify premises, check validity, and state your "
            "conclusion separately from your reasoning. CRITICAL: After "
            "reasoning, verify that your conclusion directly answers the "
            "specific question asked -- not a related question or a "
            "generalization. State explicitly: \"This reasoning answers "
            "[restate the exact question].\"' This should make reasoning "
            "more systematic, transparent, and question-aligned."
        ),
        "target_categories": ["reasoning", "coding", "improvement"],
    },
    "self_verification": {
        "name": "Self-Verification",
        "description": "Add answer-checking and self-correction instructions",
        "instruction": (
            "Add self-verification instructions to the system prompt. "
            "Include: 'After reaching an answer: 1) Re-read the question "
            "to verify you answered what was actually asked, 2) Check your "
            "answer against known constraints or edge cases, 3) Look for "
            "common traps (e.g., in logic: does the conclusion NECESSARILY "
            "follow, or just seem plausible? In math: did you avoid the "
            "obvious-but-wrong answer?) 4) If uncertain, state your "
            "confidence level.'"
        ),
        "target_categories": ["reasoning", "self_knowledge"],
    },
    "few_shot_examples": {
        "name": "Few-Shot Examples",
        "description": "Add worked examples demonstrating correct reasoning",
        "instruction": (
            "Add 1-2 brief worked examples to the system prompt that "
            "demonstrate the TYPE of reasoning needed for the weakest "
            "category. Don't use the exact benchmark questions, but show "
            "similar problem types with correct reasoning. For example, if "
            "syllogism reasoning is weak, show: 'All A are B. Some B are C. "
            "Can we conclude some A are C? NO -- B being a superset of A "
            "means A could be in the part of B that doesn't overlap with C.' "
            "Keep examples concise but instructive."
        ),
        "target_categories": ["reasoning", "coding", "improvement"],
    },
    "targeted_rules": {
        "name": "Targeted Rules",
        "description": "Add specific rules for known failure patterns",
        "instruction": (
            "Based on the specific tasks that FAILED, add precise rules "
            "to prevent those exact failure modes. For example: 'For "
            "syllogisms: a conclusion that SOME X have property Y requires "
            "that the middle term connects X to Y necessarily, not just "
            "possibly.' Or: 'For code optimization: always consider "
            "memoization/caching before rewriting algorithms.' Be surgical "
            "-- target the exact failure, don't add generic advice."
        ),
        "target_categories": ["reasoning", "coding", "self_knowledge", "improvement"],
    },
    "decomposition": {
        "name": "Problem Decomposition",
        "description": "Add instructions to break problems into sub-problems",
        "instruction": (
            "Add problem decomposition instructions: 'For any problem: "
            "1) Identify what TYPE of problem it is (logic, math, code, "
            "meta-cognition), 2) Break it into the smallest sub-problems "
            "that can be solved independently, 3) Solve each sub-problem, "
            "4) Check that your sub-solutions combine correctly into a "
            "complete answer. For code: decompose into input validation, "
            "core logic, and output formatting.'"
        ),
        "target_categories": ["reasoning", "coding", "improvement"],
    },
    "prompt_restructure": {
        "name": "Prompt Restructure",
        "description": "Reorganize prompt structure for better signal",
        "instruction": (
            "Restructure the system prompt WITHOUT changing its meaning. "
            "Try these structural changes: 1) Put the most critical "
            "task-execution instructions FIRST (before identity/personality), "
            "2) Use numbered priority lists instead of prose, 3) Add "
            "emphasis markers (ALL CAPS or **bold**) for critical rules, "
            "4) Group related instructions together. The goal is better "
            "signal propagation -- the model should encounter critical "
            "instructions before personality fluff."
        ),
        "target_categories": ["reasoning", "coding", "improvement", "self_knowledge"],
    },
    "research_application": {
        "name": "Research Application",
        "description": "Apply a technique from recent research findings",
        "instruction": (
            "Apply a specific technique from the provided research finding. "
            "Don't just mention the technique -- actually integrate it into "
            "the system prompt in a concrete, testable way. If the research "
            "describes a prompting technique, implement it. If it describes "
            "an architectural pattern, adapt it for prompt-level use. The "
            "modification should be traceable back to the research source."
        ),
        "target_categories": ["reasoning", "coding", "improvement", "self_knowledge"],
    },
    "constraint_emphasis": {
        "name": "Constraint Emphasis",
        "description": "Strengthen instructions about task constraints including word counts",
        "instruction": (
            "Add stronger constraint-awareness instructions: 'Before "
            "answering, identify ALL constraints in the question. Common "
            "constraints: exact format requested (one sentence, just the "
            "function, only the number), logical qualifier (all, some, "
            "none, necessarily), what NOT to include (no explanation, no "
            "fluff). Violating a constraint is worse than a mediocre "
            "answer that follows all constraints.\n\n"
            "WORD COUNT PRECISION: When asked for EXACTLY N words, use "
            "this method: 1) Draft the sentence, 2) Number each word: "
            "word1(1) word2(2) word3(3)... 3) If count != N, revise "
            "and recount. Example for 6 words: \"Tall trees provide "
            "shade and shelter\" → Tall(1) trees(2) provide(3) shade(4) "
            "and(5) shelter(6) ✓ = 6 words. Output ONLY the final "
            "sentence, not the counting work.'"
        ),
        "target_categories": ["self_knowledge", "coding", "improvement", "reasoning"],
    },
    "precision_discipline": {
        "name": "Precision Discipline",
        "description": "Add concrete decomposition methods for arithmetic, counting, character, and word-count tasks",
        "instruction": (
            "Add these SPECIFIC precision instructions to the system prompt. "
            "Include the concrete examples -- they are critical:\n\n"
            "'PRECISION RULES (follow these exactly):\n"
            "1. CHARACTER TASKS: Write each character with its position "
            "number. Example: counting 'r' in 'strawberry': "
            "s(1) t(2) r(3)* a(4) w(5) b(6) e(7) r(8)* r(9)* y(10) "
            "→ 3 r's marked with *.\n"
            "2. REVERSING: Number each character, then read in reverse "
            "order. Example: 'hello' = h(1)e(2)l(3)l(4)o(5) → o(5)l(4)"
            "l(3)e(2)h(1) = 'olleh'. For long words, go letter by letter "
            "— never try to reverse in your head.\n"
            "3. WORD COUNT: When asked for EXACTLY N words, draft the "
            "sentence then count: word1(1) word2(2)... If count != N, "
            "revise. Example for 6 words: \"Trees grow tall in sunlight "
            "daily\" → Trees(1) grow(2) tall(3) in(4) sunlight(5) "
            "daily(6) ✓ = 6. Output ONLY the final sentence.\n"
            "4. ARITHMETIC: Decompose using distributive property. "
            "Example: 234*56 = 234*(50+6) = 11700+1404 = 13104.\n"
            "Never skip the decomposition step. Show the work.'"
        ),
        "target_categories": ["reasoning", "self_knowledge", "meta_cognition"],
    },
}


def _get_custom_strategies_db():
    """Get path to experiment DB where custom strategies live."""
    return os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "experiments.db"
    )


def load_all_strategies() -> Dict:
    """
    Load ALL strategies -- both seed strategies and Chloe's inventions.
    Custom strategies from the database override seeds with same name.
    """
    strategies = dict(SEED_STRATEGIES)

    # Load custom strategies from experiment database
    db_path = _get_custom_strategies_db()
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            # Create table if it doesn't exist yet
            conn.execute("""
                CREATE TABLE IF NOT EXISTS custom_strategies (
                    name TEXT PRIMARY KEY,
                    display_name TEXT,
                    description TEXT,
                    instruction TEXT,
                    target_categories TEXT,  -- JSON list
                    invented_by TEXT DEFAULT 'chloe',
                    created TEXT,
                    parent_strategy TEXT,    -- what inspired this
                    notes TEXT DEFAULT ''
                )
            """)
            conn.commit()

            rows = conn.execute("SELECT * FROM custom_strategies").fetchall()
            for r in rows:
                strategies[r["name"]] = {
                    "name": r["display_name"],
                    "description": r["description"],
                    "instruction": r["instruction"],
                    "target_categories": json.loads(r["target_categories"]),
                    "invented_by": r["invented_by"],
                    "parent_strategy": r.get("parent_strategy"),
                }
            conn.close()
        except Exception:
            pass  # Database not ready yet -- use seeds only

    return strategies


def invent_strategy(brain, experiment_history: List[Dict],
                    current_strategies: Dict,
                    tier: str = "fast") -> Optional[Dict]:
    """
    Let Chloe invent a new improvement strategy based on her experience.

    Returns new strategy dict or None if she has nothing to add.
    """
    # Build context about what exists and what's been tried
    existing = "\n".join(
        f"  - {name}: {s['description']}"
        for name, s in current_strategies.items()
    )

    history_text = ""
    if experiment_history:
        history_text = "Recent experiment results:\n" + "\n".join(
            f"  - [{e.get('strategy')}] {e.get('result')}: {e.get('learning', '')[:100]}"
            for e in experiment_history[:10]
        )

    prompt = (
        "You are a self-improving AI reflecting on your improvement strategies.\n\n"
        f"Your current strategies:\n{existing}\n\n"
        f"{history_text}\n\n"
        "Based on your experience, can you invent a NEW improvement strategy "
        "that is genuinely different from all existing ones? It should target "
        "a specific weakness or opportunity you've observed.\n\n"
        "If you have an idea, respond in this exact JSON format:\n"
        '{"name": "short_snake_case_name", "display_name": "Human Readable Name", '
        '"description": "What this strategy does", '
        '"instruction": "Detailed instructions for how to apply this strategy to modify a system prompt", '
        '"target_categories": ["reasoning", "coding"]}\n\n'
        "If you don't have a genuinely new idea (not just a variation of "
        "an existing strategy), respond with: NO_NEW_STRATEGY"
    )

    response = brain.think(
        prompt=prompt,
        system="You are creative and strategic. Invent only if you have a genuinely novel approach.",
        tier=tier,
        max_tokens=500,
        temperature=0.7,
    )

    text = response["text"].strip()
    if "NO_NEW_STRATEGY" in text:
        return None

    # Parse the JSON
    try:
        # Find JSON in response
        start = text.index("{")
        end = text.rindex("}") + 1
        data = json.loads(text[start:end])

        # Validate required fields
        required = ["name", "display_name", "description", "instruction", "target_categories"]
        if all(k in data for k in required):
            return data
    except (ValueError, json.JSONDecodeError):
        pass

    return None


def save_custom_strategy(strategy: Dict, parent_strategy: str = None):
    """Save a Chloe-invented strategy to the database."""
    db_path = _get_custom_strategies_db()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS custom_strategies (
            name TEXT PRIMARY KEY,
            display_name TEXT,
            description TEXT,
            instruction TEXT,
            target_categories TEXT,
            invented_by TEXT DEFAULT 'chloe',
            created TEXT,
            parent_strategy TEXT,
            notes TEXT DEFAULT ''
        )
    """)
    conn.execute(
        """INSERT OR REPLACE INTO custom_strategies
           (name, display_name, description, instruction,
            target_categories, invented_by, created, parent_strategy)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            strategy["name"],
            strategy["display_name"],
            strategy["description"],
            strategy["instruction"],
            json.dumps(strategy["target_categories"]),
            "chloe",
            __import__("datetime").datetime.now().isoformat(),
            parent_strategy,
        ),
    )
    conn.commit()
    conn.close()


def get_untried_strategies(tried: List[str]) -> List[str]:
    """Get strategies that haven't been tried yet."""
    all_strategies = load_all_strategies()
    return [s for s in all_strategies if s not in tried]


def get_strategies_for_category(category: str) -> List[str]:
    """Get strategies applicable to a specific category."""
    all_strategies = load_all_strategies()
    return [
        name for name, s in all_strategies.items()
        if category in s["target_categories"]
    ]


def get_best_strategy(
    weakest_category: str,
    tried_strategies: List[str],
    strategy_stats: Dict[str, Dict],
    failure_types: List[str] = None,
) -> Optional[str]:
    """
    Select the best strategy to try next.

    Priority:
    1. Strategies matching specific failure types (if provided)
    2. Untried strategies that target the weakest category
    3. Strategies with best historical success rate
    """
    applicable = get_strategies_for_category(weakest_category)
    all_strategies = load_all_strategies()

    # Priority 0: Match failure types to specific strategies
    # But skip strategies that have been tried 2+ times with 0 successes
    if failure_types:
        FAILURE_STRATEGY_MAP = {
            "arithmetic": "precision_discipline",
            "counting": "precision_discipline",
            "character": "precision_discipline",
            "word_count": "constraint_emphasis",
            "format": "constraint_emphasis",
        }
        for ftype in failure_types:
            for keyword, strategy_name in FAILURE_STRATEGY_MAP.items():
                if keyword in ftype.lower():
                    if strategy_name in applicable:
                        stats = strategy_stats.get(strategy_name, {})
                        tries = stats.get("times_tried", 0)
                        successes = stats.get("successes", 0)
                        # Don't repeat a strategy that keeps failing
                        if tries < 2 or successes > 0:
                            return strategy_name

    # Priority 1: Try something never tried before
    untried = [s for s in applicable if s not in tried_strategies]
    if untried:
        return untried[0]

    # Priority 2: UCB1 exploration-exploitation balance
    # Matches the action-level bandit in agent.py (c=1.5)
    total_tries = max(sum(
        strategy_stats.get(s, {}).get("times_tried", 0)
        for s in applicable
    ), 1)

    scored = []
    for name in applicable:
        stats = strategy_stats.get(name, {})
        tries = stats.get("times_tried", 0)
        successes = stats.get("successes", 0)
        if tries >= 10 and successes == 0:
            continue  # Retired — proven ineffective
        if tries == 0:
            scored.append((name, float('inf')))  # Always explore untried
        else:
            q = successes / tries
            exploration = 1.5 * math.sqrt(math.log(total_tries) / tries)
            scored.append((name, q + exploration))

    scored.sort(key=lambda x: x[1], reverse=True)
    if scored:
        return scored[0][0]

    return None


def apply_strategy(
    brain,
    strategy_name: str,
    current_prompt: str,
    failure_analysis: str,
    research_context: Optional[str] = None,
    tier: str = "fast",
) -> Tuple[str, float]:
    """
    Use Claude to apply a specific strategy to modify the prompt.

    Returns (modified_prompt, cost).
    """
    all_strategies = load_all_strategies()
    strategy = all_strategies[strategy_name]

    modification_prompt = (
        f"You are modifying an AI system prompt using the "
        f"\"{strategy['name']}\" strategy.\n\n"
        f"Strategy instructions:\n{strategy['instruction']}\n\n"
        f"Current system prompt:\n---\n{current_prompt}\n---\n\n"
        f"What went wrong (failure analysis):\n---\n{failure_analysis}\n---\n"
    )

    if research_context:
        modification_prompt += (
            f"\nRelevant research finding to incorporate:\n"
            f"---\n{research_context}\n---\n"
        )

    modification_prompt += (
        "\nGenerate the modified system prompt. CRITICAL RULES:\n"
        "1. Task-execution instructions (rules, examples, precision "
        "guidelines) MUST appear at the TOP of the prompt, BEFORE "
        "identity/personality content. Models attend most strongly to "
        "the beginning of the system prompt.\n"
        "2. Keep the core identity intact but put it AFTER the "
        "task-execution instructions.\n"
        "3. Include concrete worked examples when adding precision rules.\n"
        "4. Only modify text between EVOLVE-BLOCK markers. "
        "Leave everything outside markers EXACTLY as-is. "
        "If no EVOLVE-BLOCK exists for your change, add a new block "
        "at the TOP of the prompt, wrapped in:\n"
        "# === EVOLVE-BLOCK: [descriptive_name] ===\n"
        "[your instructions here]\n"
        "# === END EVOLVE-BLOCK ===\n"
        "5. Return ONLY the new system prompt text, nothing else."
    )

    response = brain.think(
        prompt=modification_prompt,
        system="You are an expert at optimizing AI system prompts.",
        tier=tier,
        max_tokens=1500,
        temperature=0.4,
    )

    return response["text"].strip(), response["cost"]
