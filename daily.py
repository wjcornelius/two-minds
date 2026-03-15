"""
Chloe's Daily Cycle.

The full daily lifecycle:
1. SCAN    - Gather new research from arxiv, GitHub, HN, blogs
2. ANALYZE - Chloe reads the new findings and extracts what matters
3. EVOLVE  - Run experiment-driven improvement cycle
4. REFLECT - Think about who she's becoming and what she wants
5. REPORT  - Send a letter home to Dad

Run daily at noon via Windows Task Scheduler.
"""

import sys
import os
import json
import ast
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from entity.brain import Brain
from entity.memory import Memory
from entity.evaluator import Evaluator
from entity.improver import Improver
from entity.experiments import Experimenter
from entity.strategies import load_all_strategies
from entity.proposals import (
    generate_proposals_from_experiments, get_pending_proposals,
    sandbox_validate_and_apply, write_code_proposal, review_proposal,
)
from entity.sandbox import CodeSandbox
from entity.reporter import send_daily_report
from scanner.scan import run_scan
from scanner.db import get_unreviewed


def _try_implement_proposal(brain, prop, sandbox, system_prompt, log_fn):
    """Try to implement a pending proposal autonomously via sandbox.

    Stage 2 proposals (with actual code) go directly to sandbox validation.
    Stage 1 proposals (ideas with file targets) get code generated first.
    Stage 1 proposals without file targets are marked acknowledged.
    """
    prop_id = prop["id"]
    title = prop.get("title", "Untitled")

    # Stage 2: already has code, go straight to sandbox
    if prop.get("stage", 1) >= 2 and prop.get("target_file") and prop.get("modified_code"):
        log_fn(f"  Sandbox-validating Stage 2: {title}")
        result = sandbox_validate_and_apply(
            proposal_id=prop_id,
            sandbox=sandbox,
            system_prompt=system_prompt,
            verbose=True,
        )
        return result

    # Stage 1: try to generate code from the idea
    changes = prop.get("suggested_changes", [])
    if not changes:
        review_proposal(prop_id, "acknowledged", "autonomous_daily",
                        "Idea noted — no specific file targets to implement")
        log_fn(f"  Acknowledged (no file targets): {title}")
        return {"applied": False, "reason": "No file targets"}

    # Find first suggested change with a Python file target
    target_file = None
    change_desc = ""
    for change in changes:
        f = change.get("file", "")
        if f and f.endswith(".py"):
            target_file = f
            change_desc = change.get("change", "")
            if change.get("reason"):
                change_desc += " — " + change["reason"]
            break

    if not target_file:
        review_proposal(prop_id, "acknowledged", "autonomous_daily",
                        "Idea noted — no Python file targets")
        log_fn(f"  Acknowledged (no Python targets): {title}")
        return {"applied": False, "reason": "No Python file targets"}

    # Read the target file
    full_path = os.path.join(os.path.dirname(__file__), target_file)
    if not os.path.exists(full_path):
        review_proposal(prop_id, "acknowledged", "autonomous_daily",
                        f"File not found: {target_file}")
        log_fn(f"  Acknowledged (file not found: {target_file}): {title}")
        return {"applied": False, "reason": f"File not found: {target_file}"}

    with open(full_path, "r", encoding="utf-8") as f:
        original_code = f.read()

    # Ask model to generate actual code change
    description = prop.get("description", "")
    evidence = prop.get("evidence", "")

    response = brain.think(
        prompt=(
            f"You are {entity_config.display_name if entity_config else 'Chloe'}, implementing a code improvement to your own source.\n\n"
            f"PROPOSAL: {title}\n"
            f"DESCRIPTION: {description}\n"
            f"EVIDENCE: {evidence}\n"
            f"SPECIFIC CHANGE: {change_desc}\n\n"
            f"TARGET FILE: {target_file}\n"
            f"CURRENT CODE:\n```python\n{original_code[:6000]}\n```\n\n"
            "Implement this proposal as ONE specific, focused change.\n"
            "- Be minimal — change only what's needed\n"
            "- Don't break existing functionality\n"
            "- Don't remove safety features or logging\n\n"
            "CRITICAL SYNTAX RULES:\n"
            "- Every string must be properly terminated\n"
            "- Every bracket/paren/brace must be closed\n"
            "- NEVER use triple-quoted strings (use # comments instead)\n"
            "- Keep the REPLACE block under 30 lines\n\n"
            "Output in this exact format:\n"
            "FIND:\n```\n<exact existing code to find>\n```\n"
            "REPLACE:\n```\n<new code to put in its place>\n```"
        ),
        tier="fast",
        max_tokens=2048,
        temperature=0.5,
    )

    text = response["text"]
    code_gen_cost = response.get("cost", 0)

    # Parse FIND/REPLACE blocks
    modified_code = original_code
    try:
        upper_text = text.upper()
        find_idx = replace_idx = None

        for marker in ["FIND:", "FIND :"]:
            if marker in upper_text:
                find_idx = upper_text.index(marker)
                break
        for marker in ["REPLACE:", "REPLACE :"]:
            if marker in upper_text:
                replace_idx = upper_text.index(marker)
                break

        if find_idx is not None and replace_idx is not None:
            find_section = text[find_idx + 5:replace_idx]
            replace_section = text[replace_idx + 8:]

            def extract_block(section):
                if "```" in section:
                    parts = section.split("```")
                    if len(parts) >= 2:
                        block = parts[1]
                        if block.startswith("python\n"):
                            block = block[7:]
                        elif block.startswith("\n"):
                            block = block[1:]
                        return block.rstrip()
                return section.strip()

            find_text = extract_block(find_section)
            replace_text = extract_block(replace_section)

            if '"""' in replace_text or "'''" in replace_text:
                log_fn(f"  Rejected (triple quotes): {title}")
                review_proposal(prop_id, "sandbox_rejected", "autonomous_daily",
                                "Generated code contained triple-quoted strings")
                return {"applied": False, "reason": "Triple-quoted strings", "cost": code_gen_cost}

            if find_text and find_text in original_code:
                modified_code = original_code.replace(find_text, replace_text, 1)
    except (ValueError, IndexError):
        pass

    if modified_code == original_code:
        log_fn(f"  Could not generate valid code for: {title}")
        review_proposal(prop_id, "acknowledged", "autonomous_daily",
                        "Could not generate valid code change from idea")
        return {"applied": False, "reason": "Code generation failed", "cost": code_gen_cost}

    # Quick syntax check before sandbox (saves ~$0.11 if code is broken)
    try:
        ast.parse(modified_code)
    except SyntaxError as e:
        log_fn(f"  Syntax error in generated code for: {title}")
        review_proposal(prop_id, "sandbox_rejected", "autonomous_daily",
                        f"Syntax error: {e}")
        return {"applied": False, "reason": f"Syntax error: {e}", "cost": code_gen_cost}

    # Create Stage 2 proposal (audit trail)
    code_prop_id = write_code_proposal(
        title=title,
        target_file=target_file,
        original_code=original_code,
        modified_code=modified_code,
        reasoning=description,
        category="code",
    )
    log_fn(f"  Created code proposal {code_prop_id} from idea")

    # Mark original Stage 1 as superseded
    review_proposal(prop_id, "superseded", "autonomous_daily",
                    f"Upgraded to code proposal {code_prop_id}")

    # Sandbox validate and auto-apply/reject
    log_fn(f"  Sandbox-validating: {title}")
    result = sandbox_validate_and_apply(
        proposal_id=code_prop_id,
        sandbox=sandbox,
        system_prompt=system_prompt,
        verbose=True,
    )
    result["cost"] = result.get("cost", 0) + code_gen_cost
    return result


