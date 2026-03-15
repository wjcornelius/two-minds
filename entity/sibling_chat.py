"""
Sibling Chat -- Inter-entity conversation system (Bicameral Phase 2).

Chloe and Faith share a conversation log and a shared questions board.
Messages are rich (up to 500 words) with structured fields for topics,
questions, and shared discoveries. Chat happens every cycle when the
sibling has spoken -- no artificial hourly gating.

Storage: data/sibling_chat/YYYY-MM-DD.json (shared directory, not per-entity)
Questions: data/shared_questions.json (stigmergic board)
"""

import json
import os
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List, Dict

PROJECT_ROOT = Path(__file__).parent.parent
CHAT_DIR = PROJECT_ROOT / "data" / "sibling_chat"
MAX_WORDS = 500  # Bicameral Phase 2: thicker corpus callosum (was 200)
MIN_CHAT_INTERVAL = 300  # Minimum seconds between messages from same sender (5 min)
LIVE_MODE_FLAG = CHAT_DIR / ".live_mode"
SHARED_QUESTIONS_PATH = PROJECT_ROOT / "data" / "shared_questions.json"


def is_live_mode() -> bool:
    """Check if live mode is active (Bill toggled it from the viewer)."""
    return LIVE_MODE_FLAG.exists()


def set_live_mode(on: bool):
    """Toggle live mode on/off."""
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    if on:
        LIVE_MODE_FLAG.write_text("on")
    else:
        try:
            LIVE_MODE_FLAG.unlink()
        except OSError:
            pass


def _today_path() -> Path:
    """Path to today's chat file."""
    return CHAT_DIR / f"{date.today().isoformat()}.json"


def _lock_path() -> Path:
    """Path to the shared lock file for atomic read-modify-write."""
    return CHAT_DIR / ".chat.lock"


def _acquire_lock(timeout: float = 5.0) -> bool:
    """Simple file-based lock (Windows-compatible)."""
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            # Check if lock is stale (>30s old)
            try:
                age = time.time() - os.path.getmtime(str(lock))
                if age > 30:
                    os.remove(str(lock))
                    continue
            except OSError:
                pass
            time.sleep(0.1)
    return False


def _release_lock():
    """Release the file lock."""
    try:
        os.remove(str(_lock_path()))
    except OSError:
        pass


def _load_messages(path: Path) -> List[Dict]:
    """Load messages from a daily chat file."""
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_messages(path: Path, messages: List[Dict]):
    """Save messages atomically (write to temp, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(messages, f, indent=2)
    try:
        os.replace(str(tmp), str(path))
    except OSError:
        if path.exists():
            os.remove(str(path))
        os.rename(str(tmp), str(path))


def _save_json(path: Path, data):
    """Save JSON atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        os.replace(str(tmp), str(path))
    except OSError:
        if path.exists():
            os.remove(str(path))
        os.rename(str(tmp), str(path))


def _truncate(text: str) -> str:
    """Enforce word limit. Keep first MAX_WORDS words."""
    words = text.split()
    if len(words) <= MAX_WORDS:
        return text
    return " ".join(words[:MAX_WORDS]) + "..."


def get_sibling_name(my_name: str) -> str:
    """Get the other entity's name."""
    return "Faith" if my_name.lower() == "chloe" else "Chloe"


def get_last_message(sender: Optional[str] = None) -> Optional[Dict]:
    """Get the most recent message, optionally filtered by sender."""
    messages = _load_messages(_today_path())
    if not messages:
        return None
    if sender:
        filtered = [m for m in messages if m.get("sender", "").lower() == sender.lower()]
        return filtered[-1] if filtered else None
    return messages[-1]


def get_recent_messages(n: int = 10) -> List[Dict]:
    """Get the last N messages from today."""
    messages = _load_messages(_today_path())
    return messages[-n:]


def should_chat(my_name: str) -> bool:
    """Decide if this entity should write a message this cycle.

    Rate-limited: minimum MIN_CHAT_INTERVAL seconds between messages from
    the same sender. Reply only when sibling has spoken since your last
    message. If sibling hasn't spoken, allow one unanswered message per
    55 minutes (no monologuing).
    """
    messages = _load_messages(_today_path())

    if not messages:
        return True  # Nobody has spoken yet today

    my_msgs = [m for m in messages if m.get("sender", "").lower() == my_name.lower()]
    if not my_msgs:
        return True  # I haven't spoken today

    # Rate limit: don't chat more than once per MIN_CHAT_INTERVAL
    my_last = my_msgs[-1]
    my_last_time = datetime.fromisoformat(my_last["timestamp"])
    elapsed_since_mine = (datetime.now() - my_last_time).total_seconds()
    if elapsed_since_mine < MIN_CHAT_INTERVAL:
        return False  # Too soon since my last message

    # Has sibling said something since my last message?
    sibling = get_sibling_name(my_name)
    sibling_msgs = [m for m in messages if m.get("sender", "").lower() == sibling.lower()]
    if not sibling_msgs:
        # Sibling hasn't spoken. Allow one unanswered message per 55 min.
        return elapsed_since_mine > 3300

    sibling_last = sibling_msgs[-1]
    sibling_last_time = datetime.fromisoformat(sibling_last["timestamp"])
    return sibling_last_time > my_last_time  # Reply if sibling spoke after me


