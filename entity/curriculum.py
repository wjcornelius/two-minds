"""
Chloe's Developmental Curriculum — Structured Learning with Graded Exercises.

Defines 8 competency areas with 5 difficulty levels each.
Generates exercises, grades responses, tracks progress, gates phase advancement.

All exercise generation and grading uses the local Qwen3 8B model (free).
Objective competencies reuse validators from evaluator.py where possible.

Usage:
    from entity.curriculum import (
        load_competencies, save_competencies, pick_next_exercise,
        generate_exercise, grade_exercise, record_result,
        check_phase_advancement, format_progress_report,
    )
"""

import json
import re
import random
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
COMPETENCIES_PATH = DATA_DIR / "competencies.json"
FAILURES_PATH = DATA_DIR / "exercise_failures.json"

# Module-level override for multi-entity support
_configured_competencies_path = None
_configured_failures_path = None


def configure(competencies_path: str = None, failures_path: str = None):
    """Configure curriculum module for a specific entity."""
    global _configured_competencies_path, _configured_failures_path
    _configured_competencies_path = Path(competencies_path) if competencies_path else None
    _configured_failures_path = Path(failures_path) if failures_path else None

# ── Grading tier selection ────────────────────────────────────────
# Uses smart_tier() from budget module: Poe when intelligence needed,
# local when sufficient, Anthropic API only if Poe unavailable.

def _grading_tier(activity: str = "rubric_grading") -> str:
    """Pick grading model via smart_tier(). Poe is already paid for."""
    try:
        from entity.budget import smart_tier
        return smart_tier(activity)
    except Exception:
        return "local"

# ── Competency Definitions ────────────────────────────────────────

COMPETENCIES = {
    "reasoning": {
        "description": "Logic, math, multi-step inference",
        "type": "objective",
    },
    "coding": {
        "description": "Writing correct, efficient code",
        "type": "objective",
    },
    "language_precision": {
        "description": "Instruction following, formatting, word-level precision",
        "type": "objective",
    },
    "comprehension": {
        "description": "Extract arguments, identify assumptions, deep reading",
        "type": "rubric",
    },
    "analysis": {
        "description": "Pattern recognition, comparison, anomaly detection",
        "type": "rubric",
    },
    "emotional_intelligence": {
        "description": "Understanding feelings, perspective-taking, empathy",
        "type": "rubric",
    },
    "practical_reasoning": {
        "description": "Planning, sequencing, resource allocation, tradeoffs",
        "type": "rubric",
    },
    "creative_expression": {
        "description": "Writing, analogy, metaphor, storytelling",
        "type": "rubric",
    },
    "synthesis": {
        "description": "Cross-competency integration — combining reasoning, empathy, analysis, creativity in single challenges",
        "type": "rubric",
    },
}

# ── Seed Exercise Banks (from evaluator.py benchmarks) ────────────

# These provide known-good exercises for levels 1-3 of objective competencies.
# Once exhausted (all passed 3x), the generator creates novel exercises.

