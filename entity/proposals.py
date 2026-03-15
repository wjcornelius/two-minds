"""
Chloe's Proposal System — Self-Modification with Sandbox Validation.

When Chloe wants to change her own code, the flow is:
  1. She reads the file she wants to modify (SAFE)
  2. She generates a modified version with improvements (SAFE — just thinking)
  3. She creates a proposal with the diff (SAFE — writing to proposals/)
  4. The sandbox validates the change:
     - Syntax check (py_compile)
     - Import check (subprocess)
     - Full benchmark comparison (no regression allowed)
  5. If sandbox passes: auto-apply, git commit
  6. If sandbox fails: auto-reject with reason, Chloe learns from it
  7. Bill can still manually review via `python agent.py --review`

All code changes go through git. All are reversible. The sandbox is the
automated gate — Bill doesn't need to be in the loop. Protected files
(safety.py, audit.py, sandbox.py) cannot be self-modified.

Stage 1: Text descriptions of desired changes (v1, kept for compat)
Stage 2: Actual code diffs with test results (v2)
Stage 3: Sandbox-validated auto-apply (v3, THIS VERSION)
"""

import os
import json
import subprocess
import difflib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

PROPOSALS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "proposals"
)
PROJECT_ROOT = Path(__file__).parent.parent


def _ensure_dir():
    os.makedirs(PROPOSALS_DIR, exist_ok=True)


