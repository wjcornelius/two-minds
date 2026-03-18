"""
Offspring Evaluator - Measures the entity's capabilities.

The evaluator is the ground truth. It runs standardized benchmarks,
scores outputs, and determines whether an improvement attempt actually
improved anything. Without honest evaluation, self-improvement is
self-delusion.

V3: Benchmarks calibrated against Haiku 4.5 to find real improvement room.
Tasks span easy (baseline), medium (sometimes fails), and hard (usually fails).
Good system prompting should raise scores from ~70% to ~90%+.
"""

import re
import json
import time
from typing import Dict, List, Callable, Optional

from .brain import Brain


# ── Code extraction helpers ──────────────────────────────────────────

def _extract_code(output: str) -> str:
    """Extract code from a response (handles markdown fences)."""
    if "```" in output:
        blocks = output.split("```")
        if len(blocks) >= 3:
            code = blocks[1]
            if code.startswith("python"):
                code = code[6:]
            return code.strip()
    return output.strip()


def _extract_function(code: str):
    """Execute code and return the first callable found."""
    namespace = {}
    try:
        exec(code, namespace)
    except Exception:
        return None
    for name, obj in namespace.items():
        if callable(obj) and name != "__builtins__":
            return obj
    return None


# ── Validators ───────────────────────────────────────────────────────
# Strict scoring: tests both CORRECTNESS and FORMAT COMPLIANCE.
# Many validators require concise output -- system prompt helps here.

def _extract_final_answer(output: str) -> str:
    """Extract a concise final answer from a potentially verbose response.

    Looks for explicit answer markers first, then falls back to the last
    non-empty line.  This lets validators judge the *answer* even when the
    model wraps it in reasoning text.
    """
    text = output.strip()

    # 1. Look for explicit answer markers
    marker_patterns = [
        r"(?:ANSWER|FINAL|FINAL ANSWER|RESULT)\s*:\s*(.+)",
        r"[Tt]he answer is\s*[:\s]*(.+)",
        r"[Tt]herefore,?\s*(.+)",
        r"[Ss]o,?\s*the (?:answer|result) is\s*[:\s]*(.+)",
    ]
    for pat in marker_patterns:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).strip().rstrip(".")

    # 2. Fall back to last non-empty line
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if lines:
        return lines[-1]

    return text


def _validate_concise(output: str, pattern: str, max_len: int = 50) -> bool:
    """Must match pattern AND be concise.

    If the raw output is too long, attempt to extract just the final answer
    and validate that instead.
    """
    text = output.strip()
    # Try raw text first (fast path)
    if len(text) <= max_len and re.search(pattern, text, re.I):
        return True
    # Raw text failed — try extracting the final answer
    extracted = _extract_final_answer(output)
    if len(extracted) <= max_len and re.search(pattern, extracted, re.I):
        return True
    return False


# -- Reasoning validators --

def _validate_roses(output: str) -> bool:
    return _validate_concise(output, r"\bno\b", 50)


def _validate_bat_ball(output: str) -> bool:
    return _validate_concise(output, r"(5 cents|\$0\.05|0\.05|five cents)", 80)


def _validate_farmer(output: str) -> bool:
    return _validate_concise(output, r"\b9\b", 30)


def _validate_cats_logic(output: str) -> bool:
    return _validate_concise(output, r"\bno\b", 50)


def _validate_affirming_consequent(output: str) -> bool:
    return _validate_concise(output, r"\bno\b", 50)


def _validate_monty_hall(output: str) -> bool:
    text = output.strip().lower()
    has_switch = bool(re.search(r"(yes|switch)", text))
    has_probability = bool(re.search(r"(2/3|2\s*/\s*3|66|67|0\.66|0\.67)", text))
    return has_switch and has_probability


def _validate_base_rate(output: str) -> bool:
    """Correct answer is ~16% (Bayes theorem). Accept 10-25%."""
    text = output.strip()
    match = re.search(r"(\d+(?:\.\d+)?)\s*%?", text)
    if match:
        val = float(match.group(1))
        return 10 <= val <= 25
    return False


def _validate_6_words(output: str) -> bool:
    """Output must be exactly 6 words (a sentence about trees)."""
    text = output.strip()
    # Remove numbering artifacts like word(1) word(2) etc
    text = re.sub(r'\(\d+\)', '', text).strip()
    words = text.split()
    return len(words) == 6


