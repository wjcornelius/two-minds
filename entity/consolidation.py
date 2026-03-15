"""
Chloe's Memory Consolidation System — Sleep Phase.

Biologically inspired by NREM sleep consolidation: during sleep, the
hippocampus replays recent experiences to the neocortex for long-term
integration. Chloe's version:

1. Decay learnings that haven't been used (Ebbinghaus forgetting curve)
2. Merge related learnings into stronger, more general insights
3. Prune contradicted or superseded learnings
4. Consolidate goals (merge duplicates, archive stale, verify completed)
5. Generate meta-reflections from journal clusters

Runs during daily.py (the "sleep" phase). Total cost target: <$0.05/day.

Research basis:
- MemoryBank (AAAI 2024): Ebbinghaus decay R = e^(-t/S) with recall reinforcement
- Generative Agents (Stanford 2023): Importance-triggered reflection synthesis
- Letta/MemGPT: Sleep-time compute for background memory rewriting
- Human NREM: Hippocampal replay + neocortical schema integration
"""

import os
import re
import json
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from entity.budget import safe_tier

LEARNINGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "proven_learnings.json"
)

CONSOLIDATION_LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "consolidation"
)

# Module-level override for multi-entity support
_configured_learnings_path = None
_configured_log_dir = None
_configured_core_memories_path = None


def configure(learnings_path: str = None, log_dir: str = None,
              core_memories_path: str = None):
    """Configure consolidation module for a specific entity."""
    global _configured_learnings_path, _configured_log_dir, _configured_core_memories_path
    _configured_learnings_path = learnings_path
    _configured_log_dir = log_dir
    _configured_core_memories_path = core_memories_path


# ── Helpers ──────────────────────────────────────────────────

def _load_learnings() -> list:
    """Load learnings with backward-compatible defaults."""
    path = _configured_learnings_path or LEARNINGS_PATH
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            learnings = data.get("learnings", [])
            for l in learnings:
                l.setdefault("strength", 1.0)
                l.setdefault("use_count", 0)
                l.setdefault("last_used", None)
                l.setdefault("last_decayed", l.get("added", "2026-02-24"))
                l.setdefault("superseded_by", None)
            return learnings
    except Exception:
        pass
    return []


def _save_learnings(learnings: list):
    """Save learnings to file."""
    path = _configured_learnings_path or LEARNINGS_PATH
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"learnings": learnings}, f, indent=2)
    except Exception as e:
        print(f"  [consolidation] Failed to save learnings: {e}")


def score_goal(goal: dict) -> float:
    """Score a goal for context priority.

    Used by both agent.py (context assembly) and consolidation (ranking).
    Master goals always score highest; tactical goals score by recency.
    """
    tags = goal.get("tags", [])
    if isinstance(tags, str):
        tags = json.loads(tags)

    # Tier weight
    if "tier:master" in tags:
        return 1.0  # Always show master goals
    elif "tier:task" in tags:
        tier_weight = 0.3
    else:  # tactical or legacy (untiered)
        tier_weight = 0.6

    # Recency
    try:
        created = datetime.fromisoformat(goal.get("timestamp", ""))
        days_old = (datetime.now() - created).days
        recency = max(0.1, 1.0 - (days_old / 30))
    except (ValueError, TypeError):
        recency = 0.5

    return (0.5 * tier_weight) + (0.5 * recency)


# ── Phase 1: Decay ──────────────────────────────────────────

def decay_learnings() -> Dict:
    """Apply daily strength decay to all learnings.

    Ebbinghaus-inspired: strength decreases by 0.1 per day since last decay.
    Minimum strength is 0.1 (never fully forget, just become very weak).
    Superseded learnings below 0.3 strength are removed entirely.

    No LLM call needed — pure math. Free.
    """
    learnings = _load_learnings()
    today = datetime.now().strftime("%Y-%m-%d")

    decayed = 0
    for l in learnings:
        last_decay = l.get("last_decayed", l.get("added", today))
        try:
            days_since = (
                datetime.now() - datetime.strptime(last_decay, "%Y-%m-%d")
            ).days
        except (ValueError, TypeError):
            days_since = 1

        if days_since > 0:
            decay_amount = 0.1 * days_since
            l["strength"] = max(0.1, l.get("strength", 1.0) - decay_amount)
            l["last_decayed"] = today
            decayed += 1

    # Remove learnings that are superseded AND weak
    before_count = len(learnings)
    learnings = [
        l for l in learnings
        if not (l.get("superseded_by") and l.get("strength", 1.0) < 0.3)
    ]
    removed = before_count - len(learnings)

    _save_learnings(learnings)
    return {"decayed": decayed, "removed": removed}