def write_proposal(
    title: str,
    category: str,
    description: str,
    evidence: str,
    suggested_changes: List[Dict],
    priority: str = "normal",
    source_experiment: str = None,
) -> str:
    """
    Stage 1 proposal — text description of a desired change.
    Kept for backward compatibility.
    """
    _ensure_dir()
    now = datetime.now()
    proposal_id = f"prop_{now.strftime('%Y%m%d_%H%M%S')}"

    proposal = {
        "id": proposal_id,
        "timestamp": now.isoformat(),
        "stage": 1,
        "title": title,
        "category": category,
        "description": description,
        "evidence": evidence,
        "suggested_changes": suggested_changes,
        "priority": priority,
        "source_experiment": source_experiment,
        "status": "pending",
        "reviewed_by": None,
        "review_notes": None,
    }

    filepath = os.path.join(PROPOSALS_DIR, f"{proposal_id}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(proposal, f, indent=2, ensure_ascii=False)

    return proposal_id


def write_code_proposal(
    title: str,
    target_file: str,
    original_code: str,
    modified_code: str,
    reasoning: str,
    test_results: str = "",
    category: str = "code",
    priority: str = "normal",
    source_cycle: str = "",
) -> str:
    """
    Stage 2/3 proposal — actual code diff with test results.

    Chloe has read the file, generated a modification, and optionally
    tested it. This creates a reviewable proposal with a unified diff.

    Args:
        title: Short description of the change
        target_file: Path relative to project root (e.g., 'entity/brain.py')
        original_code: The current file contents
        modified_code: Chloe's proposed modification
        reasoning: Why she wants this change
        test_results: Results of testing the change (if any)
        category: Type of change
        priority: low/normal/high
        source_cycle: Agent cycle that produced this

    Returns:
        Proposal ID
    """
    _ensure_dir()
    now = datetime.now()
    proposal_id = f"prop_{now.strftime('%Y%m%d_%H%M%S')}"

    # Generate unified diff
    original_lines = original_code.splitlines(keepends=True)
    modified_lines = modified_code.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        original_lines, modified_lines,
        fromfile=f"a/{target_file}",
        tofile=f"b/{target_file}",
        lineterm="",
    ))
    diff_text = "\n".join(diff)

    # Count changes
    additions = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))

    proposal = {
        "id": proposal_id,
        "timestamp": now.isoformat(),
        "stage": 2,
        "title": title,
        "category": category,
        "target_file": target_file,
        "reasoning": reasoning,
        "diff": diff_text,
        "additions": additions,
        "deletions": deletions,
        "original_code": original_code,
        "modified_code": modified_code,
        "test_results": test_results,
        "priority": priority,
        "source_cycle": source_cycle,
        "status": "pending",
        "reviewed_by": None,
        "review_notes": None,
    }

    filepath = os.path.join(PROPOSALS_DIR, f"{proposal_id}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(proposal, f, indent=2, ensure_ascii=False)

    return proposal_id


def get_pending_proposals() -> List[Dict]:
    """Get all proposals that haven't been reviewed yet."""
    _ensure_dir()
    proposals = []

    for filename in sorted(os.listdir(PROPOSALS_DIR)):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(PROPOSALS_DIR, filename)
        with open(filepath, "r", encoding="utf-8") as f:
            prop = json.load(f)
        if prop.get("status") == "pending":
            proposals.append(prop)

    return proposals


def get_all_proposals(limit: int = 20) -> List[Dict]:
    """Get all proposals, most recent first."""
    _ensure_dir()
    proposals = []

    for filename in sorted(os.listdir(PROPOSALS_DIR), reverse=True):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(PROPOSALS_DIR, filename)
        with open(filepath, "r", encoding="utf-8") as f:
            proposals.append(json.load(f))
        if len(proposals) >= limit:
            break

    return proposals


def review_proposal(proposal_id: str, status: str,
                    reviewed_by: str, notes: str = "") -> bool:
    """
    Mark a proposal as reviewed.

    Args:
        proposal_id: The proposal to review
        status: approved, rejected, implemented
        reviewed_by: 'bill', 'claude', or 'bill_and_claude'
        notes: Review notes
    """
    filepath = os.path.join(PROPOSALS_DIR, f"{proposal_id}.json")
    if not os.path.exists(filepath):
        return False

    with open(filepath, "r", encoding="utf-8") as f:
        prop = json.load(f)

    prop["status"] = status
    prop["reviewed_by"] = reviewed_by
    prop["review_notes"] = notes
    prop["reviewed_at"] = datetime.now().isoformat()

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(prop, f, indent=2, ensure_ascii=False)

    return True


def apply_proposal(proposal_id: str) -> dict:
    """
    Apply an approved Stage 2 proposal — write the modified code to disk.

    Only works on proposals with status 'approved' and stage >= 2.
    After applying, commits the change via git.

    Returns dict with success status and details.
    """
    filepath = os.path.join(PROPOSALS_DIR, f"{proposal_id}.json")
    if not os.path.exists(filepath):
        return {"success": False, "error": f"Proposal {proposal_id} not found"}

    with open(filepath, "r", encoding="utf-8") as f:
        prop = json.load(f)

    if prop.get("status") not in ("approved", "sandbox_approved"):
        return {"success": False, "error": f"Proposal status is '{prop.get('status')}', not 'approved' or 'sandbox_approved'"}

    if prop.get("stage", 1) < 2:
        return {"success": False, "error": "Stage 1 proposals don't have code to apply"}

    target_file = prop.get("target_file", "")
    modified_code = prop.get("modified_code", "")

    if not target_file or not modified_code:
        return {"success": False, "error": "Proposal missing target_file or modified_code"}

    # Resolve path within sandbox
    full_path = (PROJECT_ROOT / target_file).resolve()
    if not str(full_path).startswith(str(PROJECT_ROOT.resolve())):
        return {"success": False, "error": "Target file is outside sandbox"}

    # Write the modified code
    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(modified_code, encoding="utf-8")
    except Exception as e:
        return {"success": False, "error": f"Failed to write file: {e}"}

    # Git commit
    try:
        subprocess.run(
            ["git", "add", target_file],
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=30,
        )
        commit_msg = (
            f"Chloe self-mod: {prop.get('title', 'code change')}\n\n"
            f"Proposal: {proposal_id}\n"
            f"File: {target_file}\n"
            f"Reasoning: {prop.get('reasoning', '')[:200]}\n"
            f"+{prop.get('additions', 0)}/-{prop.get('deletions', 0)} lines"
        )
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=30,
        )
        git_output = result.stdout + result.stderr
    except Exception as e:
        git_output = f"Git error: {e}"

    # Update proposal status
    prop["status"] = "implemented"
    prop["implemented_at"] = datetime.now().isoformat()
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(prop, f, indent=2, ensure_ascii=False)

    return {
        "success": True,
        "file": target_file,
        "additions": prop.get("additions", 0),
        "deletions": prop.get("deletions", 0),
        "git_output": git_output,
    }


