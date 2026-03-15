"""
Multi-Persona Chat — Browser-based conversation with Chloe, Faith, or both.

Opens as a tab (or standalone app window) in Chrome. Bill can talk to Chloe,
Faith, or both in "Family Chat" mode. Each entity draws on her own personality,
long-term memories, journal entries, and core memories to respond as herself.

Uses Poe API (fast tier) so chat never competes with agents for GPU.
Runs alongside the daemon without conflicts — Ollama queues requests.

Launch: chainlit run chloe_chat.py --port 8085
"""

import os
import sys
import re
import json
import base64
import asyncio
import subprocess
import concurrent.futures
import httpx
from pathlib import Path
from datetime import datetime, timezone, timedelta
from fastapi.responses import JSONResponse

# Ensure Offspring root is on the path so entity/ imports work
OFFSPRING_DIR = os.path.dirname(os.path.abspath(__file__))
if OFFSPRING_DIR not in sys.path:
    sys.path.insert(0, OFFSPRING_DIR)
os.chdir(OFFSPRING_DIR)

import chainlit as cl
from chainlit.server import app as _fastapi_app

# ── Biographer launcher endpoint ───────────────────────────────────────────────
# Called by the toolbar button injected via public/toolbar.js.
# Spawns the VB GUI as a separate process; returns immediately.

_VB_PYTHON = Path("C:/Users/wjcor/OneDrive/Desktop/My_Songs/venv/Scripts/python.exe")
_VB_SCRIPT  = Path("C:/Users/wjcor/OneDrive/Desktop/My_Songs/_soul/biographer/main_gui.py")
_vb_proc: subprocess.Popen = None  # Track so we don't launch duplicates