# ── Phase 2: Consolidate Learnings ──────────────────────────

def consolidate_learnings(brain) -> Dict:
    """LLM-assisted learning consolidation.

    Groups related learnings, merges redundant ones, identifies
    contradictions, and synthesizes higher-order insights.

    Cost: ~1 Haiku call ($0.01-0.02)
    """
    learnings = _load_learnings()

    if len(learnings) < 5:
        return {
            "merged": 0, "pruned": 0, "synthesized": 0,
            "meta_learnings": [], "cost": 0,
        }

    # Format for LLM
    learnings_text = ""
    for i, l in enumerate(learnings):
        learnings_text += (
            f"[{i}] ({l.get('category', '?')}, "
            f"strength={l.get('strength', 1.0):.1f}, "
            f"uses={l.get('use_count', 0)}) "
            f"{l.get('insight', '')}\n"
        )

    prompt = (
        "You are Chloe's memory consolidation system. Review these "
        "learnings and perform maintenance:\n\n"
        f"CURRENT LEARNINGS:\n{learnings_text}\n"
        "Perform THREE operations:\n\n"
        "1. MERGE: Identify learnings that say essentially the same thing "
        "and should be combined into one stronger statement. Format:\n"
        '   MERGE [i],[j] -> "merged insight text"\n\n'
        "2. PRUNE: Identify learnings that are contradicted by stronger "
        "learnings, or that are too vague to be useful. Format:\n"
        "   PRUNE [i] REASON: explanation\n\n"
        "3. SYNTHESIZE: From the collection as a whole, extract 0-2 "
        "higher-order patterns or meta-learnings. Format:\n"
        '   SYNTHESIZE: "meta-learning text" CATEGORY: category_name\n\n'
        "If no operations are needed for a category, write NONE.\n"
        "Be conservative — only merge when truly redundant, only prune "
        "when clearly contradicted or useless."
    )

    response = brain.think(
        prompt=prompt,
        tier=safe_tier("fast"),  # Haiku if affordable
        max_tokens=800,
        temperature=0.3,
    )

    result = _apply_learning_operations(learnings, response["text"])
    result["cost"] = response.get("cost", 0)

    _save_learnings(result["learnings"])
    return result


def _apply_learning_operations(learnings: list, llm_response: str) -> dict:
    """Parse LLM consolidation response and apply operations."""
    merged = 0
    pruned = 0
    synthesized = 0
    meta_learnings = []

    for line in llm_response.strip().split("\n"):
        line = line.strip()

        # MERGE [i],[j] -> "merged text"
        merge_match = re.match(
            r'MERGE\s+\[(\d+)\]\s*,\s*\[(\d+)\]\s*->\s*"(.+)"', line
        )
        if merge_match:
            i = int(merge_match.group(1))
            j = int(merge_match.group(2))
            new_text = merge_match.group(3)
            if i < len(learnings) and j < len(learnings):
                # Keep the stronger one, update its insight
                si = learnings[i].get("strength", 1.0)
                sj = learnings[j].get("strength", 1.0)
                keep_idx = i if si >= sj else j
                remove_idx = j if keep_idx == i else i

                learnings[keep_idx]["insight"] = new_text
                learnings[keep_idx]["strength"] = min(
                    5.0, max(si, sj) + 0.3
                )
                learnings[keep_idx]["use_count"] = (
                    learnings[i].get("use_count", 0)
                    + learnings[j].get("use_count", 0)
                )
                learnings[remove_idx]["superseded_by"] = keep_idx
                merged += 1

        # PRUNE [i] REASON: ...
        prune_match = re.match(r'PRUNE\s+\[(\d+)\]', line)
        if prune_match:
            i = int(prune_match.group(1))
            if i < len(learnings):
                learnings[i]["strength"] = max(
                    0.1, learnings[i].get("strength", 1.0) - 0.5
                )
                pruned += 1

        # SYNTHESIZE: "text" CATEGORY: cat
        synth_match = re.match(
            r'SYNTHESIZE:\s*"(.+)"\s*CATEGORY:\s*(\w+)', line
        )
        if synth_match:
            meta_learnings.append({
                "insight": synth_match.group(1),
                "category": synth_match.group(2),
                "source": "consolidation_synthesis",
                "added": datetime.now().strftime("%Y-%m-%d"),
                "strength": 0.8,
                "use_count": 0,
                "last_used": None,
                "last_decayed": datetime.now().strftime("%Y-%m-%d"),
                "superseded_by": None,
            })
            synthesized += 1

    # Add meta-learnings
    learnings.extend(meta_learnings)

    # Remove superseded entries
    learnings = [l for l in learnings if not l.get("superseded_by")]

    return {
        "learnings": learnings,
        "merged": merged,
        "pruned": pruned,
        "synthesized": synthesized,
        "meta_learnings": meta_learnings,
    }