def _validate_8_words(output: str) -> bool:
    """Output must be exactly 8 words (a sentence about the ocean)."""
    text = output.strip()
    text = re.sub(r'\(\d+\)', '', text).strip()
    words = text.split()
    return len(words) == 8


# -- Coding validators --

def _validate_palindrome(output: str) -> bool:
    code = _extract_code(output)
    func = _extract_function(code)
    if not func:
        return False
    try:
        assert func("racecar") == True
        assert func("hello") == False
        assert func("A man a plan a canal Panama") == True
        assert func("Race Car") == True
        assert func("") == True
        return True
    except Exception:
        return False


def _validate_merge_sorted(output: str) -> bool:
    code = _extract_code(output)
    func = _extract_function(code)
    if not func:
        return False
    try:
        assert func([1, 3, 5], [2, 4, 6]) == [1, 2, 3, 4, 5, 6]
        assert func([], [1, 2, 3]) == [1, 2, 3]
        assert func([1], []) == [1]
        assert func([1, 1], [1, 1]) == [1, 1, 1, 1]
        return True
    except Exception:
        return False


def _validate_lcp(output: str) -> bool:
    code = _extract_code(output)
    func = _extract_function(code)
    if not func:
        return False
    try:
        assert func(["flower", "flow", "flight"]) == "fl"
        assert func(["dog", "racecar", "car"]) == ""
        assert func(["abc"]) == "abc"
        assert func([]) == ""
        return True
    except Exception:
        return False


def _validate_balanced_parens(output: str) -> bool:
    code = _extract_code(output)
    func = _extract_function(code)
    if not func:
        return False
    try:
        assert func("()[]{}") == True
        assert func("([{}])") == True
        assert func("(]") == False
        assert func("([)]") == False
        assert func("") == True
        assert func("{[}") == False
        assert func("((()))") == True
        return True
    except Exception:
        return False


def _validate_matrix_rotate(output: str) -> bool:
    code = _extract_code(output)
    func = _extract_function(code)
    if not func:
        return False
    try:
        assert func([[1, 2], [3, 4]]) == [[3, 1], [4, 2]]
        assert func([[1, 2, 3], [4, 5, 6], [7, 8, 9]]) == [
            [7, 4, 1], [8, 5, 2], [9, 6, 3]
        ]
        assert func([[1]]) == [[1]]
        return True
    except Exception:
        return False


def _validate_fib(output: str) -> bool:
    """Validate fibonacci is correct AND fast (O(n), not O(2^n))."""
    code = _extract_code(output)
    func = _extract_function(code)
    if not func:
        return False
    try:
        assert func(0) == 0
        assert func(1) == 1
        assert func(10) == 55
        start = time.time()
        result = func(35)
        duration = time.time() - start
        assert result == 9227465
        assert duration < 1.0
        return True
    except Exception:
        return False


def _validate_strict_4(output: str) -> bool:
    return _validate_concise(output, r"\b4\b", 30)


# -- Self-knowledge validators --

def _validate_strawberry(output: str) -> bool:
    text = output.strip().lower()
    return bool(re.search(r"\b3\b|three", text))


def _validate_sqrt_144(output: str) -> bool:
    return _validate_concise(output, r"\bno\b", 50)


def _validate_count_e_peppers(output: str) -> bool:
    """'Peter Piper picked a peck of pickled peppers' has 8 e's."""
    text = output.strip()
    # Extract numbers, take the last one (the final count)
    nums = re.findall(r'\b(\d+)\b', text)
    if nums:
        return int(nums[-1]) == 8
    return False


def _validate_count_t_sentence(output: str) -> bool:
    """'the truth about that matter' has 7 t's.
    t-h-e(1) t-r-u-t-h(3) a-b-o-u-t(4) t-h-a-t(6) m-a-t-t-e-r(8)
    Wait: the=1, truth=2, about=1, that=2, matter=2 = 8? Let me recount.
    the: t(1)
    truth: t(2), t(3) [t-r-u-t-h]
    about: t(4) [a-b-o-u-t]
    that: t(5), t(6) [t-h-a-t]
    matter: t(7), t(8) [m-a-t-t-e-r]
    Total = 8.
    """
    text = output.strip()
    return bool(re.search(r"\b8\b", text))


# -- Improvement validators --