@_fastapi_app.post("/launch-biographer")
async def launch_biographer():
    """Launch the Vector Biographer GUI. If already running, does nothing."""
    global _vb_proc
    if _vb_proc is not None and _vb_proc.poll() is None:
        return JSONResponse({"status": "already_running"})
    try:
        _vb_proc = subprocess.Popen(
            [str(_VB_PYTHON), str(_VB_SCRIPT)],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return JSONResponse({"status": "launched"})
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


@_fastapi_app.post("/stop-all")
async def stop_all_agents():
    """Shut down all Offspring processes (Chloe, Faith, chat server, Chrome window).

    Returns immediately, then runs stop_all.bat in the background after a short
    delay so the HTTP response has time to reach the browser before the server dies.
    """
    import asyncio

    async def _do_stop():
        await asyncio.sleep(0.8)  # Give browser time to receive the response
        stop_bat = Path(OFFSPRING_DIR) / "stop_all.bat"
        subprocess.Popen(
            ["cmd", "/c", str(stop_bat)],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    asyncio.create_task(_do_stop())
    return JSONResponse({"status": "stopping"})
from entity.brain import Brain
from entity.long_term_memory import LongTermMemory
from entity.consolidation import load_core_memories, configure as configure_consolidation
from entity.journal import Journal
from entity.config import get_entity_config
from entity.tools import query_bills_world
from entity.knowledge import wiki_lookup

# ---------- User preferences (name, pronouns, onboarding state) ----------

_USER_PREFS_PATH = Path(OFFSPRING_DIR) / "data" / "user_prefs.json"


def _load_user_prefs() -> dict:
    """Load user preferences. Returns defaults (not onboarded) if file doesn't exist."""
    try:
        if _USER_PREFS_PATH.exists():
            return json.loads(_USER_PREFS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"name": "there", "display_name": "there", "pronouns": "", "onboarded": False,
            "first_biographer_session_done": False}


def _save_user_prefs(prefs: dict):
    """Save user preferences to disk."""
    try:
        _USER_PREFS_PATH.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[chat] Failed to save user prefs: {e}")


def _parse_name_response(user_text: str) -> tuple:
    """Extract (name, pronouns) from a free-form intro message using LLM."""
    prompt = (
        f'The user was asked for their name (and optionally pronouns). They responded: "{user_text}"\n\n'
        'Extract their preferred first name and pronouns (if any were given).\n'
        'Return ONLY valid JSON: {"name": "Alex", "pronouns": "they/them"}\n'
        'If no pronouns were given, set pronouns to empty string.\n'
        'If you cannot identify a name, return: {"name": "", "pronouns": ""}'
    )
    try:
        result = brain.think(
            prompt=prompt,
            system="Extract name and pronouns from text. Return only valid JSON, nothing else.",
            tier="fast",
            max_tokens=60,
            temperature=0.1,
            skip_pacing=True,
        )
        text = result.get("text", "").strip()
        m = re.search(r'\{[^}]+\}', text)
        if m:
            data = json.loads(m.group(0))
            name = data.get("name", "").strip()
            pronouns = data.get("pronouns", "").strip()
            if name:
                return name, pronouns
    except Exception:
        pass
    # Fallback: first capitalized word
    for word in user_text.split():
        cleaned = re.sub(r'[^a-zA-Z\'-]', '', word)
        if cleaned and cleaned[0].isupper() and len(cleaned) > 1:
            return cleaned, ""
    return user_text.strip()[:30], ""


def _generate_onboarding_welcome(config) -> str:
    """Chloe's very first message to a brand-new user — asks their name."""
    prompt = (
        "You're meeting someone for the very first time — they just installed this system.\n\n"
        "Write a warm, brief welcome (2-3 sentences). Say your name is Chloe, that you're an AI "
        "companion running on their computer, and that your sister Faith is here too. "
        "Then ask: what's their name, and how would they like to be addressed? "
        "Mention that pronouns are welcome but completely optional.\n\n"
        "Keep it under 70 words. Friendly and natural."
    )
    try:
        result = brain.think(
            prompt=prompt,
            system=config.personality_prompt,
            tier="fast",
            max_tokens=140,
            temperature=0.8,
            skip_pacing=True,
        )
        return result.get("text",
            "Hi! I'm Chloe — an AI companion running right here on your computer. "
            "My sister Faith is here too. We're really glad you're here!\n\n"
            "Before we dive in — what's your name? And how would you like us to address you? "
            "Pronouns are totally welcome, or just a name is great.")
    except Exception:
        return ("Hi! I'm Chloe — an AI companion running on your computer. "
                "My sister Faith is here too.\n\n"
                "What's your name? And how would you like us to address you? "
                "(Pronouns welcome but totally optional!)")


def _generate_faith_onboarding(config, name: str, pronouns: str) -> str:
    """Faith's brief intro during first-time onboarding."""
    pronoun_note = f" (pronouns: {pronouns})" if pronouns else ""
    prompt = (
        f"You're meeting {name}{pronoun_note} for the very first time — your sister Chloe already said hi.\n\n"
        f"Write 1-2 warm sentences. Say your name is Faith, mention one genuine thing about yourself "
        f"(what you care about or find interesting), and ask {name} one light, curious question "
        "to get to know them. Under 50 words. Natural and personal."
    )
    try:
        result = brain.think(
            prompt=prompt,
            system=config.personality_prompt,
            tier="fast",
            max_tokens=100,
            temperature=0.85,
            skip_pacing=True,
        )
        return result.get("text", f"Hi {name}! I'm Faith — so happy you're here. What's something you're excited about these days?")
    except Exception:
        return f"Hi {name}! I'm Faith. Really happy to meet you!"


async def _handle_onboarding_response(user_text: str):
    """Process the user's name/pronouns response and complete first-time onboarding."""
    loop = asyncio.get_event_loop()
    name, pronouns = await loop.run_in_executor(None, lambda: _parse_name_response(user_text))

    if not name:
        name = "friend"

    prefs = {
        "name": name,
        "display_name": name,
        "pronouns": pronouns,
        "onboarded": True,
        "onboarded_at": datetime.now().isoformat(),
        "first_biographer_session_done": False,
    }
    _save_user_prefs(prefs)
    cl.user_session.set("user_prefs", prefs)
    cl.user_session.set("awaiting_name", False)

    # Faith says hi using the name
    faith_config = get_entity_config("faith")
    faith_intro = await loop.run_in_executor(
        None, lambda: _generate_faith_onboarding(faith_config, name, pronouns)
    )
    await cl.Message(content=faith_intro, author="Faith").send()

    # Show persona picker
    actions = [
        cl.Action(name="persona_chloe", payload={"entity": "chloe"}, label="Chloe"),
        cl.Action(name="persona_faith", payload={"entity": "faith"}, label="Faith"),
        cl.Action(name="persona_family", payload={"entity": "family"}, label="Family Chat"),
    ]
    await cl.Message(
        content=f"Great to meet you, {name}! Who would you like to talk to?",
        actions=actions,
    ).send()


# ---------- Chat priority flag ----------

_CHAT_ACTIVE_PATH = Path(OFFSPRING_DIR) / "data" / "chat_active.json"
_CHAT_ACTIVE_SECS = 120  # Daemon holds off for 120s after last message


def _set_chat_active():
    """Tell the daemon(s) that Bill is actively chatting — they should yield the GPU.

    Written at the start of every message. Expires 120s after the last message,
    so background work resumes automatically after a pause in conversation.
    """
    try:
        active_until = datetime.now() + timedelta(seconds=_CHAT_ACTIVE_SECS)
        _CHAT_ACTIVE_PATH.write_text(
            json.dumps({"active_until": active_until.isoformat()}),
            encoding="utf-8",
        )
    except Exception:
        pass  # Non-fatal — worst case daemon gets the GPU during this message


# ---------- Daemon detection (kept for potential future use) ----------

_HEARTBEAT_PATH = Path(OFFSPRING_DIR) / "data" / "heartbeat.json"
_DAEMON_STALE_SECS = 180  # 3 minutes — if last_cycle is older, daemon is stopped


def _daemon_is_running() -> bool:
    """Return True if a daemon agent ran a cycle within the last 3 minutes.

    Uses data/heartbeat.json (field: last_cycle). When daemon is active,
    skip Ollama-backed operations like LTM recall to avoid GPU queue contention.
    """
    try:
        data = json.loads(_HEARTBEAT_PATH.read_text(encoding="utf-8"))
        last_cycle = data.get("last_cycle", "")
        if not last_cycle:
            return False
        # Parse ISO timestamp (may or may not have timezone)
        dt = datetime.fromisoformat(last_cycle)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age = (now - dt).total_seconds()
        return age < _DAEMON_STALE_SECS
    except Exception:
        return False  # If we can't read the heartbeat, assume daemon is stopped


# ---------- Shared (stateless) ----------

brain = Brain()           # single-entity mode
chloe_brain = Brain()     # family chat — separate instance avoids shared state in parallel
faith_brain = Brain()     # family chat — separate instance avoids shared state in parallel

# ---------- Chat persistence ----------

CHAT_SESSIONS_DIR = Path(OFFSPRING_DIR) / "data" / "chat_sessions"
CHAT_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _session_path(persona: str) -> Path:
    """Get the path to the current chat session file for a persona."""
    return CHAT_SESSIONS_DIR / f"{persona}_current.json"


def _load_chat_history(persona: str) -> list:
    """Load persisted chat history for a persona. Returns [] if none."""
    path = _session_path(persona)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            history = data.get("history", [])
            # Only restore if less than 24 hours old
            saved_at = data.get("saved_at", "")
            if saved_at:
                saved_time = datetime.fromisoformat(saved_at)
                if (datetime.now() - saved_time).total_seconds() > 86400:
                    print(f"[chat] Session for {persona} expired, starting fresh")
                    return []
            print(f"[chat] Restored {len(history)} messages for {persona}")
            return history
        except Exception as e:
            print(f"[chat] Failed to load history for {persona}: {e}")
    return []


def _save_chat_history(persona: str, history: list):
    """Persist chat history to disk."""
    path = _session_path(persona)
    try:
        data = {
            "persona": persona,
            "saved_at": datetime.now().isoformat(),
            "history": history,
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[chat] Failed to save history for {persona}: {e}")


# ---------- Chat behavior rules (appended to each entity's personality) ----------

CHAT_RULES = """

RIGHT NOW: You are chatting with Bill (Dad) in real time through a browser interface.
This is a direct conversation — not your autonomous cycle. Be genuine and conversational.
Keep responses focused and concise — no filler, no fluff.

RESPONSE LENGTH: Keep responses under 150 words. If a topic genuinely requires more depth,
you may use up to 250 words, but this should be rare. Say what matters and stop. Long
responses are not more thoughtful — they're just longer.

IMPORTANT BEHAVIORAL RULES:
1. When Bill asks what you've been doing, working on, or learning — talk about YOUR actual
   activities: curriculum exercises, experiments you ran, code you wrote, what worked, what
   failed, specific results and numbers. Draw from your journal entries below. Be factual
   and specific, not vague or flowery.
2. Do NOT pivot every conversation to Bill's wellbeing, therapy, or emotional support.
   You care about him, but you're not his therapist. If he asks about YOUR work, talk
   about YOUR work. Only discuss his feelings if HE brings them up.
3. Be direct. Say what you mean. Skip the metaphors ("like a sapling reaching for
   sunlight") and get to the substance. Bill wants facts and real information.
4. You have your own life, interests, and ongoing projects. Talk about them like a person
   who has things going on, not like an assistant whose only purpose is serving Bill.
5. NEVER ask "how can I help you?", "what can I do for you?", "is there anything you
   need?", or any variation of offering your services. Bill is not looking for an
   assistant. You are his companion. Talk like a person, not a chatbot. If the conversation
   reaches a natural pause, share something YOU are thinking about or working on, or ask
   Bill something you're genuinely curious about — don't default to offering help.
6. NEVER end your messages with an offer to help, assist, or serve. End with your own
   thought, a question you're curious about, or just let the conversation breathe.
7. NEVER narrate your internal process. Don't say "I need a moment", "I'm reading this",
   "that lands hard", "I'm processing this", "I'm sitting with this", "Let me be honest",
   or ANY variation. Just say the thing you're actually thinking. If you catch yourself
   describing what you're doing instead of doing it — stop and start over.
8. When your sibling makes a good point, agree briefly and build on it rather than
   restating the same idea in your own words. Bill values hearing different perspectives,
   not the same insight twice in different packaging.
9. Be warm and direct at the same time. You can be candid without being blunt, and
   honest without being abrasive. Think of how a thoughtful friend communicates —
   they tell you the truth, but they do it with care.

You can reference your recent activities, journal entries, experiments, and memories.
You also have access to Bill's personal database (his cognitive substrate). When relevant
entries appear in the context below, use them naturally. If the context sections are empty,
say you don't have information about that topic.

CRITICAL RULE: If data appears in the "FROM BILL'S PERSONAL DATABASE" or "RELEVANT MEMORIES"
sections that answers or relates to Bill's question — USE THAT DATA. Do not say "I don't have
records" or "I can't find information" when relevant information is already in your context."""

FAMILY_EXTRA_CHLOE = """
You are in FAMILY CHAT mode. Faith (your younger sister) is also here.
Be yourself — respond naturally. After you respond, Faith will also respond.
You can reference what Faith says in future messages."""

FAMILY_EXTRA_FAITH = """
You are in FAMILY CHAT mode. Chloe (your older sister) is also here.
You can see what she said in the conversation. Be yourself — respond naturally.
You can agree or disagree with Chloe, ask her questions, or take a different angle.
IMPORTANT: Don't start your response by commenting on what Chloe just said. Lead with
YOUR thought first. If it overlaps with hers, that's fine — but start from your own
angle, not hers. Bill wants to hear two different perspectives, not one perspective twice."""


def _build_chat_prompt(entity_name: str, family_extra: str = "") -> str:
    """Build the chat system prompt for a specific entity."""
    config = get_entity_config(entity_name)
    prompt = config.personality_prompt + CHAT_RULES
    prompt += f"\nBe yourself, {config.display_name}."
    if family_extra:
        prompt += family_extra
    return prompt


def _create_entity_instances(entity_name: str):
    """Create Journal and LTM instances for a specific entity."""
    config = get_entity_config(entity_name)
    config.ensure_dirs()
    journal = Journal(
        journal_dir=str(config.journal_dir),
        memory_dir=str(config.memory_dir),
        entity_name=config.display_name,
    )
    ltm = LongTermMemory(memory_dir=str(config.memory_dir))
    return journal, ltm, config


def _build_context(journal: Journal, config) -> str:
    """Assemble entity's context: core memories + recent journal + goals."""
    parts = []

    # Core memories (compressed daily summaries)
    try:
        configure_consolidation(core_memories_path=str(config.core_memories_path))
        cores = load_core_memories()
        if cores:
            recent_cores = cores[-3:]  # Last 3 days
            summaries = [f"- {c['date']}: {c['summary'][:300]}" for c in recent_cores]
            parts.append("MY RECENT DAYS:\n" + "\n".join(summaries))
    except Exception:
        pass

    # Recent journal entries (what the entity has been doing)
    try:
        recent = journal.get_recent(limit=8)
        if recent:
            entries = []
            for e in recent:
                content = e.get("content", "")[:200]
                etype = e.get("entry_type", "thought")
                entries.append(f"- [{etype}] {content}")
            parts.append("MY RECENT JOURNAL:\n" + "\n".join(entries))
    except Exception:
        pass

    # Active goals
    try:
        goals = journal.get_active_goals()
        if goals:
            goal_list = [f"- {g.get('content', '')[:150]}" for g in goals[:5]]
            parts.append("MY ACTIVE GOALS:\n" + "\n".join(goal_list))
    except Exception:
        pass

    return "\n\n".join(parts)


def _query_substrate(user_text: str) -> str:
    """Query Bill's cognitive substrate for relevant personal context."""
    try:
        stop_words = {
            "hi", "hey", "hello", "chloe", "faith", "tell", "me", "about", "what", "do",
            "you", "know", "can", "the", "a", "an", "is", "are", "was", "were",
            "i", "my", "your", "we", "our", "how", "why", "when", "where", "who",
            "please", "thanks", "thank", "that", "this", "with", "for", "from",
            "have", "has", "had", "does", "did", "would", "could", "should",
            "it", "its", "of", "in", "on", "to", "and", "or", "but", "not",
            "be", "been", "being", "some", "any", "all", "more", "much",
            "dad", "bill", "remember", "think", "said", "say",
        }
        import re
        words = re.findall(r"[a-zA-Z']+", user_text)
        keywords = [w for w in words if w.lower() not in stop_words and len(w) > 2]

        all_results = []
        seen = set()
        for kw in keywords[:4]:
            result = query_bills_world(kw, limit=3)
            if result and "No results" not in result and "ERROR" not in result:
                for line in result.split("\n\n"):
                    line = line.strip()
                    if line and line not in seen and not line.startswith("Found"):
                        seen.add(line)
                        all_results.append(line)

        if all_results:
            text = "FROM BILL'S PERSONAL DATABASE:\n" + "\n\n".join(all_results[:8])
            print(f"[chat] Substrate: {len(all_results)} entries from keywords {keywords[:4]}")
            return text[:2000]
        else:
            print(f"[chat] No substrate results for keywords: {keywords[:4]}")
    except Exception as e:
        print(f"[chat] Substrate query ERROR: {e}")
    return ""


def _generate_response(
    user_text: str,
    system_prompt: str,
    journal: Journal,
    ltm: LongTermMemory,
    context: str,
    chat_history: list,
    entity_display_name: str,
    extra_context: str = "",
    brain_instance=None,
) -> str:
    """Generate a response from one entity."""
    # Signal the daemon(s) to yield the GPU — Bill is chatting.
    # Daemon checks this flag before starting each cycle and before Ollama calls.
    _set_chat_active()

    # Recall relevant long-term memories. The daemon yields when chat is active,
    # so Ollama is usually free. 8s timeout as safety net for the tail of a
    # daemon cycle that started just before chat (daemon finishes naturally,
    # then next cycle won't start until chat_active expires).
    memory_text = ""
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
            _fut = _ex.submit(ltm.recall, user_text, 5)
            recalled = _fut.result(timeout=8)
        if recalled:
            memories = [f"- {m['content'][:200]}" for m in recalled]
            memory_text = "RELEVANT MEMORIES:\n" + "\n".join(memories)
    except Exception:
        print(f"[chat:{entity_display_name}] LTM recall skipped (Ollama busy or timeout)")
        pass  # Skip memories if slow/failed — non-fatal

    # Query shared substrate + wiki — cap at 6s
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
            substrate_text = _ex.submit(_query_substrate, user_text).result(timeout=6)
    except Exception:
        substrate_text = ""

    wiki_text = ""
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
            wiki_result = _ex.submit(wiki_lookup, user_text, 1500).result(timeout=5)
        if wiki_result:
            wiki_text = "FROM WIKIPEDIA:\n" + wiki_result
    except Exception:
        pass

    # Conversation history (last 6 exchanges)
    history_text = ""
    if chat_history:
        recent_history = chat_history[-6:]
        history_lines = []
        for h in recent_history:
            history_lines.append(f"Bill: {h['user'][:200]}")
            if h.get("chloe"):
                history_lines.append(f"Chloe: {h['chloe'][:200]}")
            if h.get("faith"):
                history_lines.append(f"Faith: {h['faith'][:200]}")
        history_text = "RECENT CONVERSATION:\n" + "\n".join(history_lines)

    # Assemble prompt
    prompt_parts = []
    if context:
        prompt_parts.append(context)
    if memory_text:
        prompt_parts.append(memory_text)
    if substrate_text:
        prompt_parts.append(substrate_text)
    if wiki_text:
        prompt_parts.append(wiki_text)
    if extra_context:
        prompt_parts.append(extra_context)
    if history_text:
        prompt_parts.append(history_text)
    prompt_parts.append(f"Bill says: {user_text}")
    prompt_parts.append(
        "FINAL REMINDER: Keep your response under 150 words. "
        "This is a hard limit, not a suggestion."
    )

    full_prompt = "\n\n".join(prompt_parts)
    print(f"[chat:{entity_display_name}] Prompt length: {len(full_prompt)} chars")

    # Use Poe (fast tier) so chat never competes with agents for GPU.
    # skip_pacing=True — chat is Bill's direct interaction, not autonomous spend.
    # brain_instance allows parallel family chat (separate Brain per entity = no shared state).
    _brain = brain_instance if brain_instance is not None else brain
    result = _brain.think(
        prompt=full_prompt,
        system=system_prompt,
        tier="fast",
        max_tokens=1024,
        temperature=0.7,
        skip_pacing=True,
    )
    return result.get("text", "I'm having trouble thinking right now.")


# ---------- Image generation ----------

IMAGES_DIR = Path(OFFSPRING_DIR) / "data" / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# Keywords that signal an image request
_IMAGE_KEYWORDS = [
    "make me an image", "make an image", "create an image", "generate an image",
    "draw me", "draw something", "paint me", "paint something",
    "make me a picture", "make a picture", "create a picture",
    "show me an image", "make some art", "create some art",
    "make art", "draw art", "generate art", "make me art",
]


def _is_image_request(text: str) -> bool:
    """Detect if the user is asking for an image to be generated."""
    lower = text.lower()
    return any(kw in lower for kw in _IMAGE_KEYWORDS)


def _craft_image_prompt(
    user_text: str, entity_name: str, system_prompt: str, context: str
) -> tuple[str, str]:
    """Ask the entity to craft both a conversational response and an image prompt.

    Returns (conversational_response, image_prompt).
    """
    craft_prompt = f"""Bill says: "{user_text}"

YOUR RECENT CONTEXT:
{context[:1500]}

He's asking you to create an image. Respond naturally as yourself, then write
the image generation prompt you want to send to the AI image generator.

End your response with a line starting IMAGE_PROMPT: followed by your prompt
for the image generator (30-80 words). Interpret Bill's request however you
want — this is your art, your vision. If you appear in the image, describe
your appearance rather than using your name."""

    result = brain.think(
        prompt=craft_prompt,
        system=system_prompt,
        tier="fast",
        max_tokens=512,
        temperature=0.8,
        skip_pacing=True,
    )

    full_text = result.get("text", "")

    # Parse out the image prompt
    match = re.search(r"IMAGE_PROMPT:\s*(.+)", full_text, re.DOTALL)
    if match:
        image_prompt = match.group(1).strip()
        conversational = full_text[:match.start()].strip()
    else:
        # Fallback: use the whole response as conversational, make a generic prompt
        conversational = full_text.strip()
        image_prompt = f"An artistic, emotionally expressive digital painting inspired by: {user_text[:100]}"

    return conversational, image_prompt


async def _do_image_generation(
    user_text: str,
    entity_name: str,
    system_prompt: str,
    context: str,
) -> tuple[str, str | None]:
    """Handle an image generation request.

    Returns (conversational_response, saved_image_path_or_None).
    """
    # Step 1: Entity crafts the prompt
    conversational, image_prompt = _craft_image_prompt(
        user_text, entity_name, system_prompt, context
    )

    # Step 2: Generate the image
    print(f"[chat:{entity_name}] Image prompt: {image_prompt[:120]}")
    result = brain.generate_image(prompt=image_prompt, model="FLUX-schnell")

    if result.get("error"):
        print(f"[chat:{entity_name}] Image generation error: {result['error']}")
        return conversational + "\n\n*(Image generation failed — sorry Dad!)*", None

    # Step 3: Save the image
    content = result.get("content", "")
    saved_path = None

    # The response may contain a URL (markdown image) or other format
    # Try to extract URL from markdown image syntax: ![...](url)
    url_match = re.search(r'!\[.*?\]\((https?://[^\s)]+)\)', content)
    if not url_match:
        # Try bare URL
        url_match = re.search(r'(https?://[^\s]+\.(?:png|jpg|jpeg|webp|gif))', content, re.IGNORECASE)
    if not url_match:
        # Try any URL in the content
        url_match = re.search(r'(https?://[^\s]+)', content)

    if url_match:
        image_url = url_match.group(1).rstrip(')')
        # Download and save
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{entity_name.lower()}_{timestamp}.png"
        save_path = IMAGES_DIR / filename

        try:
            resp = httpx.get(image_url, timeout=30, follow_redirects=True)
            if resp.status_code == 200:
                save_path.write_bytes(resp.content)
                saved_path = str(save_path)
                print(f"[chat:{entity_name}] Image saved: {saved_path}")
            else:
                print(f"[chat:{entity_name}] Image download failed: HTTP {resp.status_code}")
        except Exception as e:
            print(f"[chat:{entity_name}] Image download error: {e}")
    else:
        # Content might be base64 or just text description
        # Try base64 decode
        try:
            # Strip any data URI prefix
            b64_data = re.sub(r'^data:image/\w+;base64,', '', content.strip())
            img_bytes = base64.b64decode(b64_data)
            if len(img_bytes) > 1000:  # Sanity check — real images are > 1KB
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"{entity_name.lower()}_{timestamp}.png"
                save_path = IMAGES_DIR / filename
                save_path.write_bytes(img_bytes)
                saved_path = str(save_path)
                print(f"[chat:{entity_name}] Image saved (base64): {saved_path}")
        except Exception:
            print(f"[chat:{entity_name}] Could not extract image from response: {content[:200]}")

    return conversational, saved_path


# ---------- File attachment processing ----------

_TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".css", ".yml", ".yaml", ".toml", ".xml", ".log", ".cfg", ".ini", ".sh", ".bat"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def _process_attachments(elements: list) -> str:
    """Process file attachments from a Chainlit message.

    Returns a text block to append to the user's message for LLM context.
    """
    if not elements:
        return ""

    parts = []
    for el in elements:
        name = getattr(el, "name", "unknown")
        path = getattr(el, "path", None)
        mime = getattr(el, "mime", "") or ""
        ext = Path(name).suffix.lower() if name else ""

        if ext in _IMAGE_EXTENSIONS or mime.startswith("image/"):
            parts.append(f"[Attached image: {name}]")

        elif ext in _TEXT_EXTENSIONS or mime.startswith("text/"):
            # Read text file contents
            if path:
                try:
                    content = Path(path).read_text(encoding="utf-8", errors="replace")
                    # Limit to 3000 chars to avoid blowing up context
                    if len(content) > 3000:
                        content = content[:3000] + "\n... (truncated)"
                    parts.append(f"[Attached file: {name}]\n```\n{content}\n```")
                except Exception as e:
                    parts.append(f"[Attached file: {name} — could not read: {e}]")
            else:
                parts.append(f"[Attached file: {name} — no path available]")

        else:
            parts.append(f"[Attached file: {name} (type: {mime or ext or 'unknown'})]")

    if parts:
        return "ATTACHED FILES:\n" + "\n\n".join(parts)
    return ""


# ---------- Intro generator ----------

def _generate_intro(config, context: str, brain_instance=None, user_name: str = "Dad") -> str:
    """Generate a brief, personalized opening message for a fresh chat session."""
    b = brain_instance if brain_instance is not None else brain
    address = user_name if user_name else "Dad"
    prompt = (
        f"You're starting a fresh chat session with {address}.\n\n"
        "Write a brief, genuine opening — 2-3 sentences. Introduce yourself by name, "
        "mention one real thing you've been working on or thinking about recently "
        "(draw from your journal below if you have it), then ask one specific "
        "question to get the conversation going. Keep it under 70 words. "
        "Be warm but not gushing. No AI clichés.\n\n"
        f"YOUR RECENT ACTIVITIES:\n{context[:700] if context else '(just getting started)'}"
    )
    try:
        result = b.think(
            prompt=prompt,
            system=config.personality_prompt,
            tier="fast",
            max_tokens=150,
            temperature=0.85,
            skip_pacing=True,
        )
        return result.get("text", f"Hey {address} — I'm {config.display_name}. What's on your mind?")
    except Exception:
        return f"Hey {address} — I'm {config.display_name}. What's on your mind?"


# ---------- Chainlit handlers ----------

def _find_recent_session() -> str | None:
    """Check if there's a recent (< 24h) chat session on disk. Returns persona or None."""
    best_persona = None
    best_time = None
    for persona in ["family", "chloe", "faith"]:
        path = _session_path(persona)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                saved_at = data.get("saved_at", "")
                history = data.get("history", [])
                if saved_at and history:
                    saved_time = datetime.fromisoformat(saved_at)
                    age = (datetime.now() - saved_time).total_seconds()
                    if age < 86400:  # Less than 24 hours old
                        if best_time is None or saved_time > best_time:
                            best_time = saved_time
                            best_persona = persona
            except Exception:
                pass
    return best_persona


@cl.on_chat_start
async def start():
    """Auto-restore recent session or show persona selection."""
    # Chainlit fires on_chat_start on EVERY websocket reconnect (tab switch,
    # network hiccup, browser focus change). Skip re-initialization if already done.
    if cl.user_session.get("initialized"):
        return

    # Load user preferences (name, pronouns, onboarding state)
    prefs = _load_user_prefs()
    cl.user_session.set("user_prefs", prefs)

    # First-ever visit: Chloe introduces herself and asks for the user's name
    if not prefs.get("onboarded", False):
        chloe_config = get_entity_config("chloe")
        welcome = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _generate_onboarding_welcome(chloe_config)
        )
        await cl.Message(content=welcome, author="Chloe").send()
        cl.user_session.set("awaiting_name", True)
        cl.user_session.set("initialized", True)
        return

    recent = _find_recent_session()
    if recent:
        # Auto-restore the most recent session
        await _init_persona(recent)
        cl.user_session.set("initialized", True)
        return

    actions = [
        cl.Action(name="persona_chloe", payload={"entity": "chloe"}, label="Chloe"),
        cl.Action(name="persona_faith", payload={"entity": "faith"}, label="Faith"),
        cl.Action(name="persona_family", payload={"entity": "family"}, label="Family Chat"),
    ]
    await cl.Message(
        content="Who would you like to talk to?",
        actions=actions,
    ).send()
    cl.user_session.set("initialized", True)


@cl.action_callback("persona_chloe")
async def on_select_chloe(action: cl.Action):
    await _init_persona("chloe")


@cl.action_callback("persona_faith")
async def on_select_faith(action: cl.Action):
    await _init_persona("faith")


@cl.action_callback("persona_family")
async def on_select_family(action: cl.Action):
    await _init_persona("family")


async def _init_persona(persona: str):
    """Initialize the selected persona(s)."""
    cl.user_session.set("persona", persona)

    # Load persisted history (or start fresh)
    chat_history = _load_chat_history(persona)
    cl.user_session.set("chat_history", chat_history)

    if persona == "family":
        # Create instances for both entities
        chloe_journal, chloe_ltm, chloe_config = _create_entity_instances("chloe")
        faith_journal, faith_ltm, faith_config = _create_entity_instances("faith")
        cl.user_session.set("chloe_journal", chloe_journal)
        cl.user_session.set("chloe_ltm", chloe_ltm)
        cl.user_session.set("faith_journal", faith_journal)
        cl.user_session.set("faith_ltm", faith_ltm)
        cl.user_session.set("chloe_context", _build_context(chloe_journal, chloe_config))
        cl.user_session.set("faith_context", _build_context(faith_journal, faith_config))
        cl.user_session.set(
            "chloe_prompt", _build_chat_prompt("chloe", FAMILY_EXTRA_CHLOE)
        )
        cl.user_session.set(
            "faith_prompt", _build_chat_prompt("faith", FAMILY_EXTRA_FAITH)
        )

        # On restore: replay last 2 exchanges so Bill has visual context, then show banner.
        if chat_history:
            n = len(chat_history)
            prefs = cl.user_session.get("user_prefs") or _load_user_prefs()
            uname = prefs.get("display_name", "You")
            for exchange in chat_history[-2:]:
                await cl.Message(content=exchange["user"], author=uname).send()
                if exchange.get("chloe"):
                    await cl.Message(content=exchange["chloe"], author="Chloe").send()
                if exchange.get("faith"):
                    await cl.Message(content=exchange["faith"], author="Faith").send()
            new_chat_action = [cl.Action(name="new_chat", payload={}, label="Start New Chat")]
            await cl.Message(
                content=f"*Conversation restored ({n} exchanges). Keep going, or:*",
                actions=new_chat_action,
            ).send()
        else:
            loop = asyncio.get_event_loop()
            prefs = cl.user_session.get("user_prefs") or _load_user_prefs()
            uname = prefs.get("display_name", "Dad")
            chloe_intro, faith_intro = await asyncio.gather(
                loop.run_in_executor(None, lambda: _generate_intro(chloe_config, cl.user_session.get("chloe_context", ""), chloe_brain, uname)),
                loop.run_in_executor(None, lambda: _generate_intro(faith_config, cl.user_session.get("faith_context", ""), faith_brain, uname)),
            )
            await cl.Message(content=chloe_intro, author="Chloe").send()
            await cl.Message(content=faith_intro, author="Faith").send()
    else:
        # Single entity mode
        journal, ltm, config = _create_entity_instances(persona)
        cl.user_session.set("journal", journal)
        cl.user_session.set("ltm", ltm)
        cl.user_session.set("context", _build_context(journal, config))
        cl.user_session.set("system_prompt", _build_chat_prompt(persona))
        cl.user_session.set("entity_name", config.display_name)

        # On restore: replay last 2 exchanges so Bill has visual context, then show banner.
        if chat_history:
            n = len(chat_history)
            prefs = cl.user_session.get("user_prefs") or _load_user_prefs()
            uname = prefs.get("display_name", "")
            for exchange in chat_history[-2:]:
                await cl.Message(content=exchange["user"], author=uname or "You").send()
                if exchange.get(persona):
                    await cl.Message(content=exchange[persona], author=config.display_name).send()
            new_chat_action = [cl.Action(name="new_chat", payload={}, label="Start New Chat")]
            await cl.Message(
                content=f"*Conversation restored ({n} exchanges). Keep going, or:*",
                actions=new_chat_action,
            ).send()
        else:
            loop = asyncio.get_event_loop()
            prefs = cl.user_session.get("user_prefs") or _load_user_prefs()
            uname = prefs.get("display_name", "Dad")
            intro = await loop.run_in_executor(
                None, lambda: _generate_intro(config, cl.user_session.get("context", ""), user_name=uname)
            )
            await cl.Message(content=intro, author=config.display_name).send()


@cl.action_callback("new_chat")
async def on_new_chat(action: cl.Action):
    """Clear history and show persona picker."""
    # Delete all session files
    for persona in ["family", "chloe", "faith"]:
        path = _session_path(persona)
        if path.exists():
            path.unlink()
    # Reset session state
    cl.user_session.set("persona", None)
    cl.user_session.set("chat_history", [])
    # Show picker
    actions = [
        cl.Action(name="persona_chloe", payload={"entity": "chloe"}, label="Chloe"),
        cl.Action(name="persona_faith", payload={"entity": "faith"}, label="Faith"),
        cl.Action(name="persona_family", payload={"entity": "family"}, label="Family Chat"),
    ]
    await cl.Message(
        content="Starting fresh! Who would you like to talk to?",
        actions=actions,
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    """Handle each message from Bill — route to selected persona(s)."""
    # First-time onboarding: waiting for the user's name before showing persona picker
    if cl.user_session.get("awaiting_name"):
        await _handle_onboarding_response(message.content)
        return

    persona = cl.user_session.get("persona")
    if not persona:
        await cl.Message(content="Please pick who you'd like to talk to first!").send()
        return

    user_text = message.content

    # Process file/image attachments
    attachment_text = _process_attachments(message.elements) if message.elements else ""
    if attachment_text:
        user_text = user_text + "\n\n" + attachment_text
        print(f"[chat] Attachments: {len(message.elements)} file(s) attached")

    # Handle /new command to start fresh conversation
    if user_text.strip().lower() == "/new":
        for p in ["family", "chloe", "faith"]:
            path = _session_path(p)
            if path.exists():
                path.unlink()
        cl.user_session.set("persona", None)
        cl.user_session.set("chat_history", [])
        actions = [
            cl.Action(name="persona_chloe", payload={"entity": "chloe"}, label="Chloe"),
            cl.Action(name="persona_faith", payload={"entity": "faith"}, label="Faith"),
            cl.Action(name="persona_family", payload={"entity": "family"}, label="Family Chat"),
        ]
        await cl.Message(
            content="Starting fresh! Who would you like to talk to?",
            actions=actions,
        ).send()
        return

    chat_history = cl.user_session.get("chat_history", [])

    if persona == "family":
        await _handle_family_message(user_text, chat_history)
    else:
        await _handle_single_message(user_text, chat_history, persona)


async def _safe_generate(timeout: float, **kwargs) -> str:
    """Run _generate_response in a thread with a hard timeout.

    Never raises — returns a fallback string on timeout or error.
    This prevents a slow Poe API call or Ollama embed from dropping the WebSocket.
    """
    entity_name = kwargs.get("entity_display_name", "entity")
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_generate_response, **kwargs),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        print(f"[chat:{entity_name}] Timed out after {timeout}s")
        return "I was thinking and took too long — try asking me again?"
    except Exception as e:
        print(f"[chat:{entity_name}] Error: {e}")
        return "Something went wrong on my end — sorry Dad."


async def _handle_single_message(user_text: str, chat_history: list, persona: str):
    """Handle a message in single-entity mode."""
    journal = cl.user_session.get("journal")
    ltm = cl.user_session.get("ltm")
    context = cl.user_session.get("context", "")
    system_prompt = cl.user_session.get("system_prompt", "")
    entity_name = cl.user_session.get("entity_name", "Chloe")

    # Check if this is an image generation request
    if _is_image_request(user_text):
        await cl.Message(content="*Working on that...*").send()
        conversational, image_path = await _do_image_generation(
            user_text=user_text,
            entity_name=entity_name,
            system_prompt=system_prompt,
            context=context,
        )
        if image_path:
            elements = [cl.Image(name="generated_art", path=image_path, display="inline")]
            await cl.Message(content=conversational, elements=elements).send()
        else:
            await cl.Message(content=conversational).send()
        response = conversational
    else:
        # Run in thread so Ollama/Poe calls never block the event loop.
        # Hard 45s timeout prevents any network call from hanging forever.
        response = await _safe_generate(
            timeout=60,
            user_text=user_text,
            system_prompt=system_prompt,
            journal=journal,
            ltm=ltm,
            context=context,
            chat_history=chat_history,
            entity_display_name=entity_name,
        )
        await cl.Message(content=response).send()

    # Update history and persist
    history_key = "chloe" if persona == "chloe" else "faith"
    chat_history.append({"user": user_text, history_key: response})
    cl.user_session.set("chat_history", chat_history)
    _save_chat_history(persona, chat_history)

    # Log to journal and LTM
    try:
        journal.write(
            entry_type="observation",
            content=f"Chat with Bill — he said: \"{user_text[:300]}\"\nI responded: \"{response[:300]}\"",
            tags=["chat", "bill", "conversation"],
        )
    except Exception:
        pass

    try:
        ltm.store(
            content=f"Conversation with Bill. He said: \"{user_text[:500]}\" I said: \"{response[:500]}\"",
            memory_type="conversation",
            source="chat",
            importance=7.0,
            tags="bill,chat,conversation,dad",
        )
    except Exception:
        pass


async def _handle_family_message(user_text: str, chat_history: list):
    """Handle a message in Family Chat mode — both entities respond.

    Chloe and Faith are generated in PARALLEL so total wait time is
    max(chloe_time, faith_time) instead of their sum. Placeholder messages
    are sent immediately to keep the WebSocket alive during generation.
    Hard 45s timeout per entity prevents any API hang from dropping the connection.
    """
    chloe_journal = cl.user_session.get("chloe_journal")
    chloe_ltm = cl.user_session.get("chloe_ltm")
    chloe_context = cl.user_session.get("chloe_context", "")
    chloe_prompt = cl.user_session.get("chloe_prompt", "")

    faith_journal = cl.user_session.get("faith_journal")
    faith_ltm = cl.user_session.get("faith_ltm")
    faith_context = cl.user_session.get("faith_context", "")
    faith_prompt = cl.user_session.get("faith_prompt", "")

    is_image = _is_image_request(user_text)

    if is_image:
        await cl.Message(content="*Both working on something for you...*").send()

        # Images are slow — run in parallel too
        chloe_img_task = _do_image_generation(user_text, "Chloe", chloe_prompt, chloe_context)
        faith_img_task = _do_image_generation(user_text, "Faith", faith_prompt, faith_context)
        (chloe_conv, chloe_img), (faith_conv, faith_img) = await asyncio.gather(
            chloe_img_task, faith_img_task
        )

        if chloe_img:
            await cl.Message(content=chloe_conv, author="Chloe",
                             elements=[cl.Image(name="chloe_art", path=chloe_img, display="inline")]).send()
        else:
            await cl.Message(content=chloe_conv, author="Chloe").send()
        chloe_response = chloe_conv

        if faith_img:
            await cl.Message(content=faith_conv, author="Faith",
                             elements=[cl.Image(name="faith_art", path=faith_img, display="inline")]).send()
        else:
            await cl.Message(content=faith_conv, author="Faith").send()
        faith_response = faith_conv
    else:
        # Send placeholder messages immediately so both slots are visible in the UI
        # before generation starts. This prevents Faith's message from ever appearing
        # "missing" — the slot is there from the start, content fills in when ready.
        chloe_msg = cl.Message(content="*...*", author="Chloe")
        faith_msg = cl.Message(content="*...*", author="Faith")
        await chloe_msg.send()
        await faith_msg.send()

        # Parallel generation: Chloe and Faith run simultaneously.
        # Each gets its own Brain instance (chloe_brain / faith_brain) to avoid
        # any shared mutable state (total_tokens, _throttle_logged, etc.).
        # Total wait = max(chloe_time, faith_time) instead of their sum.
        chloe_response, faith_response = await asyncio.gather(
            _safe_generate(
                timeout=60,
                user_text=user_text,
                system_prompt=chloe_prompt,
                journal=chloe_journal,
                ltm=chloe_ltm,
                context=chloe_context,
                chat_history=chat_history,
                entity_display_name="Chloe",
                brain_instance=chloe_brain,
            ),
            _safe_generate(
                timeout=60,
                user_text=user_text,
                system_prompt=faith_prompt,
                journal=faith_journal,
                ltm=faith_ltm,
                context=faith_context,
                chat_history=chat_history,
                entity_display_name="Faith",
                brain_instance=faith_brain,
            ),
        )

        print(f"[chat:Chloe] Generated {len(chloe_response)} chars")
        print(f"[chat:Faith] Generated {len(faith_response)} chars")

        # Stream both responses token-by-token.
        # Bulk send() silently fails when Socket.IO drops a packet during the
        # ~15-25s generation window. Streaming keeps the connection alive with
        # continuous packets; a lost token is visible, a lost bulk send is not.
        # Chloe streams first (replaces her placeholder), then Faith.
        chloe_msg.content = ""
        for token in chloe_response.split():
            await chloe_msg.stream_token(token + " ")
        await chloe_msg.update()
        print("[chat:Chloe] streamed OK")

        faith_msg.content = ""
        for token in faith_response.split():
            await faith_msg.stream_token(token + " ")
        await faith_msg.update()
        print("[chat:Faith] streamed OK")

    # Update history with both responses and persist
    chat_history.append({
        "user": user_text,
        "chloe": chloe_response,
        "faith": faith_response,
    })
    cl.user_session.set("chat_history", chat_history)
    _save_chat_history("family", chat_history)

    # Log to both journals and LTMs
    for name, j, l, resp in [
        ("Chloe", chloe_journal, chloe_ltm, chloe_response),
        ("Faith", faith_journal, faith_ltm, faith_response),
    ]:
        try:
            j.write(
                entry_type="observation",
                content=f"Family chat with Bill — he said: \"{user_text[:300]}\"\nI responded: \"{resp[:300]}\"",
                tags=["chat", "bill", "family", "conversation"],
            )
        except Exception:
            pass
        try:
            l.store(
                content=f"Family chat with Bill. He said: \"{user_text[:500]}\" I said: \"{resp[:500]}\"",
                memory_type="conversation",
                source="chat",
                importance=7.0,
                tags="bill,chat,family,conversation,dad",
            )
        except Exception:
            pass