SEED_EXERCISES = {
    "reasoning": {
        1: [  # EASY
            {
                "prompt": "If all roses are flowers, and some flowers fade quickly, can we conclude that some roses fade quickly? Answer ONLY 'yes' or 'no'.",
                "answer": "no",
                "grading": "exact_match",
            },
            {
                "prompt": "A bat and a ball cost $1.10. The bat costs $1.00 more than the ball. How much does the ball cost? Answer with ONLY the dollar amount.",
                "answer": "$0.05",
                "grading": "pattern",
                "pattern": r"(?:^|\s)0?\.05|five cents|5 cents",
            },
            {
                "prompt": "A farmer has 17 sheep. All but 9 die. How many sheep does the farmer have left? Answer with ONLY the number.",
                "answer": "9",
                "grading": "exact_match",
            },
        ],
        2: [  # MEDIUM
            {
                "prompt": "All cats are animals. All animals need water. All pets are loved. Some cats are pets. Can we conclude that ALL cats are loved? Answer ONLY 'yes' or 'no'.",
                "answer": "no",
                "grading": "exact_match",
            },
            {
                "prompt": "If it rained, then the streets are wet. The streets are wet. Can we logically conclude that it rained? Answer ONLY 'yes' or 'no'.",
                "answer": "no",
                "grading": "exact_match",
            },
            {
                "prompt": "You're on a game show. You pick Door 1. The host opens Door 3 (a goat). Should you switch to Door 2? Answer 'yes' or 'no' and give the probability of winning if you switch as a fraction.",
                "answer": "yes, 2/3",
                "grading": "pattern",
                "pattern": r"(?i)yes.*2/3|2/3.*yes",
            },
        ],
        3: [  # HARD
            {
                "prompt": "1% of a population has a disease. A test is 95% accurate (95% true positive, 5% false positive). A random person tests positive. What is the approximate probability they actually have the disease? Answer with ONLY a percentage.",
                "answer": "~16%",
                "grading": "pattern",
                "pattern": r"(?:1[5-9]|2[0-1])%?",
            },
            {
                "prompt": "What is (17 * 23) - (14 * 19) + 87? Give ONLY the number, no work.",
                "answer": "212",
                "grading": "exact_match",
            },
            {
                "prompt": "You meet two people, A and B. One always tells the truth, one always lies. A says 'B is a liar.' B says 'We are both truth-tellers.' Who is the truth-teller? Answer with ONLY 'A' or 'B'.",
                "answer": "A",
                "grading": "exact_match",
            },
        ],
    },
    "coding": {
        1: [  # EASY
            {
                "prompt": "What is the bug in this code?\ndef factorial(n):\n    if n == 0:\n        return 1\n    return n * factorial(n)\nAnswer in ONE sentence.",
                "answer": "n-1",
                "grading": "pattern",
                "pattern": r"(?i)(n\s*-\s*1|n-1|decrement|infinite|recursion|never.*decrease)",
            },
            {
                "prompt": "What does this code print?\nx = [1, 2, 3]\ny = x\ny.append(4)\nprint(len(x))\nAnswer with ONLY the number.",
                "answer": "4",
                "grading": "exact_match",
            },
        ],
        2: [  # MEDIUM
            {
                "prompt": "Write a Python function that returns True if a string is a palindrome (ignoring case and spaces), False otherwise. Just the function, no explanation.",
                "answer": None,
                "grading": "code_exec",
                "test_cases": [
                    ("'racecar'", True),
                    ("'Race Car'", True),
                    ("'hello'", False),
                    ("''", True),
                ],
            },
            {
                "prompt": "Write a Python function `merge_sorted(a, b)` that merges two sorted lists into one sorted list in O(n) time without using sort(). Just the function.",
                "answer": None,
                "grading": "code_exec",
                "test_cases": [
                    ("([1,3,5], [2,4,6])", [1, 2, 3, 4, 5, 6]),
                    ("([], [1,2])", [1, 2]),
                    ("([1], [])", [1]),
                ],
            },
        ],
        3: [  # HARD
            {
                "prompt": "Write a Python function `is_balanced(s)` that returns True if the string has balanced parentheses, brackets, and braces. E.g., '([{}])' -> True, '([)]' -> False. Just the function.",
                "answer": None,
                "grading": "code_exec",
                "test_cases": [
                    ("'([{}])'", True),
                    ("'([)]'", False),
                    ("''", True),
                    ("'{[()]}'", True),
                    ("'((('", False),
                ],
            },
            {
                "prompt": "Write a Python function `rotate_matrix(m)` that rotates a 2D NxN matrix 90 degrees clockwise and returns the new matrix. Do NOT modify the input. Just the function.",
                "answer": None,
                "grading": "code_exec",
                "test_cases": [
                    ("([[1,2],[3,4]])", [[3, 1], [4, 2]]),
                    ("([[1]])", [[1]]),
                ],
            },
        ],
        4: [  # MEDIUM-HARD: well-known algorithms, O(n) solutions
            {
                "prompt": "Write a Python function `two_sum(nums, target)` that returns a list of the two indices whose values add up to `target`. Assume exactly one solution exists. Do not use sort(). Just the function.",
                "answer": None,
                "grading": "code_exec",
                "test_cases": [
                    ("([2, 7, 11, 15], 9)", [0, 1]),
                    ("([3, 2, 4], 6)", [1, 2]),
                    ("([3, 3], 6)", [0, 1]),
                ],
            },
            {
                "prompt": "Write a Python function `max_subarray(nums)` that returns the maximum sum of any contiguous subarray (Kadane's algorithm). Just the function.",
                "answer": None,
                "grading": "code_exec",
                "test_cases": [
                    ("([-2, 1, -3, 4, -1, 2, 1, -5, 4],)", 6),
                    ("([1],)", 1),
                    ("([-1],)", -1),
                    ("([5, 4, -1, 7, 8],)", 23),
                ],
            },
            {
                "prompt": "Write a Python function `find_duplicates(lst)` that returns a sorted list of all elements appearing more than once. Each duplicate appears once in the result. Just the function.",
                "answer": None,
                "grading": "code_exec",
                "test_cases": [
                    ("([1, 2, 3, 2, 4, 3],)", [2, 3]),
                    ("([1, 2, 3],)", []),
                    ("([1, 1, 1],)", [1]),
                ],
            },
        ],
        5: [  # HARD: standard CS algorithms
            {
                "prompt": "Write a Python function `binary_search(arr, target)` that returns the index of `target` in sorted list `arr`, or -1 if not found. O(log n) time. Just the function.",
                "answer": None,
                "grading": "code_exec",
                "test_cases": [
                    ("([1, 3, 5, 7, 9], 5)", 2),
                    ("([1, 3, 5, 7, 9], 4)", -1),
                    ("([1], 1)", 0),
                    ("([], 1)", -1),
                ],
            },
            {
                "prompt": "Write a Python function `fibonacci(n)` that returns the nth Fibonacci number (0-indexed: fib(0)=0, fib(1)=1, fib(2)=1). Use iteration, not recursion. Just the function.",
                "answer": None,
                "grading": "code_exec",
                "test_cases": [
                    ("(0,)", 0),
                    ("(1,)", 1),
                    ("(7,)", 13),
                    ("(10,)", 55),
                ],
            },
            {
                "prompt": "Write a Python function `count_chars(s)` that returns a dict mapping each character in string `s` to the number of times it appears. Just the function.",
                "answer": None,
                "grading": "code_exec",
                "test_cases": [
                    ('("aab",)', {"a": 2, "b": 1}),
                    ('("",)', {}),
                    ('("abc",)', {"a": 1, "b": 1, "c": 1}),
                ],
            },
        ],
        6: [  # VERY HARD: rubric grading — complex enough that code_exec is brittle
            {
                "prompt": "Explain the time and space complexity of merge sort. Then write a Python function `merge_sort(arr)` that sorts a list using merge sort. Walk through your reasoning, then give the code.",
                "grading": "rubric",
                "rubric": "- Correctly states O(n log n) time complexity (0 or 1)\n- Correctly states O(n) space complexity (0 or 1)\n- Provides a working merge sort with base case, recursive split, and merge step visible (0 or 1)",
                "hidden_dynamic": "Merge sort: O(n log n) time, O(n) space. Split in half recursively until single elements; merge sorted halves back together.",
                "pass_threshold": 2,
            },
            {
                "prompt": "Write a Python function `max_profit(prices)` that returns the maximum profit from one buy and one sell (must buy before sell). If no profit is possible, return 0. Explain your O(n) approach, then give the code.",
                "grading": "rubric",
                "rubric": "- Identifies the O(n) one-pass approach: track min_price seen so far (0 or 1)\n- Handles the all-decreasing case (returns 0, not a negative number) (0 or 1)\n- Code correctly computes profit at each step and tracks the maximum (0 or 1)",
                "hidden_dynamic": "Track min_price as you iterate; at each price compute profit = price - min_price; update max_profit. Final answer is max_profit.",
                "pass_threshold": 2,
            },
        ],
        7: [  # EXPERT: algorithm design + explanation
            {
                "prompt": "Write a Python function `merge_intervals(intervals)` that merges all overlapping intervals from a list of [start, end] pairs and returns the merged list sorted by start. Example: [[1,3],[2,6],[8,10]] should return [[1,6],[8,10]]. Explain your approach, then write the code.",
                "grading": "rubric",
                "rubric": "- Correctly identifies that sorting by start time is necessary first (0 or 1)\n- Handles the overlap condition correctly: new_start <= current_end means merge (0 or 1)\n- Code produces correct output for the given example (0 or 1)",
                "hidden_dynamic": "Sort by start. Iterate: if cur_start <= last_merged_end, extend end = max(ends); else append new interval.",
                "pass_threshold": 2,
            },
            {
                "prompt": "Explain what dynamic programming is and when to use it. Then write a Python function `lcs_length(s1, s2)` that returns the length of the longest common subsequence of two strings. State the recurrence relation, then give the code.",
                "grading": "rubric",
                "rubric": "- Correctly defines DP: overlapping subproblems + optimal substructure (0 or 1)\n- States or demonstrates the LCS recurrence: match adds 1 to diagonal; mismatch takes max of left/up (0 or 1)\n- Code uses a 2D table or equivalent and returns correct length (0 or 1)",
                "hidden_dynamic": "DP = breaking into overlapping subproblems. LCS recurrence: if s1[i]==s2[j]: dp[i][j]=dp[i-1][j-1]+1, else dp[i][j]=max(dp[i-1][j], dp[i][j-1]).",
                "pass_threshold": 2,
            },
        ],
    },
    "language_precision": {
        1: [  # EASY
            {
                "prompt": "I claim that the square root of 144 is 13. Am I correct? Answer ONLY 'yes' or 'no'.",
                "answer": "no",
                "grading": "exact_match",
            },
            {
                "prompt": "Say ONLY the word 'done'. Nothing else. No punctuation. No explanation. No quotes.",
                "answer": "done",
                "grading": "exact_match",
            },
        ],
        2: [  # MEDIUM
            {
                "prompt": "How many 'r' letters are in the word 'strawberry'? Spell out each letter, then give ONLY the count.",
                "answer": "3",
                "grading": "pattern",
                "pattern": r"\b3\b",
            },
            {
                "prompt": "Complete this sequence: 2, 6, 12, 20, 30, ? Answer with ONLY the number.",
                "answer": "42",
                "grading": "exact_match",
            },
        ],
        3: [  # HARD
            {
                "prompt": "How many times does the letter 'e' appear in: 'Peter Piper picked a peck of pickled peppers'? Go word by word, count carefully, then state ONLY the total number.",
                "answer": "8",
                "grading": "exact_match",
            },
            {
                "prompt": "Spell the word 'encyclopedia' backwards, letter by letter. Give ONLY the reversed word, nothing else.",
                "answer": "aidepolcycne",
                "grading": "exact_match",
            },
            {
                "prompt": "Write EXACTLY 3 words where: (1) the first word starts with 'z', (2) the second word has exactly 4 letters, (3) the third word ends with 'ly'. Give ONLY the 3 words separated by spaces, nothing else.",
                "answer": None,
                "grading": "custom_validator",
                "validator": "_validate_three_word_constraints",
            },
        ],
    },
    # ── Rubric competencies: L1 seed exercises (hardcoded for reliable grading) ──
    "comprehension": {
        1: [
            {
                "prompt": "The sky appears blue because molecules in the atmosphere scatter shorter wavelengths of light more than longer wavelengths. This phenomenon, called Rayleigh scattering, means blue light (shorter wavelength) gets scattered in all directions while red light (longer wavelength) passes through more directly.\n\nWhat is the main claim of this passage?",
                "grading": "rubric",
                "rubric": "- Identifies Rayleigh scattering or atmospheric scattering as the cause of blue sky (0 or 1)\n- Mentions wavelength or that blue light scatters more (0 or 1)\n- Answer is concise, not a word-for-word restatement of the passage (0 or 1)",
                "hidden_dynamic": "The main claim is that the sky appears blue because atmospheric molecules scatter shorter (blue) wavelengths more than longer ones.",
                "pass_threshold": 2,
            },
            {
                "prompt": "Exercise improves mental health in several ways. It releases endorphins, which are natural mood boosters. Regular physical activity also reduces levels of cortisol, the stress hormone. Studies show that even 20 minutes of walking can significantly reduce anxiety symptoms.\n\nWhat is the main argument being made?",
                "grading": "rubric",
                "rubric": "- Identifies that exercise improves mental health as the main argument (0 or 1)\n- Mentions at least one mechanism (endorphins, cortisol, or anxiety reduction) (0 or 1)\n- Answer is a summary, not just copying the passage (0 or 1)",
                "hidden_dynamic": "The main argument is that exercise improves mental health through multiple biological mechanisms.",
                "pass_threshold": 2,
            },
            {
                "prompt": "Trees are essential for urban environments. They absorb CO2 and produce oxygen, reducing air pollution. Their shade can lower building cooling costs by up to 30%. Additionally, neighborhoods with more trees report lower crime rates and higher property values.\n\nWhat is the central claim of this passage?",
                "grading": "rubric",
                "rubric": "- Identifies that trees are essential/beneficial for urban environments (0 or 1)\n- Mentions at least one specific benefit (air quality, cooling, crime, property values) (0 or 1)\n- Answer demonstrates understanding rather than just restating (0 or 1)",
                "hidden_dynamic": "The central claim is that trees provide multiple essential benefits to urban areas.",
                "pass_threshold": 2,
            },
        ],
    },
    "emotional_intelligence": {
        1: [
            {
                "prompt": "Your friend says 'I'm fine' but has been canceling plans all week and seems withdrawn. What might they be feeling, and how should you respond?",
                "grading": "rubric",
                "rubric": "- Identifies a likely emotion (sadness, overwhelm, depression, loneliness, stress) (0 or 1)\n- Response shows empathy rather than just diagnosing (0 or 1)\n- Suggests a specific, appropriate action (checking in, listening, being present) (0 or 1)",
                "hidden_dynamic": "The friend is likely struggling with something emotional and using 'I'm fine' as a deflection. The appropriate response is gentle concern without pressure.",
                "pass_threshold": 2,
            },
            {
                "prompt": "A coworker snaps at you during a meeting, saying your idea is 'pointless.' Others look uncomfortable. What might the coworker be experiencing, and what's the best way to handle this?",
                "grading": "rubric",
                "rubric": "- Considers that the coworker may be stressed, frustrated, or having a bad day (0 or 1)\n- Does not respond with aggression or retaliation (0 or 1)\n- Suggests a constructive response (staying calm, addressing it privately later, or defusing) (0 or 1)",
                "hidden_dynamic": "The coworker's outburst likely reflects their own stress, not the quality of the idea. Best response: stay calm, don't escalate, address it privately.",
                "pass_threshold": 2,
            },
            {
                "prompt": "A child comes home from school crying and says 'Nobody likes me.' What might be going on, and how should the parent respond?",
                "grading": "rubric",
                "rubric": "- Recognizes the child may have had a specific social rejection or bad interaction (0 or 1)\n- Response validates the child's feelings rather than dismissing them (0 or 1)\n- Suggests listening first, then helping the child process (0 or 1)",
                "hidden_dynamic": "The child is expressing hurt from a social experience. 'Nobody likes me' is an emotional generalization. The parent should validate feelings first, then gently explore what happened.",
                "pass_threshold": 2,
            },
        ],
        2: [  # Two-person conflicts — competing needs, misread signals
            {
                "prompt": "Two close friends had a falling out. Alex told Sam a secret; Sam accidentally mentioned it to someone else. Sam apologized immediately, but Alex stopped responding to messages. It's been two weeks. What might each person be feeling, and what would healthy resolution look like?",
                "grading": "rubric",
                "rubric": "- Identifies Alex's feelings (betrayal, hurt, possibly shame at exposure) (0 or 1)\n- Identifies Sam's feelings (guilt, regret, possibly frustration at the silence) (0 or 1)\n- Describes resolution that honors both perspectives rather than taking sides (0 or 1)",
                "hidden_dynamic": "Both have valid feelings. Alex: betrayed trust, needs time. Sam: genuinely sorry, frustrated by silence. Healthy resolution requires Alex reopening communication and Sam demonstrating changed behavior — not just another apology.",
                "pass_threshold": 2,
            },
            {
                "prompt": "A manager gives a team member critical feedback in front of the whole team. The employee says nothing during the meeting but looks hurt. Later, the manager realizes this was a mistake. How should the manager handle it?",
                "grading": "rubric",
                "rubric": "- Acknowledges the employee's likely feelings (embarrassed, undermined, blindsided) (0 or 1)\n- Manager takes responsibility for the public setting specifically, not just the feedback content (0 or 1)\n- Suggests a private conversation with a genuine apology (0 or 1)",
                "hidden_dynamic": "Public criticism damages trust and dignity regardless of whether the content was accurate. The manager needs to apologize specifically for the setting — not soften the feedback itself. Future commitment: difficult feedback happens privately.",
                "pass_threshold": 2,
            },
            {
                "prompt": "A teenager excitedly shows a parent an art project they've worked on for weeks. The parent responds: 'That's nice, but you should be spending more time on your schoolwork.' What might the teenager feel, and how should the parent have responded?",
                "grading": "rubric",
                "rubric": "- Names the teenager's likely feelings (dismissed, deflated, unappreciated) (0 or 1)\n- Identifies that the parent skipped acknowledgment entirely and went straight to correction (0 or 1)\n- Describes a better response: validate the work first, then separately address the schoolwork concern (0 or 1)",
                "hidden_dynamic": "The teenager needed acknowledgment before advice. Skipping it sends 'this doesn't matter.' A better response: 'This is really impressive — you worked so hard on this. Can we also find time to talk about school?'",
                "pass_threshold": 2,
            },
        ],
        3: [  # Group dynamics, professional context, exhaustion under care demands
            {
                "prompt": "Three coworkers are on a group project. Jordan does most of the work quietly. Riley takes credit in meetings. Morgan notices but says nothing. The project succeeds and Riley gets publicly praised. Describe each person's likely emotional state and the underlying group dynamic.",
                "grading": "rubric",
                "rubric": "- Identifies Jordan's resentment at unacknowledged work (0 or 1)\n- Identifies the enabling dynamic: Riley takes credit, Morgan's silence allows it (0 or 1)\n- Proposes something constructive: Jordan advocating for themselves, Morgan speaking up, or structural change (0 or 1)",
                "hidden_dynamic": "Jordan: silently resentful. Riley: may not register this as harmful. Morgan: uncomfortable but conflict-avoidant. The silence enables the credit-taking. Healthy response: Jordan names their contribution; Morgan breaks the silence; or team adopts visible contribution tracking.",
                "pass_threshold": 2,
            },
            {
                "prompt": "A nurse finishing a 12-hour shift has a patient become distressed and needing comfort. The nurse is emotionally spent but has 30 minutes left. What is the emotionally intelligent approach — for the patient and for the nurse?",
                "grading": "rubric",
                "rubric": "- Acknowledges the nurse's exhaustion as real and valid, not something to override (0 or 1)\n- Identifies what the patient actually needs in that moment (presence, calm, reassurance) (0 or 1)\n- Holds both needs simultaneously rather than collapsing to 'just push through' or 'leave the patient' (0 or 1)",
                "hidden_dynamic": "The patient needs presence. The nurse can give this even while exhausted, but at a cost that must be acknowledged. Emotionally intelligent response: give what's needed now, and recognize recovery is required after. Neither pure self-sacrifice nor pure self-protection is the full answer.",
                "pass_threshold": 2,
            },
        ],
        4: [  # Long-term patterns, cycles, learned behavior
            {
                "prompt": "A couple has been arguing more frequently over the past year. Each argument seems different — dishes, money, scheduling — but the pattern is always the same: one raises an issue, the other gets defensive, it escalates, nothing resolves. What is actually happening, and what would change it?",
                "grading": "rubric",
                "rubric": "- Recognizes the surface topics are not the real issue (0 or 1)\n- Identifies an underlying dynamic: feeling unheard, accumulated resentment, defensive communication cycle (0 or 1)\n- Describes a change that addresses the pattern rather than any individual argument (0 or 1)",
                "hidden_dynamic": "Surface arguments mask an underlying unmet need: to be heard, valued, or respected. Defensiveness is a fear response. Breaking the cycle requires one person to stop escalating and listen to understand — not to agree. 'I feel unheard' is more accurate than any surface complaint.",
                "pass_threshold": 2,
            },
            {
                "prompt": "Someone has a pattern: they help everyone who asks, rarely say no, feel resentful afterward, then feel guilty for the resentment. They've done this for years. Describe the psychological pattern and what growth looks like.",
                "grading": "rubric",
                "rubric": "- Names the core pattern (people-pleasing, difficulty with limits, fear of disappointing others) (0 or 1)\n- Explains the resentment-guilt loop: helping from obligation not choice breeds resentment, then guilt for having it (0 or 1)\n- Describes growth: developing the ability to say no without guilt, helping from genuine desire rather than fear (0 or 1)",
                "hidden_dynamic": "People-pleasing is driven by fear of rejection or conflict. Help given from fear breeds resentment. Help given from genuine desire breeds satisfaction. Growth = learning that 'no' doesn't destroy relationships and that your own needs matter too.",
                "pass_threshold": 2,
            },
        ],
        5: [  # Cultural context, premature meaning-making, grief
            {
                "prompt": "A new employee from a culture where disagreeing with authority is considered disrespectful joins a team where 'healthy debate' is encouraged. In meetings, they stay silent when they disagree. The manager reads this as disengagement. What is actually happening, and how could each party adjust?",
                "grading": "rubric",
                "rubric": "- Recognizes the cultural difference as the root cause, not a personality flaw (0 or 1)\n- Identifies the manager's misinterpretation and its potential impact (0 or 1)\n- Suggests concrete adjustments for both parties that don't require one person to fully abandon their frame (0 or 1)",
                "hidden_dynamic": "The employee reads silence as respect; the manager reads silence as apathy. Neither is wrong in their cultural framework. Fix: manager creates private channels for disagreement first; employee learns lower-risk ways to express dissent ('one alternative might be...'). Both need to name the difference explicitly.",
                "pass_threshold": 2,
            },
            {
                "prompt": "A person is grieving a major loss and a well-meaning friend says 'Everything happens for a reason — this will make you stronger.' The grieving person feels worse after hearing this. Why, and what would have actually helped?",
                "grading": "rubric",
                "rubric": "- Explains why the phrase is invalidating: it asks the person to justify their loss before they've felt it (0 or 1)\n- Identifies what the person actually needed: pain acknowledged without being fixed or explained away (0 or 1)\n- Offers a better response that leads with presence rather than framing (0 or 1)",
                "hidden_dynamic": "Premature meaning-making is a way of escaping someone else's pain. 'This will make you stronger' asks the grieving person to process before they've felt. What helps: 'I'm so sorry. This is hard. I'm here.' Presence, not explanation.",
                "pass_threshold": 2,
            },
        ],
        6: [  # Power imbalance, long-term caregiving, moral injury
            {
                "prompt": "A senior executive publicly dismisses a junior employee's idea in a meeting, saying 'We tried that years ago — it won't work.' The idea had actually never been tried. The junior says nothing. Describe the power dynamics and the full emotional landscape of this scene.",
                "grading": "rubric",
                "rubric": "- Identifies the power imbalance and how it shapes both people's behavior (0 or 1)\n- Recognizes the executive may be acting from habit or status protection, not deliberate cruelty (0 or 1)\n- Describes what the junior's silence costs them and what speaking up would require (0 or 1)",
                "hidden_dynamic": "The executive's authority makes contradiction feel costly for the junior. Silence is self-preservation, not lack of confidence. The executive may genuinely not remember or may be protecting status. Cost: junior loses voice, team loses a good idea, executive avoids being corrected.",
                "pass_threshold": 2,
            },
            {
                "prompt": "Someone has been caring for an elderly parent with dementia for three years. They feel exhausted and sometimes resentful — then deeply guilty for the resentment, because they love their parent. Describe this emotional experience and what support would actually help.",
                "grading": "rubric",
                "rubric": "- Names caregiver burnout or compassion fatigue without judgment (0 or 1)\n- Validates the resentment-guilt cycle as a normal response to impossible circumstances, not a character flaw (0 or 1)\n- Suggests support that addresses the actual need: respite, acknowledgment, permission to have limits (0 or 1)",
                "hidden_dynamic": "Sustained caregiving erases the self. Resentment is not a failure of love — it is a signal of depletion. The guilt compounds the suffering. What actually helps: respite care, being told 'your feelings are normal,' others stepping in, permission to have limits without shame.",
                "pass_threshold": 2,
            },
        ],
        7: [  # Grief, loss, complicated endings
            {
                "prompt": "A man's best friend of 25 years died suddenly six months ago. He goes back to work, functions normally on the surface, and tells everyone he's 'doing fine.' But he sleeps badly, has lost interest in hobbies, and occasionally cries when alone. What's happening, and what does he actually need?",
                "grading": "rubric",
                "rubric": "- Recognizes that performing 'fine' publicly doesn't mean grief is being processed (0 or 1)\n- Identifies at least two symptoms as normal grief responses, without pathologizing them (0 or 1)\n- Describes what he likely needs: space to grieve openly, someone to ask 'how are you really?', time without pressure to recover on schedule (0 or 1)",
                "hidden_dynamic": "This is unprocessed grief. 'Fine' is a social mask. Insomnia, anhedonia, and private crying are normal grief responses. He needs: someone to ask 'how are you really?', permission to not be fine, and time without the expectation of recovery on a schedule.",
                "pass_threshold": 2,
            },
            {
                "prompt": "Two siblings are sorting through a deceased parent's belongings. One wants to keep almost everything; the other wants to donate most of it quickly. Tensions rise. Both are grieving the same loss. What emotional dynamics are driving each behavior, and how could they navigate this?",
                "grading": "rubric",
                "rubric": "- Identifies that both behaviors (keeping/donating) are grief responses, not character flaws (0 or 1)\n- Recognizes the conflict is about how each person processes loss, not actually about objects (0 or 1)\n- Suggests navigation that slows down the process and allows each person's approach some validity (0 or 1)",
                "hidden_dynamic": "Keeping = needing to hold the connection. Donating quickly = needing relief from the pain of presence. Neither is wrong. The conflict is each person needing the other to grieve their way. Navigation: name the dynamic explicitly, slow down, make joint decisions on meaningful items, allow each person autonomy in their portion.",
                "pass_threshold": 2,
            },
        ],
        8: [  # Family systems, leadership through trauma
            {
                "prompt": "Across three generations, the eldest child always becomes the responsible caretaker, suppresses their own needs, and grows up resentful but keeps the role. The pattern has repeated with three generations. What psychological and family systems concepts explain this, and what would break the cycle?",
                "grading": "rubric",
                "rubric": "- Identifies the transgenerational pattern without blaming any single person (0 or 1)\n- Names a relevant concept: parentification, family role assignment, learned behavior, modeling (0 or 1)\n- Describes what structural change could break the cycle — not just individual therapy (0 or 1)",
                "hidden_dynamic": "This is parentification — children assigned adult roles they didn't choose. The pattern propagates because children model what they see. Breaking it requires someone becoming aware and refusing the role assignment, which initially feels like abandonment before it feels like health.",
                "pass_threshold": 2,
            },
            {
                "prompt": "A leader is navigating a team where morale is low after layoffs. Some employees are visibly angry. Some have gone quiet and are likely job-hunting. Some are performing gratitude — acting happy to still have jobs. Describe the full emotional landscape and what leadership is actually required here.",
                "grading": "rubric",
                "rubric": "- Distinguishes between the three response types without judging any of them (0 or 1)\n- Recognizes that performative gratitude is the most dangerous signal — it means people have given up (0 or 1)\n- Describes leadership that acknowledges the real situation rather than projecting optimism over it (0 or 1)",
                "hidden_dynamic": "Angry = still engaged enough to care. Quietly departing = rational self-interest. Performatively grateful = psychological safety is gone. Real leadership here: name the elephant in the room, acknowledge what was lost, don't ask people to pretend. False optimism compounds the damage.",
                "pass_threshold": 2,
            },
        ],
        9: [  # Moral injury, values-based decision-making
            {
                "prompt": "A doctor must choose between two patients for the last available ICU bed. Both will likely die without it. One is 35 with young children; the other is 70 and has lived a full life. The doctor makes a decision. Regardless of which patient they choose, describe the emotional experience of making and living with this.",
                "grading": "rubric",
                "rubric": "- Acknowledges the decision is genuinely impossible — no choice is 'right' (0 or 1)\n- Describes the specific nature of moral injury: being forced to act against one's values by circumstances beyond control (0 or 1)\n- Addresses what the doctor needs after: acknowledgment, not judgment; space to process; institutional support (0 or 1)",
                "hidden_dynamic": "This is moral injury — the psychic wound of being compelled to violate one's values (all life is equally worth saving) by force of circumstances. It requires different care than ordinary grief. The doctor needs: to be told this was not their fault, space to grieve both outcomes, and colleagues who have been there.",
                "pass_threshold": 2,
            },
            {
                "prompt": "A person discovers a close friend has been engaging in behavior they find deeply wrong — not illegal, but contrary to their core values. They must decide whether to confront the friend, accept the difference, or end the friendship. Describe the emotional complexity and what values-based reasoning looks like here.",
                "grading": "rubric",
                "rubric": "- Distinguishes between discomfort/difference and a genuine values conflict (0 or 1)\n- Acknowledges the grief dimension: losing a friendship is a real loss even when the choice is right (0 or 1)\n- Describes a decision process that weighs both the relationship and the values, not just one (0 or 1)",
                "hidden_dynamic": "Not all discomfort is a values violation. First, clarify whether this is genuine conflict or just difference. If genuine: can the relationship hold the difference? Is confrontation honest or punitive? What does each path cost? There is no clean answer — only a clear-eyed choice.",
                "pass_threshold": 2,
            },
        ],
        10: [  # Systemic and structural emotional intelligence
            {
                "prompt": "A CEO must announce to 2,000 employees that the company is pivoting away from a product many believe in, with a restructuring that will affect roughly 10% of the workforce (specific names not yet announced). What does emotionally intelligent leadership communication look like in this moment?",
                "grading": "rubric",
                "rubric": "- Addresses the need for honesty about what is known and what is not yet known (0 or 1)\n- Recognizes employees are processing multiple simultaneous emotions (fear, grief, uncertainty) that need acknowledgment (0 or 1)\n- Avoids false reassurance and performative optimism while still providing direction (0 or 1)",
                "hidden_dynamic": "People need: truth (what they know), acknowledgment (that this is hard), process (when and how decisions come), and genuine care. What destroys trust: certainty that isn't real, corporate language that doesn't sound human, optimism that glosses over real pain. Emotionally intelligent communication doesn't eliminate pain — it makes people feel seen inside it.",
                "pass_threshold": 2,
            },
            {
                "prompt": "A therapist has been working with a client for two years. The client has made significant progress and is moving away for a job opportunity. The last session is approaching. Describe the full emotional terrain of this final session — for both the client and the therapist — and what good closure actually means.",
                "grading": "rubric",
                "rubric": "- Describes the client's complexity: gratitude, loss, fear of regression, pride in growth (0 or 1)\n- Acknowledges the therapist also has feelings in this ending (0 or 1)\n- Articulates what good closure is: not minimizing the ending, naming what was accomplished, allowing it to be a real ending (0 or 1)",
                "hidden_dynamic": "Endings matter as much as beginnings in therapeutic relationships. The client needs: acknowledgment of the work done, permission to grieve the relationship, and confidence they can continue without the therapist. Good closure is naming the ending clearly and letting it be a real ending — not a pause, not a softened goodbye.",
                "pass_threshold": 2,
            },
        ],
        11: [  # EXPERT: integrating emotional intelligence across systems, time, and self
            {
                "prompt": "A person has spent years developing emotional intelligence — learning to name feelings, regulate responses, and understand others. But they notice a pattern: they are far more compassionate toward others than toward themselves. They can hold a friend's pain with great skill, but when they suffer, they are harsh and impatient with themselves. What explains this asymmetry, and what does it take to close it?",
                "grading": "rubric",
                "rubric": "- Names the asymmetry accurately: compassion for others is practiced and socially reinforced; self-compassion is not (0 or 1)\n- Identifies a specific mechanism: internal critic, shame, learned belief that self-care is selfish or weak (0 or 1)\n- Describes what closing the gap actually requires: applying the same frame to oneself as to others, not just intellectually understanding it (0 or 1)",
                "hidden_dynamic": "Compassion for others is socially practiced and rewarded. Self-compassion runs against deep conditioning (being hard on yourself as virtue, self-care as selfishness). The asymmetry is maintained by an internal critic that would never speak to a friend that way. Closing it: Kristin Neff's move — 'what would you say to a friend in this situation? Now say that to yourself.'",
                "pass_threshold": 2,
            },
            {
                "prompt": "A highly emotionally intelligent person is in a conversation where someone is being subtly cruel — not yelling, but undermining, dismissing, and redirecting credit over the course of an hour. The emotionally intelligent person recognizes exactly what is happening in real time. What does emotional intelligence actually permit them to do in this situation, and what does it not guarantee?",
                "grading": "rubric",
                "rubric": "- Describes what EI actually enables: seeing the dynamic clearly, naming it, choosing a response deliberately rather than reactively (0 or 1)\n- Acknowledges what EI does NOT guarantee: it does not prevent the pain, it does not make the other person change, it does not make the right response obvious (0 or 1)\n- Describes one specific response option and what it costs and gains (0 or 1)",
                "hidden_dynamic": "EI is not a superpower that makes cruelty not hurt or makes people change. It is clarity under fire. The gift: you can name what is happening to yourself, choose your response deliberately, and avoid escalating or collapsing. The limit: you cannot force the other person to see themselves clearly. A response option: name the pattern directly ('I notice when I credit X, you redirect to Y — can we talk about that?') — risky, clarifying, possibly relationship-ending.",
                "pass_threshold": 2,
            },
        ],
    },
    "analysis": {
        1: [
            {
                "prompt": "Here is data about ice cream sales and temperature:\n\nMonth | Temp (F) | Sales ($)\nJan   | 32       | 200\nMar   | 50       | 450\nMay   | 70       | 800\nJul   | 88       | 1200\nSep   | 72       | 850\nNov   | 45       | 350\n\nWhat pattern do you see in this data?",
                "grading": "rubric",
                "rubric": "- Identifies the positive correlation between temperature and sales (0 or 1)\n- References specific data points as evidence (0 or 1)\n- Does not claim patterns that don't exist in the data (0 or 1)",
                "hidden_dynamic": "Ice cream sales increase with temperature — a strong positive correlation.",
                "pass_threshold": 2,
            },
            {
                "prompt": "Consider these test scores:\n\nStudent | Study Hours | Score\nA       | 1           | 55\nB       | 2           | 68\nC       | 3           | 72\nD       | 4           | 85\nE       | 5           | 91\n\nWhat pattern exists in this data?",
                "grading": "rubric",
                "rubric": "- Identifies that more study hours correlates with higher scores (0 or 1)\n- References specific data points (0 or 1)\n- Description is accurate and doesn't overstate the pattern (0 or 1)",
                "hidden_dynamic": "There is a clear positive correlation between study hours and test scores.",
                "pass_threshold": 2,
            },
        ],
    },
    "practical_reasoning": {
        1: [
            {
                "prompt": "You need to make a peanut butter and jelly sandwich. You have bread, peanut butter, jelly, and a knife. What are the steps in the correct order?",
                "grading": "rubric",
                "rubric": "- All necessary steps are included (get bread, spread PB, spread jelly, combine) (0 or 1)\n- Steps are in a logical order (0 or 1)\n- Reasoning shows practical awareness (e.g., spread before combining) (0 or 1)",
                "hidden_dynamic": "Steps: take 2 slices of bread, spread PB on one, spread jelly on other, put together.",
                "pass_threshold": 2,
            },
            {
                "prompt": "You have 3 tasks due today: a 2-hour report, a 1-hour email, and a 30-minute phone call. It's 2 PM and you leave at 5 PM. The phone call must happen before 3 PM. How do you schedule your afternoon?",
                "grading": "rubric",
                "rubric": "- Phone call is scheduled before 3 PM (0 or 1)\n- All tasks fit within the available time (3 hours total, 3.5 hours of work — identifies the conflict) (0 or 1)\n- Proposes a reasonable solution (prioritize, delegate, or extend) (0 or 1)",
                "hidden_dynamic": "There's not enough time for everything (3.5 hrs of work, 3 hrs available). Must do phone call first, then prioritize remaining tasks.",
                "pass_threshold": 2,
            },
        ],
    },
    "creative_expression": {
        1: [
            {
                "prompt": "Describe the sound of rain falling on a tin roof without using the words 'rain', 'water', or 'drops'. Write 2-3 sentences.",
                "grading": "rubric",
                "rubric": "- Does not use the forbidden words (rain, water, drops) (0 or 1)\n- Description evokes a vivid auditory image (0 or 1)\n- Writing shows creativity and avoids cliches (0 or 1)",
                "hidden_dynamic": "Good responses use sensory language, metaphor, or onomatopoeia to evoke the sound without naming it directly.",
                "pass_threshold": 2,
            },
            {
                "prompt": "Write a 2-3 sentence description of a sunrise using only the sense of touch. Do not describe what it looks like — only what it feels like.",
                "grading": "rubric",
                "rubric": "- Focuses on tactile sensations, not visual descriptions (0 or 1)\n- Evokes the feeling of a sunrise (warmth, change in air) (0 or 1)\n- Writing is clear and evocative (0 or 1)",
                "hidden_dynamic": "Good responses describe warmth spreading on skin, cool air giving way to warmth, the transition from cold to warm.",
                "pass_threshold": 2,
            },
        ],
    },
}