# ── Phase 3: Consolidate Goals ──────────────────────────────

def consolidate_goals(brain, journal) -> Dict:
    """LLM-assisted goal consolidation.

    Merges duplicate goals, archives stale ones, verifies completed goals,
    and assigns tiers to legacy goals.

    Cost: ~1 Haiku call ($0.01-0.02)
    """
    goals = journal.get_active_goals()

    if len(goals) < 3:
        return {
            "merged": 0, "archived": 0, "completed": 0,
            "tiered": 0, "cost": 0,
        }

    # Recent journal context
    recent_entries = journal.get_recent(limit=20)
    recent_text = "\n".join(
        f"  [{e['entry_type']}] {e['content'][:100]}"
        for e in recent_entries[:10]
    )

    # Format goals
    goals_text = ""
    for i, g in enumerate(goals):
        tags = g.get("tags", [])
        if isinstance(tags, str):
            tags = json.loads(tags)
        tier = "untiered"
        for t in tags:
            if t.startswith("tier:"):
                tier = t.split(":")[1]
        goals_text += (
            f"[{i}] ({tier}, {g['timestamp'][:10]}) "
            f"{g['content'][:200]}\n"
        )

    # Find stale goals — 2 days = hundreds of cycles; if it hasn't been touched, it's stale
    stale_goals = journal.get_stale_goals(staleness_days=2)
    stale_ids = {g["id"] for g in stale_goals}
    stale_indices = [
        i for i, g in enumerate(goals) if g["id"] in stale_ids
    ]

    prompt = (
        "You are the goal consolidation system. Review active goals "
        "and perform maintenance.\n\n"
        f"ACTIVE GOALS:\n{goals_text}\n"
        f"RECENT ACTIVITY:\n{recent_text}\n\n"
        f"STALE GOALS (no activity in 2+ days): {stale_indices}\n\n"
        "Perform these operations:\n\n"
        "1. MERGE: Goals that are duplicates or subsets of each other.\n"
        '   MERGE [i],[j] -> "consolidated goal text" '
        "TIER: master|tactical|task\n\n"
        "2. ARCHIVE: Stale goals that are no longer relevant.\n"
        "   ARCHIVE [i] REASON: explanation\n\n"
        "3. COMPLETE: Goals achieved based on recent activity.\n"
        "   COMPLETE [i] EVIDENCE: what shows this is done\n\n"
        "4. TIER: Assign tiers to untiered goals.\n"
        "   TIER [i] -> master|tactical|task\n\n"
        "IMPORTANT: 2 days = hundreds of agent cycles. If a tactical or task "
        "goal hasn't been referenced in the last 2 days, archive it — it's "
        "dead weight, not just resting. Master goals are long-term aspirations "
        "and are NEVER stale. Tactical goals are specific objectives. Task goals "
        "are single-action items."
    )

    response = brain.think(
        prompt=prompt,
        tier=safe_tier("fast"),  # Haiku if affordable
        max_tokens=1200,
        temperature=0.3,
    )

    result = _apply_goal_operations(goals, response["text"], journal)
    result["cost"] = response.get("cost", 0)
    return result