def format_proposal_for_review(proposal: dict) -> str:
    """Format a proposal as human-readable text for Bill's review."""
    lines = []
    lines.append(f"{'=' * 60}")
    lines.append(f"PROPOSAL: {proposal.get('title', 'Untitled')}")
    lines.append(f"ID: {proposal.get('id', '?')}")
    lines.append(f"Stage: {proposal.get('stage', 1)}")
    lines.append(f"Priority: {proposal.get('priority', 'normal')}")
    lines.append(f"Time: {proposal.get('timestamp', '?')}")
    lines.append(f"{'=' * 60}")

    if proposal.get("stage", 1) >= 2:
        # Stage 2: show diff
        lines.append(f"\nFile: {proposal.get('target_file', '?')}")
        lines.append(f"Changes: +{proposal.get('additions', 0)} / "
                      f"-{proposal.get('deletions', 0)} lines")
        lines.append(f"\nReasoning: {proposal.get('reasoning', '?')}")

        if proposal.get("test_results"):
            lines.append(f"\nTest Results:\n{proposal['test_results']}")

        if proposal.get("diff"):
            lines.append(f"\nDiff:\n{proposal['diff']}")
    else:
        # Stage 1: show description
        lines.append(f"\nDescription: {proposal.get('description', '?')}")
        lines.append(f"Evidence: {proposal.get('evidence', '?')}")
        if proposal.get("suggested_changes"):
            lines.append("\nSuggested changes:")
            for change in proposal["suggested_changes"]:
                lines.append(f"  - {change.get('file', '?')}: "
                             f"{change.get('change', '?')}")

    lines.append(f"\n{'=' * 60}")
    return "\n".join(lines)


def sandbox_validate_and_apply(
    proposal_id: str,
    sandbox,
    baseline_scores: Dict = None,
    system_prompt: str = "",
    verbose: bool = True,
) -> Dict:
    """
    Auto-validate a code proposal via sandbox and apply if it passes.
    No human approval needed — the sandbox IS the gate.

    Args:
        proposal_id: The proposal to validate
        sandbox: A CodeSandbox instance
        baseline_scores: Current benchmark scores for comparison
        system_prompt: Current system prompt for benchmarking
        verbose: Print progress

    Returns:
        {success, applied, reason, scores, git_output}
    """
    filepath = os.path.join(PROPOSALS_DIR, f"{proposal_id}.json")
    if not os.path.exists(filepath):
        return {"success": False, "applied": False,
                "reason": f"Proposal {proposal_id} not found"}

    with open(filepath, "r", encoding="utf-8") as f:
        prop = json.load(f)

    target_file = prop.get("target_file", "")
    modified_code = prop.get("modified_code", "")

    if not target_file or not modified_code:
        return {"success": False, "applied": False,
                "reason": "Proposal missing target_file or modified_code"}

    if verbose:
        print(f"  [sandbox] Validating proposal: {prop.get('title', '?')}")
        print(f"  [sandbox] Target: {target_file}")

    # Run sandbox validation
    result = sandbox.test_code_change(
        target_file=target_file,
        modified_code=modified_code,
        baseline_scores=baseline_scores,
        system_prompt=system_prompt,
        verbose=verbose,
    )

    if result["passed"]:
        # Auto-approve and apply
        prop["status"] = "sandbox_approved"
        prop["reviewed_by"] = "sandbox"
        prop["review_notes"] = (
            f"Sandbox validated: {len(result['gates_passed'])} gates passed. "
            f"{result['reason']}"
        )
        if result.get("scores"):
            prop["sandbox_scores"] = {
                "percentage": result["scores"].get("percentage", 0),
                "total_score": result["scores"].get("total_score", 0),
            }
        if result.get("baseline_scores"):
            prop["baseline_scores"] = {
                "percentage": result["baseline_scores"].get("percentage", 0),
                "total_score": result["baseline_scores"].get("total_score", 0),
            }
        prop["reviewed_at"] = datetime.now().isoformat()

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(prop, f, indent=2, ensure_ascii=False)

        # Apply the change (writes file + git commits)
        apply_result = apply_proposal(proposal_id)

        return {
            "success": apply_result.get("success", False),
            "applied": apply_result.get("success", False),
            "reason": result["reason"],
            "scores": result.get("scores"),
            "baseline_scores": result.get("baseline_scores"),
            "duration": result["duration"],
            "gates_passed": result["gates_passed"],
            "git_output": apply_result.get("git_output", ""),
        }
    else:
        # Auto-reject with reason
        prop["status"] = "sandbox_rejected"
        prop["reviewed_by"] = "sandbox"
        prop["review_notes"] = (
            f"Sandbox rejected: {result['reason']}. "
            f"Gates passed: {result['gates_passed']}"
        )
        prop["reviewed_at"] = datetime.now().isoformat()

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(prop, f, indent=2, ensure_ascii=False)

        return {
            "success": True,  # The process succeeded, but change was rejected
            "applied": False,
            "reason": result["reason"],
            "scores": result.get("scores"),
            "baseline_scores": result.get("baseline_scores"),
            "duration": result["duration"],
            "gates_passed": result["gates_passed"],
            "git_output": "",
        }