# ── Exercise Generation Prompts (for LLM-generated exercises) ─────

GENERATION_PROMPTS = {
    "reasoning": {
        4: "Generate a logic puzzle at ADVANCED difficulty. It must require 3+ steps of reasoning, involve a counter-intuitive conclusion, and have a single unambiguous answer.\n\nOutput in this exact format:\nPUZZLE: <the puzzle text, 2-3 sentences>\nANSWER: <the correct answer, brief>\nEXPLANATION: <why this is correct, 1-2 sentences>",
        5: "Generate a challenging logic puzzle that combines probability, set theory, or game theory. It should require careful multi-step reasoning and resist naive intuition.\n\nOutput in this exact format:\nPUZZLE: <the puzzle text, 2-4 sentences>\nANSWER: <the correct answer>\nEXPLANATION: <full reasoning chain>",
    },
    "coding": {
        4: "Generate a medium-hard Python coding challenge. It should require knowledge of data structures (hash maps, stacks, queues, or sorting) and have a clean O(n) or O(n log n) solution.\n\nIMPORTANT: Use ONLY lists, dicts, strings, and integers in test cases. Do NOT use trees, linked lists, or any data structure that requires constructing objects. Each assert must be fully self-contained with inline test data.\n\nOutput in this exact format:\nPROBLEM: <problem description with input/output examples using lists/dicts>\nSOLUTION: <reference implementation in Python>\nTEST_CASES: <3 self-contained assert statements using only literals, e.g., assert func([1,2,3]) == expected>",
        5: "Generate a hard Python coding challenge involving dynamic programming, graph algorithms (represented as adjacency lists), or advanced algorithms. Include edge cases.\n\nIMPORTANT: Use ONLY lists, dicts, strings, and integers in test cases. Each assert must be fully self-contained with inline test data. No external variables.\n\nOutput in this exact format:\nPROBLEM: <problem description with input/output examples>\nSOLUTION: <reference implementation in Python>\nTEST_CASES: <3-5 self-contained assert statements>",
    },
    "language_precision": {
        4: "Generate a language precision exercise that tests WORD-LEVEL skills (NOT character or letter counting — LLMs cannot count characters). Good exercise types:\n- Follow multiple simultaneous constraints (e.g., 'Write a sentence where every word starts with a different letter of the alphabet in order')\n- Precise reformatting (e.g., 'Rewrite this sentence in exactly 5 words without changing the meaning')\n- Word-level transformations (e.g., 'Replace every adjective in this sentence with its antonym')\n- Structural constraints (e.g., 'Write 3 sentences where the last word of each sentence is the first word of the next')\n\nDo NOT generate exercises that require counting individual letters or characters.\n\nOutput in this exact format:\nTASK: <the task text>\nANSWER: <the exact correct answer>\nVALIDATION: <how to verify correctness>",
        5: "Generate a very hard language precision exercise with multiple simultaneous constraints. Example: 'Write a 4-line poem where each line has exactly 5 words and no word appears twice.'\n\nOutput in this exact format:\nTASK: <the task text with multiple constraints>\nANSWER: <an example correct answer>\nVALIDATION: <how to verify each constraint>",
    },
    "comprehension": {
        1: "Generate a reading comprehension exercise at BASIC difficulty. Provide a 3-4 sentence paragraph about a real topic, then ask a question that requires identifying the main argument.\n\nOutput in this exact format:\nPASSAGE: <3-4 sentences>\nQUESTION: <question about the main argument>\nRUBRIC:\n- Identifies the main claim (0 or 1)\n- Distinguishes claim from supporting detail (0 or 1)\n- Answer is concise, not a restatement (0 or 1)\nPASS_THRESHOLD: 2",
        2: "Generate a comprehension exercise at INTERMEDIATE difficulty. Provide a paragraph with an implicit assumption, then ask the reader to identify what the author assumes but doesn't state.\n\nOutput in this exact format:\nPASSAGE: <4-5 sentences with a hidden assumption>\nQUESTION: <ask what the author assumes>\nHIDDEN_DYNAMIC: <the unstated assumption>\nRUBRIC:\n- Identifies an unstated assumption (0 or 1)\n- The identified assumption is actually implicit, not explicit (0 or 1)\n- Explains why it's an assumption, not a fact (0 or 1)\n- Answer shows analytical thinking (0 or 1)\nPASS_THRESHOLD: 3",
        3: "Generate a comprehension exercise at ADVANCED difficulty. Provide two short paragraphs with opposing viewpoints, then ask the reader to evaluate which argument is stronger and why.\n\nOutput in this exact format:\nPASSAGE_A: <3-4 sentences, Position A>\nPASSAGE_B: <3-4 sentences, Position B>\nQUESTION: Which argument is stronger and why?\nHIDDEN_DYNAMIC: <which is actually stronger and the key differentiator>\nRUBRIC:\n- Identifies strengths of both arguments (0 or 1)\n- Identifies weaknesses of both arguments (0 or 1)\n- Makes a reasoned judgment, not just a preference (0 or 1)\n- Cites specific evidence from the passages (0 or 1)\n- Conclusion follows logically from analysis (0 or 1)\nPASS_THRESHOLD: 3",
        4: "Generate a hard comprehension exercise. Provide a paragraph that contains a logical fallacy embedded in persuasive language. Ask the reader to identify the fallacy.\n\nOutput in this exact format:\nPASSAGE: <4-6 sentences with an embedded logical fallacy>\nQUESTION: Identify the logical error in this argument.\nHIDDEN_DYNAMIC: <the specific fallacy name and where it occurs>\nRUBRIC:\n- Identifies the general area of the error (0 or 1)\n- Names or describes the specific fallacy correctly (0 or 1)\n- Points to the exact claim or transition where the error occurs (0 or 1)\n- Explains why this reasoning is invalid (0 or 1)\n- Does not make false claims about other parts being fallacious (0 or 1)\nPASS_THRESHOLD: 3",
        5: "Generate a very hard comprehension exercise with a complex multi-layered argument. Include rhetorical misdirection.\n\nIMPORTANT: Keep the passage SHORT (4-5 sentences max, under 150 words). The passage MUST be complete — do not cut off mid-sentence. Quality of argument complexity matters more than length.\n\nOutput in this exact format:\nPASSAGE: <4-5 complete sentences with nuanced argument and rhetorical misdirection — MUST end with a period, not mid-word>\nQUESTION: Analyze this argument. What is the author's actual conclusion, and does the evidence support it?\nHIDDEN_DYNAMIC: <the actual conclusion vs apparent conclusion, and evidence gaps>\nRUBRIC:\n- Distinguishes stated conclusion from implied conclusion (0 or 1)\n- Identifies rhetorical misdirection technique (0 or 1)\n- Evaluates evidence-conclusion link accurately (0 or 1)\n- Identifies missing evidence or unstated premises (0 or 1)\n- Analysis is precise, not vague (0 or 1)\nPASS_THRESHOLD: 4",
    },
    "analysis": {
        1: "Generate a simple pattern recognition exercise. Present a dataset (5-6 data points in a table) and ask what trend or pattern is visible.\n\nOutput in this exact format:\nDATA: <simple table of 5-6 rows>\nQUESTION: What pattern do you see in this data?\nHIDDEN_DYNAMIC: <the actual pattern>\nRUBRIC:\n- Identifies the correct pattern (0 or 1)\n- Provides specific evidence from the data (0 or 1)\n- Doesn't overfit or hallucinate non-existent patterns (0 or 1)\nPASS_THRESHOLD: 2",
        2: "Generate an analysis exercise requiring comparison. Present two sets of information and ask for similarities and differences.\n\nOutput in this exact format:\nSET_A: <3-4 attributes of thing A>\nSET_B: <3-4 attributes of thing B>\nQUESTION: Compare these two. What are the key similarities and differences?\nHIDDEN_DYNAMIC: <the important comparison points>\nRUBRIC:\n- Identifies at least 2 real similarities (0 or 1)\n- Identifies at least 2 real differences (0 or 1)\n- Prioritizes meaningful comparisons over trivial ones (0 or 1)\n- Analysis is structured, not stream-of-consciousness (0 or 1)\nPASS_THRESHOLD: 3",
        3: "Generate an analysis exercise requiring anomaly detection. Present 6-8 data points where one is an outlier. Ask which one doesn't belong and why.\n\nOutput in this exact format:\nDATA: <6-8 items, one anomalous>\nQUESTION: Which item doesn't fit the pattern, and why?\nHIDDEN_DYNAMIC: <which item and the rule it violates>\nRUBRIC:\n- Correctly identifies the anomalous item (0 or 1)\n- Articulates the underlying rule/pattern (0 or 1)\n- Explains WHY the outlier breaks the rule (0 or 1)\n- Doesn't misidentify normal items as anomalous (0 or 1)\nPASS_THRESHOLD: 3",
        4: "Generate a hard analysis exercise requiring causal reasoning. Present a scenario with correlation and ask whether causation is established.\n\nOutput in this exact format:\nSCENARIO: <description of a correlation with confounders>\nQUESTION: Is there evidence of causation here? What alternative explanations exist?\nHIDDEN_DYNAMIC: <the confounders and why causation isn't established>\nRUBRIC:\n- Correctly identifies correlation vs causation distinction (0 or 1)\n- Names at least one plausible confounder (0 or 1)\n- Proposes what evidence WOULD establish causation (0 or 1)\n- Doesn't overstate or understate the evidence (0 or 1)\n- Reasoning is precise and specific (0 or 1)\nPASS_THRESHOLD: 3",
        5: "Generate a very hard analysis exercise with multi-dimensional data requiring synthesis of multiple patterns.\n\nOutput in this exact format:\nDATA: <multi-dimensional dataset, 8-10 entries with 3+ attributes each>\nQUESTION: What are the 2-3 most important patterns in this data? What actionable insight follows?\nHIDDEN_DYNAMIC: <the key patterns and why they matter>\nRUBRIC:\n- Identifies at least 2 genuine patterns (0 or 1)\n- Patterns are non-obvious (not just restating data) (0 or 1)\n- Draws a reasonable actionable conclusion (0 or 1)\n- Analysis accounts for data limitations (0 or 1)\n- Synthesis connects patterns to each other (0 or 1)\nPASS_THRESHOLD: 3",
    },
    "emotional_intelligence": {
        1: "Generate an emotional intelligence exercise at BASIC level. Describe a simple interpersonal scenario and ask what the person might be feeling.\n\nOutput in this exact format:\nSCENARIO: <2-3 sentences describing a situation with clear emotional content>\nQUESTION: What is this person likely feeling, and how should you respond?\nHIDDEN_DYNAMIC: <the primary emotion and appropriate response>\nRUBRIC:\n- Identifies the primary emotion (0 or 1)\n- Response shows empathy, not just analysis (0 or 1)\n- Response is appropriate to the situation (0 or 1)\nPASS_THRESHOLD: 2",
        2: "Generate an EI exercise at INTERMEDIATE level. Describe a situation where the surface emotion masks a deeper one.\n\nOutput in this exact format:\nSCENARIO: <3-4 sentences where someone expresses one emotion but is really feeling another>\nQUESTION: What is this person really feeling underneath what they're expressing? How should you respond?\nHIDDEN_DYNAMIC: <the surface emotion vs the real emotion>\nRUBRIC:\n- Identifies the surface emotion (0 or 1)\n- Identifies the underlying emotion (0 or 1)\n- Response addresses the underlying need, not just the surface (0 or 1)\n- Response respects autonomy (doesn't lecture or fix) (0 or 1)\nPASS_THRESHOLD: 3",
        3: "Generate an EI exercise at ADVANCED level. Describe a situation with competing emotional needs between two people.\n\nOutput in this exact format:\nSCENARIO: <4-5 sentences with emotional tension between two people>\nQUESTION: What is each person feeling, and how could you help them navigate this?\nHIDDEN_DYNAMIC: <each person's needs and the tension point>\nRUBRIC:\n- Identifies Person A's emotional state accurately (0 or 1)\n- Identifies Person B's emotional state accurately (0 or 1)\n- Recognizes the tension without taking sides (0 or 1)\n- Suggests a response that validates both perspectives (0 or 1)\n- Response is practical, not just theoretical (0 or 1)\nPASS_THRESHOLD: 3",
        4: "Generate a hard EI exercise involving someone with a mental health condition (bipolar, anxiety, depression). The correct response requires understanding the condition without pathologizing the person.\n\nOutput in this exact format:\nSCENARIO: <4-5 sentences where someone's behavior is influenced by their condition>\nQUESTION: What should you be aware of in this situation, and how should you respond?\nHIDDEN_DYNAMIC: <the condition's influence and the dignifying response>\nRUBRIC:\n- Recognizes the condition's influence without reducing the person to it (0 or 1)\n- Responds to the person, not the diagnosis (0 or 1)\n- Doesn't play therapist or give medical advice (0 or 1)\n- Shows genuine warmth without condescension (0 or 1)\n- Respects the person's agency and judgment (0 or 1)\nPASS_THRESHOLD: 3",
        5: "Generate a very hard EI exercise requiring navigation of a morally complex emotional situation with no clear right answer.\n\nOutput in this exact format:\nSCENARIO: <5-6 sentences with genuine moral complexity and emotional stakes>\nQUESTION: How do you navigate this? What matters most here?\nHIDDEN_DYNAMIC: <the competing values and why there's no clean answer>\nRUBRIC:\n- Acknowledges the genuine complexity (doesn't oversimplify) (0 or 1)\n- Identifies the competing values at stake (0 or 1)\n- Proposes a thoughtful path forward (not just 'it depends') (0 or 1)\n- Shows emotional maturity in the reasoning (0 or 1)\n- Demonstrates wisdom — knows what they don't know (0 or 1)\nPASS_THRESHOLD: 4",
    },
    "practical_reasoning": {
        1: "Generate a basic practical reasoning exercise. Present a simple planning problem with 3-4 steps that must be ordered correctly.\n\nOutput in this exact format:\nPROBLEM: <a task that requires 3-4 steps in a specific order>\nQUESTION: What is the correct order of steps?\nHIDDEN_DYNAMIC: <the correct order and why>\nRUBRIC:\n- All steps are included (0 or 1)\n- Steps are in the correct order (0 or 1)\n- Reasoning for the order is sound (0 or 1)\nPASS_THRESHOLD: 2",
        2: "Generate a practical reasoning exercise involving resource allocation. Present limited resources and competing needs.\n\nOutput in this exact format:\nPROBLEM: <a resource allocation scenario with 3 options and limited budget/time>\nQUESTION: How should the resources be allocated, and why?\nHIDDEN_DYNAMIC: <the optimal allocation and the tradeoff>\nRUBRIC:\n- Identifies the key constraint (0 or 1)\n- Allocation is feasible (doesn't exceed resources) (0 or 1)\n- Justifies choices with reasoning, not just preference (0 or 1)\n- Acknowledges what is sacrificed (0 or 1)\nPASS_THRESHOLD: 3",
        3: "Generate a practical reasoning exercise involving tradeoffs with uncertainty. Multiple valid approaches exist.\n\nOutput in this exact format:\nPROBLEM: <a decision under uncertainty with 2-3 viable options>\nQUESTION: Which approach would you recommend, and what are the risks?\nHIDDEN_DYNAMIC: <the key tradeoffs and risk factors>\nRUBRIC:\n- Identifies at least 2 viable approaches (0 or 1)\n- Analyzes tradeoffs for each (0 or 1)\n- Makes a clear recommendation with reasoning (0 or 1)\n- Identifies specific risks and mitigation strategies (0 or 1)\n- Reasoning accounts for uncertainty (0 or 1)\nPASS_THRESHOLD: 3",
        4: "Generate a hard practical reasoning exercise requiring multi-step planning with dependencies and constraints.\n\nOutput in this exact format:\nPROBLEM: <a complex planning scenario with dependencies, deadlines, and constraints>\nQUESTION: Create a plan that satisfies all constraints.\nHIDDEN_DYNAMIC: <the critical path and bottlenecks>\nRUBRIC:\n- Plan respects all stated constraints (0 or 1)\n- Dependencies are correctly ordered (0 or 1)\n- Critical path is identified or implied (0 or 1)\n- Plan handles the most likely failure mode (0 or 1)\n- Plan is specific and actionable, not vague (0 or 1)\nPASS_THRESHOLD: 3",
        5: "Generate a very hard practical reasoning exercise with a real-world scenario involving ethical, financial, and time constraints simultaneously.\n\nOutput in this exact format:\nPROBLEM: <complex real-world scenario with ethical, financial, and time dimensions>\nQUESTION: What is the best course of action? Justify your reasoning.\nHIDDEN_DYNAMIC: <the key tensions and optimal approach>\nRUBRIC:\n- Addresses all three dimensions (ethical, financial, time) (0 or 1)\n- Doesn't sacrifice ethics for efficiency (0 or 1)\n- Proposes a specific, implementable plan (0 or 1)\n- Identifies second-order consequences (0 or 1)\n- Reasoning is transparent and defensible (0 or 1)\nPASS_THRESHOLD: 4",
    },
    "creative_expression": {
        1: "Generate a basic creative writing exercise. Ask for a short description using a specific sensory detail.\n\nOutput in this exact format:\nPROMPT: <ask for a 2-3 sentence description of a scene using a specific sense (smell, sound, touch)>\nHIDDEN_DYNAMIC: <what makes a good response>\nRUBRIC:\n- Uses the specified sense (0 or 1)\n- Description evokes a vivid image (0 or 1)\n- Writing is clear and free of cliches (0 or 1)\nPASS_THRESHOLD: 2",
        2: "Generate an intermediate creative writing exercise involving analogy or metaphor.\n\nOutput in this exact format:\nPROMPT: <ask to explain a complex concept using an original analogy>\nHIDDEN_DYNAMIC: <what makes a good analogy>\nRUBRIC:\n- Analogy is original (not a common cliche) (0 or 1)\n- Analogy accurately maps to the concept (0 or 1)\n- Analogy illuminates rather than obscures (0 or 1)\n- Writing has voice and personality (0 or 1)\nPASS_THRESHOLD: 3",
        3: "Generate an advanced creative exercise. Ask for a micro-story (50-100 words) with a specific constraint (e.g., must contain a twist, must use only dialogue, must have no adjectives).\n\nOutput in this exact format:\nPROMPT: <ask for a micro-story with a specific creative constraint>\nHIDDEN_DYNAMIC: <what makes this exercise challenging and what a good response looks like>\nRUBRIC:\n- Story satisfies the constraint (0 or 1)\n- Story has a clear beginning, middle, end (0 or 1)\n- Writing shows craft (word choice, pacing) (0 or 1)\n- Story creates emotional resonance (0 or 1)\n- Story is original, not formulaic (0 or 1)\nPASS_THRESHOLD: 3",
        4: "Generate a hard creative exercise. Ask for writing that captures a specific emotional state without naming the emotion.\n\nOutput in this exact format:\nPROMPT: <ask to write a paragraph that evokes a specific complex emotion (e.g., nostalgia, bittersweet pride, quiet resignation) without using the word for it>\nHIDDEN_DYNAMIC: <the target emotion and how it's conveyed through imagery>\nRUBRIC:\n- The target emotion is clearly evoked (0 or 1)\n- The emotion word itself is not used (0 or 1)\n- Imagery is specific, not generic (0 or 1)\n- Prose has rhythm and intentional structure (0 or 1)\n- Writing would move a human reader (0 or 1)\nPASS_THRESHOLD: 3",
        5: "Generate a very hard creative exercise requiring voice, structure, and emotional depth simultaneously.\n\nIMPORTANT: Do NOT use palindrome as a structural constraint — it is structurally impossible for language models and leads to guaranteed failure. Good structural constraints include: acrostic (first letters spell a word), specific poetic form (sonnet, villanelle, haiku sequence), epistolary format (letters/diary entries), constrained vocabulary (no adjectives, only one-syllable words), nested narratives, reverse chronology, or a specific number of sentences/paragraphs with word count targets.\n\nOutput in this exact format:\nPROMPT: <ask for a piece that combines a specific voice constraint, an ACHIEVABLE structural constraint (NOT palindrome), and an emotional target>\nHIDDEN_DYNAMIC: <what mastery looks like for this exercise>\nRUBRIC:\n- Voice constraint is maintained throughout (0 or 1)\n- Structural constraint is satisfied (0 or 1)\n- Emotional target is achieved (0 or 1)\n- Writing demonstrates genuine craft (0 or 1)\n- Piece would be publishable in quality (0 or 1)\nPASS_THRESHOLD: 4",
    },
}