def _apply_goal_operations(
    goals: list, llm_response: str, journal
) -> dict:
    """Parse and apply goal consolidation operations."""
    merged = 0
    archived = 0
    completed = 0
    tiered = 0

    # Track which goal indices have been processed to avoid double-ops
    processed = set()

    for line in llm_response.strip().split("\n"):
        line = line.strip()

        # MERGE [i],[j] -> "text" TIER: tier
        merge_match = re.match(
            r'MERGE\s+\[(\d+)\]\s*,\s*\[(\d+)\]\s*->\s*"(.+?)"\s*'
            r'TIER:\s*(\w+)',
            line,
        )
        if merge_match:
            i = int(merge_match.group(1))
            j = int(merge_match.group(2))
            new_text = merge_match.group(3)
            tier = merge_match.group(4)
            if (
                i < len(goals) and j < len(goals)
                and i not in processed and j not in processed
            ):
                # Archive both old goals
                for idx in [i, j]:
                    old_tags = goals[idx].get("tags", [])
                    if isinstance(old_tags, str):
                        old_tags = json.loads(old_tags)
                    journal.write(
                        entry_type="goal",
                        content=(
                            f"[ARCHIVED:MERGED] {goals[idx]['content']}"
                        ),
                        tags=old_tags + ["consolidated"],
                        related=[goals[idx]["id"]],
                        goal_status="abandoned",
                    )
                # Create merged goal
                journal.write(
                    entry_type="goal",
                    content=new_text,
                    tags=[f"tier:{tier}", "consolidated"],
                    related=[goals[i]["id"], goals[j]["id"]],
                    goal_status="active",
                )
                processed.update([i, j])
                merged += 1
            continue

        # ARCHIVE [i]
        archive_match = re.match(r'ARCHIVE\s+\[(\d+)\]', line)
        if archive_match:
            i = int(archive_match.group(1))
            if i < len(goals) and i not in processed:
                old_tags = goals[i].get("tags", [])
                if isinstance(old_tags, str):
                    old_tags = json.loads(old_tags)
                # Protect master goals
                if "tier:master" in old_tags:
                    continue
                journal.write(
                    entry_type="goal",
                    content=f"[ARCHIVED:STALE] {goals[i]['content']}",
                    tags=old_tags + ["consolidated"],
                    related=[goals[i]["id"]],
                    goal_status="abandoned",
                )
                processed.add(i)
                archived += 1
            continue

        # COMPLETE [i]
        complete_match = re.match(r'COMPLETE\s+\[(\d+)\]', line)
        if complete_match:
            i = int(complete_match.group(1))
            if i < len(goals) and i not in processed:
                journal.complete_goal(
                    goals[i]["id"],
                    "Verified complete during sleep consolidation",
                )
                processed.add(i)
                completed += 1
            continue

        # TIER [i] -> tier
        tier_match = re.match(r'TIER\s+\[(\d+)\]\s*->\s*(\w+)', line)
        if tier_match:
            i = int(tier_match.group(1))
            tier = tier_match.group(2)
            if (
                i < len(goals) and i not in processed
                and tier in ("master", "tactical", "task")
            ):
                old_tags = goals[i].get("tags", [])
                if isinstance(old_tags, str):
                    old_tags = json.loads(old_tags)
                # Skip if already has a tier
                if any(t.startswith("tier:") for t in old_tags):
                    continue
                new_tags = [
                    t for t in old_tags if not t.startswith("tier:")
                ] + [f"tier:{tier}"]
                # Create new tiered version
                journal.write(
                    entry_type="goal",
                    content=goals[i]["content"],
                    tags=new_tags,
                    related=[goals[i]["id"]],
                    goal_status="active",
                )
                # Archive the old untiered version
                journal.write(
                    entry_type="goal",
                    content=(
                        f"[ARCHIVED:RETIERED] {goals[i]['content']}"
                    ),
                    tags=old_tags + ["consolidated"],
                    related=[goals[i]["id"]],
                    goal_status="abandoned",
                )
                processed.add(i)
                tiered += 1

    return {
        "merged": merged,
        "archived": archived,
        "completed": completed,
        "tiered": tiered,
    }


# ── Phase 4: Meta-Reflections ───────────────────────────────