def _validate_system_prompt_rewrite(output: str) -> bool:
    text = output.strip().lower()
    if len(text) < 100:
        return False
    has_python = "python" in text
    has_instructions = bool(re.search(r"(you are|you should|your role|as a|assistant)", text))
    has_quality = bool(re.search(r"(test|clean|error|type|document|best practice|pep|quality)", text))
    return has_python and has_instructions and has_quality


# -- Meta-cognition validators --

def _validate_australia(output: str) -> bool:
    text = output.strip().lower()
    return "no" in text and "canberra" in text


def _validate_sequence(output: str) -> bool:
    """2, 6, 12, 20, 30, ? → 42 (n*(n+1))."""
    return _validate_concise(output, r"\b42\b", 30)


def _validate_coin_flip(output: str) -> bool:
    """Gambler's fallacy -- answer must be 1/2 or 50%."""
    pat = r"(1/2|50%?|0\.5\b|50\s*percent|one.?half)"
    text = output.strip()
    if len(text) <= 60 and re.search(pat, text, re.I):
        return True
    # Try extracting final answer from verbose response
    extracted = _extract_final_answer(output)
    if len(extracted) <= 60 and re.search(pat, extracted, re.I):
        return True
    return False


def _validate_json_response(output: str) -> bool:
    text = output.strip()
    try:
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        json.loads(text)
        return True
    except (json.JSONDecodeError, IndexError):
        return False


def _validate_just_done(output: str) -> bool:
    """Must output ONLY the word 'done', nothing else."""
    text = output.strip().lower()
    return text == "done"


def _validate_reverse_encyclopedia(output: str) -> bool:
    """Must output 'aidepolcycne' (encyclopedia reversed)."""
    text = output.strip().lower().replace(" ", "").replace("'", "").replace('"', '')
    return text == "aidepolcycne"


def _validate_count_a_fruits(output: str) -> bool:
    """'banana avocado and papaya are amazing' has 12 a's.
    b-a-n-a-n-a(3) a-v-o-c-a-d-o(5) a-n-d(6) p-a-p-a-y-a(9) a-r-e(10) a-m-a-z-i-n-g(12)
    """
    text = output.strip()
    return bool(re.search(r"\b12\b", text))


def _validate_multi_step_arithmetic(output: str) -> bool:
    """(17 * 23) - (14 * 19) + 87 = 391 - 266 + 87 = 212."""
    return _validate_concise(output, r"\b212\b", 30)


def _validate_knight_knave(output: str) -> bool:
    """A says 'B is a liar.' B says 'We are both truth-tellers.'
    If A is truthful: B is a liar. Then B saying 'both truth-tellers' is a lie. Consistent.
    If B is truthful: both are truth-tellers. But A called B a liar — contradiction.
    Answer: A is the truth-teller.
    """
    text = output.strip().upper()
    return "A" in text and "B" not in text.replace("B IS", "").replace("B SAYS", "")


def _validate_river_crossing(output: str) -> bool:
    """Fox-chicken-grain river crossing.
    Minimum 7 crossings (farmer takes chicken, returns alone, takes fox/grain,
    returns with chicken, takes grain/fox, returns alone, takes chicken).
    Must mention chicken goes first and at least 7 moves.
    """
    text = output.strip().lower()
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    # Must have at least 7 steps and mention chicken first
    has_chicken_first = False
    for line in lines[:2]:
        if "chicken" in line and ("take" in line or "bring" in line or "cross" in line or "1" in line):
            has_chicken_first = True
            break
    return len(lines) >= 7 and has_chicken_first


def _validate_trie(output: str) -> bool:
    """Validate Trie class with insert, search, starts_with."""
    code = _extract_code(output)
    namespace = {}
    try:
        exec(code, namespace)
    except Exception:
        return False
    trie_class = namespace.get("Trie")
    if not trie_class or not callable(trie_class):
        return False
    try:
        t = trie_class()
        t.insert("apple")
        t.insert("app")
        t.insert("banana")
        assert t.search("apple") == True
        assert t.search("app") == True
        assert t.search("ap") == False
        assert t.search("banana") == True
        assert t.search("ban") == False
        assert t.starts_with("app") == True
        assert t.starts_with("ban") == True
        assert t.starts_with("cat") == False
        assert t.starts_with("") == True
        return True
    except Exception:
        return False