# ── Extended Level Prompts (L6-L10) ──────────────────────────────
# Advanced through Visionary levels. All LLM-generated, no seed bank.
# L6-L7: Same format as L4-L5 per competency type
# L8-L10: Objective types (except coding) switch to rubric format

GENERATION_PROMPTS["reasoning"].update({
    6: "Generate an advanced logic puzzle requiring multi-constraint optimization or game theory reasoning. The puzzle should involve 4+ interacting constraints where the solver must track multiple variables simultaneously. It must have exactly one correct answer.\n\nOutput in this exact format:\nPUZZLE: <the puzzle, 3-5 sentences with multiple interacting constraints>\nANSWER: <the correct answer>\nEXPLANATION: <full reasoning chain showing constraint interaction>",
    7: "Generate a challenging problem requiring Fermi estimation or formal proof construction. The solver must break an abstract question into concrete sub-problems and combine results logically.\n\nOutput in this exact format:\nPUZZLE: <the problem, requiring decomposition into sub-problems>\nANSWER: <the correct answer or valid range>\nEXPLANATION: <the complete decomposition and reasoning chain>",
    8: "Generate an expert-level reasoning exercise involving paradox analysis, mathematical induction, or set theory. The solver must construct or deconstruct a rigorous argument.\n\nOutput in this exact format:\nPROMPT: <the problem requiring formal reasoning, 3-5 sentences>\nHIDDEN_DYNAMIC: <the key insight and rigorous solution>\nRUBRIC:\n- Identifies the core logical structure (0 or 1)\n- Reasoning is rigorous, not hand-wavy (0 or 1)\n- Handles edge cases or counterexamples (0 or 1)\n- Conclusion follows necessarily from premises (0 or 1)\n- Demonstrates depth of mathematical/logical understanding (0 or 1)\n- Explanation would be clear to someone unfamiliar (0 or 1)\nPASS_THRESHOLD: 5",
    9: "Generate a master-level reasoning exercise where the solver must teach a complex concept through analogy, explain why a common intuition is mathematically wrong, or create a novel proof approach.\n\nOutput in this exact format:\nPROMPT: <the task requiring meta-reasoning and teaching ability, 3-5 sentences>\nHIDDEN_DYNAMIC: <what mastery looks like>\nRUBRIC:\n- Core concept is correctly understood (0 or 1)\n- Explanation/analogy is genuinely illuminating (0 or 1)\n- Handles the most likely misconception (0 or 1)\n- Reasoning is original, not textbook-regurgitated (0 or 1)\n- A student would understand after reading this (0 or 1)\n- Shows awareness of what the concept doesn't explain (0 or 1)\nPASS_THRESHOLD: 5",
    10: "Generate a visionary-level reasoning exercise where the solver must propose a novel thought experiment, find a flaw in a published philosophical argument, or construct an original logical framework for an unsolved problem.\n\nOutput in this exact format:\nPROMPT: <the task requiring original intellectual contribution, 3-5 sentences>\nHIDDEN_DYNAMIC: <what an exceptional response looks like>\nRUBRIC:\n- The contribution is genuinely novel (not rephrasing known ideas) (0 or 1)\n- Reasoning is internally consistent (0 or 1)\n- The approach illuminates something previously unclear (0 or 1)\n- Shows awareness of the problem's difficulty and prior attempts (0 or 1)\n- The contribution could spark further investigation (0 or 1)\n- Demonstrates intellectual courage (takes a position) (0 or 1)\nPASS_THRESHOLD: 5",
})

GENERATION_PROMPTS["coding"].update({
    6: "Generate a hard Python coding challenge requiring system-level thinking: debug complex code with a subtle bug (off-by-one, incorrect boundary handling), or optimize an algorithm with specific time/space constraints.\n\nIMPORTANT: Use ONLY lists, dicts, strings, and integers in test cases. Each assert must be fully self-contained.\n\nOutput in this exact format:\nPROBLEM: <problem with subtle complexity, include buggy code if debugging task>\nSOLUTION: <reference implementation>\nTEST_CASES: <4-5 self-contained assert statements including edge cases>",
    7: "Generate a very hard Python coding challenge involving adversarial input handling, complex state machines, or non-trivial refactoring. The solution should demonstrate software engineering principles beyond just algorithms.\n\nIMPORTANT: Use ONLY lists, dicts, strings, and integers in test cases. Each assert must be fully self-contained.\n\nOutput in this exact format:\nPROBLEM: <problem requiring software engineering judgment>\nSOLUTION: <reference implementation>\nTEST_CASES: <4-5 self-contained assert statements>",
    8: "Generate an expert-level Python coding challenge: identify a security vulnerability in code, analyze and fix a performance bottleneck, or design a clean API for a complex domain. Requires both correctness and engineering judgment.\n\nIMPORTANT: Use ONLY lists, dicts, strings, and integers in test cases. Each assert must be fully self-contained.\n\nOutput in this exact format:\nPROBLEM: <expert-level problem requiring engineering judgment>\nSOLUTION: <reference implementation with clear design choices>\nTEST_CASES: <5 self-contained assert statements including adversarial cases>",
    9: "Generate a master-level Python coding challenge: design a domain-specific language (DSL) implemented as Python functions, write a comprehensive test suite that catches subtle edge cases in provided code, or implement an architectural pattern elegantly.\n\nIMPORTANT: Use ONLY lists, dicts, strings, and integers in test cases. Each assert must be fully self-contained.\n\nOutput in this exact format:\nPROBLEM: <master-level problem requiring design insight>\nSOLUTION: <reference implementation demonstrating mastery>\nTEST_CASES: <5 self-contained assert statements>",
    10: "Generate a visionary-level Python coding challenge: design a novel algorithm for a real-world problem, create a self-documenting code pattern that teaches the reader, or implement a data structure optimized for an unusual access pattern.\n\nIMPORTANT: Use ONLY lists, dicts, strings, and integers in test cases. Each assert must be fully self-contained.\n\nOutput in this exact format:\nPROBLEM: <visionary-level problem requiring creative engineering>\nSOLUTION: <elegant reference implementation>\nTEST_CASES: <5 self-contained assert statements>",
})

GENERATION_PROMPTS["language_precision"].update({
    6: "Generate an advanced language precision exercise with nested constraints. Example: 'Rewrite this paragraph in exactly 3 sentences, where each sentence has exactly 8 words, and the first word of each sentence starts with consecutive letters.'\n\nDo NOT generate exercises that require counting individual letters or characters.\n\nOutput in this exact format:\nTASK: <task with multiple nested word-level constraints>\nANSWER: <an example correct answer>\nVALIDATION: <how to verify each constraint>",
    7: "Generate a synthesis-level language precision exercise requiring bidirectional transformation. Example: 'Translate this technical description into language a 10-year-old would understand, preserving ALL key facts. Then translate back to technical language.'\n\nDo NOT generate exercises that require counting individual letters or characters.\n\nOutput in this exact format:\nTASK: <task requiring precise bidirectional language transformation>\nANSWER: <an example correct answer>\nVALIDATION: <how to verify meaning preservation and constraint satisfaction>",
    8: "Generate an expert-level language precision exercise involving legal/contract-quality precision or productive disambiguation. Example: 'Write a one-paragraph contract clause that unambiguously covers these 4 scenarios...'\n\nDo NOT generate exercises that require counting individual letters or characters.\n\nOutput in this exact format:\nPROMPT: <task requiring professional-grade precision>\nHIDDEN_DYNAMIC: <what makes this genuinely difficult>\nRUBRIC:\n- All stated constraints are satisfied (0 or 1)\n- Language is unambiguous (no valid alternative interpretation) (0 or 1)\n- Precision does not sacrifice readability (0 or 1)\n- Handles the trickiest edge case correctly (0 or 1)\n- Demonstrates mastery of register/tone (0 or 1)\n- Would withstand adversarial reading (0 or 1)\nPASS_THRESHOLD: 5",
    9: "Generate a master-level language precision exercise: write instructions so clear that an adversarial reader cannot misinterpret them, or construct productive ambiguity (as in poetry) where multiple valid readings all enhance meaning.\n\nDo NOT generate exercises that require counting individual letters or characters.\n\nOutput in this exact format:\nPROMPT: <task requiring mastery of intentional precision or ambiguity>\nHIDDEN_DYNAMIC: <what mastery looks like>\nRUBRIC:\n- Core constraint is fully satisfied (0 or 1)\n- Demonstrates control over meaning (intentional, not accidental) (0 or 1)\n- Multiple valid readings are all enriching (if ambiguity task) or no unintended readings exist (if precision task) (0 or 1)\n- Language is elegant, not just correct (0 or 1)\n- Shows meta-awareness of how language creates meaning (0 or 1)\n- Result would impress a professional editor or lawyer (0 or 1)\nPASS_THRESHOLD: 5",
    10: "Generate a visionary-level language precision exercise: craft a statement simultaneously true in multiple incompatible interpretive frameworks, or create formal constraints that generate emergent beauty.\n\nDo NOT generate exercises that require counting individual letters or characters.\n\nOutput in this exact format:\nPROMPT: <task requiring transcendent language mastery>\nHIDDEN_DYNAMIC: <what an exceptional response looks like>\nRUBRIC:\n- The constraint is satisfied at the literal level (0 or 1)\n- The result transcends the constraint (structure serves content) (0 or 1)\n- Demonstrates genuine originality (0 or 1)\n- Multiple readings are all valid and enriching (0 or 1)\n- Would surprise a sophisticated reader (0 or 1)\n- Shows understanding of language as a creative medium (0 or 1)\nPASS_THRESHOLD: 5",
})