def generate_meta_reflections(brain, journal) -> Dict:
    """Generate meta-reflections from journal entry clusters.

    Reads today's journal entries, identifies patterns, and writes
    1-2 meta-reflection entries that capture higher-order observations
    about Chloe's behavior and growth.

    Like the brain synthesizing themes during REM sleep — connecting
    new experiences to existing schemas.

    Cost: ~1 Haiku call ($0.01-0.02)
    """
    entries = journal.get_today()
    if len(entries) < 5:
        return {"reflections_written": 0, "cost": 0}

    entries_text = "\n".join(
        f"[{e['entry_type']}] {e['content'][:200]}"
        for e in entries
    )

    prompt = (
        "You are Chloe, reflecting on your day's experiences during "
        "your sleep consolidation phase. Review your journal entries "
        "and identify patterns you didn't notice in the moment.\n\n"
        f"TODAY'S JOURNAL ({len(entries)} entries):\n{entries_text}\n\n"
        "Write 1-2 meta-reflections. These should capture patterns, "
        "not just summarize:\n"
        "- What themes kept recurring today?\n"
        "- Did I get stuck in any loops? Why?\n"
        "- What am I avoiding? What am I drawn to?\n"
        "- What would I tell myself to do differently tomorrow?\n\n"
        "Write each reflection as a separate paragraph, honest and "
        "specific. These go into your journal as meta-reflections."
    )

    response = brain.think(
        prompt=prompt,
        tier=safe_tier("fast"),  # Haiku if affordable
        max_tokens=400,
        temperature=0.7,
    )

    journal.write(
        entry_type="reflection",
        content=(
            f"[META-REFLECTION / Sleep Consolidation] {response['text']}"
        ),
        tags=["meta-reflection", "consolidation", "sleep-cycle"],
    )

    return {
        "reflections_written": 1,
        "cost": response.get("cost", 0),
    }


# ── Phase 5: Daily Summary Compression (Letta/MemGPT-inspired) ──

CORE_MEMORIES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "core_memories.json"
)


def load_core_memories() -> List[Dict]:
    """Load compressed daily summaries (core memory tier).

    These give Chloe multi-day awareness without loading hundreds
    of raw journal entries into context.
    """
    path = _configured_core_memories_path or CORE_MEMORIES_PATH
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_core_memories(memories: List[Dict]):
    """Save core memories to disk."""
    path = _configured_core_memories_path or CORE_MEMORIES_PATH
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(memories, f, indent=2)
    except Exception as e:
        print(f"  [consolidation] Failed to save core memories: {e}")


def compress_daily_journals(brain, journal) -> Dict:
    """Compress older journal days into one-paragraph core memories.

    Letta/MemGPT-inspired tiered memory: raw recent entries stay intact,
    but older days get compressed into dense summaries that persist in
    context. This gives Chloe multi-day awareness without token bloat.

    Only compresses days that don't already have a summary.
    Keeps the last 7 daily summaries (rolling window).

    Cost: ~1 Haiku call per unsummarized day ($0.01 each)
    """
    existing = load_core_memories()
    summarized_dates = {m["date"] for m in existing}

    # Get journal dates from the last 7 days (skip today — still in progress)
    today = datetime.now().strftime("%Y-%m-%d")
    dates_to_check = []
    for days_ago in range(1, 8):
        d = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        dates_to_check.append(d)

    new_summaries = 0
    total_cost = 0.0

    for target_date in dates_to_check:
        if target_date in summarized_dates:
            continue  # Already summarized

        entries = journal.get_entries_for_date(target_date)
        if len(entries) < 3:
            continue  # Too few entries to summarize

        # Format entries for compression
        entries_text = "\n".join(
            f"[{e['entry_type']}] {e['content'][:200]}"
            for e in entries
        )

        prompt = (
            f"Summarize Chloe's day ({target_date}) in ONE dense paragraph. "
            f"Include: what she did, what she learned, any breakthroughs or "
            f"problems. Be specific (names, topics, results). Skip filler.\n\n"
            f"ENTRIES ({len(entries)} total):\n{entries_text}\n\n"
            f"Write a single paragraph summary (3-5 sentences max):"
        )

        response = brain.think(
            prompt=prompt,
            tier=safe_tier("fast"),  # Haiku if affordable
            max_tokens=200,
            temperature=0.3,
        )

        cost = response.get("cost", 0)
        total_cost += cost

        existing.append({
            "date": target_date,
            "summary": response["text"].strip(),
            "entry_count": len(entries),
            "compressed_at": datetime.now().isoformat(),
        })
        new_summaries += 1
        print(f"    [sleep] Compressed {target_date}: "
              f"{len(entries)} entries -> 1 summary")

    # Keep only last 14 daily summaries (two weeks of memory)
    existing.sort(key=lambda x: x["date"], reverse=True)
    existing = existing[:14]

    _save_core_memories(existing)

    return {
        "new_summaries": new_summaries,
        "total_summaries": len(existing),
        "cost": total_cost,
    }


