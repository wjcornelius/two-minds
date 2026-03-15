"""
Chloe's Hierarchical Planner — GoalAct-inspired strategic direction.

Every ~20 cycles, a Claude API call sets a multi-cycle strategic plan
with 3-5 subgoals. Each heartbeat cycle, the local model selects actions
that serve the current subgoal, giving temporal coherence instead of
every cycle being an isolated decision.

Cost: ~$0.01-0.02 per planning call (Haiku), ~$0.15/day at one call
per 30 minutes.
"""

import os
import json
from datetime import datetime
from typing import Optional, Dict


PLAN_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "current_plan.json"
)


def load_plan(plan_path: str = None) -> Optional[Dict]:
    """Load current strategic plan from disk."""
    path = plan_path or PLAN_PATH
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def save_plan(plan: Dict, plan_path: str = None):
    """Save strategic plan to disk."""
    path = plan_path or PLAN_PATH
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=2)
    except Exception as e:
        print(f"  [planner] Failed to save: {e}")


def advance_subgoal(plan: Dict) -> Dict:
    """Move to the next subgoal in the plan."""
    idx = plan.get("current_subgoal_index", 0)
    subgoals = plan.get("subgoals", [])
    if idx < len(subgoals) - 1:
        plan["current_subgoal_index"] = idx + 1
        plan["subgoals"][idx]["status"] = "completed"
        save_plan(plan)
        print(f"  [planner] Advanced to subgoal {idx + 1}: "
              f"{subgoals[idx + 1]['description'][:60]}")
    else:
        # All subgoals done — plan is complete
        plan["status"] = "completed"
        save_plan(plan)
        print(f"  [planner] All subgoals completed!")
    return plan


def get_current_subgoal(plan: Dict) -> Optional[str]:
    """Get the current subgoal description."""
    if not plan or plan.get("status") == "completed":
        return None
    idx = plan.get("current_subgoal_index", 0)
    subgoals = plan.get("subgoals", [])
    if idx < len(subgoals):
        return subgoals[idx].get("description", "")
    return None


def needs_replanning(plan: Optional[Dict], cycle_count: int,
                     replan_interval: int = 20) -> bool:
    """Check if it's time for a new strategic plan.

    Replans when:
    - No plan exists
    - Current plan is completed
    - It's been replan_interval cycles since last plan
    """
    if plan is None:
        return True
    if plan.get("status") == "completed":
        return True
    # Replan every N cycles
    planned_at = plan.get("planned_at_cycle", 0)
    if cycle_count - planned_at >= replan_interval:
        return True
    return False