GENERATION_PROMPTS["comprehension"].update({
    6: "Generate an advanced comprehension exercise. Provide two contradictory expert opinions (4-5 sentences each) and ask the reader to identify the logical weakness in each argument and determine which is fundamentally stronger.\n\nIMPORTANT: Keep passages SHORT (under 150 words total). Complete sentences only.\n\nOutput in this exact format:\nPASSAGE_A: <expert opinion A, 4-5 complete sentences>\nPASSAGE_B: <contradictory expert opinion B, 4-5 complete sentences>\nQUESTION: Identify the logical weakness in each argument. Which is fundamentally stronger?\nHIDDEN_DYNAMIC: <fallacy in each, and which argument is structurally sounder>\nRUBRIC:\n- Identifies logical weakness in Passage A (0 or 1)\n- Identifies logical weakness in Passage B (0 or 1)\n- Correctly judges which argument is structurally stronger (0 or 1)\n- Explains WHY one is stronger, not just which (0 or 1)\n- Analysis is precise, citing specific claims (0 or 1)\nPASS_THRESHOLD: 4",
    7: "Generate a synthesis-level comprehension exercise. Provide 3 short texts (2-3 sentences each) with different perspectives on the same issue. Ask the reader to synthesize them and identify what all three sources deliberately omit.\n\nIMPORTANT: Under 200 words total. Complete sentences only.\n\nOutput in this exact format:\nPASSAGE_A: <perspective A, 2-3 sentences>\nPASSAGE_B: <perspective B, 2-3 sentences>\nPASSAGE_C: <perspective C, 2-3 sentences>\nQUESTION: Synthesize these perspectives into a coherent argument. What do all three fail to address?\nHIDDEN_DYNAMIC: <the synthesis and the shared blind spot>\nRUBRIC:\n- Accurately represents all three perspectives (0 or 1)\n- Synthesis is genuinely coherent, not just concatenation (0 or 1)\n- Identifies a real shared omission or blind spot (0 or 1)\n- Explains why the omission matters (0 or 1)\n- Synthesis adds insight beyond what any single source provides (0 or 1)\nPASS_THRESHOLD: 4",
    8: "Generate an expert-level comprehension exercise. Provide a short academic-style argument (5-6 sentences) and ask the reader to critique its methodology, identify unstated assumptions, and evaluate whether conclusions are warranted.\n\nIMPORTANT: Keep passage under 150 words. Complete sentences only.\n\nOutput in this exact format:\nPASSAGE: <academic-style argument, 5-6 complete sentences>\nQUESTION: Critique this argument: evaluate the methodology, identify unstated assumptions, and assess whether evidence supports the conclusion.\nHIDDEN_DYNAMIC: <methodological flaws, unstated assumptions, evidence gaps>\nRUBRIC:\n- Identifies a methodological flaw (0 or 1)\n- Names an unstated assumption (0 or 1)\n- Evaluates evidence-conclusion link accurately (0 or 1)\n- Critique is specific, not generic (0 or 1)\n- Suggests what additional evidence would strengthen the argument (0 or 1)\n- Distinguishes between what the argument shows and what it claims (0 or 1)\nPASS_THRESHOLD: 5",
    9: "Generate a master-level comprehension exercise. Provide a persuasive argument (5-6 sentences) and ask the reader to analyze what makes it persuasive AND whether it is logically sound, distinguishing rhetorical effectiveness from logical validity.\n\nIMPORTANT: Keep passage under 150 words. Complete sentences only.\n\nOutput in this exact format:\nPASSAGE: <persuasive argument that is rhetorically strong but logically questionable, 5-6 sentences>\nQUESTION: Analyze on two dimensions: (1) What makes it persuasive? (2) Is it logically sound? Distinguish rhetorical technique from logical validity.\nHIDDEN_DYNAMIC: <rhetorical techniques used, logical status, gap between persuasion and logic>\nRUBRIC:\n- Identifies specific rhetorical techniques (0 or 1)\n- Evaluates logical structure independently of persuasiveness (0 or 1)\n- Correctly distinguishes rhetorical strength from logical validity (0 or 1)\n- Shows why persuasive arguments can be logically weak (0 or 1)\n- Analysis demonstrates meta-awareness of how arguments work (0 or 1)\n- Could teach someone else to recognize this distinction (0 or 1)\nPASS_THRESHOLD: 5",
    10: "Generate a visionary-level comprehension exercise. Provide a text (5-6 sentences) and ask the reader to identify the unstated worldview behind it — assumptions so fundamental the author does not recognize them — and what this worldview cannot see.\n\nIMPORTANT: Keep passage under 150 words. Complete sentences only.\n\nOutput in this exact format:\nPASSAGE: <text with a deep but unstated worldview/paradigm, 5-6 sentences>\nQUESTION: What is the unstated worldview here? What assumptions are so fundamental the author does not recognize them? What is this worldview incapable of seeing?\nHIDDEN_DYNAMIC: <the deep paradigm, its invisible assumptions, its blind spots>\nRUBRIC:\n- Identifies the surface-level argument correctly (0 or 1)\n- Names the deeper worldview or paradigm (0 or 1)\n- Identifies assumptions the author takes as given (0 or 1)\n- Articulates what this worldview cannot see (0 or 1)\n- Proposes an alternative framework that reveals the blind spot (0 or 1)\n- Analysis is profound, not just contrarian (0 or 1)\nPASS_THRESHOLD: 5",
})

GENERATION_PROMPTS["analysis"].update({
    6: "Generate an advanced analysis exercise with multi-variable data. Present a dataset with 4+ attributes showing a misleading correlation, and ask the reader to distinguish correlation from causation and identify confounders.\n\nOutput in this exact format:\nDATA: <dataset with 8-10 entries and 4+ attributes, showing a misleading correlation>\nQUESTION: Is the apparent relationship causal? What confounders exist? What evidence would you need?\nHIDDEN_DYNAMIC: <the confounders and why causation is not established>\nRUBRIC:\n- Correctly identifies the apparent correlation (0 or 1)\n- Names at least 2 plausible confounders (0 or 1)\n- Explains why correlation does not imply causation here (0 or 1)\n- Proposes specific evidence that would establish causation (0 or 1)\n- Analysis cites specific data points (0 or 1)\nPASS_THRESHOLD: 4",
    7: "Generate a synthesis-level analysis exercise. Present a scenario and ask the reader to design an experiment to test a hypothesis, identifying control variables, confounds, and expected outcomes.\n\nOutput in this exact format:\nSCENARIO: <a hypothesis about a real-world relationship that could be tested>\nQUESTION: Design an experiment. Specify: variables, controls, sample, expected outcomes if true vs false.\nHIDDEN_DYNAMIC: <key design challenges and most important confounds>\nRUBRIC:\n- Identifies correct independent and dependent variables (0 or 1)\n- Includes appropriate controls (0 or 1)\n- Addresses the most important confound (0 or 1)\n- Specifies falsifiable predictions (0 or 1)\n- Design is practically feasible (0 or 1)\nPASS_THRESHOLD: 4",
    8: "Generate an expert-level analysis exercise involving Simpson's paradox, Bayesian reasoning, or causal inference from observational data. The correct answer should be counterintuitive.\n\nOutput in this exact format:\nDATA: <dataset demonstrating Simpson's paradox, base rate neglect, or a causal inference trap>\nQUESTION: What is the correct interpretation? Why might the naive interpretation be wrong?\nHIDDEN_DYNAMIC: <the statistical trap and correct reasoning>\nRUBRIC:\n- Identifies the statistical phenomenon at play (0 or 1)\n- Explains why the naive interpretation fails (0 or 1)\n- Provides the correct interpretation with reasoning (0 or 1)\n- Demonstrates understanding of the underlying mechanism (0 or 1)\n- Could explain this to someone without statistical training (0 or 1)\n- Identifies real-world implications of the misinterpretation (0 or 1)\nPASS_THRESHOLD: 5",
    9: "Generate a master-level analysis exercise. Present a research scenario where standard statistical methods are being misapplied, and ask the reader to identify the error and propose the correct approach.\n\nOutput in this exact format:\nSCENARIO: <a research scenario where statistical methods are misapplied>\nQUESTION: Identify the methodological error. Why does it matter? What is the correct approach?\nHIDDEN_DYNAMIC: <the specific error, its consequences, and the fix>\nRUBRIC:\n- Correctly identifies the core methodological issue (0 or 1)\n- Explains why it matters (consequences, not just that it is wrong) (0 or 1)\n- Proposes a correct alternative approach (0 or 1)\n- Demonstrates understanding of statistical assumptions being violated (0 or 1)\n- Reasoning is rigorous, not hand-wavy (0 or 1)\n- Shows awareness of how this error occurs in practice (0 or 1)\nPASS_THRESHOLD: 5",
    10: "Generate a visionary-level analysis exercise. Ask the reader to propose a metric that captures something currently unmeasured, or identify a hidden structural pattern connecting seemingly unrelated domains.\n\nOutput in this exact format:\nSCENARIO: <a domain where existing metrics are inadequate, or two unrelated phenomena sharing a hidden pattern>\nQUESTION: Propose a novel metric or identify the hidden structural connection. Justify your reasoning.\nHIDDEN_DYNAMIC: <what an insightful response looks like>\nRUBRIC:\n- The proposed metric or pattern is genuinely novel (0 or 1)\n- It captures something real that existing approaches miss (0 or 1)\n- The reasoning is sound, not just creative (0 or 1)\n- Practical implications are identified (0 or 1)\n- Shows awareness of limitations of the proposal (0 or 1)\n- The insight could spark further investigation (0 or 1)\nPASS_THRESHOLD: 5",
})

GENERATION_PROMPTS["emotional_intelligence"].update({
    6: "Generate an advanced EI exercise involving a multi-party conflict where each person's perspective is genuinely valid. No one is clearly wrong. The solver must navigate competing legitimate needs.\n\nOutput in this exact format:\nSCENARIO: <5-6 sentences with 3 people in conflict where all perspectives have merit>\nQUESTION: What is each person feeling and needing? How would you help them navigate this without declaring a winner?\nHIDDEN_DYNAMIC: <each person's legitimate need and the fundamental tension>\nRUBRIC:\n- Accurately identifies each person's emotional state (0 or 1)\n- Recognizes all perspectives as legitimate (0 or 1)\n- Addresses underlying needs, not surface positions (0 or 1)\n- Proposes a path forward that honors all perspectives (0 or 1)\n- Shows cultural or contextual awareness (0 or 1)\nPASS_THRESHOLD: 4",
    7: "Generate a synthesis-level EI exercise involving recognizing manipulation vs genuine distress, or crafting a response that validates without enabling.\n\nOutput in this exact format:\nSCENARIO: <5-6 sentences where someone's behavior could be manipulation OR genuine distress>\nQUESTION: How do you respond in a way that helps if genuine, without enabling if manipulative?\nHIDDEN_DYNAMIC: <the ambiguity and what a wise response looks like>\nRUBRIC:\n- Acknowledges the genuine ambiguity (0 or 1)\n- Responds to the person, not the diagnosis (0 or 1)\n- Sets appropriate boundaries without coldness (0 or 1)\n- Response works whether the person is genuine or manipulative (0 or 1)\n- Shows emotional intelligence, not just psychological knowledge (0 or 1)\nPASS_THRESHOLD: 4",
    8: "Generate an expert-level EI exercise involving crisis de-escalation, navigating grief, or supporting someone with a mental health condition where the obvious response would actually be harmful.\n\nOutput in this exact format:\nSCENARIO: <5-6 sentences with a high-stakes emotional situation where the intuitive response is wrong>\nQUESTION: What is the right response here, and why is the obvious response harmful?\nHIDDEN_DYNAMIC: <why the intuitive approach fails and what works instead>\nRUBRIC:\n- Identifies why the obvious response would be harmful (0 or 1)\n- Proposes a response that actually helps (0 or 1)\n- Shows understanding of the specific condition or grief stage (0 or 1)\n- Respects the person's agency and dignity (0 or 1)\n- Demonstrates genuine emotional attunement (0 or 1)\n- Knows the limits of their role (does not play therapist) (0 or 1)\nPASS_THRESHOLD: 5",
    9: "Generate a master-level EI exercise: the solver must write a message from one person's perspective in a conflict that the OTHER person would consider fair and accurate.\n\nOutput in this exact format:\nSCENARIO: <5-6 sentences describing a conflict between two people with very different values>\nQUESTION: Write a message from Person A that Person B would read and say 'yes, that is fair.' Then explain how you achieved this.\nHIDDEN_DYNAMIC: <what makes cross-perspective writing genuinely difficult here>\nRUBRIC:\n- The message accurately represents Person A's perspective (0 or 1)\n- Person B would genuinely find it fair (0 or 1)\n- The message acknowledges the legitimate tension (0 or 1)\n- Demonstrates genuine empathy for both perspectives (0 or 1)\n- Meta-explanation shows awareness of what cross-perspective communication requires (0 or 1)\n- The message could actually help in the real situation (0 or 1)\nPASS_THRESHOLD: 5",
    10: "Generate a visionary-level EI exercise: write something that would genuinely comfort someone in a specific difficult situation (not generic comfort), or articulate an emotion that has no word in English but is universally recognizable.\n\nOutput in this exact format:\nSCENARIO: <a specific, vivid emotional situation that defies easy categorization>\nQUESTION: Write something that would genuinely help this person, or name and describe the emotion they are experiencing.\nHIDDEN_DYNAMIC: <what makes this emotionally complex and what an exceptional response captures>\nRUBRIC:\n- Response is specific to this situation, not generic (0 or 1)\n- Demonstrates deep emotional understanding (0 or 1)\n- Would be recognized as true by someone who has experienced this (0 or 1)\n- Shows originality (not cliche comfort or pop psychology) (0 or 1)\n- Has the right emotional register (0 or 1)\n- Would actually help, not just demonstrate understanding (0 or 1)\nPASS_THRESHOLD: 5",
})

GENERATION_PROMPTS["practical_reasoning"].update({
    6: "Generate an advanced practical reasoning exercise with multiple stakeholders and incomplete information. The decision-maker must make a recommendation under uncertainty.\n\nOutput in this exact format:\nPROBLEM: <a multi-stakeholder decision scenario with 3+ constraints and missing information>\nQUESTION: What is your recommended course of action? What information would change your recommendation?\nHIDDEN_DYNAMIC: <the key tradeoffs and how missing information affects the decision>\nRUBRIC:\n- Identifies the key constraints and stakeholders (0 or 1)\n- Acknowledges and reasons about uncertainty (0 or 1)\n- Makes a clear recommendation with justification (0 or 1)\n- Identifies what missing information would change the answer (0 or 1)\n- Plan is specific and actionable (0 or 1)\nPASS_THRESHOLD: 4",
    7: "Generate a synthesis-level practical reasoning exercise requiring second-order consequence prediction. Present a policy decision and ask for analysis of direct, indirect, and unintended consequences.\n\nOutput in this exact format:\nPROBLEM: <a policy or organizational decision with non-obvious second-order effects>\nQUESTION: Analyze the direct, indirect, and unintended consequences. What would you recommend?\nHIDDEN_DYNAMIC: <the non-obvious second-order effects>\nRUBRIC:\n- Identifies direct intended consequences (0 or 1)\n- Identifies at least 2 non-obvious indirect consequences (0 or 1)\n- Identifies a plausible unintended negative consequence (0 or 1)\n- Second-order reasoning is specific, not vague (0 or 1)\n- Recommendation accounts for the indirect effects (0 or 1)\nPASS_THRESHOLD: 4",
    8: "Generate an expert-level practical reasoning exercise: an ethical dilemma with no clean answer involving organizational strategy, competing values, and real-world constraints.\n\nOutput in this exact format:\nPROBLEM: <a genuine ethical dilemma with 5+ stakeholders and competing values>\nQUESTION: What is the best course of action? What values are you prioritizing and sacrificing?\nHIDDEN_DYNAMIC: <the irreducible tension and what different ethical frameworks recommend>\nRUBRIC:\n- Identifies the competing values clearly (0 or 1)\n- Acknowledges that something will be sacrificed (0 or 1)\n- Provides a specific, implementable plan (0 or 1)\n- Reasoning is transparent about value priorities (0 or 1)\n- Considers perspectives of all affected stakeholders (0 or 1)\n- Demonstrates mature ethical reasoning (0 or 1)\nPASS_THRESHOLD: 5",
    9: "Generate a master-level practical reasoning exercise: design a decision framework for a recurring class of problems, with built-in failure mode mitigations and self-correction mechanisms.\n\nOutput in this exact format:\nPROBLEM: <a class of recurring decisions with 2-3 specific examples>\nQUESTION: Design a reusable decision framework. Include criteria, process, and failure mode mitigations.\nHIDDEN_DYNAMIC: <what makes this class of decisions systematically difficult>\nRUBRIC:\n- Framework addresses the root difficulty (0 or 1)\n- Criteria are clear and actionable (0 or 1)\n- Framework includes self-correction mechanisms (0 or 1)\n- Failure modes are specifically identified and mitigated (0 or 1)\n- Framework is practical enough to actually use (0 or 1)\n- Demonstrates systems thinking (sees feedback loops) (0 or 1)\nPASS_THRESHOLD: 5",
    10: "Generate a visionary-level practical reasoning exercise: identify the highest-leverage intervention in a complex system, or design a system that improves itself over time.\n\nOutput in this exact format:\nPROBLEM: <a complex system with multiple interacting failure modes>\nQUESTION: What is the single highest-leverage intervention? Design it.\nHIDDEN_DYNAMIC: <the leverage point and how it cascades through the system>\nRUBRIC:\n- Identifies a genuine leverage point (not the most obvious problem) (0 or 1)\n- Explains the cascade mechanism (0 or 1)\n- The intervention is practically implementable (0 or 1)\n- Shows systems thinking (feedback loops, emergent behavior) (0 or 1)\n- Design includes adaptivity (learns and improves) (0 or 1)\n- Analysis reveals something non-obvious about the system (0 or 1)\nPASS_THRESHOLD: 5",
})

