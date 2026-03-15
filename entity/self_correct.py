"""Self-correction loop for exercises with verifiable grading.

Pattern: generate → grade → if fail, correct with specific feedback → re-grade

Instead of brute-force retrying, this feeds the exact failure reason back to the
model so it can make targeted corrections. Works with any competency that has
programmatic/verifiable grading:
- language_precision: "Line 1 has 8 words, need 6"
- coding: "Failed: assert add(2, 3) == 5" or "Input (5): expected 25, got 24"

Reusable by any competency that has programmatic/verifiable grading.
"""

from entity.curriculum import grade_exercise


def _build_correction_prompt(exercise, answer, feedback, competency):
    """Build a competency-appropriate correction prompt."""
    base = (
        f"Your previous answer FAILED verification.\n"
        f"SPECIFIC FAILURE: {feedback}\n\n"
        f"Original task:\n{exercise['prompt']}\n\n"
        f"Your previous answer:\n{answer}\n\n"
    )

    if competency == "coding":
        base += (
            "Debug your code by tracing through the failing test case step by step. "
            "Identify the exact line where the logic goes wrong, then fix it. "
            "Output ONLY the corrected Python function — no explanation, no markdown fences."
        )
    elif competency == "language_precision":
        base += (
            "Rewrite your COMPLETE answer, fixing the problem above. "
            "Verify each constraint by counting carefully before writing each line. "
            "Output ONLY the corrected text — no commentary, no explanations."
        )
    else:
        base += (
            "Rewrite your COMPLETE answer, fixing the problem above. "
            "Output ONLY the corrected answer — no commentary, no explanations."
        )

    return base


def self_correct(brain, exercise, initial_answer, grade_result,
                 system_prompt, tier, max_retries=2):
    """Attempt to self-correct a failed answer using specific grading feedback.

    Args:
        brain: Brain instance for LLM calls
        exercise: The exercise dict (with prompt, competency, etc.)
        initial_answer: The model's first attempt
        grade_result: Grade result from grade_exercise() — must have 'feedback'
        system_prompt: System prompt for the model
        tier: Model tier to use for correction attempts
        max_retries: Maximum correction attempts (default 2)

    Returns:
        (final_answer, final_grade, total_cost, retries_used)
    """
    if grade_result["passed"]:
        return initial_answer, grade_result, 0, 0

    competency = exercise.get("competency", "")
    answer = initial_answer
    total_cost = 0
    grade = grade_result

    for retry in range(max_retries):
        feedback = grade["feedback"]

        correction_prompt = _build_correction_prompt(
            exercise, answer, feedback, competency
        )

        try:
            response = brain.think(
                prompt=correction_prompt,
                system=system_prompt,
                tier=tier,
                max_tokens=8192,
                temperature=0.2,  # Lower temp for corrections
            )
            answer = response.get("text", "").strip()
            total_cost += response.get("cost", 0)
        except Exception as e:
            print(f"  Self-correction attempt {retry + 1} error: {e}")
            break

        # Re-grade the corrected answer
        grade = grade_exercise(exercise, answer, brain)

        if grade["passed"]:
            print(f"  Self-correction PASSED on attempt {retry + 1}")
            return answer, grade, total_cost, retry + 1

        print(f"  Self-correction attempt {retry + 1}: {grade['feedback'][:80]}")

    return answer, grade, total_cost, max_retries