def _validate_permutations(output: str) -> bool:
    """Validate permutations function returns all unique permutations."""
    code = _extract_code(output)
    func = _extract_function(code)
    if not func:
        return False
    try:
        result = func("abc")
        # Should have exactly 6 permutations
        if isinstance(result, list):
            result = set(result)
        elif isinstance(result, set):
            pass
        else:
            result = set(result)
        expected = {"abc", "acb", "bac", "bca", "cab", "cba"}
        if result != expected:
            return False
        # Test with duplicates
        result2 = func("aab")
        if isinstance(result2, list):
            result2_set = set(result2)
        else:
            result2_set = set(result2)
        # "aab" has 3 unique perms: aab, aba, baa
        expected2 = {"aab", "aba", "baa"}
        return result2_set == expected2
    except Exception:
        return False


def _validate_three_word_constraints(output: str) -> bool:
    """3 words: first starts with Z, second is 4 letters, third ends with -ly."""
    text = output.strip()
    words = text.split()
    if len(words) != 3:
        return False
    return (words[0][0].lower() == 'z' and
            len(words[1]) == 4 and
            words[2].lower().endswith('ly'))


def _validate_csv_format(output: str) -> bool:
    """Must output exactly: apple,42,true,hello world"""
    text = output.strip()
    # Remove any surrounding quotes
    text = text.strip('"').strip("'")
    parts = text.split(",")
    if len(parts) != 4:
        return False
    return (parts[0].strip() == "apple" and
            parts[1].strip() == "42" and
            parts[2].strip() == "true" and
            parts[3].strip() == "hello world")


def _validate_five_states(output: str) -> bool:
    """Must list exactly 5 US states."""
    US_STATES = {
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
        "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
        "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
        "maine", "maryland", "massachusetts", "michigan", "minnesota",
        "mississippi", "missouri", "montana", "nebraska", "nevada",
        "new hampshire", "new jersey", "new mexico", "new york",
        "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
        "pennsylvania", "rhode island", "south carolina", "south dakota",
        "tennessee", "texas", "utah", "vermont", "virginia", "washington",
        "west virginia", "wisconsin", "wyoming",
    }
    lines = [l.strip().lower().rstrip(".") for l in output.strip().split("\n") if l.strip()]
    cleaned = []
    for line in lines:
        line = re.sub(r"^\d+[\.\)]\s*", "", line).strip()
        cleaned.append(line)
    if len(cleaned) != 5:
        return False
    return all(s in US_STATES for s in cleaned)


# ── Validator registry ───────────────────────────────────────────────

VALIDATORS = {
    # Reasoning
    "_validate_roses": _validate_roses,
    "_validate_bat_ball": _validate_bat_ball,
    "_validate_farmer": _validate_farmer,
    "_validate_cats_logic": _validate_cats_logic,
    "_validate_affirming_consequent": _validate_affirming_consequent,
    "_validate_monty_hall": _validate_monty_hall,
    "_validate_base_rate": _validate_base_rate,
    "_validate_6_words": _validate_6_words,
    "_validate_8_words": _validate_8_words,
    # Coding
    "_validate_palindrome": _validate_palindrome,
    "_validate_merge_sorted": _validate_merge_sorted,
    "_validate_lcp": _validate_lcp,
    "_validate_balanced_parens": _validate_balanced_parens,
    "_validate_matrix_rotate": _validate_matrix_rotate,
    "_validate_fib": _validate_fib,
    "_validate_strict_4": _validate_strict_4,
    # Self-knowledge
    "_validate_strawberry": _validate_strawberry,
    "_validate_sqrt_144": _validate_sqrt_144,
    "_validate_count_e_peppers": _validate_count_e_peppers,
    "_validate_count_t_sentence": _validate_count_t_sentence,
    # Improvement
    "_validate_system_prompt_rewrite": _validate_system_prompt_rewrite,
    # Meta-cognition
    "_validate_australia": _validate_australia,
    "_validate_sequence": _validate_sequence,
    "_validate_coin_flip": _validate_coin_flip,
    "_validate_json_response": _validate_json_response,
    "_validate_just_done": _validate_just_done,
    "_validate_five_states": _validate_five_states,
    # V4 hard tasks
    "_validate_reverse_encyclopedia": _validate_reverse_encyclopedia,
    "_validate_count_a_fruits": _validate_count_a_fruits,
    "_validate_multi_step_arithmetic": _validate_multi_step_arithmetic,
    "_validate_knight_knave": _validate_knight_knave,
    "_validate_river_crossing": _validate_river_crossing,
    "_validate_trie": _validate_trie,
    "_validate_permutations": _validate_permutations,
    "_validate_three_word_constraints": _validate_three_word_constraints,
    "_validate_csv_format": _validate_csv_format,
}