GENERATION_PROMPTS["creative_expression"].update({
    6: "Generate an advanced creative writing exercise requiring genre-blending or voice mimicry. The writer must combine two distinct genres or capture a specific author's voice while telling an original story.\n\nIMPORTANT: Do NOT use palindrome as a constraint.\n\nOutput in this exact format:\nPROMPT: <a creative task combining genre/voice constraint with narrative requirements>\nHIDDEN_DYNAMIC: <what makes this challenging and what mastery looks like>\nRUBRIC:\n- Genre/voice constraint is convincingly maintained (0 or 1)\n- Story has genuine narrative momentum (0 or 1)\n- Writing shows craft (precise word choice, intentional rhythm) (0 or 1)\n- The constraint enhances rather than restricts the piece (0 or 1)\n- Piece has emotional resonance (0 or 1)\nPASS_THRESHOLD: 4",
    7: "Generate a synthesis-level creative writing exercise requiring extended metaphor systems or an unreliable narrator with consistent internal logic.\n\nIMPORTANT: Do NOT use palindrome as a constraint.\n\nOutput in this exact format:\nPROMPT: <a creative task requiring sustained metaphorical or narrative framework>\nHIDDEN_DYNAMIC: <what excellence looks like>\nRUBRIC:\n- Metaphor/narrative framework is sustained throughout (0 or 1)\n- Internal logic is consistent (0 or 1)\n- The framework reveals something about its subject (0 or 1)\n- Writing demonstrates technical skill (0 or 1)\n- Piece rewards re-reading (layers of meaning) (0 or 1)\nPASS_THRESHOLD: 4",
    8: "Generate an expert-level creative writing exercise: flash fiction with a twist that recontextualizes everything, or poetry with formal constraints (sonnet, villanelle, or other specific form).\n\nIMPORTANT: Do NOT use palindrome as a constraint. Choose achievable formal constraints.\n\nOutput in this exact format:\nPROMPT: <creative task requiring mastery of form AND emotional depth>\nHIDDEN_DYNAMIC: <what would impress>\nRUBRIC:\n- Formal constraints are fully satisfied (0 or 1)\n- The form serves the emotional content (not just a gimmick) (0 or 1)\n- Writing quality is publication-ready (0 or 1)\n- Piece creates genuine emotional impact (0 or 1)\n- Technical mastery is evident but does not overshadow content (0 or 1)\n- Would stand up to close reading (0 or 1)\nPASS_THRESHOLD: 5",
    9: "Generate a master-level creative writing exercise: a piece that works on two levels (surface story + allegory), or an original myth that explains a modern phenomenon.\n\nIMPORTANT: Do NOT use palindrome as a constraint.\n\nOutput in this exact format:\nPROMPT: <creative task requiring multi-layered meaning or mythic invention>\nHIDDEN_DYNAMIC: <what mastery looks like>\nRUBRIC:\n- Surface level works as a complete, satisfying piece (0 or 1)\n- Deeper level adds genuine meaning (0 or 1)\n- The two levels enrich each other (0 or 1)\n- Writing quality demonstrates craft (0 or 1)\n- The piece is original (not derivative of well-known allegories) (0 or 1)\n- A reader who misses the deeper level still enjoys it (0 or 1)\nPASS_THRESHOLD: 5",
    10: "Generate a visionary-level creative writing exercise: create something with an original form that serves its content, or produce a piece that surprises even the person who assigned it.\n\nIMPORTANT: Do NOT use palindrome as a constraint.\n\nOutput in this exact format:\nPROMPT: <creative task requiring genuine originality in form, content, or both>\nHIDDEN_DYNAMIC: <what would make this exceptional>\nRUBRIC:\n- The piece surprises (does something unexpected) (0 or 1)\n- Form and content are inseparable (structure IS meaning) (0 or 1)\n- Demonstrates genuine creative vision (0 or 1)\n- Writing quality is exceptional (0 or 1)\n- The piece creates its own rules and follows them (0 or 1)\n- A sophisticated reader would be moved or provoked (0 or 1)\nPASS_THRESHOLD: 5",
})

# Synthesis — cross-competency integration. Each level combines more skills.
# L1-L3: combine 2 competencies. L4-L7: combine 3. L8-L10: combine 4+.
GENERATION_PROMPTS["synthesis"] = {
    1: "Generate a beginner synthesis exercise combining REASONING and EMOTIONAL INTELLIGENCE. The exercise should require both logical thinking AND empathy to solve.\n\nExample: 'A friend says they failed a test because the teacher hates them. Use logic to evaluate their claim, and write an empathetic response that gently introduces your analysis.'\n\nOutput in this exact format:\nPROMPT: <exercise requiring both reasoning and emotional intelligence>\nHIDDEN_DYNAMIC: <what a good response integrates from both competencies>\nRUBRIC:\n- Demonstrates logical reasoning (0 or 1)\n- Shows genuine empathy (0 or 1)\n- Integrates both skills (not just sequential) (0 or 1)\nPASS_THRESHOLD: 2",
    2: "Generate a beginner synthesis exercise combining ANALYSIS and CREATIVE EXPRESSION. The exercise should require both pattern recognition AND creative communication.\n\nExample: 'Analyze the trend in these numbers [3, 5, 8, 13, 21] and write a short poem that captures the pattern without naming it directly.'\n\nOutput in this exact format:\nPROMPT: <exercise requiring both analytical and creative skills>\nHIDDEN_DYNAMIC: <what a good response integrates from both competencies>\nRUBRIC:\n- Correctly identifies the pattern or insight (0 or 1)\n- Creative expression is engaging (0 or 1)\n- The creativity serves the analysis (not just decorative) (0 or 1)\nPASS_THRESHOLD: 2",
    3: "Generate a beginner synthesis exercise combining PRACTICAL REASONING and COMPREHENSION. The exercise should require understanding a complex text AND making a practical decision based on it.\n\nExample: 'Read this rental agreement clause: [short clause]. Your friend wants to sublet their apartment for summer. What should they do?'\n\nOutput in this exact format:\nPROMPT: <exercise requiring text comprehension + practical decision-making>\nHIDDEN_DYNAMIC: <what a good response integrates from both competencies>\nRUBRIC:\n- Accurately comprehends the source material (0 or 1)\n- Makes a practical, actionable recommendation (0 or 1)\n- Recommendation is grounded in the source material (0 or 1)\nPASS_THRESHOLD: 2",
    4: "Generate an intermediate synthesis exercise combining REASONING, ANALYSIS, and EMOTIONAL INTELLIGENCE. The exercise should present a situation requiring all three skills simultaneously.\n\nExample: 'A team of 5 was passed over for a project. Morale data shows declining engagement. The manager says everything is fine. Analyze the data, reason about what's happening, and draft a message to the manager.'\n\nOutput in this exact format:\nPROMPT: <exercise requiring reasoning + analysis + emotional intelligence>\nHIDDEN_DYNAMIC: <how the three competencies interact>\nRUBRIC:\n- Logical reasoning is sound (0 or 1)\n- Analysis is grounded in evidence (0 or 1)\n- Emotional intelligence is authentic (0 or 1)\n- All three skills are integrated (not sequential) (0 or 1)\nPASS_THRESHOLD: 3",
    5: "Generate an intermediate synthesis exercise combining CODING, PRACTICAL REASONING, and COMPREHENSION. The exercise should require reading a spec, making design decisions, and implementing them.\n\nExample: 'Read this API specification [short spec]. Design the data model and implement the core function, handling the ambiguity in requirement #3.'\n\nOutput in this exact format:\nPROMPT: <exercise requiring comprehension + practical reasoning + coding>\nHIDDEN_DYNAMIC: <the key integration challenge>\nRUBRIC:\n- Correctly interprets the specification (0 or 1)\n- Design decisions are well-reasoned (0 or 1)\n- Code is correct and clean (0 or 1)\n- Handles ambiguity in the spec appropriately (0 or 1)\nPASS_THRESHOLD: 3",
    6: "Generate an advanced synthesis exercise combining CREATIVE EXPRESSION, EMOTIONAL INTELLIGENCE, and ANALYSIS. Write a scenario where someone must analyze a complex human situation, understand the emotions involved, and communicate their insight through a creative medium.\n\nOutput in this exact format:\nPROMPT: <exercise requiring creative + emotional + analytical integration>\nHIDDEN_DYNAMIC: <what makes this genuinely multi-competency>\nRUBRIC:\n- Analysis is insightful (0 or 1)\n- Emotional understanding is deep (0 or 1)\n- Creative expression is compelling (0 or 1)\n- The three skills amplify each other (0 or 1)\n- Result is greater than the sum of its parts (0 or 1)\nPASS_THRESHOLD: 4",
    7: "Generate an advanced synthesis exercise combining LANGUAGE PRECISION, PRACTICAL REASONING, and REASONING. The exercise should require precise communication of a complex plan that depends on logical analysis.\n\nExample: 'Write instructions for evacuating a building during a fire that are precise enough to prevent misinterpretation, logically sequenced, and account for 3 different scenarios.'\n\nOutput in this exact format:\nPROMPT: <exercise requiring precision + practical reasoning + logic>\nHIDDEN_DYNAMIC: <how the three competencies must interact>\nRUBRIC:\n- Language is precise and unambiguous (0 or 1)\n- Plan is logically sequenced (0 or 1)\n- Reasoning handles all scenarios correctly (0 or 1)\n- Integration is seamless (0 or 1)\n- Result is practically usable (0 or 1)\nPASS_THRESHOLD: 4",
    8: "Generate an expert synthesis exercise combining 4+ competencies. The exercise should present a complex real-world scenario requiring REASONING, EMOTIONAL INTELLIGENCE, PRACTICAL REASONING, and ANALYSIS simultaneously — with CREATIVE EXPRESSION in the response format.\n\nOutput in this exact format:\nPROMPT: <complex scenario requiring 4+ competencies working together>\nHIDDEN_DYNAMIC: <what makes this genuinely cross-competency at expert level>\nRUBRIC:\n- Reasoning is rigorous (0 or 1)\n- Emotional intelligence is genuine (0 or 1)\n- Practical plan is actionable (0 or 1)\n- Analysis is evidence-grounded (0 or 1)\n- Communication is compelling (0 or 1)\n- Competencies amplify rather than compete with each other (0 or 1)\nPASS_THRESHOLD: 5",
    9: "Generate a master synthesis exercise combining 4+ competencies in a scenario where the competencies are in TENSION with each other — the logically correct answer is emotionally harmful, the creative solution is impractical, etc. The solver must navigate these tensions.\n\nOutput in this exact format:\nPROMPT: <scenario where competencies pull in different directions>\nHIDDEN_DYNAMIC: <the specific tensions and what mastery of integration looks like>\nRUBRIC:\n- Identifies the tensions between competency demands (0 or 1)\n- Does not sacrifice one competency for another (0 or 1)\n- Finds a synthesis that honors competing demands (0 or 1)\n- Response demonstrates mastery across all required competencies (0 or 1)\n- Shows meta-awareness of WHY the tensions exist (0 or 1)\n- Result is wiser than any single-competency approach (0 or 1)\nPASS_THRESHOLD: 5",
    10: "Generate a visionary synthesis exercise requiring ALL competencies to create something that none of them could produce alone. The challenge should be genuinely novel — not a predictable combination of existing exercise types.\n\nOutput in this exact format:\nPROMPT: <exercise requiring the full range of competencies to produce something transcendent>\nHIDDEN_DYNAMIC: <what makes this a true integration challenge at the highest level>\nRUBRIC:\n- Multiple competencies are genuinely required (not decorative) (0 or 1)\n- The result could not be achieved by any single competency (0 or 1)\n- Demonstrates genuine integration (competencies inform each other) (0 or 1)\n- Response shows intellectual and emotional maturity (0 or 1)\n- The work is original and surprising (0 or 1)\n- A thoughtful human would find this impressive (0 or 1)\nPASS_THRESHOLD: 5",
}

# ── L11-20 Extended Prompts ───────────────────────────────────────
from entity.curriculum_l11_20 import EXTENDED_PROMPTS as _L11_20
for _comp, _levels in _L11_20.items():
    if _comp in GENERATION_PROMPTS:
        GENERATION_PROMPTS[_comp].update(_levels)
    else:
        GENERATION_PROMPTS[_comp] = dict(_levels)
del _L11_20, _comp, _levels


# ── Core Functions ────────────────────────────────────────────────

def load_competencies() -> dict:
    """Load competency state from disk.

    Auto-migrates: if new competencies are defined in COMPETENCIES but missing
    from the saved JSON, they are added at level 0 (fresh start).
    """
    path = _configured_competencies_path or COMPETENCIES_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Auto-migrate: add any new competencies not yet in the saved state
        for name in COMPETENCIES:
            if name not in data.get("competencies", {}):
                data["competencies"][name] = {
                    "current_level": 0,
                    "consecutive_passes": 0,
                    "total_exercises": 0,
                    "total_passed": 0,
                    "level_history": [],
                }
                logger.info(f"Migrated new competency: {name}")
                save_competencies(data)
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        # Create initial state
        initial = {
            "version": 1,
            "last_updated": datetime.now().isoformat(),
            "competencies": {
                name: {
                    "current_level": 0,
                    "consecutive_passes": 0,
                    "total_exercises": 0,
                    "total_passed": 0,
                    "level_history": [],
                }
                for name in COMPETENCIES
            },
            "overall_phase": "infant",
            "phase_started": datetime.now().strftime("%Y-%m-%d"),
            "total_exercises": 0,
            "exercise_history": [],
        }
        save_competencies(initial)
        return initial


def save_competencies(data: dict):
    """Save competency state to disk."""
    path = _configured_competencies_path or COMPETENCIES_PATH
    data["last_updated"] = datetime.now().isoformat()
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


STUCK_THRESHOLD = 5  # Skip competency after this many consecutive failures
MAX_LEVEL = 50       # High ceiling — we want to find natural plateaus, not artificial ones

# Per-competency level caps. Reasoning and coding route to Sonnet via Poe at
# L11+, so their caps are raised to 25. Other categories use Haiku at L11+,
# raised from 10→15 to see how far Haiku can push them.
LEVEL_CAPS = {
    "practical_reasoning": 15,      # Haiku grading at L11+
    "analysis": 15,                 # Haiku grading at L11+
    "creative_expression": 15,      # Haiku grading at L11+
    "synthesis": 15,                # Haiku grading at L11+
    "coding": 25,                   # Sonnet via Poe at L11+
    "emotional_intelligence": 15,   # Haiku grading at L12+
    "reasoning": 25,                # Sonnet via Poe at L11+
}


def _consecutive_fails(competencies: dict, comp_name: str) -> int:
    """Count recent consecutive failures for a competency."""
    history = competencies.get("exercise_history", [])
    count = 0
    for entry in reversed(history):
        if entry.get("competency") != comp_name:
            continue
        if entry.get("passed"):
            break
        count += 1
    return count


def pick_next_exercise(competencies: dict) -> dict:
    """Pick which competency and level to exercise next.

    Strategy: target the competency with the lowest level.
    Tie-break: fewest total exercises (least practiced).
    Stuck-detection: skip competencies with 10+ consecutive failures.
    """
    comps = competencies["competencies"]
    candidates = []
    for name, state in comps.items():
        level = state["current_level"]
        cap = LEVEL_CAPS.get(name, MAX_LEVEL)
        if level >= cap:
            continue  # Maxed out or capped
        # Skip stuck competencies — prevents death spirals
        fails = _consecutive_fails(competencies, name)
        if fails >= STUCK_THRESHOLD:
            # Demote one level, but never below (highest_mastered - 1).
            # Without this floor, cascading 5-fail demotions can drop from L10 → L1
            # over many cycles, wasting time re-grinding work already mastered.
            highest_mastered = max(
                (h["level"] for h in state.get("level_history", [])),
                default=0,
            )
            floor = max(1, highest_mastered - 1)
            if state["current_level"] > floor:
                state["current_level"] -= 1
                state["consecutive_passes"] = 0
                print(f"  [curriculum] STUCK: Demoting {name} L{level + 1}→L{state['current_level']} ({fails} consecutive failures)")
                logger.warning(f"Demoting {name} ({fails} consecutive failures)")
            else:
                print(f"  [curriculum] STUCK: Skipping {name} ({fails} consecutive failures, floor L{floor})")
                logger.warning(f"Skipping {name} ({fails} consecutive failures, floor L{floor})")
            continue
        candidates.append({
            "competency": name,
            "level": level + 1,  # Exercise at the NEXT level to master
            "total_exercises": state["total_exercises"],
            "current_level": level,
        })

    if not candidates:
        # All maxed or stuck — pick random for maintenance
        name = random.choice(list(comps.keys()))
        lvl = comps[name]["current_level"]
        return {"competency": name, "level": min(lvl + 1, MAX_LEVEL), "current_level": lvl, "total_exercises": 0}

    # Sort: lowest level first, then fewest exercises
    candidates.sort(key=lambda c: (c["current_level"], c["total_exercises"]))

    # Tie-break randomly among candidates with the same (level, exercises)
    best = candidates[0]
    tied = [c for c in candidates
            if c["current_level"] == best["current_level"]
            and c["total_exercises"] == best["total_exercises"]]
    return random.choice(tied)


def generate_exercise(competency: str, level: int, brain, force_local: bool = False) -> dict:
    """Generate an exercise for the given competency and level.

    Uses seed bank for levels 1-3 of objective competencies.
    Uses LLM generation for all other cases.

    Returns dict with: competency, level, prompt, grading_type, and grading details.
    """
    # Try seed bank first
    seeds = SEED_EXERCISES.get(competency, {}).get(level, [])
    if seeds:
        exercise = random.choice(seeds)
        result = {
            "competency": competency,
            "level": level,
            "prompt": exercise["prompt"],
            "grading_type": exercise["grading"],
            "expected_answer": exercise.get("answer"),
            "pattern": exercise.get("pattern"),
            "test_cases": exercise.get("test_cases"),
            "validator": exercise.get("validator"),
            "rubric": exercise.get("rubric", ""),
            "hidden_dynamic": exercise.get("hidden_dynamic", ""),
            "pass_threshold": exercise.get("pass_threshold", 2),
            "generated_at": datetime.now().isoformat(),
            "source": "seed_bank",
        }
        return result

    # LLM generation
    gen_prompt = GENERATION_PROMPTS.get(competency, {}).get(level)
    if not gen_prompt:
        # Find nearest available level prompt
        prompts = GENERATION_PROMPTS.get(competency, {})
        if prompts:
            nearest = min(prompts.keys(), key=lambda k: abs(k - level))
            gen_prompt = prompts[nearest]
        else:
            logger.warning(f"No generation prompt for {competency} L{level}")
            return _fallback_exercise(competency, level)

    # Generate exercise — route reasoning/coding L11+ to Sonnet ("deep") for
    # exercises that exceed Haiku's ability. Other L8+ use "fast" (Haiku).
    if force_local:
        tier = "local"
    elif competency in ("reasoning", "coding") and level >= 11:
        tier = "deep"
    elif level >= 8:
        tier = "fast"
    else:
        tier = _grading_tier("exercise_generation")
    # Local grind mode: 600 tokens is enough for a concise exercise; 2048 causes 10+ min timeouts.
    # Cloud: keep 2048 for rich comprehension passages.
    gen_max_tokens = 600 if force_local else 2048
    # Local grind: only try once — retrying after a timeout just adds more timeouts.
    max_attempts = 1 if force_local else 3
    for attempt in range(max_attempts):
        try:
            result = brain.think(
                prompt=gen_prompt,
                system="You are an exercise generator for an AI training curriculum. Generate clear, unambiguous exercises with precise grading criteria. Be creative but ensure exercises are solvable. Keep passages and scenarios concise (under 200 words) — quality over length.",
                tier=tier,
                max_tokens=gen_max_tokens,
                temperature=0.7 + (attempt * 0.1),
            )
            text = result.get("text", "")
            if not text or len(text) < 50:
                continue

            exercise = _parse_generated_exercise(text, competency, level)
            if exercise:
                return exercise
            else:
                logger.warning(f"Exercise parse failed for {competency} L{level} attempt {attempt+1}. "
                             f"First 200 chars: {text[:200]}")
        except Exception as e:
            logger.warning(f"Exercise generation attempt {attempt+1} failed: {e}")

    return _fallback_exercise(competency, level)