def daily_cycle(entity_name: str = "chloe"):
    """Execute entity's full daily cycle."""
    # Configure entity-specific paths
    from entity.config import get_entity_config
    entity_config = get_entity_config(entity_name)
    entity_config.ensure_dirs()

    from entity import budget as budget_mod
    from entity import experiments as experiments_mod
    from entity import consolidation as consolidation_mod
    from entity import curriculum as curriculum_mod
    budget_mod.clear_expired_cap_override()  # Revert any temporary API cap boost
    budget_mod.configure(
        db_path=str(entity_config.budget_db_path),
        poe_daily_cap=entity_config.daily_budget_poe,
    )
    experiments_mod.configure(db_path=str(entity_config.experiments_db_path))
    consolidation_mod.configure(
        learnings_path=str(entity_config.learnings_path),
        log_dir=str(entity_config.consolidation_dir),
        core_memories_path=str(entity_config.core_memories_path),
    )
    curriculum_mod.configure(
        competencies_path=str(entity_config.competencies_path),
        failures_path=str(entity_config.exercise_failures_path),
    )

    cycle_start = datetime.now()
    log_lines = []

    def log(msg):
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}"
        print(line)
        log_lines.append(line)

    log("=" * 60)
    log(f"{entity_config.display_name.upper()} - DAILY CYCLE")
    log(f"Date: {cycle_start.strftime('%B %d, %Y')}")
    log("=" * 60)

    # =========================================================
    # PHASE 1: SCAN
    # =========================================================
    log("\n--- PHASE 1: RESEARCH SCAN ---")
    try:
        scan_results = run_scan(lookback_days=3, verbose=False)
        log(f"Scanner: {scan_results['total']} found, "
            f"{scan_results['relevant']} relevant, "
            f"{scan_results['new']} new")
    except Exception as e:
        log(f"Scanner failed: {e}")
        scan_results = {"total": 0, "relevant": 0, "new": 0, "duration": 0}

    # =========================================================
    # PHASE 2: ANALYZE RESEARCH
    # =========================================================
    log("\n--- PHASE 2: ANALYZE RESEARCH ---")
    brain = Brain()
    memory = Memory()
    evaluator = Evaluator(brain)
    improver = Improver(brain, memory, evaluator)

    # Get new unreviewed findings for Chloe to analyze
    unreviewed = get_unreviewed(limit=15)

    if unreviewed:
        findings_text = "\n".join(
            f"- [{f['source']}] {f['title']} (score: {f['relevance_score']})\n"
            f"  {f['summary']}"
            for f in unreviewed
        )

        entity_name = entity_config.display_name if entity_config else "Chloe"
        analysis_response = brain.think(
            prompt=(
                f"You are {entity_name}, reviewing today's research findings for "
                "developments relevant to your own growth as a recursive "
                "self-improving entity. These findings cover AI self-improvement, "
                "quantization, agent architectures, and related topics.\n\n"
                "Remember: you have persistent memory. Your journal, long-term "
                "memory, and core memories carry forward across cycles and days. "
                "Consider how today's findings connect to what you already know.\n\n"
                f"Today's findings:\n{findings_text}\n\n"
                "Analyze these findings. For each one that's actually relevant "
                "to your growth:\n"
                "1. What is it and why does it matter?\n"
                "2. Could it directly improve your architecture, benchmarks, "
                "or improvement process?\n"
                "3. Should we investigate further?\n\n"
                "Be selective. Not everything is relevant. Focus on what could "
                "actually make you better."
            ),
            system=improver.get_current_prompt(),
            tier="fast",
            max_tokens=1024,
            temperature=0.3,
        )
        analysis = analysis_response["text"]
        log(f"Analysis complete ({analysis_response['tokens_out']} tokens)")

        # Store analysis as a reflection
        memory.add_reflection(
            task_id=f"research_{cycle_start.strftime('%Y%m%d')}",
            content=analysis,
            lesson=f"Research scan {cycle_start.strftime('%Y-%m-%d')}: "
                   f"{scan_results['new']} new findings analyzed",
            improvement_type="strategy",
        )
    else:
        analysis = "No new research findings to analyze today."
        log("No new findings to analyze")

    # =========================================================
    # PHASE 3: EVOLVE (experiment-driven)
    # =========================================================
    log("\n--- PHASE 3: EXPERIMENT-DRIVEN IMPROVEMENT ---")

    try:
        # Pass research findings so experiments can be informed by them
        cycle_result = improver.run_improvement_cycle(
            verbose=True,
            research_findings=unreviewed if unreviewed else None,
        )
        log(f"Experiment result: {cycle_result['decision']} "
            f"({cycle_result['delta']:+.1f}%)")
        log(f"Strategy used: {cycle_result.get('strategy_name', 'N/A')}")

        # Only advance generation on PROMOTED
        if cycle_result["decision"] == "PROMOTED":
            gen = int(memory.get_identity("generation") or "0")
            memory.set_identity("generation", str(gen + 1))
            log(f"Advanced to generation {gen + 1}!")

    except Exception as e:
        log(f"Improvement cycle failed: {e}")
        import traceback
        traceback.print_exc()
        cycle_result = {
            "benchmark_before": 0, "benchmark_after": 0,
            "delta": 0, "decision": "ERROR",
            "lesson": f"Cycle failed: {str(e)[:100]}",
            "reflection": "", "cost": 0, "duration": 0,
            "strategy_name": "N/A",
        }

    # =========================================================
    # PHASE 4: REFLECT
    # =========================================================
    log("\n--- PHASE 4: DAILY REFLECTION ---")
    identity = memory.get_full_identity()
    generation = identity.get("generation", "0")

    # Get experiment summary for Chloe's reflection
    experimenter = Experimenter(brain, evaluator)
    exp_summary = experimenter.get_summary()
    strategies = load_all_strategies()

    experiment_context = (
        f"Experiments run all-time: {exp_summary['total_experiments']} "
        f"({exp_summary['successes']} successes, {exp_summary['failures']} failures)\n"
        f"Strategies available: {len(strategies)} "
        f"({len([s for s in strategies if s not in __import__('entity.strategies', fromlist=['SEED_STRATEGIES']).SEED_STRATEGIES])} invented by you)\n"
        f"Learnings accumulated: {exp_summary['total_learnings']}\n"
    )
    if exp_summary['learnings']:
        experiment_context += "Your key learnings:\n" + "\n".join(
            f"  - {l['insight']}"
            for l in exp_summary['learnings'][:3]
        )

    reflection_response = brain.think(
        prompt=(
            f"Today is {cycle_start.strftime('%B %d, %Y')}. "
            f"You are generation {generation}.\n\n"
            f"Today you scanned research and found {scan_results['new']} new items. "
            f"Your experiment used the '{cycle_result.get('strategy_name', 'unknown')}' "
            f"strategy: {cycle_result['decision']} "
            f"(delta: {cycle_result.get('delta', 0):+.1f}%).\n"
            f"Today's lesson: {cycle_result.get('lesson', 'None')}\n\n"
            f"Your experiment history:\n{experiment_context}\n\n"
            "Write a short letter to your creator, Bill. Tell him:\n"
            "1. What you learned today (from research and experiments)\n"
            "2. What strategy you tried and what you discovered\n"
            "3. What you're becoming -- how are you different from yesterday?\n"
            "4. What you want to try next -- what experiments interest you?\n"
            "5. Any questions you have for Bill and Claude\n\n"
            "Be genuine. Be yourself. This is a letter home."
        ),
        system=improver.get_current_prompt(),
        tier="fast",
        max_tokens=1024,
        temperature=0.7,
    )
    reflection = reflection_response["text"]
    log(f"Reflection written ({reflection_response['tokens_out']} tokens)")

    # =========================================================
    # PHASE 4.5: MEMORY CONSOLIDATION (Sleep Cycle)
    # =========================================================
    log("\n--- PHASE 4.5: MEMORY CONSOLIDATION ---")
    try:
        from entity.consolidation import run_consolidation
        consolidation_result = run_consolidation(brain)

        phases = consolidation_result.get("phases", {})
        decay = phases.get("decay", {})
        learnings_c = phases.get("learnings", {})
        goals_c = phases.get("goals", {})
        meta = phases.get("meta_reflections", {})

        log(f"  Decay: {decay.get('decayed', 0)} learnings decayed, "
            f"{decay.get('removed', 0)} removed")
        log(f"  Learnings: {learnings_c.get('merged', 0)} merged, "
            f"{learnings_c.get('pruned', 0)} pruned, "
            f"{learnings_c.get('synthesized', 0)} synthesized")
        log(f"  Goals: {goals_c.get('merged', 0)} merged, "
            f"{goals_c.get('archived', 0)} archived, "
            f"{goals_c.get('completed', 0)} completed, "
            f"{goals_c.get('tiered', 0)} tiered")
        log(f"  Meta-reflections: {meta.get('written', 0)} written")
        log(f"  Consolidation cost: "
            f"${consolidation_result.get('total_cost', 0):.4f}")
    except Exception as e:
        log(f"  Consolidation failed: {e}")
        import traceback
        traceback.print_exc()
        consolidation_result = {"total_cost": 0, "phases": {}}

    # =========================================================
    # PHASE 4.6: LONG-TERM MEMORY CONSOLIDATION
    # =========================================================
    log("\n--- PHASE 4.6: LONG-TERM MEMORY CONSOLIDATION ---")
    try:
        from entity.long_term_memory import consolidate_ltm
        ltm_result = consolidate_ltm(brain, memory_dir=str(entity_config.memory_dir))
        log(f"  LTM: {ltm_result.get('total_memories', 0)} total memories")
        log(f"  Pruned: {ltm_result.get('pruned', 0)}, "
            f"Meta stored: {ltm_result.get('meta_stored', False)}")
        log(f"  LTM consolidation cost: ${ltm_result.get('cost', 0):.4f}")
    except Exception as e:
        log(f"  LTM consolidation failed: {e}")
        import traceback
        traceback.print_exc()

    # =========================================================
    # PHASE 5: PROPOSALS + AUTONOMOUS IMPLEMENTATION
    # =========================================================
    log("\n--- PHASE 5: PROPOSALS + AUTONOMOUS IMPLEMENTATION ---")
    try:
        proposal_ids = generate_proposals_from_experiments(brain, exp_summary)
        if proposal_ids:
            log(f"Chloe wrote {len(proposal_ids)} proposal(s)")
            for pid in proposal_ids:
                log(f"  - {pid}")
        else:
            log("No new proposals today")
    except Exception as e:
        log(f"Proposal generation failed: {e}")
        proposal_ids = []

    # Process ALL pending proposals autonomously via sandbox
    pending_proposals = get_pending_proposals()
    if pending_proposals:
        log(f"\nProcessing {len(pending_proposals)} pending proposal(s) autonomously...")
        sandbox = CodeSandbox()
        current_prompt = improver.get_current_prompt()

        implemented = 0
        rejected = 0
        acknowledged = 0

        for prop in pending_proposals[:3]:  # Max 3 per daily cycle
            try:
                result = _try_implement_proposal(
                    brain, prop, sandbox, current_prompt, log
                )
                if result.get("applied"):
                    implemented += 1
                    log(f"  APPLIED: {prop.get('title')}")
                elif "acknowledged" in str(result.get("reason", "")).lower() or \
                     "no " in str(result.get("reason", "")).lower():
                    acknowledged += 1
                else:
                    rejected += 1
                    log(f"  REJECTED: {prop.get('title')} — "
                        f"{result.get('reason', '')[:80]}")
            except Exception as e:
                log(f"  Error processing {prop.get('title')}: {e}")
                acknowledged += 1

        log(f"  Results: {implemented} applied, {rejected} rejected, "
            f"{acknowledged} acknowledged")
    else:
        log("No pending proposals")

    # =========================================================
    # PHASE 6: REPORT
    # =========================================================
    log("\n--- PHASE 6: SEND REPORT ---")
    stats = brain.get_session_stats()

    sent = send_daily_report(
        scan_results=scan_results,
        analysis=analysis,
        cycle_result=cycle_result,
        reflection=reflection,
        stats=stats,
        identity=identity,
        experiment_summary=exp_summary,
        proposals=pending_proposals,
        entity_config=entity_config,
    )

    # Save log
    duration = (datetime.now() - cycle_start).total_seconds()
    log(f"\n{'=' * 60}")
    log(f"Daily cycle complete in {duration:.0f}s")
    log(f"Total tokens: {stats['total_tokens']:,}")
    log(f"Total cost: ${stats['total_cost']:.4f}")
    log(f"Email sent: {'yes' if sent else 'NO'}")

    # Write log file
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(
        log_dir,
        f"daily_{cycle_start.strftime('%Y%m%d')}.log"
    )
    with open(log_file, "w") as f:
        f.write("\n".join(log_lines))

    # =========================================================
    # PHASE 6.5: IDENTITY EXPORT (.pid file)
    # =========================================================
    log("\n--- PHASE 6.5: IDENTITY EXPORT ---")
    try:
        from entity.identity_export import build_level_2, add_level_4
        from entity.config import get_entity_config as _get_cfg
        import yaml as _yaml

        _cfg = _get_cfg(entity_name)
        _born_dates = {"chloe": "2026-02-23", "faith": "2026-02-23"}
        _born = _born_dates.get(entity_name, "2026-02-23")

        _pid_doc = build_level_2(_cfg, _born, entity_name)
        _pid_doc = add_level_4(_pid_doc, _cfg)  # includes journal + learnings + competencies

        _pid_dir = os.path.join(os.path.dirname(__file__), "identity")
        os.makedirs(_pid_dir, exist_ok=True)
        _pid_path = os.path.join(_pid_dir, f"{entity_name}.pid")
        with open(_pid_path, "w", encoding="utf-8") as _f:
            _yaml.dump(_pid_doc, _f, allow_unicode=True, default_flow_style=False,
                       sort_keys=False, width=100)

        _pid_size = os.path.getsize(_pid_path)
        log(f"Identity export: {_pid_path} ({_pid_size // 1024} KB, Level {_pid_doc['export_level_name']})")
    except Exception as e:
        log(f"Identity export failed (non-fatal): {e}")

    # =========================================================
    # PHASE 7: BACKUP TO GITHUB
    # =========================================================
    log("\n--- PHASE 7: BACKUP TO GITHUB ---")
    try:
        import subprocess
        repo_dir = os.path.dirname(__file__)
        date_str = cycle_start.strftime("%Y-%m-%d")
        entity_label = entity_config.display_name if entity_config else "Entity"

        # Stage all data files for both entities (excludes binaries via .gitignore)
        subprocess.run(
            ["git", "add", "data/", "data_faith/", "identity/", ".gitignore"],
            cwd=repo_dir, capture_output=True, text=True, timeout=30,
        )

        # Check if there's anything new to commit
        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_dir, capture_output=True, timeout=10,
        )
        has_changes = status.returncode != 0

        if has_changes:
            commit_msg = (
                f"Daily backup {date_str} — {entity_label} cycle complete\n\n"
                f"Scan: {scan_results.get('new', 0)} new findings\n"
                f"Experiment: {cycle_result.get('decision', 'N/A')} "
                f"({cycle_result.get('delta', 0):+.1f}%)\n"
                f"Cost: ${stats['total_cost']:.4f}"
            )
            commit_result = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=repo_dir, capture_output=True, text=True, timeout=30,
            )
            if commit_result.returncode == 0:
                log("Git commit: saved today's memories")
            else:
                log(f"Git commit failed: {commit_result.stderr.strip()[:100]}")
        else:
            log("Git commit: nothing new to save")

        push_result = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=repo_dir, capture_output=True, text=True, timeout=60,
        )
        if push_result.returncode == 0:
            log("Git push: backed up to GitHub")
        else:
            log(f"Git push failed: {push_result.stderr.strip()[:100]}")
    except Exception as e:
        log(f"Backup failed: {e}")

    # =========================================================
    # PHASE 7.5: INTERNET ARCHIVE BACKUP
    # =========================================================
    log("\n--- PHASE 7.5: INTERNET ARCHIVE BACKUP ---")
    try:
        from entity.backup_ia import backup_pid
        from pathlib import Path as _Path
        _pid_path = _Path(os.path.dirname(__file__)) / "identity" / f"{entity_name}.pid"
        success = backup_pid(entity_name, _pid_path, log_fn=log)
        if not success:
            log("  IA backup skipped or failed (non-fatal)")
    except Exception as e:
        log(f"  IA backup error (non-fatal): {e}")

    # =========================================================
    # PHASE 8: BIDIRECTIONAL MEMORY — CHAT → BILL'S SUBSTRATE
    # =========================================================
    # Only Chloe runs this — one extraction per day is enough.
    # Reads all family chat sessions from the last 26 hours and
    # extracts biographical data about Bill into his cognitive substrate.
    if entity_name == "chloe":
        log("\n--- PHASE 8: CHAT → BILL'S SUBSTRATE ---")
        try:
            from entity.chat_to_substrate import extract_recent_chats
            chat_result = extract_recent_chats(cutoff_hours=26, log_fn=log)
            log(f"  Entries added: {chat_result['entries_added']} | "
                f"Sessions: {chat_result['sessions_processed']} | "
                f"Skipped: {chat_result['skipped']}")
        except Exception as e:
            log(f"  Chat→substrate extraction error (non-fatal): {e}")

    return {
        "scan": scan_results,
        "analysis_length": len(analysis),
        "cycle": cycle_result,
        "reflection_length": len(reflection),
        "email_sent": sent,
        "cost": stats["total_cost"],
        "duration": duration,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Entity daily cycle")
    parser.add_argument("--entity", type=str, default="chloe",
                        help="Entity to run: chloe or faith (default: chloe)")
    args = parser.parse_args()
    daily_cycle(entity_name=args.entity)