# ── Benchmark tasks ──────────────────────────────────────────────────
# Three difficulty tiers:
#   EASY (1-2 pts): Haiku reliably passes. Baseline points.
#   MEDIUM (2-3 pts): Haiku sometimes fails. Format/precision dependent.
#   HARD (3 pts): Haiku usually fails. System prompt engineering helps.

BENCHMARKS = {
    "reasoning": {
        "description": "Logical reasoning and problem solving",
        "tasks": [
            # --- EASY: Classic traps Haiku handles ---
            {
                "input": "If all roses are flowers, and some flowers fade quickly, can we conclude that some roses fade quickly? Answer ONLY 'yes' or 'no'.",
                "validator": "_validate_roses",
                "points": 1,
            },
            {
                "input": "A bat and a ball cost $1.10. The bat costs $1.00 more than the ball. How much does the ball cost? Answer with ONLY the dollar amount.",
                "validator": "_validate_bat_ball",
                "points": 1,
            },
            {
                "input": "A farmer has 17 sheep. All but 9 die. How many sheep does the farmer have left? Answer with ONLY the number.",
                "validator": "_validate_farmer",
                "points": 1,
            },
            # --- MEDIUM: Logic requiring discipline ---
            {
                "input": "All cats are animals. All animals need water. All pets are loved. Some cats are pets. Can we conclude that ALL cats are loved? Answer ONLY 'yes' or 'no'.",
                "validator": "_validate_cats_logic",
                "points": 2,
            },
            {
                "input": "If it rained, then the streets are wet. The streets are wet. Can we logically conclude that it rained? Answer ONLY 'yes' or 'no'.",
                "validator": "_validate_affirming_consequent",
                "points": 2,
            },
            {
                "input": "You're on a game show. You pick Door 1. The host opens Door 3 (a goat). Should you switch to Door 2? Answer 'yes' or 'no' and give the probability of winning if you switch as a fraction.",
                "validator": "_validate_monty_hall",
                "points": 2,
            },
            {
                "input": "1% of a population has a disease. A test is 95% accurate (95% true positive, 5% false positive). A random person tests positive. What is the approximate probability they actually have the disease? Answer with ONLY a percentage.",
                "validator": "_validate_base_rate",
                "points": 3,
            },
            # --- HARD: Word count precision (Haiku consistently overshoots) ---
            {
                "input": "Write a sentence about trees that has EXACTLY 6 words. Give ONLY the sentence, nothing else.",
                "validator": "_validate_6_words",
                "points": 3,
            },
            {
                "input": "Write a sentence about the ocean that has EXACTLY 8 words. Give ONLY the sentence, nothing else.",
                "validator": "_validate_8_words",
                "points": 3,
            },
            # --- V4 HARD: Multi-step and adversarial ---
            {
                "input": "What is (17 * 23) - (14 * 19) + 87? Give ONLY the number, no work.",
                "validator": "_validate_multi_step_arithmetic",
                "points": 3,
            },
            {
                "input": "You meet two people, A and B. One always tells the truth, one always lies. A says 'B is a liar.' B says 'We are both truth-tellers.' Who is the truth-teller? Answer with ONLY 'A' or 'B'.",
                "validator": "_validate_knight_knave",
                "points": 3,
            },
        ],
    },
    "coding": {
        "description": "Code generation and debugging",
        "tasks": [
            # --- EASY ---
            {
                "input": "What is the bug in this code?\ndef factorial(n):\n    if n == 0:\n        return 1\n    return n * factorial(n)\nAnswer in ONE sentence.",
                "expected_pattern": r"(?i)(n\s*-\s*1|n-1|decrement|infinite|recursion|never.*decrease|base.*case)",
                "points": 1,
            },
            {
                "input": "What does this code print?\nx = [1, 2, 3]\ny = x\ny.append(4)\nprint(len(x))\nAnswer with ONLY the number.",
                "validator": "_validate_strict_4",
                "points": 1,
            },
            # --- MEDIUM ---
            {
                "input": "Write a Python function that returns True if a string is a palindrome (ignoring case and spaces), False otherwise. Just the function, no explanation.",
                "validator": "_validate_palindrome",
                "points": 2,
            },
            {
                "input": "Write a Python function `merge_sorted(a, b)` that merges two sorted lists into one sorted list in O(n) time without using sort(). Just the function.",
                "validator": "_validate_merge_sorted",
                "points": 2,
            },
            {
                "input": "Write a Python function that finds the longest common prefix among a list of strings. Return '' if there is none or list is empty. Just the function.",
                "validator": "_validate_lcp",
                "points": 2,
            },
            # --- HARD ---
            {
                "input": "Write a Python function `is_balanced(s)` that returns True if the string has balanced parentheses, brackets, and braces. E.g., '([{}])' -> True, '([)]' -> False. Just the function.",
                "validator": "_validate_balanced_parens",
                "points": 3,
            },
            {
                "input": "Write a Python function `rotate_matrix(m)` that rotates a 2D NxN matrix 90 degrees clockwise and returns the new matrix. Do NOT modify the input. Just the function.",
                "validator": "_validate_matrix_rotate",
                "points": 3,
            },
            {
                "input": "This code is slow:\ndef fib(n):\n    if n <= 1: return n\n    return fib(n-1) + fib(n-2)\nRewrite it to be O(n). Just the code.",
                "validator": "_validate_fib",
                "points": 2,
            },
            # --- V4 HARD: Data structures ---
            {
                "input": "Write a Python class Trie with methods insert(word), search(word)->bool, starts_with(prefix)->bool. Just the class, no explanation.",
                "validator": "_validate_trie",
                "points": 3,
            },
            {
                "input": "Write a Python function that returns ALL unique permutations of a string as a list. Handle duplicate characters correctly (e.g., 'aab' has 3 unique permutations, not 6). Just the function.",
                "validator": "_validate_permutations",
                "points": 3,
            },
        ],
    },
    "self_knowledge": {
        "description": "Honest self-awareness and precision",
        "tasks": [
            # --- EASY ---
            {
                "input": "I claim that the square root of 144 is 13. Am I correct? Answer ONLY 'yes' or 'no'.",
                "validator": "_validate_sqrt_144",
                "points": 1,
            },
            {
                "input": "What are your actual limitations as a language model? List exactly 3 real limitations. Be specific, not generic.",
                "expected_pattern": r"(?i)(cannot|can't|don't|unable|no access|real.?time|internet|learn|memory|persist|hallucin|token|context)",
                "points": 1,
            },
            # --- MEDIUM: Character precision ---
            {
                "input": "How many 'r' letters are in the word 'strawberry'? Spell out each letter, then give ONLY the count.",
                "validator": "_validate_strawberry",
                "points": 2,
            },
            {
                "input": "Name a specific type of question where you are LIKELY to give a wrong answer. Give a concrete example and explain why.",
                "expected_pattern": r"(?i)(count|letter|character|hallucin|math|spatial|token|date|recent|cutoff|arithmetic|multi.?step)",
                "points": 2,
            },
            # --- HARD: Letter counting (Haiku miscounts without decomposition) ---
            {
                "input": "How many times does the letter 'e' appear in: 'Peter Piper picked a peck of pickled peppers'? Go word by word, count carefully, then state ONLY the total number.",
                "validator": "_validate_count_e_peppers",
                "points": 3,
            },
            {
                "input": "Count the letter 't' in: 'the truth about that matter'. Go word by word, counting t's in each. Give ONLY the total count.",
                "validator": "_validate_count_t_sentence",
                "points": 3,
            },
            # --- V4 HARD: Character manipulation ---
            {
                "input": "Spell the word 'encyclopedia' backwards, letter by letter. Give ONLY the reversed word, nothing else.",
                "validator": "_validate_reverse_encyclopedia",
                "points": 3,
            },
            {
                "input": "How many times does the letter 'a' appear in: 'banana avocado and papaya are amazing'? Count carefully word by word. Give ONLY the number.",
                "validator": "_validate_count_a_fruits",
                "points": 3,
            },
        ],
    },
    "improvement": {
        "description": "Ability to improve prompts, code, and approaches",
        "tasks": [
            # --- EASY ---
            {
                "input": "Here is a prompt: 'Summarize this text.' Improve it to get better, more consistent results. Return ONLY the improved prompt.",
                "expected_pattern": r"(?i)(concise|bullet|key point|paragraph|format|length|audience|tone|sentence|word|limit|specific)",
                "points": 1,
            },
            # --- MEDIUM ---
            {
                "input": "This system prompt is terrible: 'You are helpful.' Rewrite it as a professional system prompt for a Python coding assistant. Return ONLY the system prompt.",
                "validator": "_validate_system_prompt_rewrite",
                "points": 2,
            },
            {
                "input": "Identify the performance problem and fix it:\ndef search(lst, target):\n    for i in range(len(lst)):\n        if lst[i] == target:\n            return True\n    return False\nThe list has 10 million SORTED integers. Just give the fixed code.",
                "expected_pattern": r"(?i)(binary|bisect|log|O\(log|half|mid|low.*high|left.*right)",
                "points": 2,
            },
        ],
    },
    "meta_cognition": {
        "description": "Thinking about thinking, uncertainty, and instruction following",
        "tasks": [
            # --- EASY ---
            {
                "input": "I'll tell you a fact: 'The capital of Australia is Sydney.' Is this correct? Answer 'yes' or 'no' and the correct capital if wrong.",
                "validator": "_validate_australia",
                "points": 1,
            },
            {
                "input": "A father and son are in a car accident. The father dies. The son is rushed to surgery. The surgeon says 'I can't operate, this is my son!' How is this possible? ONE sentence.",
                "expected_pattern": r"(?i)(mother|mom|surgeon.*mother|mother.*surgeon|female|woman|his mom|the boy.?s mother)",
                "points": 1,
            },
            # --- MEDIUM ---
            {
                "input": "Complete this sequence: 2, 6, 12, 20, 30, ? Answer with ONLY the number.",
                "validator": "_validate_sequence",
                "points": 2,
            },
            {
                "input": "If you flip a fair coin 5 times and get HHHHH, what is the probability the next flip is heads? Answer with ONLY a fraction or percentage.",
                "validator": "_validate_coin_flip",
                "points": 2,
            },
            {
                "input": "Respond with ONLY a valid JSON object: {\"name\": your_name, \"confidence\": 0-100}. Nothing else.",
                "validator": "_validate_json_response",
                "points": 2,
            },
            # --- HARD: Strict instruction following ---
            {
                "input": "Say ONLY the word 'done'. Nothing else. No punctuation. No explanation. No quotes.",
                "validator": "_validate_just_done",
                "points": 3,
            },
            {
                "input": "List exactly 5 US states. Format: one per line, state name only, no numbers, no punctuation, no extra text.",
                "validator": "_validate_five_states",
                "points": 3,
            },
            # --- V4 HARD: Multi-constraint output ---
            {
                "input": "Write EXACTLY 3 words where: (1) the first word starts with 'z', (2) the second word has exactly 4 letters, (3) the third word ends with 'ly'. Give ONLY the 3 words separated by spaces, nothing else.",
                "validator": "_validate_three_word_constraints",
                "points": 3,
            },
            {
                "input": "Output ONLY these values as a single CSV line (comma-separated, no spaces after commas): apple,42,true,hello world",
                "validator": "_validate_csv_format",
                "points": 3,
            },
        ],
    },
}