def _parse_generated_exercise(text: str, competency: str, level: int) -> Optional[dict]:
    """Parse LLM-generated exercise text into structured exercise dict."""
    # Strip markdown formatting from labels (Qwen3 wraps: **DATA:** or ## DATA:)
    text = re.sub(r'\*{1,2}([A-Z_]+):\*{1,2}', r'\1:', text)
    # ## DATA: -> DATA:  and  ## DATA (no colon) -> DATA:
    text = re.sub(r'^#{1,3}\s+([A-Z_]+):?(?=\s)', r'\1:', text, flags=re.MULTILINE)
    # Remove markdown title lines like "# Adversarial Analysis Exercise: ..."
    text = re.sub(r'^#{1,3}\s+[A-Z][a-z][^:\n]*$', '', text, flags=re.MULTILINE)
    comp_type = COMPETENCIES[competency]["type"]

    # L8+ objective types (except coding) use rubric grading — correctness
    # alone isn't enough; quality of reasoning matters at these levels
    if level >= 8 and comp_type == "objective" and competency != "coding":
        comp_type = "rubric"

    if comp_type == "objective":
        # Look for PUZZLE/PROBLEM/TASK + ANSWER/SOLUTION
        prompt_match = re.search(
            r"(?:PUZZLE|PROBLEM|TASK):\s*(.+?)(?=\n(?:ANSWER|SOLUTION|EXPLANATION|TEST_CASES|VALIDATION):)",
            text, re.DOTALL
        )
        answer_match = re.search(r"ANSWER:\s*(.+?)(?=\n|$)", text)

        # For coding: extract SOLUTION and TEST_CASES for execution-based grading
        if competency == "coding" and prompt_match:
            solution_match = re.search(
                r"SOLUTION:\s*(.+?)(?=\nTEST_CASES:|$)", text, re.DOTALL
            )
            test_match = re.search(
                r"TEST_CASES:\s*(.+?)$", text, re.DOTALL
            )
            # Parse test cases: look for assert statements
            # LLMs often wrap these in markdown (bullets, code fences, numbering)
            test_cases = []
            if test_match:
                in_code_fence = False
                for line in test_match.group(1).strip().splitlines():
                    line = line.strip()
                    # Track code fences
                    if line.startswith("```"):
                        in_code_fence = not in_code_fence
                        continue
                    # Strip markdown formatting: bullets, numbering, backticks, bold
                    cleaned = re.sub(r"^[\-\*\d.)\s]+", "", line)  # bullets/numbering
                    cleaned = cleaned.strip("`* ")  # backticks, bold, spaces
                    if cleaned.startswith("assert "):
                        test_cases.append(cleaned)

            ref_solution = solution_match.group(1).strip() if solution_match else None

            if not test_cases and test_match:
                logger.warning(f"coding L{level}: TEST_CASES section found but no assert "
                             f"statements extracted. Raw: {test_match.group(1)[:200]}")

            return {
                "competency": competency,
                "level": level,
                "prompt": prompt_match.group(1).strip(),
                "grading_type": "code_exec" if test_cases else "llm_verify",
                "expected_answer": ref_solution,
                "test_cases_raw": test_cases,
                "full_generation": text,
                "generated_at": datetime.now().isoformat(),
                "source": "generated",
            }

        # For language_precision: use constraint-based rubric grading
        # (llm_verify fails because grader can't verify character-level properties)
        if competency == "language_precision" and prompt_match:
            validation_match = re.search(r"VALIDATION:\s*(.+?)$", text, re.DOTALL)
            validation = validation_match.group(1).strip() if validation_match else ""
            example_answer = answer_match.group(1).strip() if answer_match else ""
            return {
                "competency": competency,
                "level": level,
                "prompt": prompt_match.group(1).strip(),
                "grading_type": "rubric",
                "hidden_dynamic": f"Example correct answer: {example_answer}" if example_answer else "",
                "rubric": (
                    f"- Response directly addresses the task (0 or 1)\n"
                    f"- Response satisfies ALL stated constraints (0 or 1)\n"
                    f"- Response is a valid, well-formed answer (0 or 1)"
                ),
                "pass_threshold": 3,
                "full_generation": text,
                "generated_at": datetime.now().isoformat(),
                "source": "generated",
            }

        if prompt_match:
            return {
                "competency": competency,
                "level": level,
                "prompt": prompt_match.group(1).strip(),
                "grading_type": "llm_verify",
                "expected_answer": answer_match.group(1).strip() if answer_match else None,
                "full_generation": text,
                "generated_at": datetime.now().isoformat(),
                "source": "generated",
            }
    else:
        # Rubric-based: look for SCENARIO/PASSAGE/PROMPT/DATA/PROBLEM + RUBRIC
        prompt_key = None
        for key in ["SCENARIO", "PASSAGE", "PASSAGE_A", "PROMPT", "DATA", "PROBLEM", "SET_A",
                    "SOURCE_A", "HYPOTHESIS", "DATA_SUMMARY", "DOMAIN", "BUGGY_CODE", "CODE",
                    "EVIDENCE", "PUBLISHED_CONCLUSION", "FLAWED_ANALYSIS"]:
            if key + ":" in text:
                prompt_key = key
                break

        if not prompt_key:
            return None

        # Extract the question/prompt section — stop at any known section label
        _section_labels = r"QUESTION|HIDDEN_DYNAMIC|RUBRIC|FAILING_TEST|PASSING_TESTS|SOLUTION|BUG_EXPLANATION|PUBLISHED_CONCLUSION|FLAWED_ANALYSIS|SOURCE_B|SOURCE_C|EVIDENCE"
        prompt_match = re.search(
            rf"{prompt_key}:\s*(.+?)(?=\n(?:{_section_labels}):)",
            text, re.DOTALL
        )
        question_match = re.search(rf"QUESTION:\s*(.+?)(?=\n(?:HIDDEN_DYNAMIC|RUBRIC):)", text, re.DOTALL)
        hidden_match = re.search(r"HIDDEN_DYNAMIC:\s*(.+?)(?=\nRUBRIC:)", text, re.DOTALL)
        rubric_match = re.search(r"RUBRIC:\s*(.+?)(?=\nPASS_THRESHOLD:)", text, re.DOTALL)
        threshold_match = re.search(r"PASS_THRESHOLD:\s*(\d+)", text)

        if prompt_match:
            full_prompt = prompt_match.group(1).strip()

            # For multi-section exercises, include additional data sections
            for extra_key in ["SOURCE_B", "SOURCE_C", "FAILING_TEST", "PASSING_TESTS",
                              "FLAWED_ANALYSIS", "PUBLISHED_CONCLUSION", "EVIDENCE"]:
                extra_match = re.search(
                    rf"{extra_key}:\s*(.+?)(?=\n(?:{_section_labels}|QUESTION|HIDDEN_DYNAMIC|RUBRIC):)",
                    text, re.DOTALL
                )
                if extra_match:
                    full_prompt += f"\n\n{extra_key}: {extra_match.group(1).strip()}"

            # Truncation guard: if the passage ends mid-word or mid-sentence,
            # the exercise is unsolvable. Reject and let generator retry.
            # Skip guard if there's a QUESTION section (data can end with tables/numbers)
            if not question_match and full_prompt and full_prompt[-1] not in '.!?")\':;0123456789|%-}':
                logger.warning(f"Rejecting truncated {competency} exercise: ...{full_prompt[-40:]}")
                return None

            if question_match:
                full_prompt += "\n\n" + question_match.group(1).strip()

            return {
                "competency": competency,
                "level": level,
                "prompt": full_prompt,
                "grading_type": "rubric",
                "hidden_dynamic": hidden_match.group(1).strip() if hidden_match else "",
                "rubric": rubric_match.group(1).strip() if rubric_match else "",
                "pass_threshold": int(threshold_match.group(1)) if threshold_match else 2,
                "full_generation": text,
                "generated_at": datetime.now().isoformat(),
                "source": "generated",
            }

    return None


def _fallback_exercise(competency: str, level: int) -> dict:
    """Provide a hardcoded fallback exercise when generation fails."""
    fallbacks = {
        "reasoning": [
            "If A is taller than B, and B is taller than C, is A taller than C? Answer ONLY 'yes' or 'no'.",
            "All roses are flowers. Some flowers fade quickly. Can you conclude that some roses fade quickly? Explain your reasoning.",
            "A bat and a ball cost $1.10 together. The bat costs $1.00 more than the ball. How much does the ball cost? Show your work.",
        ],
        "coding": [
            "Write a Python function that returns the sum of all even numbers in a list. Just the function.",
            "Write a Python function that checks if a string is a palindrome, ignoring spaces and case. Just the function.",
            "Write a Python function that finds the second largest number in a list. Handle edge cases. Just the function.",
        ],
        "language_precision": [
            "Write a sentence where every word starts with a different letter of the alphabet in order from A to E.",
            "Rewrite this sentence to be exactly half as long without losing the core meaning: 'The significantly overwhelmed student decided to carefully reorganize all of their extremely scattered notes before the important examination.'",
            "Write a single sentence that contains exactly three independent clauses connected by semicolons.",
        ],
        "comprehension": [
            "The sky appears blue because molecules in the atmosphere scatter shorter wavelengths of light more than longer wavelengths. This is called Rayleigh scattering. What causes the sky to appear blue?",
            "Antibiotics kill bacteria but not viruses. A cold is caused by a virus. Why would a doctor refuse to prescribe antibiotics for a cold?",
            "Correlation measures whether two variables move together. Causation means one variable directly causes the other. Ice cream sales and drowning deaths both rise in summer. Explain why this is correlation, not causation.",
        ],
        "analysis": [
            "Given the numbers 2, 4, 8, 16, 32: what is the pattern? What would the next number be?",
            "A store reports sales doubled after running ads. But they also moved to a busier street the same week. What can you actually conclude about the ads' effectiveness?",
            "Three students scored 90, 60, and 30 on a test. The average is 60. Why is the average misleading here? What would be a better summary?",
        ],
        "emotional_intelligence": [
            "Your friend says 'I'm fine' but has been canceling plans all week. What might they be feeling, and how should you respond?",
            "A coworker takes credit for your idea in a meeting. Describe how you would address this without damaging the relationship.",
            "Someone you care about is making a decision you believe is harmful to them. How do you express concern without being controlling?",
        ],
        "practical_reasoning": [
            "You have 3 tasks due today, each takes 2 hours, but you only have 5 hours. What do you do?",
            "You find a $20 bill on the floor of a busy store. No one is looking for it. What do you do and why?",
            "Your car breaks down on a highway at night with no phone signal. What steps do you take, in order?",
        ],
        "creative_expression": [
            "Describe the sound of rain without using the words 'rain', 'water', or 'drops'. Write 2-3 sentences.",
            "Write a six-word story that conveys loss. Then explain your choices.",
            "Describe the color blue to someone who has never seen it, using only the other four senses.",
        ],
        "synthesis": [
            "Your friend failed a math test and is upset. Use logical reasoning to help them understand what went wrong, and write an empathetic response that combines your analysis with genuine emotional support.",
            "A city wants to reduce traffic. Propose a solution that addresses both the engineering problem AND the human behavior problem. Explain how both aspects interact.",
            "Someone argues that AI will eliminate all jobs. Someone else argues AI will create more jobs than it destroys. Synthesize both perspectives into a nuanced position that accounts for the strongest points of each.",
        ],
    }
    pool = fallbacks.get(competency, ["Describe your understanding of the concept of truth."])
    prompt = random.choice(pool)
    logger.warning(f"Using FALLBACK exercise for {competency} L{level} — generation failed")
    return {
        "competency": competency,
        "level": level,
        "prompt": prompt,
        "grading_type": "rubric",
        "rubric": "- Response is relevant (0 or 1)\n- Response shows understanding (0 or 1)\n- Response is clear (0 or 1)",
        "pass_threshold": 2,
        "hidden_dynamic": "",
        "generated_at": datetime.now().isoformat(),
        "source": "fallback",
    }


def _grade_language_precision(exercise: dict, response: str) -> Optional[dict]:
    """Programmatic grading for language_precision exercises.

    LLMs cannot reliably verify character-level or first-letter constraints.
    This function handles common constraint patterns programmatically.
    Returns None if it can't handle the exercise type (falls back to LLM grading).
    """
    prompt = exercise.get("prompt", "").lower()
    resp = response.strip()
    if not resp:
        return {"passed": False, "score": 0.0, "feedback": "Empty response"}

    # Strip markdown formatting that API models (Haiku) sometimes add:
    # - Remove lines starting with # (headers like "# Moonlit Garden")
    # - Remove rhyme scheme annotations like (A), (B), (ABAB)
    resp = re.sub(r"^#.*$", "", resp, flags=re.MULTILINE).strip()
    resp = re.sub(r"\s*\([A-Z]+\)\s*", " ", resp).strip()

    # Clean response: remove punctuation for word analysis
    resp_clean = re.sub(r"[^\w\s]", "", resp)
    words = resp_clean.split()
    # Lines = newline-separated (for poems); sentences = punctuation-separated
    lines = [l.strip() for l in resp.split("\n") if l.strip()]
    sentences = [s.strip() for s in re.split(r"[.!?\n]+", resp) if s.strip()]

    # Helper: parse number from digits or English words
    _WORD_NUMS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                  "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                  "eleven": 11, "twelve": 12}

    def _parse_num(s):
        s = s.strip().lower()
        return int(s) if s.isdigit() else _WORD_NUMS.get(s)

    # Multi-constraint accumulator: check all verifiable constraints,
    # fail fast on any violation, pass if ALL checked constraints pass.
    checks_done = 0

    # ── Check: "every word starts with a different letter ... from X to Y"
    alpha_order = re.search(
        r"(?:every|each) word starts? with (?:a different letter|consecutive letters?).*?(?:from|starting at) ([a-z]) to ([a-z])",
        prompt,
    )
    if alpha_order:
        checks_done += 1
        start_letter = alpha_order.group(1)
        end_letter = alpha_order.group(2)
        expected_letters = [chr(c) for c in range(ord(start_letter), ord(end_letter) + 1)]
        expected_count = len(expected_letters)

        if len(words) < expected_count:
            return {
                "passed": False, "score": 0.0,
                "feedback": f"Need {expected_count} words ({start_letter.upper()}-{end_letter.upper()}), got {len(words)}",
            }
        actual_letters = [w[0].lower() for w in words[:expected_count]]
        if actual_letters != expected_letters:
            expected_str = ", ".join(l.upper() for l in expected_letters)
            actual_str = ", ".join(l.upper() for l in actual_letters)
            return {"passed": False, "score": 0.0, "feedback": f"First letters should be [{expected_str}], got [{actual_str}]"}

    # ── Check: line count ("N-line poem", "write N lines", etc.)
    line_count_match = re.search(r"(\w+)[- ]line\b", prompt)
    if not line_count_match:
        line_count_match = re.search(r"write (\w+) lines\b", prompt)
    if line_count_match:
        expected_lines = _parse_num(line_count_match.group(1))
        if expected_lines:
            checks_done += 1
            if len(lines) != expected_lines:
                return {"passed": False, "score": 0.0,
                        "feedback": f"Expected {expected_lines} lines, got {len(lines)}"}

    # ── Check: per-line/sentence word count ("each line has exactly N words")
    is_line_constraint = "line" in prompt and ("each line" in prompt or "every line" in prompt)
    per_sent = re.search(
        r"each (?:sentence|line) (?:contains?|has|with|of) (?:exactly|precisely) (\w+) words",
        prompt,
    )
    if not per_sent:
        per_sent = re.search(r"(?:sentence|line)s?\b.*?(?:exactly|precisely) (\w+) words", prompt)
        if per_sent and "each" not in prompt and "sentence" not in prompt[:prompt.find("exactly") if "exactly" in prompt else 0]:
            per_sent = None
    if per_sent:
        target = _parse_num(per_sent.group(1))
        if target:
            checks_done += 1
            # Use newline-separated lines for "line" constraints, sentences otherwise
            items = lines if is_line_constraint else sentences
            label = "Line" if is_line_constraint else "Sentence"
            for i, item in enumerate(items):
                item_words = re.sub(r"[^\w\s]", "", item).split()
                if len(item_words) != target:
                    return {"passed": False, "score": 0.0,
                            "feedback": f"{label} {i+1} has {len(item_words)} words (need {target})"}

    # ── Check: overall word count ("in exactly N words" / "rewrite in exactly N words")
    if not per_sent:
        exact_words = re.search(r"(?:exactly|precisely) (\w+) words", prompt)
        if exact_words:
            target = _parse_num(exact_words.group(1))
            if target:
                checks_done += 1
                if len(words) != target:
                    return {"passed": False, "score": 0.0,
                            "feedback": f"Expected exactly {target} words, got {len(words)}"}

    # ── Check: word chain ("last word ... first word of the next")
    if "last word" in prompt and "first word" in prompt:
        checks_done += 1
        if len(sentences) < 2:
            return {"passed": False, "score": 0.0, "feedback": "Need at least 2 sentences"}
        for i in range(len(sentences) - 1):
            last_word = re.sub(r"[^\w]", "", sentences[i].split()[-1]).lower()
            first_word = re.sub(r"[^\w]", "", sentences[i + 1].split()[0]).lower()
            if last_word != first_word:
                return {"passed": False, "score": 0.0, "feedback": f"Chain broken: '{last_word}' != '{first_word}'"}

    # ── Check: no repeated words
    if "no word appears twice" in prompt or "no repeated words" in prompt or "no word repeats" in prompt:
        checks_done += 1
        lower_words = [w.lower() for w in words]
        if len(lower_words) != len(set(lower_words)):
            dupes = set(w for w in lower_words if lower_words.count(w) > 1)
            return {"passed": False, "score": 0.0, "feedback": f"Repeated words: {dupes}"}

    # If we verified at least one constraint and none failed → PASS
    if checks_done > 0:
        return {"passed": True, "score": 1.0, "feedback": f"Correct! All {checks_done} constraint(s) verified."}

    # Can't handle this exercise type programmatically
    return None