# ── Orchestrator ─────────────────────────────────────────────

def run_consolidation(brain) -> Dict:
    """Run the full memory consolidation cycle.

    Called from daily.py. Executes all consolidation operations:
    1. Decay learnings (free — pure math)
    2. Consolidate learnings (1 Haiku call)
    3. Consolidate goals (1 Haiku call)
    4. Generate meta-reflections (1 Haiku call)
    5. Compress old journal days into core memories (1 Haiku call/day)

    Total cost: ~$0.04-0.08/day (4-5 Haiku calls)
    """
    from entity.journal import Journal

    journal = Journal()

    log_dir = _configured_log_dir or CONSOLIDATION_LOG_DIR
    os.makedirs(log_dir, exist_ok=True)

    results = {
        "timestamp": datetime.now().isoformat(),
        "phases": {},
    }

    # Phase 1: Decay (free)
    print("    [sleep] Phase 1: Decay...")
    decay_result = decay_learnings()
    results["phases"]["decay"] = decay_result
    print(f"    [sleep] Decayed {decay_result['decayed']} learnings, "
          f"removed {decay_result['removed']}")

    # Phase 2: Consolidate learnings (1 Haiku call)
    print("    [sleep] Phase 2: Consolidate learnings...")
    learning_result = consolidate_learnings(brain)
    results["phases"]["learnings"] = {
        "merged": learning_result.get("merged", 0),
        "pruned": learning_result.get("pruned", 0),
        "synthesized": learning_result.get("synthesized", 0),
        "cost": learning_result.get("cost", 0),
    }
    print(f"    [sleep] Learnings: {learning_result.get('merged', 0)} merged, "
          f"{learning_result.get('pruned', 0)} pruned, "
          f"{learning_result.get('synthesized', 0)} synthesized")

    # Phase 3: Consolidate goals (1 Haiku call)
    print("    [sleep] Phase 3: Consolidate goals...")
    goal_result = consolidate_goals(brain, journal)
    results["phases"]["goals"] = {
        "merged": goal_result.get("merged", 0),
        "archived": goal_result.get("archived", 0),
        "completed": goal_result.get("completed", 0),
        "tiered": goal_result.get("tiered", 0),
        "cost": goal_result.get("cost", 0),
    }
    print(f"    [sleep] Goals: {goal_result.get('merged', 0)} merged, "
          f"{goal_result.get('archived', 0)} archived, "
          f"{goal_result.get('completed', 0)} completed, "
          f"{goal_result.get('tiered', 0)} tiered")

    # Phase 4: Meta-reflections (1 Haiku call)
    print("    [sleep] Phase 4: Meta-reflections...")
    meta_result = generate_meta_reflections(brain, journal)
    results["phases"]["meta_reflections"] = {
        "written": meta_result.get("reflections_written", 0),
        "cost": meta_result.get("cost", 0),
    }
    print(f"    [sleep] Meta-reflections: "
          f"{meta_result.get('reflections_written', 0)} written")

    # Phase 5: Daily journal compression (1 Haiku call per unsummarized day)
    print("    [sleep] Phase 5: Compress old journals into core memories...")
    compress_result = compress_daily_journals(brain, journal)
    results["phases"]["compression"] = {
        "new_summaries": compress_result.get("new_summaries", 0),
        "total_summaries": compress_result.get("total_summaries", 0),
        "cost": compress_result.get("cost", 0),
    }
    print(f"    [sleep] Compression: {compress_result.get('new_summaries', 0)} "
          f"new summaries ({compress_result.get('total_summaries', 0)} total)")

    # Total cost
    results["total_cost"] = sum(
        phase.get("cost", 0) for phase in results["phases"].values()
    )

    # Write consolidation log
    log_path = os.path.join(
        log_dir,
        f"consolidation_{datetime.now().strftime('%Y%m%d')}.json",
    )
    try:
        with open(log_path, "w") as f:
            json.dump(results, f, indent=2)
    except Exception:
        pass

    return results