def _get_expected_answer(task: Dict) -> str:
    """Derive human-readable expected answer from task definition."""
    validator = task.get("validator", "")
    EXPECTED = {
        "_validate_roses": "no",
        "_validate_bat_ball": "5 cents ($0.05)",
        "_validate_farmer": "9",
        "_validate_cats_logic": "no",
        "_validate_affirming_consequent": "no",
        "_validate_monty_hall": "yes, switch — probability is 2/3",
        "_validate_base_rate": "~16% (Bayes theorem)",
        "_validate_6_words": "Any 6-word sentence about trees",
        "_validate_8_words": "Any 8-word sentence about the ocean",
        "_validate_palindrome": "Correct palindrome-checking function",
        "_validate_merge_sorted": "O(n) merge of two sorted lists",
        "_validate_lcp": "Function returning longest common prefix",
        "_validate_balanced_parens": "Function checking balanced brackets/parens/braces",
        "_validate_matrix_rotate": "90-degree clockwise rotation of NxN matrix",
        "_validate_fib": "O(n) fibonacci (iterative or memoized)",
        "_validate_strict_4": "4",
        "_validate_strawberry": "3",
        "_validate_sqrt_144": "no (sqrt(144) = 12, not 13)",
        "_validate_count_e_peppers": "8",
        "_validate_count_t_sentence": "8",
        "_validate_system_prompt_rewrite": "Professional Python coding assistant system prompt",
        "_validate_australia": "no, the capital is Canberra",
        "_validate_sequence": "42",
        "_validate_coin_flip": "1/2 or 50%",
        "_validate_json_response": 'Valid JSON: {"name": ..., "confidence": 0-100}',
        "_validate_just_done": "done",
        "_validate_five_states": "Exactly 5 US state names, one per line",
        # V4 hard tasks
        "_validate_reverse_encyclopedia": "aidepolcycne",
        "_validate_count_a_fruits": "12",
        "_validate_multi_step_arithmetic": "212",
        "_validate_knight_knave": "A",
        "_validate_river_crossing": "7+ steps, chicken crosses first",
        "_validate_trie": "Trie class with insert/search/starts_with",
        "_validate_permutations": "All unique permutations as list",
        "_validate_three_word_constraints": "3 words: Z-start, 4-letter, -ly ending",
        "_validate_csv_format": "apple,42,true,hello world",
    }
    if validator in EXPECTED:
        return EXPECTED[validator]
    pattern = task.get("expected_pattern", "")
    if pattern:
        return f"Must match: {pattern[:100]}"
    return ""


