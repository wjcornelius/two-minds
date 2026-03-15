"""
Chloe's Skill Library — Voyager-inspired capability accumulation.

Each successful experiment or code change is stored as a reusable "skill."
Before proposing new experiments, the library is queried to prevent
redundant work. Skills that are too similar to existing ones get blocked,
forcing genuine novelty.

Uses keyword-based similarity (no ML dependencies required).
"""

import os
import re
import json
from datetime import datetime
from typing import Optional, List, Dict

SKILLS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "skills.json"
)


def _extract_keywords(text: str) -> set:
    """Extract significant keywords from text."""
    stop_words = {
        "the", "and", "for", "that", "this", "with", "from", "have",
        "has", "had", "are", "was", "were", "been", "being", "will",
        "would", "could", "should", "may", "might", "can", "did",
        "does", "not", "but", "also", "about", "into", "more", "than",
        "them", "they", "their", "there", "then", "when", "what",
        "which", "who", "how", "all", "each", "every", "both",
        "some", "any", "most", "other", "new", "used", "code",
        "file", "change", "added", "improved", "fixed", "test",
        "experiment", "benchmark", "result", "applied", "rejected",
    }
    words = set()
    for word in re.findall(r'[a-z]{4,}', text.lower()):
        if word not in stop_words:
            words.add(word)
    return words


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity between two keyword sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def load_skills(skills_path: str = None) -> List[Dict]:
    """Load skill library from disk."""
    path = skills_path or SKILLS_PATH
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def save_skills(skills: List[Dict], skills_path: str = None):
    """Save skill library to disk."""
    path = skills_path or SKILLS_PATH
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(skills, f, indent=2)
    except Exception as e:
        print(f"  [skills] Failed to save: {e}")


def add_skill(
    title: str,
    description: str,
    action_type: str,
    target_file: str = "",
    outcome: str = "",
    source_cycle: str = "",
) -> bool:
    """Add a verified skill to the library.

    Only called after successful experiments or applied code changes.
    Returns True if added, False if too similar to existing skill.
    """
    skills = load_skills()

    # Build keyword fingerprint
    text = f"{title} {description} {outcome} {target_file}"
    keywords = list(_extract_keywords(text))

    # Check for duplicates (similarity > 0.7)
    new_kw_set = set(keywords)
    for existing in skills:
        existing_kw = set(existing.get("keywords", []))
        if _jaccard(new_kw_set, existing_kw) > 0.7:
            # Too similar — reinforce existing skill instead
            existing["use_count"] = existing.get("use_count", 0) + 1
            existing["last_seen"] = datetime.now().strftime("%Y-%m-%d")
            save_skills(skills)
            print(f"  [skills] Reinforced existing: {existing['title'][:60]}")
            return False

    # Add new skill
    skill = {
        "id": f"sk_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "title": title,
        "description": description[:300],
        "action_type": action_type,
        "target_file": target_file,
        "outcome": outcome[:200],
        "keywords": keywords,
        "added": datetime.now().strftime("%Y-%m-%d"),
        "source_cycle": source_cycle,
        "use_count": 0,
        "last_seen": None,
    }
    skills.append(skill)

    # Cap at 200 skills
    if len(skills) > 200:
        skills = skills[-200:]

    save_skills(skills)
    print(f"  [skills] Added: {title[:60]}")
    return True


def find_similar(description: str, threshold: float = 0.5) -> List[Dict]:
    """Find skills similar to a description.

    Returns skills with Jaccard similarity above threshold,
    sorted by similarity (highest first).
    """
    skills = load_skills()
    if not skills:
        return []

    query_keywords = _extract_keywords(description)
    if not query_keywords:
        return []

    results = []
    for skill in skills:
        skill_kw = set(skill.get("keywords", []))
        sim = _jaccard(query_keywords, skill_kw)
        if sim >= threshold:
            results.append({**skill, "_similarity": round(sim, 3)})

    results.sort(key=lambda x: x["_similarity"], reverse=True)
    return results[:5]


def check_novelty_gate(description: str) -> Optional[str]:
    """Check if a proposed experiment is too similar to existing skills.

    Returns None if novel enough to proceed.
    Returns a message explaining the overlap if too similar (>0.7).
    """
    similar = find_similar(description, threshold=0.7)
    if similar:
        top = similar[0]
        return (
            f"Too similar to existing skill '{top['title']}' "
            f"(similarity={top['_similarity']:.0%}). "
            f"Try something genuinely different, or compose existing skills."
        )
    return None  # Novel enough