def create_plan(brain, journal, cycle_count: int,
                budget_remaining: float,
                competencies: dict = None) -> Dict:
    """Use Claude API to create a strategic plan.

    Args:
        brain: Brain instance for API calls
        journal: Journal instance for recent context
        cycle_count: Current cycle number
        budget_remaining: Remaining daemon budget

    Returns:
        Plan dict with goal, subgoals, and metadata
    """
    # Gather context
    recent = journal.get_recent(limit=10)
    journal_context = "\n".join(
        f"[{e['entry_type']}] {e['content'][:150]}"
        for e in recent
    )

    goals = journal.get_active_goals()
    goals_text = "\n".join(
        g["content"][:100] for g in goals[:5]
    ) or "(no active goals)"

    # Extract recently researched topic clusters to prevent repetitive plans
    recent_research_topics = []
    for e in recent:
        content = e.get("content", "")
        if "Researched" in content and "'" in content:
            try:
                start = content.index("'") + 1
                end = content.index("'", start)
                topic = content[start:end][:60]
                if topic:
                    recent_research_topics.append(topic)
            except ValueError:
                pass
    recent_topics_warning = ""
    if recent_research_topics:
        recent_topics_warning = (
            f"\nRECENTLY RESEARCHED TOPICS (avoid repeating these topic clusters):\n"
            + "\n".join(f"  - {t}" for t in recent_research_topics[:5])
            + "\n"
        )

    # Load previous plan for continuity
    old_plan = load_plan()
    continuity = ""
    if old_plan and old_plan.get("status") != "completed":
        continuity = (
            f"\nPREVIOUS PLAN (partially completed): {old_plan.get('goal', '')}\n"
            f"Completed subgoals: {sum(1 for s in old_plan.get('subgoals', []) if s.get('status') == 'completed')}"
            f"/{len(old_plan.get('subgoals', []))}\n"
        )

    # Build competency context for the planner
    competency_context = ""
    if competencies:
        phase = competencies.get("overall_phase", "infant")
        comps = competencies.get("competencies", {})
        weakest = sorted(comps.items(), key=lambda x: x[1].get("current_level", 0))
        weakest_text = ", ".join(
            f"{name} (L{state['current_level']})"
            for name, state in weakest[:3]
        )
        total_ex = competencies.get("total_exercises", 0)
        competency_context = (
            f"\nDEVELOPMENTAL PHASE: {phase.title()} ({total_ex} exercises completed)\n"
            f"WEAKEST COMPETENCIES: {weakest_text}\n"
            f"Focus your plan on activities that will strengthen these weak areas.\n"
        )

    prompt = f"""You are an autonomous AI agent growing under guidance.
Cycle {cycle_count}. Budget: ${budget_remaining:.3f} remaining.
{competency_context}
Recent activity:
{journal_context}
{continuity}{recent_topics_warning}
Create a STRATEGIC PLAN for the next ~20 cycles (~30 minutes).
Your job is to set the ACTION MIX and RHYTHM — not specific topics.
The curiosity system picks topics. The curriculum tests competencies.
Your plan guides PACING: what types of actions to do, in what order.

Available actions: research, experiment, code_experiment, reflect, explore_bills_world, self_study, set_goal

Respond in EXACTLY this JSON format (no other text):
{{
  "goal": "One-sentence objective about what KIND of growth to pursue",
  "subgoals": [
    {{"description": "Brief description of what to do (action type, not specific topic)", "action_hint": "action_name"}},
    {{"description": "Next phase", "action_hint": "action_name"}},
    {{"description": "Consolidation", "action_hint": "action_name"}}
  ]
}}

Rules:
- 3-5 subgoals, each 3-7 cycles
- FOCUS ON ACTION MIX: alternate between learning (research/self_study), doing (experiment/code_experiment), and integrating (reflect/explore_bills_world)
- Do NOT specify narrow topics like "formal logic" or "constraint satisfaction" — let the curiosity system choose topics
- Good descriptions: "Research something new in a weak competency area", "Run a code experiment to test understanding", "Reflect on recent learning and consolidate"
- Bad descriptions: "Research how constraint-based reasoning enables problem-solving" (too specific, locks the entity into one topic for 20 cycles)
- Budget awareness: if budget < $0.10, favor free actions (reflect, self_study, curriculum)
- At most ONE explore_bills_world subgoal per plan"""

    from entity.budget import safe_tier
    response = brain.think(
        prompt=prompt,
        tier=safe_tier("fast"),  # Haiku if affordable
        max_tokens=500,
        temperature=0.7,
    )

    cost = response.get("cost", 0)

    # Parse the JSON response
    text = response["text"].strip()
    # Extract JSON from response (model may wrap in markdown)
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        plan_data = json.loads(text)
    except json.JSONDecodeError:
        # Fallback plan
        plan_data = {
            "goal": "Build knowledge and grow as a capable companion",
            "subgoals": [
                {"description": "Research a topic that builds practical knowledge", "action_hint": "research"},
                {"description": "Explore Bill's database to understand his patterns", "action_hint": "explore_bills_world"},
                {"description": "Reflect on findings and set a goal", "action_hint": "reflect"},
            ]
        }

    # Enrich with metadata
    plan = {
        "goal": plan_data.get("goal", ""),
        "subgoals": [
            {
                "description": sg.get("description", ""),
                "action_hint": sg.get("action_hint", "research"),
                "status": "pending",
            }
            for sg in plan_data.get("subgoals", [])
        ],
        "current_subgoal_index": 0,
        "planned_at_cycle": cycle_count,
        "planned_at": datetime.now().isoformat(),
        "status": "active",
        "cost": cost,
    }

    # Mark first subgoal as active
    if plan["subgoals"]:
        plan["subgoals"][0]["status"] = "active"

    save_plan(plan)
    print(f"  [planner] New plan: {plan['goal'][:80]}")
    print(f"  [planner] {len(plan['subgoals'])} subgoals, "
          f"first: {plan['subgoals'][0]['description'][:60]}")

    return plan