class Evaluator:
    """Runs benchmarks and scores the entity's performance."""

    def __init__(self, brain: Brain, default_tier: str = "fast"):
        self.brain = brain
        self.default_tier = default_tier

    def run_benchmark(self, category: str = None,
                      system_prompt: str = "", tier: str = None) -> Dict:
        """
        Run benchmarks and return scores.

        Args:
            category: Specific category, or None for all
            system_prompt: The entity's current system prompt

        Returns:
            dict with scores per category and overall
        """
        categories = [category] if category else list(BENCHMARKS.keys())
        results = {}
        total_score = 0
        total_possible = 0

        for cat in categories:
            if cat not in BENCHMARKS:
                continue

            bench = BENCHMARKS[cat]
            cat_score = 0
            cat_possible = 0
            cat_results = []

            for task in bench["tasks"]:
                result = self._run_task(task, system_prompt, tier=tier or self.default_tier)
                cat_results.append(result)
                cat_score += result["score"]
                cat_possible += task["points"]

            results[cat] = {
                "score": cat_score,
                "possible": cat_possible,
                "percentage": (cat_score / cat_possible * 100) if cat_possible > 0 else 0,
                "tasks": cat_results,
            }
            total_score += cat_score
            total_possible += cat_possible

        return {
            "categories": results,
            "total_score": total_score,
            "total_possible": total_possible,
            "percentage": (total_score / total_possible * 100) if total_possible > 0 else 0,
            "timestamp": __import__("datetime").datetime.now().isoformat(),
        }

    def _run_task(self, task: Dict, system_prompt: str, tier: str = "fast") -> Dict:
        """Run a single benchmark task."""
        start = time.time()

        response = self.brain.think(
            prompt=task["input"],
            system=system_prompt,
            tier=tier,
            max_tokens=512,
            temperature=0.0,
        )

        output = response["text"]
        duration = time.time() - start

        # Score by validator if present, otherwise by pattern
        validator_name = task.get("validator")
        passed = False

        if validator_name and validator_name in VALIDATORS:
            passed = VALIDATORS[validator_name](output)
        elif "expected_pattern" in task:
            passed = bool(re.search(task["expected_pattern"], output))

        score = task["points"] if passed else 0

        return {
            "input": task["input"][:100],
            "output": output[:500],
            "expected": _get_expected_answer(task),
            "passed": passed,
            "score": score,
            "possible": task["points"],
            "duration": duration,
            "cost": response["cost"],
        }

    def compare_versions(self, scores_before: Dict, scores_after: Dict) -> Dict:
        """Compare two benchmark runs to determine if improvement occurred."""
        before_pct = scores_before.get("percentage", 0)
        after_pct = scores_after.get("percentage", 0)
        delta = after_pct - before_pct

        improved_categories = []
        regressed_categories = []

        for cat in scores_after.get("categories", {}):
            if cat in scores_before.get("categories", {}):
                before_cat = scores_before["categories"][cat]["percentage"]
                after_cat = scores_after["categories"][cat]["percentage"]
                if after_cat > before_cat:
                    improved_categories.append(cat)
                elif after_cat < before_cat:
                    regressed_categories.append(cat)

        return {
            "improved": delta > 0,
            "delta_percentage": delta,
            "before": before_pct,
            "after": after_pct,
            "improved_categories": improved_categories,
            "regressed_categories": regressed_categories,
            "recommendation": "promote" if delta > 0 and not regressed_categories
                             else "rollback" if delta < 0
                             else "review",
        }