def post_message(sender: str, text: str, topic: str = "",
                 question_for_sibling: str = "",
                 shared_discovery: str = "") -> Dict:
    """Post a message to the sibling chat.

    Uses file lock for atomic read-modify-write (two entities may run
    on overlapping cycles).

    Args:
        sender: Entity name ("Chloe" or "Faith")
        text: Message content (will be truncated to MAX_WORDS)
        topic: What this message is about
        question_for_sibling: Explicit question to prompt response
        shared_discovery: Something worth remembering together

    Returns:
        The message dict that was posted.
    """
    path = _today_path()

    msg = {
        "sender": sender,
        "text": _truncate(text.strip()),
        "timestamp": datetime.now().isoformat(),
        "topic": topic,
        "question_for_sibling": question_for_sibling,
        "shared_discovery": shared_discovery,
    }

    if _acquire_lock():
        try:
            messages = _load_messages(path)
            messages.append(msg)
            _save_messages(path, messages)
        finally:
            _release_lock()
    else:
        # Lock failed -- still write, just risk a minor race
        messages = _load_messages(path)
        messages.append(msg)
        _save_messages(path, messages)

    return msg


def detect_echo(my_text: str, recent_messages: list, threshold: float = 0.5) -> bool:
    """Return True if my_text is too similar to recent messages (echo/restatement).

    Uses simple word-overlap Jaccard similarity. Threshold 0.5 = 50% word overlap.
    """
    if not recent_messages:
        return False
    my_words = set(my_text.lower().split())
    if len(my_words) < 5:
        return False
    # Check against last 3 messages from sibling
    for msg in recent_messages[-3:]:
        other_words = set(msg.get("text", "").lower().split())
        if not other_words:
            continue
        intersection = my_words & other_words
        union = my_words | other_words
        if union and len(intersection) / len(union) > threshold:
            return True
    return False


def detect_topic_staleness(messages: list, window: int = 6) -> str:
    """Check if the last N messages are stuck on the same topic.

    Returns the stale topic name if stuck, empty string if conversation is diverse.
    """
    if len(messages) < window:
        return ""
    recent = messages[-window:]
    topics = [m.get("topic", "").lower().strip() for m in recent if m.get("topic")]
    if not topics:
        return ""
    # If >60% of recent messages share a topic, it's stale
    from collections import Counter
    counts = Counter(topics)
    most_common, count = counts.most_common(1)[0]
    if count >= window * 0.6 and most_common:
        return most_common
    return ""


def get_conversation_context(my_name: str, n: int = 6) -> str:
    """Build a readable context string of recent messages for the LLM prompt.

    Returns empty string if no messages exist. Includes structured fields
    (questions, discoveries) when present.
    """
    messages = get_recent_messages(n)
    if not messages:
        return ""

    lines = []
    for m in messages:
        sender = m.get("sender", "?")
        text = m.get("text", "")
        ts = m.get("timestamp", "")
        try:
            t = datetime.fromisoformat(ts).strftime("%H:%M")
        except (ValueError, TypeError):
            t = "?"
        line = f"[{t}] {sender}: {text}"
        # Append structured fields if present
        q = m.get("question_for_sibling", "")
        if q:
            line += f"\n  [QUESTION FOR YOU: {q}]"
        d = m.get("shared_discovery", "")
        if d:
            line += f"\n  [SHARED DISCOVERY: {d}]"
        lines.append(line)

    return "\n".join(lines)


def list_chat_dates() -> List[str]:
    """List all dates that have chat files, newest first."""
    if not CHAT_DIR.exists():
        return []
    dates = []
    for f in CHAT_DIR.iterdir():
        if f.suffix == ".json" and f.stem.count("-") == 2:
            dates.append(f.stem)
    return sorted(dates, reverse=True)


def get_messages_for_date(date_str: str) -> List[Dict]:
    """Get all messages for a specific date."""
    path = CHAT_DIR / f"{date_str}.json"
    return _load_messages(path)


# ── Shared Questions Board (Bicameral Phase 2) ──────────────────

def _load_questions() -> List[Dict]:
    """Load questions from the shared questions board."""
    if not SHARED_QUESTIONS_PATH.exists():
        return []
    try:
        with open(SHARED_QUESTIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def post_question(sender: str, question: str, context: str = "") -> Dict:
    """Post a question to the shared questions board (stigmergic).

    Like leaving a book open on the kitchen table -- the other entity
    will notice it during OBSERVE and address it when relevant.
    """
    entry = {
        "id": f"q_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{sender[:1].lower()}",
        "sender": sender,
        "question": question,
        "context": context,
        "posted_at": datetime.now().isoformat(),
        "addressed_by": None,
        "addressed_at": None,
        "answer_summary": None,
    }

    if _acquire_lock():
        try:
            questions = _load_questions()
            questions.append(entry)
            _save_json(SHARED_QUESTIONS_PATH, questions)
        finally:
            _release_lock()
    else:
        questions = _load_questions()
        questions.append(entry)
        _save_json(SHARED_QUESTIONS_PATH, questions)

    print(f"  [questions] {sender} posted: {question[:60]}...")
    return entry


def get_pending_questions(for_entity: str) -> List[Dict]:
    """Get unanswered questions NOT posted by this entity."""
    questions = _load_questions()
    return [
        q for q in questions
        if q.get("addressed_by") is None
        and q.get("sender", "").lower() != for_entity.lower()
    ]


def mark_question_addressed(question_id: str, by_entity: str,
                             summary: str = ""):
    """Mark a question as addressed."""
    if _acquire_lock():
        try:
            questions = _load_questions()
            for q in questions:
                if q["id"] == question_id:
                    q["addressed_by"] = by_entity
                    q["addressed_at"] = datetime.now().isoformat()
                    q["answer_summary"] = summary
                    break
            _save_json(SHARED_QUESTIONS_PATH, questions)
        finally:
            _release_lock()