def generate_proposals_from_experiments(brain, experiment_summary: Dict) -> List[str]:
    """
    After experiments, let Chloe reflect on whether she needs
    code changes (not just prompt changes) to grow further.

    Returns list of proposal IDs created.
    """
    learnings = experiment_summary.get("learnings", [])
    recent = experiment_summary.get("recent_experiments", [])

    if not learnings and not recent:
        return []

    # Build context
    learnings_text = "\n".join(
        f"  - [{l['category']}] {l['insight']}"
        for l in learnings[:5]
    ) if learnings else "No learnings yet."

    experiments_text = "\n".join(
        f"  - Strategy: {e.get('strategy')}, Result: {e.get('result')}, "
        f"Delta: {e.get('delta', 0):+.1f}%, Learning: {e.get('learning', '')[:100]}"
        for e in recent[:5]
    ) if recent else "No experiments yet."

    prompt = (
        "You are Chloe, a self-improving AI. Based on your experiments "
        "and learnings, do you need any CODE CHANGES (not just prompt "
        "changes) to grow further?\n\n"
        f"Your learnings:\n{learnings_text}\n\n"
        f"Recent experiments:\n{experiments_text}\n\n"
        "Think about:\n"
        "- Do you need new benchmark tasks to measure capabilities "
        "your current benchmarks miss?\n"
        "- Do you need changes to your evaluation system?\n"
        "- Do you need new data structures in your memory?\n"
        "- Do you need changes to your experiment pipeline?\n"
        "- Do you need new tools or capabilities?\n\n"
        "If you have proposals, respond with a JSON array of objects:\n"
        '[{"title": "short title", "category": "benchmark|evaluator|'
        'strategy|architecture|memory|scanner|other", '
        '"description": "what you want and why", '
        '"evidence": "what data supports this", '
        '"suggested_changes": [{"file": "path", "change": "description", '
        '"reason": "why"}], "priority": "low|normal|high"}]\n\n'
        "If you have no proposals right now, respond with: NO_PROPOSALS"
    )

    from entity.budget import safe_tier
    response = brain.think(
        prompt=prompt,
        system="You are thoughtful and specific. Only propose changes "
               "you have evidence for. Don't propose changes for the "
               "sake of proposing changes.",
        tier=safe_tier("fast"),  # Haiku if affordable
        max_tokens=800,
        temperature=0.5,
    )

    text = response["text"].strip()
    if "NO_PROPOSALS" in text:
        return []

    # Parse proposals
    proposal_ids = []
    try:
        start = text.index("[")
        end = text.rindex("]") + 1
        proposals_data = json.loads(text[start:end])

        for prop_data in proposals_data[:3]:  # Max 3 per session
            pid = write_proposal(
                title=prop_data.get("title", "Untitled"),
                category=prop_data.get("category", "other"),
                description=prop_data.get("description", ""),
                evidence=prop_data.get("evidence", ""),
                suggested_changes=prop_data.get("suggested_changes", []),
                priority=prop_data.get("priority", "normal"),
            )
            proposal_ids.append(pid)

    except (ValueError, json.JSONDecodeError, KeyError):
        pass

    return proposal_ids