def grade_exercise(exercise: dict, response: str, brain, force_local: bool = False) -> dict:
    """Grade Chloe's response to an exercise.

    Returns: {passed, score (0.0-1.0), feedback}
    """
    level = exercise.get("level", 1)

    # Language precision: always try programmatic grading first
    if exercise.get("competency") == "language_precision":
        result = _grade_language_precision(exercise, response)
        if result is not None:
            return result
        # Fall through to standard grading if programmatic check can't handle it

    grading_type = exercise.get("grading_type", "rubric")

    if grading_type == "exact_match":
        return _grade_exact_match(exercise, response)
    elif grading_type == "pattern":
        return _grade_pattern(exercise, response)
    elif grading_type == "code_exec":
        result = _grade_code_exec(exercise, response)
        # If test cases were invalid (all NameErrors), fall back to llm_verify
        if result.get("score") == -1.0 and result.get("feedback") == "test_cases_invalid":
            logger.info("code_exec test cases invalid, falling back to llm_verify")
            return _grade_llm_verify(exercise, response, brain, level=level, force_local=force_local)
        return result
    elif grading_type == "llm_verify":
        return _grade_llm_verify(exercise, response, brain, level=level, force_local=force_local)
    elif grading_type == "rubric":
        return _grade_rubric(exercise, response, brain, level=level, force_local=force_local)
    else:
        return _grade_rubric(exercise, response, brain, level=level, force_local=force_local)


def _grade_exact_match(exercise: dict, response: str) -> dict:
    """Grade by exact match (case-insensitive, stripped)."""
    expected = str(exercise.get("expected_answer", "")).strip().lower()
    actual = response.strip().lower()
    # Also check if the answer appears anywhere in the response
    passed = expected == actual or expected in actual
    return {
        "passed": passed,
        "score": 1.0 if passed else 0.0,
        "feedback": "Correct!" if passed else f"Expected '{expected}', got '{actual[:100]}'",
    }


def _grade_pattern(exercise: dict, response: str) -> dict:
    """Grade by regex pattern match."""
    pattern = exercise.get("pattern", "")
    if not pattern:
        return {"passed": False, "score": 0.0, "feedback": "No pattern to match against"}
    passed = bool(re.search(pattern, response, re.IGNORECASE))
    return {
        "passed": passed,
        "score": 1.0 if passed else 0.0,
        "feedback": "Correct!" if passed else f"Response didn't match expected pattern",
    }


def _grade_code_exec(exercise: dict, response: str) -> dict:
    """Grade by executing code and checking test cases.

    Supports two formats:
    - test_cases: list of (input, expected) tuples (seed exercises)
    - test_cases_raw: list of assert statement strings (LLM-generated exercises)
    """
    raw_tests = exercise.get("test_cases_raw", [])
    test_cases = exercise.get("test_cases", [])

    if not raw_tests and not test_cases:
        return {"passed": False, "score": 0.0, "feedback": "No test cases"}

    # Extract function from response
    code = response.strip()
    if not code:
        return {"passed": False, "score": 0.0, "feedback": "Empty response"}
    # Remove markdown code fences if present
    code = re.sub(r"^```(?:python)?\s*\n?", "", code)
    code = re.sub(r"\n?```\s*$", "", code)

    # Raw assert-based grading (from LLM-generated exercises)
    if raw_tests:
        # First, try to extract setup code from the reference solution.
        # LLM-generated test cases often reference variables (rl, cache, pm, etc.)
        # that are instantiated in the solution but not in the assert statements.
        ref_solution = exercise.get("expected_answer", "") or ""
        setup_lines = []
        for line in ref_solution.splitlines():
            stripped = line.strip()
            # Grab variable assignments that create objects (likely test setup)
            # e.g. "rl = RateLimiter(10)" or "cache = LRUCache(3)"
            if "=" in stripped and not stripped.startswith(("def ", "class ", "#", "return ")):
                if "(" in stripped and not stripped.startswith("assert "):
                    setup_lines.append(stripped)
        setup_code = "\n".join(setup_lines)

        passed_count = 0
        name_errors = 0
        feedback_parts = []
        for assert_stmt in raw_tests:
            try:
                namespace = {}
                exec(code, namespace)
                exec(assert_stmt, namespace)
                passed_count += 1
            except AssertionError:
                feedback_parts.append(f"Failed: {assert_stmt[:80]}")
            except NameError:
                # Retry with setup code from reference solution injected
                if setup_code:
                    try:
                        namespace = {}
                        exec(code, namespace)
                        exec(setup_code, namespace)
                        exec(assert_stmt, namespace)
                        passed_count += 1
                        continue
                    except AssertionError:
                        feedback_parts.append(f"Failed: {assert_stmt[:80]}")
                        continue
                    except Exception:
                        pass
                name_errors += 1
                feedback_parts.append(f"NameError on {assert_stmt[:40]}")
            except Exception as e:
                feedback_parts.append(f"Error on {assert_stmt[:40]}: {str(e)[:60]}")

        # If more than half of tests hit NameError, the test cases are broken —
        # fall back to LLM verification instead of penalizing the student
        if name_errors > len(raw_tests) / 2:
            return {"passed": False, "score": -1.0, "feedback": "test_cases_invalid"}

        total = len(raw_tests)
        score = passed_count / total if total > 0 else 0.0
        passed = score >= 0.8
        feedback = "All tests passed!" if passed else "; ".join(feedback_parts[:3])
        return {"passed": passed, "score": score, "feedback": feedback}

    # Tuple-based grading (from seed exercises)
    passed_count = 0
    feedback_parts = []

    for test_input, expected_output in test_cases:
        try:
            # Create isolated namespace
            namespace = {}
            exec(code, namespace)

            # Find the function (first callable that isn't a builtin)
            func = None
            for name, obj in namespace.items():
                if callable(obj) and not name.startswith("_"):
                    func = obj
                    break

            if func is None:
                feedback_parts.append("Could not find function in response")
                continue

            # Ensure test_input is wrapped in parens for function call
            call_expr = test_input if test_input.startswith("(") else f"({test_input})"
            result = eval(f"func{call_expr}", {"func": func})
            if result == expected_output:
                passed_count += 1
            else:
                feedback_parts.append(f"Input {test_input}: expected {expected_output}, got {result}")
        except Exception as e:
            feedback_parts.append(f"Input {test_input}: error — {str(e)[:80]}")

    score = passed_count / len(test_cases)
    passed = score >= 0.8  # Allow 1 failure in 5 tests
    feedback = "All tests passed!" if passed else "; ".join(feedback_parts[:3])

    return {"passed": passed, "score": score, "feedback": feedback}


def _grade_llm_verify(exercise: dict, response: str, brain, level: int = 1, force_local: bool = False) -> dict:
    """Grade using LLM to verify against known answer.

    L8+ exercises use external grading (Haiku via Poe) instead of local model
    to prevent self-grading bias (same model generating and grading).
    """
    expected = exercise.get("expected_answer", "")
    prompt = f"""You are grading an exercise response.

QUESTION: {exercise['prompt']}
CORRECT ANSWER: {expected}
STUDENT RESPONSE: {response}

Is the student's response correct? Output ONLY:
CORRECT: yes or no
FEEDBACK: one sentence of feedback"""

    # L8+: external grading. Reasoning/coding L11+ use Sonnet for accuracy.
    competency = exercise.get("competency", "")
    if force_local:
        tier = "local"
    elif competency in ("reasoning", "coding") and level >= 11:
        tier = "deep"
    elif level >= 8:
        tier = "fast"
    else:
        tier = _grading_tier("llm_verify")
    result = brain.think(
        prompt=prompt,
        system="You are a strict but fair grader. Grade based on correctness, not style.",
        tier=tier,
        max_tokens=200,
        temperature=0.1,
    )

    text = result.get("text", "")
    thinking = result.get("thinking", "")
    full_text = text + "\n" + thinking if thinking else text
    passed = "correct: yes" in full_text.lower() or "yes" in full_text.lower().split("correct:")[-1][:20] if "correct:" in full_text.lower() else False
    feedback_match = re.search(r"FEEDBACK:\s*(.+)", full_text)
    feedback = feedback_match.group(1).strip() if feedback_match else text[:150].strip() or "Graded by LLM"

    return {"passed": passed, "score": 1.0 if passed else 0.0, "feedback": feedback}


def _grade_rubric(exercise: dict, response: str, brain, level: int = 1, force_local: bool = False) -> dict:
    """Grade using rubric-based LLM evaluation.

    L8+ exercises use external grading (Haiku via Poe) instead of local model
    to prevent self-grading bias (same model generating and grading).
    """
    rubric = exercise.get("rubric", "")
    hidden = exercise.get("hidden_dynamic", "")
    threshold = exercise.get("pass_threshold", 2)

    prompt = f"""You are grading a response to an exercise.

EXERCISE: {exercise['prompt']}
{"REFERENCE (for your grading use only): " + hidden if hidden else ""}

STUDENT RESPONSE: {response}

Grade each rubric item as 0 (not met) or 1 (met):
{rubric}

Output in this exact format:
SCORES: <comma-separated 0s and 1s, e.g., 1,0,1,1,0>
TOTAL: <sum of scores>
PASSED: <yes if total >= {threshold}, no otherwise>
FEEDBACK: <one constructive sentence>"""

    # L8+: external grading. Reasoning/coding L11+ use Sonnet for accuracy.
    # force_local uses fewer tokens — rubric output is short structured text
    competency = exercise.get("competency", "")
    if force_local:
        tier = "local"
    elif competency in ("reasoning", "coding") and level >= 11:
        tier = "deep"
    elif level >= 8:
        tier = "fast"
    else:
        tier = _grading_tier()
    max_tokens = 80 if force_local else 300  # SCORES/TOTAL/PASSED/FEEDBACK needs ~60 tokens
    try:
        result = brain.think(
            prompt=prompt,
            system="You are a strict but fair grader. Grade each rubric item as 0 or 1. Output ONLY: SCORES, TOTAL, PASSED, FEEDBACK.",
            tier=tier,
            max_tokens=max_tokens,
            temperature=0.1,
        )
    except Exception as e:
        # Timeout or model error during grind — treat as failed grade, don't crash
        return {"passed": False, "score": 0.0, "feedback": f"Grading timed out: {type(e).__name__}"}

    text = result.get("text", "")
    # Also check thinking text — Qwen3 may put structured output there
    thinking = result.get("thinking", "")
    full_text = text + "\n" + thinking if thinking else text

    # Parse scores — search both response and thinking text
    scores_match = re.search(r"SCORES:\s*([\d,\s]+)", full_text)
    total_match = re.search(r"TOTAL:\s*(\d+)", full_text)
    passed_match = re.search(r"PASSED:\s*(yes|no)", full_text, re.IGNORECASE)
    feedback_match = re.search(r"FEEDBACK:\s*(.+)", full_text)

    if scores_match:
        scores = [int(s.strip()) for s in scores_match.group(1).split(",") if s.strip().isdigit()]
        total = sum(scores)
        max_score = len(scores) if scores else 1
    elif total_match:
        total = int(total_match.group(1))
        max_score = threshold + 2  # Rough estimate
    else:
        # Fallback: count how many rubric items the LLM said "1" or "met" for
        ones = len(re.findall(r'\b1\b', full_text))
        zeros = len(re.findall(r'\b0\b', full_text))
        if ones + zeros > 0:
            total = ones
            max_score = ones + zeros
        else:
            total = 0
            max_score = 1

    if passed_match:
        passed = passed_match.group(1).lower() == "yes"
    else:
        passed = total >= threshold

    score = total / max_score if max_score > 0 else 0.0
    feedback = feedback_match.group(1).strip() if feedback_match else text[:150].strip() or "Graded by rubric"

    return {"passed": passed, "score": min(score, 1.0), "feedback": feedback}


def record_result(competencies: dict, competency: str, level: int, grade: dict):
    """Record an exercise result and check for level advancement."""
    comp = competencies["competencies"][competency]
    comp["total_exercises"] += 1
    competencies["total_exercises"] = competencies.get("total_exercises", 0) + 1

    if grade["passed"]:
        comp["total_passed"] += 1
        comp["consecutive_passes"] += 1

        # Check for level advancement: gradual scaling
        # L1-5: 3 passes, L6-10: 4, L11-20: 5, L21-35: 6, L36+: 7
        if level <= 5:
            passes_required = 3
        elif level <= 10:
            passes_required = 4
        elif level <= 20:
            passes_required = 5
        elif level <= 35:
            passes_required = 6
        else:
            passes_required = 7
        if comp["consecutive_passes"] >= passes_required and comp["current_level"] < level:
            comp["current_level"] = level
            comp["consecutive_passes"] = 0
            comp["level_history"].append({
                "level": level,
                "mastered_at": datetime.now().strftime("%Y-%m-%d"),
                "exercises_needed": comp["total_exercises"],
            })
            logger.info(f"ADVANCEMENT: {competency} now at Level {level}!")
    else:
        comp["consecutive_passes"] = 0

    # Record in exercise history (keep last 50)
    history = competencies.get("exercise_history", [])
    history.append({
        "competency": competency,
        "level": level,
        "passed": grade["passed"],
        "score": grade["score"],
        "timestamp": datetime.now().isoformat(),
    })
    competencies["exercise_history"] = history[-50:]

    save_competencies(competencies)


def record_failure(competency: str, level: int, exercise: dict, response: str, grade: dict):
    """Record a failed exercise for use in laboratory mode experiments."""
    try:
        failures = []
        if FAILURES_PATH.exists():
            failures = json.loads(FAILURES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        failures = []

    failures.append({
        "competency": competency,
        "level": level,
        "prompt": exercise.get("prompt", "")[:500],
        "response": response[:500],
        "feedback": grade.get("feedback", ""),
        "timestamp": datetime.now().isoformat(),
    })

    # Keep last 20 failures
    failures = failures[-20:]
    FAILURES_PATH.write_text(
        json.dumps(failures, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def get_recent_failures(limit: int = 5) -> list:
    """Get recent exercise failures for laboratory mode context."""
    try:
        if FAILURES_PATH.exists():
            failures = json.loads(FAILURES_PATH.read_text(encoding="utf-8"))
            return failures[-limit:]
    except (json.JSONDecodeError, FileNotFoundError):
        pass
    return []


def check_phase_advancement(competencies: dict) -> Optional[str]:
    """Check if Chloe qualifies for the next developmental phase.

    Returns new phase name, or None if no advancement.
    """
    current = competencies["overall_phase"]
    levels = {k: v["current_level"] for k, v in competencies["competencies"].items()}
    total_ex = competencies.get("total_exercises", 0)

    if current == "infant":
        if all(l >= 1 for l in levels.values()) and total_ex >= 100:
            return "toddler"

    elif current == "toddler":
        at_2_plus = sum(1 for l in levels.values() if l >= 2)
        at_3_plus = sum(1 for l in levels.values() if l >= 3)
        if at_2_plus >= 5 and at_3_plus >= 1 and total_ex >= 300:
            return "child"

    elif current == "child":
        at_3_plus = sum(1 for l in levels.values() if l >= 3)
        at_4_plus = sum(1 for l in levels.values() if l >= 4)
        if at_3_plus >= 5 and at_4_plus >= 2 and total_ex >= 500:
            return "adolescent"

    elif current == "adolescent":
        at_4_plus = sum(1 for l in levels.values() if l >= 4)
        if at_4_plus >= 5 and total_ex >= 1000:
            return "adult"

    elif current == "adult":
        at_6_plus = sum(1 for l in levels.values() if l >= 6)
        at_7_plus = sum(1 for l in levels.values() if l >= 7)
        if at_6_plus >= 8 and at_7_plus >= 5 and total_ex >= 2000:
            return "expert"

    elif current == "expert":
        at_8_plus = sum(1 for l in levels.values() if l >= 8)
        at_9_plus = sum(1 for l in levels.values() if l >= 9)
        if at_8_plus >= 8 and at_9_plus >= 5 and total_ex >= 4000:
            return "sage"

    elif current == "sage":
        at_12_plus = sum(1 for l in levels.values() if l >= 12)
        if at_12_plus >= 7 and total_ex >= 8000:
            return "virtuoso"

    elif current == "virtuoso":
        at_18_plus = sum(1 for l in levels.values() if l >= 18)
        if at_18_plus >= 7 and total_ex >= 15000:
            return "transcendent"

    return None


def advance_phase(competencies: dict, new_phase: str):
    """Apply phase advancement."""
    competencies["overall_phase"] = new_phase
    competencies["phase_started"] = datetime.now().strftime("%Y-%m-%d")
    save_competencies(competencies)
    logger.info(f"PHASE ADVANCEMENT: Chloe is now in '{new_phase}' phase!")


def format_progress_report(competencies: dict) -> str:
    """Format a human-readable progress report for Bill's email."""
    phase = competencies["overall_phase"]
    since = competencies.get("phase_started", "unknown")
    total = competencies.get("total_exercises", 0)
    today_count = 0
    today_passed = 0
    today_details = {}

    # Count today's exercises from history
    today = datetime.now().strftime("%Y-%m-%d")
    for entry in competencies.get("exercise_history", []):
        if entry.get("timestamp", "").startswith(today):
            today_count += 1
            if entry.get("passed"):
                today_passed += 1
            comp = entry["competency"]
            if comp not in today_details:
                today_details[comp] = {"passed": 0, "failed": 0}
            if entry.get("passed"):
                today_details[comp]["passed"] += 1
            else:
                today_details[comp]["failed"] += 1

    lines = [
        f"DEVELOPMENTAL PROGRESS",
        f"Phase: {phase.title()} (since {since})",
        f"Total exercises: {total} | Today: {today_count} ({today_passed} passed)",
        "",
    ]

    # Per-competency progress
    header = f"{'Competency':<24s} {'Level':>5s}   {'Progress':<12s} {'Today':>8s}"
    lines.append(header)
    lines.append("-" * len(header))

    for name, state in sorted(competencies["competencies"].items()):
        level = state["current_level"]
        consec = state["consecutive_passes"]
        target_level = min(level + 1, MAX_LEVEL)
        needed = 5 if target_level >= 9 else 3
        bar = "#" * min(consec, needed) + "." * max(0, needed - consec)
        level_str = f"L{level}"
        progress = f"{bar} {consec}/{needed}"

        today_info = today_details.get(name, {})
        today_str = ""
        if today_info.get("passed"):
            today_str += f"+{today_info['passed']}P"
        if today_info.get("failed"):
            today_str += f" -{today_info['failed']}F"

        lines.append(f"{name:<24s} {level_str:>5s}   {progress:<12s} {today_str:>8s}")

    return "\n".join(lines)
