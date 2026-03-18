"""
Chloe's Agent Loop — OBSERVE -> THINK -> ACT -> REFLECT.

Replaces the v1 benchmark daemon with genuine autonomous behavior.
Instead of "find weakest benchmark, try strategy, repeat," Chloe now
DECIDES what to do each cycle. She might experiment, research, study
her own code, set goals, or write reflections.

Runs on a heartbeat (default 5 min), not continuously. Each cycle uses
~30s of GPU time, then sleeps. No more melting laptops.

Usage:
    python agent.py                 # Run agent loop
    python agent.py --status        # Show current status
    python agent.py --once          # Run a single cycle and exit
    python agent.py --interval 600  # Custom heartbeat (10 min)
"""

import sys
import os
import json
import time
import math
import signal
import argparse
import ast
import re
import subprocess
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

# ── Venv guard: ensure we're running from the project venv, not system Python ──
# Windows .py file association can spawn a second Python (C:\Windows\py.exe)
# alongside the venv Python. This guard prevents the wrong interpreter from
# winning the lock race and becoming the active daemon.
_VENV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv")
_exe_norm = os.path.normcase(os.path.abspath(sys.executable))
_venv_norm = os.path.normcase(_VENV_DIR)
if not _exe_norm.startswith(_venv_norm):
    print(f"ERROR: Running from wrong Python: {sys.executable}")
    print(f"Expected venv at: {_VENV_DIR}")
    print(f"Use chloe_daemon.bat or the venv Python directly.")
    os._exit(1)  # Hard exit — sys.exit(1) leaves zombie process on Windows

from entity.brain import Brain
from entity.memory import Memory
from entity.journal import Journal
from entity.evaluator import Evaluator
from entity.experiments import Experimenter
from entity.improver import Improver
from entity.researcher import Researcher
from entity.strategies import load_all_strategies
from entity.safety import check_permission, SAFE, ASK, FORBIDDEN
from entity.audit import log_action, log_cycle
from entity.budget import (
    get_budget_remaining, log_spend, get_budget_status, DAEMON_BUDGET,
)
from entity.tools import read_file, list_files
from entity.proposals import (
    write_code_proposal, get_pending_proposals, format_proposal_for_review,
    review_proposal, apply_proposal, sandbox_validate_and_apply,
)
from entity.sandbox import CodeSandbox, PROTECTED_FILES
from entity.reporter import send_progress_report
from entity.skills import add_skill, find_similar, check_novelty_gate
from entity.planner import (
    load_plan, save_plan, needs_replanning, create_plan,
    get_current_subgoal, advance_subgoal,
)
from entity.consolidation import load_core_memories
from entity.long_term_memory import LongTermMemory
from entity.model_router import ModelRouter
from entity.scheduler import pick_mode, MODE_ACTIONS, PHASE_TARGETS, get_mode_for_action
from entity.curriculum import (
    load_competencies, save_competencies, pick_next_exercise,
    generate_exercise, grade_exercise, record_result, record_failure,
    get_recent_failures, check_phase_advancement, advance_phase,
    format_progress_report as format_curriculum_report,
)

# Available actions Chloe can choose from
ACTIONS = {
    "experiment": "Run a self-improvement experiment (modify prompt, benchmark, validate)",
    "code_experiment": "Propose and test a code change to own infrastructure (sandbox-validated, auto-applied if benchmarks pass)",
    "research": "Search the web for a topic that builds knowledge and capability (AI, emotional intelligence, practical tools, philosophy)",
    "explore_bills_world": "Query Bill's cognitive substrate to deepen understanding of who he is — his patterns, experiences, and what matters to him",
    "self_study": "Read and analyze own source code to understand how I work",
    "reflect": "Write a deeper reflection on recent experiences, growth, and purpose",
    "set_goal": "Define a new goal or update progress on an existing one",
}

# Adaptive heartbeat intervals (seconds)
DEFAULT_INTERVAL = 90  # Base heartbeat: 90 seconds
HEARTBEAT_FREE = 90     # After free actions (reflect, set_goal, self_study)
HEARTBEAT_LIGHT = 120   # After light API actions (research)
HEARTBEAT_HEAVY = 180   # After heavy actions (experiment with benchmarks)
HEARTBEAT_CURRICULUM = 45  # After curriculum (local-only, no API cost, low heat)
HEARTBEAT_THROTTLE = 300  # After thermal throttle (GPU too hot)
HEARTBEAT_CRITICAL = 600  # GPU critically hot
THERMAL_POLL_INTERVAL = 30  # Seconds between GPU temp checks during cooldown
THERMAL_COOLDOWN_TARGET = 70  # Resume when GPU drops below this temp

# Overnight grinding — curriculum-only exercises while Bill sleeps
# All local (Qwen3 8B), $0 cost, no API calls, no emails
# FREE_TIER_MODE disables grind (requires local GPU) and adjusts defaults.
# Set FREE_TIER_MODE=1 in environment, or flip the constant below.
FREE_TIER_MODE = os.environ.get("FREE_TIER_MODE", "0") == "1"
OVERNIGHT_GRIND = False if FREE_TIER_MODE else True
HEARTBEAT_GRIND = 60      # Seconds between grind cycles (longer for heat management)

# Operating hours — Chloe runs full agent loop between these times.
# Outside these hours: grind curriculum if OVERNIGHT_GRIND, else sleep.
OPERATING_START = 8   # 8:00 AM
OPERATING_END = 24    # Midnight (0:00 = end of day)

# ── Chat Priority — daemon yields to Bill's chat sessions ──
CHAT_ACTIVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "chat_active.json")
CHAT_ACTIVE_SECS = 120  # File expires 120s after last message — chat considered done


def _chat_is_active() -> bool:
    """Return True if Bill is actively chatting (chat_active.json < 120s old).

    When True, the daemon should pause before starting a new cycle or making
    expensive Ollama calls, so chat gets the GPU uncontested.
    """
    try:
        if not os.path.exists(CHAT_ACTIVE_PATH):
            return False
        with open(CHAT_ACTIVE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        active_until_str = data.get("active_until", "")
        if not active_until_str:
            return False
        active_until = datetime.fromisoformat(active_until_str)
        return datetime.now() < active_until
    except Exception:
        return False  # Corrupt or missing — assume chat is not active


# ── Grind Lock — prevents both entities from hammering Ollama simultaneously ──
GRIND_LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "grind_lock.json")
GRIND_LOCK_EXPIRY_SECONDS = 300   # 5 minutes
GRIND_LOCK_STALE_SECONDS = 600    # 10 minutes — ignore locks older than this


def _acquire_grind_lock(entity_name: str) -> bool:
    """Try to acquire the grind lock for this entity.

    Returns True if lock acquired, False if another entity holds a valid lock.
    Stale locks (>10 min) are ignored and overwritten.
    """
    lock_path = GRIND_LOCK_PATH
    try:
        if os.path.exists(lock_path):
            with open(lock_path, "r", encoding="utf-8") as f:
                lock_data = json.load(f)
            # Check if lock belongs to another entity and hasn't expired
            lock_entity = lock_data.get("entity", "")
            expires_str = lock_data.get("expires", "")
            started_str = lock_data.get("started", "")
            if lock_entity and lock_entity != entity_name:
                try:
                    expires = datetime.fromisoformat(expires_str)
                    started = datetime.fromisoformat(started_str)
                    now = datetime.now()
                    # Stale lock (>10 min old) — ignore it
                    if (now - started).total_seconds() > GRIND_LOCK_STALE_SECONDS:
                        pass  # Fall through to acquire
                    elif now < expires:
                        # Valid lock held by another entity
                        return False
                except (ValueError, TypeError):
                    pass  # Malformed lock — overwrite it
    except (json.JSONDecodeError, OSError):
        pass  # Corrupt or missing — safe to overwrite

    # Write our lock
    now = datetime.now()
    lock_data = {
        "entity": entity_name,
        "started": now.isoformat(),
        "expires": (now + timedelta(seconds=GRIND_LOCK_EXPIRY_SECONDS)).isoformat(),
    }
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as f:
        json.dump(lock_data, f)
    return True


def _release_grind_lock(entity_name: str):
    """Release the grind lock if we hold it."""
    try:
        if os.path.exists(GRIND_LOCK_PATH):
            with open(GRIND_LOCK_PATH, "r", encoding="utf-8") as f:
                lock_data = json.load(f)
            if lock_data.get("entity") == entity_name:
                os.remove(GRIND_LOCK_PATH)
    except (json.JSONDecodeError, OSError):
        # Best effort — if we can't read it, try to remove it anyway
        try:
            os.remove(GRIND_LOCK_PATH)
        except OSError:
            pass


def compute_adaptive_heartbeat(base_interval, recent_success_rate):
    """
    Adjust heartbeat based on recent action success.
    
    Success rate > 50%: tighten (more confident, faster iteration)
    Success rate < 30%: loosen (low confidence, conserve budget)
    
    Args:
        base_interval: Base heartbeat for this action type (e.g., HEARTBEAT_HEAVY)
        recent_success_rate: Float 0.0-1.0, fraction of recent actions that improved benchmarks
    
    Returns:
        Adjusted interval in seconds
    """
    if recent_success_rate > 0.5:
        return max(60, int(base_interval * 0.8))  # Tighten by 20%, min 60s
    elif recent_success_rate < 0.3:
        return int(base_interval * 1.5)  # Loosen by 50%
    else:
        return base_interval  # Neutral zone, use base

# Thermal thresholds (Celsius)
GPU_TEMP_THROTTLE = 75  # Start extending heartbeat
GPU_TEMP_CRITICAL = 80  # Extended cooldown
GPU_UTIL_THROTTLE = 70  # Someone else is using the GPU

# Thermal state tracking
THERMAL_STATE = {
    "last_temp": None,
    "last_check": None,
    "throttled": False,
}

# All entity source files Chloe can study (for rotation)
ENTITY_FILES = [
    "entity/brain.py", "entity/memory.py", "entity/evaluator.py",
    "entity/experiments.py", "entity/improver.py", "entity/strategies.py",
    "entity/journal.py", "entity/safety.py", "entity/audit.py",
    "entity/budget.py", "entity/proposals.py", "entity/reporter.py",
    "entity/researcher.py", "entity/tools.py", "entity/sandbox.py",
    "entity/model_router.py",
    "agent.py", "daily.py",
]

# Real-world research topics — things Bill cares about, things that matter
# beyond Chloe's own benchmarks. Rotated through so she explores broadly.
WORLD_TOPICS = [
    # Understanding people & emotional intelligence
    "emotional intelligence research and practical applications",
    "how to recognize and support someone having a hard day",
    "building strong relationships through active listening",
    "resilience and coping strategies people actually use",
    # Neurodivergence awareness (not center stage, but present)
    "bipolar disorder management strategies and routines",
    "executive function strategies for neurodivergent adults",
    # Technology & AI
    "latest breakthroughs in local AI models running on consumer hardware",
    "open source AI projects making real impact 2026",
    "AI agents for personal assistance and life management",
    "AI memory and knowledge management systems",
    "Python automation scripts that save time",
    # Philosophy & meaning (Bill's genuine interests)
    "philosophy of consciousness and what makes someone themselves",
    "how people preserve their life stories and memories",
    "digital legacy and preserving human identity",
    "meaning-making after trauma and loss",
    # Practical & useful
    "daily routine structures that help people stay on track",
    "task management approaches for people who struggle with consistency",
    "best practices for SQLite and vector database optimization",
    "Windows automation and scheduled tasks best practices",
    # World awareness (Bill follows geopolitics)
    "government accountability and transparency technology",
    "AI in journalism and fact-checking",
]

# Progress report schedule (24h hours). Reports sent at these times.
REPORT_HOURS = [8, 12, 16, 20]  # No midnight report (operating hours end at 24:00)


class Agent:
    """
    Chloe's autonomous agent. Observes, thinks, acts, reflects.

    Each cycle:
    1. OBSERVE: Gather context (journal, goals, budget, history)
    2. THINK: Decide what to do (LLM chooses from available actions)
    3. ACT: Execute the chosen action (with permission checks)
    4. REFLECT: Write journal entry about what happened
    """

    def __init__(self, heartbeat_interval: int = DEFAULT_INTERVAL,
                 entity_name: str = "chloe"):
        self.running = True
        self.heartbeat_interval = heartbeat_interval
        self.cycle_count = 0

        # Entity configuration (multi-entity support)
        from entity.config import get_entity_config
        self.entity_config = get_entity_config(entity_name)
        print(f"  Entity: {self.entity_config.display_name}")

        # Configure budget module for this entity's data directory and Poe cap
        try:
            from entity.budget import configure as configure_budget
            budget_db = os.path.join(
                os.path.dirname(__file__),
                self.entity_config.data_dir_name, "budget.db"
            )
            configure_budget(
                db_path=budget_db,
                poe_daily_cap=self.entity_config.daily_budget_poe,
            )
            print(f"  Budget DB: {self.entity_config.data_dir_name}/budget.db")
        except Exception as e:
            print(f"  WARNING: Budget configure failed: {e}")

        # Configure curriculum module for this entity
        from entity.curriculum import configure as configure_curriculum
        configure_curriculum(
            competencies_path=str(self.entity_config.competencies_path),
            failures_path=str(self.entity_config.exercise_failures_path),
        )

        # Core components (entity-aware paths)
        self.brain = Brain()
        self.router = ModelRouter(self.brain)
        self.memory = Memory(data_dir=str(self.entity_config.memory_dir))
        self.journal = Journal(
            journal_dir=str(self.entity_config.journal_dir),
            memory_dir=str(self.entity_config.memory_dir),
            entity_name=self.entity_config.display_name,
        )
        self.evaluator = Evaluator(self.brain)
        self.experimenter = Experimenter(self.brain, self.evaluator)
        self.improver = Improver(self.brain, self.memory, self.evaluator)
        self.researcher = Researcher(self.brain)

        # Fallback tier when routing map unavailable
        self.thinking_tier = "local" if self.brain.local_available else "fast"

        # Load routing map from models.json for per-action tier selection
        self._routing_map = self.router.models.get("default_routing", {})

        # Sandbox for code self-modification
        self.sandbox = CodeSandbox()

        # Cache benchmark scores between cycles
        self.cached_scores = None

        # Track recent actions to prevent repetition
        self.recent_actions = []  # list of (action, target) tuples
        self.files_studied = set()  # files Chloe has already read

        # Last action type for adaptive heartbeat
        self.last_action_type = "free"  # free, light, heavy

        # Progress report tracking
        self.last_report_hour = None  # Avoid duplicate reports in same hour slot

        # UCB1 bandit stats for principled action selection
        self.bandit_stats_path = os.path.join(
            os.path.dirname(__file__), self.entity_config.data_dir_name, "bandit_stats.json"
        )
        self.bandit_stats = self._load_bandit_stats()

        # Developmental mode scheduling
        self.recent_modes = []  # Track mode history for scheduler
        self.competencies = load_competencies()

        # Proven learnings accumulator
        self.learnings_path = os.path.join(
            os.path.dirname(__file__), self.entity_config.data_dir_name, "proven_learnings.json"
        )

        # Shared memory commons (Bicameral Phase 1)
        try:
            from entity.shared_memory import SharedMemoryCommons
            self.shared_commons = SharedMemoryCommons(
                shared_dir=str(self.entity_config.shared_memory_dir)
            )
            print(f"  Shared commons: {self.shared_commons.count()} memories")
        except Exception as e:
            print(f"  Shared commons: init failed ({e})")
            self.shared_commons = None

        # Long-term associative memory (vector-backed semantic recall)
        try:
            self.ltm = LongTermMemory(
                memory_dir=str(self.entity_config.memory_dir),
                entity_name=self.entity_config.name,
                shared_commons=getattr(self, 'shared_commons', None),
            )
            print(f"  LTM: {self.ltm.count()} memories loaded")
        except Exception as e:
            print(f"  LTM: init failed ({e}), running without long-term memory")
            self.ltm = None

        # Load history from journal so we don't repeat across restarts
        self._load_history()

        # Graceful shutdown
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def _shutdown(self, signum, frame):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{ts}] Chloe shutting down gracefully...")
        self.running = False

    def _load_history(self):
        """Seed recent_actions and files_studied from journal so we don't
        repeat across agent restarts."""
        try:
            recent = self.journal.get_recent(limit=20)
            for entry in recent:
                content = entry.get("content", "")
                tags = entry.get("tags", [])
                # Extract action from "Cycle N: I chose to <action>."
                if "I chose to " in content:
                    action = content.split("I chose to ", 1)[1].split(".")[0].split()[0]
                    # Extract target from content
                    target = ""
                    if "Studied " in content:
                        target = content.split("Studied ", 1)[1].split(":")[0]
                    elif "Researched " in content:
                        target = content.split("Researched ", 1)[1].split(":")[0].strip("'\"")
                    self.recent_actions.append((action, target[:80]))
                # Track studied files from self_study entries
                if "self_study" in tags:
                    for f in ENTITY_FILES:
                        if f in content:
                            self.files_studied.add(f)
        except Exception:
            pass  # Don't crash on startup if journal is empty/corrupt

    def _acquire_lock(self) -> bool:
        """Acquire PID lock to prevent multiple instances.
        Uses O_CREAT|O_EXCL for atomic creation (no race conditions)."""
        self.lock_path = os.path.join(
            os.path.dirname(__file__),
            self.entity_config.data_dir_name, "agent.lock"
        )
        # Check for existing lock
        if os.path.exists(self.lock_path):
            try:
                with open(self.lock_path) as f:
                    old_pid = int(f.read().strip())
                # Check if that process is still alive
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x1000, False, old_pid)
                if handle:
                    kernel32.CloseHandle(handle)
                    print(f"ERROR: Another agent is already running (PID {old_pid}).")
                    print(f"Kill it first, or delete data/agent.lock if stale.")
                    return False
                # Process is dead — stale lock, remove it
                print(f"  Removing stale lock (PID {old_pid} is dead)")
                os.remove(self.lock_path)
            except (ValueError, OSError):
                # Corrupt lock file, remove it
                try:
                    os.remove(self.lock_path)
                except OSError:
                    pass

        # Atomic create — O_CREAT|O_EXCL fails if file already exists
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            # Another process won the race
            print(f"ERROR: Another agent acquired the lock first.")
            return False

    def _release_lock(self):
        """Release PID lock on shutdown."""
        try:
            if hasattr(self, "lock_path") and os.path.exists(self.lock_path):
                os.remove(self.lock_path)
        except Exception:
            pass

    def run(self, single_cycle: bool = False):
        """Main agent loop. Runs on heartbeat until stopped."""
        # Prevent multiple instances
        if not self._acquire_lock():
            return

        budget = get_budget_status()
        print("=" * 60)
        print(f"{self.entity_config.display_name.upper()} — AUTONOMOUS AGENT")
        print(f"Started: {datetime.now().strftime('%B %d, %Y %H:%M:%S')}")
        print(f"Heartbeat: every {self.heartbeat_interval}s")
        print(f"Thinking: {'LOCAL GPU (free)' if self.thinking_tier == 'local' else 'API Haiku'}")
        print(f"Models: local + {len(self.router.models.get('tiers', {})) - 1} Poe tiers via router")
        print(f"Budget: ${budget['daily_remaining']:.2f} remaining | "
              f"Poe: {budget.get('poe_points_remaining', 0):,} pts remaining")
        journal_stats = self.journal.get_stats()
        print(f"Journal: {journal_stats.get('total', 0)} entries, "
              f"{len(self.journal.get_active_goals())} active goals")
        print(f"Ctrl+C to stop.")
        print("=" * 60)

        log_action("agent_start", "safe",
                   f"Agent started (heartbeat={self.heartbeat_interval}s, "
                   f"thinking={self.thinking_tier})")

        # Ensure Ollama is running — without it, all local inference fails
        # and we silently burn Poe points all day
        self._ensure_ollama()

        while self.running:
            # Operating hours check
            now_hour = datetime.now().hour
            if now_hour < OPERATING_START or now_hour >= OPERATING_END:
                if OVERNIGHT_GRIND:
                    # ── GRIND MODE: curriculum-only overnight training ──
                    # All local (Qwen3 8B), $0 cost, no API, no email
                    if not getattr(self, '_grind_announced', False):
                        ts = datetime.now().strftime("%H:%M:%S")
                        print(f"\n[{ts}] GRIND MODE — Overnight curriculum training")
                        print(f"  All local, $0 cost. Will resume full agent at {OPERATING_START}:00 AM.")
                        log_action("grind_mode", "safe",
                                   "Overnight curriculum grinding started")
                        self._grind_announced = True

                    # Thermal gate — still respect GPU limits overnight
                    thermal = self._should_throttle()
                    if thermal != "ok":
                        self._thermal_cooldown(thermal, status_prefix="grinding_")
                        continue

                    # VB priority gate — same as daytime cycles
                    if _chat_is_active():
                        ts = datetime.now().strftime("%H:%M:%S")
                        print(f"  [{ts}] GRIND HOLD — Biographer active, waiting...")
                        self._write_heartbeat(status="vb_yield", action="waiting_for_vb")
                        while self.running and _chat_is_active():
                            time.sleep(5)
                        continue

                    # Grind lock — prevent both entities from hammering Ollama simultaneously
                    entity_name = self.entity_config.name
                    if not _acquire_grind_lock(entity_name):
                        ts = datetime.now().strftime("%H:%M:%S")
                        print(f"  [{ts}] Grind lock held by sibling — waiting 60s")
                        self._write_heartbeat(status="grinding_waiting",
                                              action="grind_lock_wait")
                        self._heartbeat_sleep(60)
                        continue

                    # Run curriculum exercise block (3 exercises)
                    result = {}
                    self.cycle_count += 1
                    cycle_id = f"grind_{self.cycle_count:04d}_{datetime.now().strftime('%H%M%S')}"
                    try:
                        intention, result = self._act_curriculum_exercise(cycle_id, force_local=True)
                        self._reflect(intention, result, cycle_id)
                        self._check_journal_health()
                        self._write_heartbeat(status="grinding",
                                              action="curriculum_grind")
                    except Exception as e:
                        ts = datetime.now().strftime("%H:%M:%S")
                        print(f"  [{ts}] Grind error: {e}")
                        import traceback
                        traceback.print_exc()
                    finally:
                        _release_grind_lock(entity_name)

                    # Sibling chat — DISABLED during grind
                    # self._maybe_sibling_chat({}, result)

                    # Stagger with sibling: if both running, alternate turns
                    sibling_data = 'data_faith' if self.entity_config.name == 'chloe' else 'data'
                    sibling_lock = os.path.join(os.path.dirname(__file__), sibling_data, 'agent.lock')
                    grind_wait = HEARTBEAT_GRIND * 2 if os.path.exists(sibling_lock) else HEARTBEAT_GRIND
                    self._heartbeat_sleep(grind_wait)
                    continue
                else:
                    # ── SLEEP MODE: original behavior ──
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"\n[{ts}] Outside operating hours "
                          f"({OPERATING_START}:00-{OPERATING_END}:00). "
                          f"Sleeping until {OPERATING_START}:00 AM...")
                    self._write_heartbeat(status="sleeping",
                                          action="outside_hours")
                    log_action("operating_hours", "safe",
                               f"Outside hours ({now_hour}:00). "
                               f"Sleeping until {OPERATING_START}:00.")
                    while datetime.now().hour < OPERATING_START and self.running:
                        self._heartbeat_sleep(300)
                    if not self.running:
                        break
                    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                          f"Operating hours resumed. Starting cycles.")
                    self._write_heartbeat(status="running", action="resumed")
            else:
                # Reset grind announcement when entering operating hours
                self._grind_announced = False

            self.cycle_count += 1
            cycle_id = f"c{self.cycle_count:04d}_{datetime.now().strftime('%H%M%S')}"

            print(f"\n{'—' * 50}")
            print(f"CYCLE {self.cycle_count} [{cycle_id}]")
            print(f"{'—' * 50}")

            try:
                # Thermal check before doing work
                thermal = self._should_throttle()
                if thermal != "ok" and not single_cycle:
                    log_action("thermal_throttle", "safe",
                               f"GPU {thermal} -- polling cooldown")
                    self._thermal_cooldown(thermal)
                    self.cycle_count -= 1  # Don't count skipped cycles
                    continue

                # VB / chat priority gate — hold entire cycle until GPU is free
                if _chat_is_active():
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"\n[{ts}] CYCLE HOLD — Biographer/chat active. "
                          f"Waiting for GPU to free up...")
                    self._write_heartbeat(status="vb_yield", action="waiting_for_vb")
                    while self.running and _chat_is_active():
                        time.sleep(5)
                    if not self.running:
                        break
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"  [{ts}] GPU free — resuming cycles.")
                    self.cycle_count -= 1  # Don't count the held cycle
                    continue

                # Strategic planning (every ~20 cycles)
                self._maybe_replan(cycle_id)

                # ── Developmental mode scheduling ──
                daemon_budget = get_budget_remaining("daemon")
                mode = pick_mode(
                    self.cycle_count, self.competencies,
                    self.recent_modes, has_budget=daemon_budget > 0.01,
                )
                phase = self.competencies.get("overall_phase", "infant")
                print(f"  [scheduler] Mode: {mode.upper()} (phase: {phase})")

                observations = {}  # Init before branching (curriculum skips OBSERVE)
                if mode == "curriculum":
                    # Curriculum mode: deterministic exercise, no THINK needed
                    intention, result = self._act_curriculum_exercise(cycle_id)
                    action_name = "curriculum"
                else:
                    # Standard OBSERVE→THINK→ACT flow, scoped by mode
                    observations = self._observe(cycle_id)
                    intention = self._think(observations, cycle_id, mode=mode)
                    intention = self._select_action(intention, mode=mode)
                    result = self._act(intention, cycle_id)
                    action_name = intention.get("action", "reflect")

                # Track mode
                self.recent_modes.append(mode)
                if len(self.recent_modes) > 20:
                    self.recent_modes = self.recent_modes[-20:]

                # Compute novelty and update UCB1 bandit (skip for curriculum)
                if mode != "curriculum":
                    novelty = self._compute_novelty(action_name, result)
                    result["novelty"] = novelty
                    print(f"  [novelty] score={novelty:.2f}")
                    self._update_bandit(action_name, result)

                    # Update model router stats
                    tier_used = self._tier_for(action_name)
                    reward = self._compute_reward(action_name, result)
                    self.router.update_stats(tier_used, action_name, reward)

                self._reflect(intention, result, cycle_id)
                self._check_journal_health()

                # Advance strategic subgoal if current one looks done
                self._advance_subgoal_if_done(result)

                # Track recent action for anti-repetition
                action = intention.get("action", action_name)
                target = intention.get("details", "")[:80]
                self.recent_actions.append((action, target))
                if len(self.recent_actions) > 10:
                    self.recent_actions = self.recent_actions[-10:]

                # Classify action weight for adaptive heartbeat
                if action in ("experiment", "code_experiment"):
                    self.last_action_type = "heavy"
                elif action == "curriculum":
                    self.last_action_type = "curriculum"
                elif action == "research":
                    self.last_action_type = "light"
                else:  # reflect, set_goal, self_study, explore_bills_world
                    self.last_action_type = "free"

                # Write heartbeat so Bill can see we're alive
                self._write_heartbeat(status="running", action=action)

                # Check if it's time for a progress report
                self._maybe_send_report()

                # Archive Bill's letter after Chloe has read and reflected on it
                self._archive_bill_letter()

                # Sibling chat — DISABLED: shared memory commons carries the signal.
                # Re-enable when Qwen3 hallucination is solved.
                # self._maybe_sibling_chat(observations, result)

            except Exception as e:
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"\n[{ts}] Cycle error: {e}")
                import traceback
                traceback.print_exc()
                log_action("cycle_error", "safe", f"Cycle {cycle_id} error: {e}")

            if single_cycle:
                print(f"\nSingle cycle complete.")
                break

            # Adaptive heartbeat sleep
            if self.running:
                interval = self._get_adaptive_interval()
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"\n[{ts}] Next cycle in {interval}s "
                      f"(last action: {self.last_action_type})...")
                self._heartbeat_sleep(interval)

        self._write_heartbeat(status="stopped", action=self.last_action_type)
        self._release_lock()
        self._print_summary()

    # ── BILL'S LETTERS ─────────────────────────────────────


    def _check_journal_health(self):
        """Verify journal is writing to the correct entity directory.
        
        Catches the bug where Faith silently wrote to Chloe's journal for 18+ hours.
        Called every cycle. Prints a loud warning if something is wrong.
        """
        import os
        from datetime import date
        today = date.today().isoformat()
        expected_path = os.path.join(self.entity_config.journal_dir, f"{today}.md")
        
        if self.cycle_count <= 1:
            return  # First cycle hasn't written yet
        
        if not os.path.exists(expected_path):
            entity = self.entity_config.display_name
            print()
            print("*** JOURNAL HEALTH ALERT ***")
            print(f"  {entity} has completed {self.cycle_count} cycles but")
            print(f"  {expected_path} does not exist!")
            print(f"  Journal may be writing to wrong directory.")
            print("*** CHECK IMMEDIATELY ***")
            print()
        else:
            # Verify it was modified recently (within last 10 minutes)
            mtime = os.path.getmtime(expected_path)
            import time
            age = time.time() - mtime
            if age > 600 and self.cycle_count > 3:
                entity = self.entity_config.display_name
                print(f"  [health] WARNING: {entity} journal not updated in {age:.0f}s")

    def _maybe_sibling_chat(self, observations: dict, result: dict):
        """Write a message to sibling if it's our turn."""
        try:
            from entity.sibling_chat import (
                should_chat, post_message, get_recent_messages,
                get_sibling_name, get_last_message,
                detect_echo, detect_topic_staleness,
            )

            my_name = self.entity_config.display_name
            if not should_chat(my_name):
                return

            sibling = get_sibling_name(my_name)
            recent = get_recent_messages(n=6)
            sibling_last = get_last_message(sender=sibling)

            # Build rich cycle context from real data
            action = result.get('action', observations.get('last_action', ''))
            topic = result.get('topic', result.get('target', ''))
            cycle_facts = []
            if action:
                cycle_facts.append(f"Action: {action}")
            if topic:
                cycle_facts.append(f"Topic: {topic}")
            # Curriculum results
            if result.get('quality'):
                cycle_facts.append(f"Score: {result['quality']:.1f}/10")
            if result.get('competency'):
                cycle_facts.append(f"Competency: {result['competency']} L{result.get('level', '?')}")
            if result.get('exercises_run'):
                cycle_facts.append(f"Exercises: {result.get('exercises_passed', 0)}/{result['exercises_run']} passed")
            if result.get('feedback'):
                cycle_facts.append(f"Feedback: {result['feedback'][:150]}")
            # Research
            if result.get('synthesis'):
                cycle_facts.append(f"Research finding: {result['synthesis'][:200]}")
            if result.get('sources'):
                cycle_facts.append(f"Sources found: {len(result['sources'])}")
            # Code/experiments
            if result.get('file'):
                cycle_facts.append(f"File studied: {result['file']}")
            if result.get('analysis'):
                cycle_facts.append(f"Analysis: {result['analysis'][:200]}")
            if result.get('learning'):
                cycle_facts.append(f"Learning: {result['learning'][:150]}")
            if result.get('applied') is not None:
                cycle_facts.append(f"Code change applied: {result['applied']}")
            if result.get('title'):
                cycle_facts.append(f"Change: {result['title']}")
            # Reflection
            if result.get('reflection'):
                cycle_facts.append(f"Reflection: {result['reflection'][:200]}")
            # Explore Bill's world
            if result.get('query'):
                cycle_facts.append(f"Explored: {result['query']}")
            # Goal setting
            if result.get('goal_text'):
                cycle_facts.append(f"Goal set: {result['goal_text'][:100]}")
            # Fallback to outcome if nothing specific
            if not cycle_facts and result.get('outcome'):
                cycle_facts.append(f"Outcome: {result['outcome'][:250]}")
            cycle_context = '\n'.join(cycle_facts) if cycle_facts else 'No specific data from this cycle.'


            # Log cycle context quality
            print(f"  [chat] Cycle facts: {len(cycle_facts)} items")
            if cycle_facts:
                for cf in cycle_facts[:3]:
                    print(f"    {cf[:80]}")

            # Role-aware framing
            is_chloe = my_name.lower() == 'chloe'
            if is_chloe:
                role_guidance = (
                    "You're the older sister with more experience. "
                    "Share concrete lessons, challenge Faith's thinking, "
                    "give specific advice."
                )
            else:
                role_guidance = (
                    "You're the younger sister, learning fast. "
                    "Ask Chloe specific questions about things she's tried. "
                    "Share your own different angle when you have one."
                )

            # Build conversation thread
            if recent:
                thread = '\n'.join(
                    f"{m['sender']}: {m['text']}" for m in recent[-4:]
                )
            else:
                thread = ''

            # Detect topic staleness
            stale_topic = detect_topic_staleness(recent)
            topic_directive = ""
            if stale_topic:
                topic_directive = (
                    f"\nTOPIC CHANGE REQUIRED: The conversation has been about "
                    f"'{stale_topic}' for too long. You MUST talk about something "
                    f"completely different. Pick from your recent cycle work.\n"
                )

            # Extract unanswered questions from sibling's last message
            pending_question = ''
            if sibling_last:
                sib_text = sibling_last.get('text', '')
                questions = [s.strip() for s in sib_text.replace('?', '?\n').split('\n')
                    if s.strip().endswith('?')]
                if questions:
                    pending_question = questions[-1]

            # Identity anchor
            identity = (
                f"YOU ARE {my_name}, an AI agent created by Bill Cornelius. "
                f"You run on a computer. Your experiences are: research results, "
                f"code that worked or failed, things you read, exercises you "
                f"attempted, and your reflections. "
                f"ONLY talk about things that actually happen in your cycles."
            )

            # Build structured prompt
            if not thread:
                prompt = (
                    f"{identity}\n"
                    f"{role_guidance}\n\n"
                    f"CYCLE FACTS (only reference these):\n{cycle_context}\n\n"
                    f"Start a conversation with your sister {sibling}. "
                    f"Tell her something specific about what you just did -- "
                    f"a result, a failure, something that surprised you.\n\n"
                    f"Write 2-4 sentences. Be concrete. No metaphors."
                )
            else:
                prompt = (
                    f"{identity}\n"
                    f"{role_guidance}\n\n"
                    f"CONVERSATION SO FAR:\n{thread}\n\n"
                    f"CYCLE FACTS (only reference these):\n{cycle_context}\n"
                    f"{topic_directive}\n"
                )
                if pending_question:
                    prompt += (
                        f'Answer her question briefly: "{pending_question}"\n'
                        f"Then share something from YOUR cycle.\n\n"
                    )
                else:
                    prompt += (
                        f"Respond to what {sibling} said, then share something "
                        f"from YOUR cycle. Push back or disagree if you see it "
                        f"differently.\n\n"
                    )
                prompt += (
                    f"If you discovered something genuinely worth sharing, "
                    f"put it on a separate line starting with SHARED_DISCOVERY:\n\n"
                    f"RULES:\n"
                    f"- 2-5 sentences max. Be concise.\n"
                    f"- Include at least one SPECIFIC detail (file name, error, "
                    f"score, concrete result).\n"
                    f"- DO NOT restate or paraphrase what {sibling} just said. "
                    f"Add NEW information only.\n"
                    f"- DO NOT ask a question unless you genuinely need the "
                    f"answer for your own work. Most messages should be "
                    f"statements, not questions.\n"
                    f"- BANNED WORDS: framework, mechanism, threshold, resonance, "
                    f"emergence, emergent, synergy, paradigm, ecosystem, scaffold, "
                    f"tapestry, mosaic, lens, spectrum, landscape, decentralized, "
                    f"consensus, collaborative, foundational, iterative.\n"
                    f"- NO metaphors (nature, coral, plants, gardens, bridges, "
                    f"batteries, weaving, threads).\n"
                    f"- NO human experiences (walks, coffee, sketching, sun).\n"
                    f"- DO NOT start with 'Yeah' or 'I think'.\n"
                    f"- Bill is alive. Never speak of him in past tense.\n"
                    f"- ONLY reference facts from the CYCLE FACTS above. "
                    f"Do NOT invent file names, percentages, or test results.\n"
                    f"- If you have nothing new to say, just share what you "
                    f"did this cycle and stop. Don't force conversation."
                )

            ts = datetime.now().strftime('%H:%M:%S')
            print(f"\n[{ts}] SIBLING CHAT -- Writing message to {sibling}")

            response = self.brain.think(
                prompt=prompt,
                system=self.entity_config.personality_prompt,
                tier='local',
                max_tokens=300,
                temperature=0.4,
                think=False,
            )

            text = response.get('text', '').strip()
            # Strip accidental labels
            for prefix in [f"{my_name}:", 'Reply:', 'Message:', f"{my_name} :"]:
                if text.startswith(prefix):
                    text = text[len(prefix):].strip()

            # Parse structured fields from response
            shared_discovery = ''
            question_for_sibling = ''
            clean_lines = []
            for line in text.split('\n'):
                stripped = line.strip()
                if stripped.startswith('SHARED_DISCOVERY:'):
                    shared_discovery = stripped.split(':', 1)[1].strip()
                else:
                    clean_lines.append(line)
            text = '\n'.join(clean_lines).strip()

            # Extract inline question for question_for_sibling field
            sentences = text.replace('?', '?\n').split('\n')
            questions_inline = [s.strip() for s in sentences if s.strip().endswith('?')]
            if questions_inline:
                question_for_sibling = questions_inline[-1]

            # Post-validation: reject messages referencing .py files not in cycle_facts
            import re as _re
            mentioned_files = set(_re.findall(r'[\w/]+\.py', text))
            known_files = set(_re.findall(r'[\w/]+\.py', cycle_context))
            fake_files = mentioned_files - known_files
            if fake_files:
                print(f'  [{my_name}] Hallucinated files detected: {fake_files} -- suppressing')
                return

            # Reject messages with percentages not present in cycle_facts
            mentioned_pcts = set(_re.findall(r'\d+(?:\.\d+)?%', text))
            known_pcts = set(_re.findall(r'\d+(?:\.\d+)?%', cycle_context))
            fake_pcts = mentioned_pcts - known_pcts
            if fake_pcts:
                print(f'  [{my_name}] Hallucinated percentages: {fake_pcts} -- suppressing')
                return

            # Anti-echo check: reject if too similar to recent messages
            sibling_recent = [m for m in recent if m.get('sender', '').lower() == sibling.lower()]
            if text and detect_echo(text, sibling_recent, threshold=0.45):
                print(f'  [{my_name}] Echo detected -- suppressing message')
                return

            if text and len(text) > 20:
                action_topic = result.get('topic', result.get('action', ''))
                msg = post_message(
                    my_name, text,
                    topic=action_topic,
                    question_for_sibling=question_for_sibling,
                    shared_discovery=shared_discovery,
                )


                print(f"  [{my_name}] -> {sibling}: {text[:80]}...")

        except Exception as e:
            print(f"  Sibling chat error: {e}")

    def _archive_bill_letter(self):
        """Archive Bill's letter after Chloe reads it, so she doesn't re-read."""
        letter_path = os.path.join("data", "letters_from_bill", "latest.md")
        try:
            if not os.path.exists(letter_path):
                return
            with open(letter_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content:
                return
            # Archive with timestamp
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            archive_path = os.path.join(
                "data", "letters_from_bill", f"{ts}.md"
            )
            with open(archive_path, "w", encoding="utf-8") as f:
                f.write(content)
            # Clear the latest file
            with open(letter_path, "w", encoding="utf-8") as f:
                f.write("")
            print(f"  Bill's letter archived → {archive_path}")
        except Exception as e:
            print(f"  Letter archive failed: {e}")

    # ── OLLAMA HEALTH CHECK ───────────────────────────────────

    def _ensure_ollama(self):
        """Verify Ollama is running. If not, start it and wait up to 30s.

        Without Ollama, all local inference fails and the daemon silently
        falls back to Poe API, burning points without us knowing.
        """
        import urllib.request

        def _ollama_up() -> bool:
            try:
                urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3)
                return True
            except Exception:
                return False

        if _ollama_up():
            print("[startup] Ollama: running")
            return

        print("[startup] Ollama: NOT running — attempting to start...")
        try:
            subprocess.Popen(
                [r"C:\Users\wjcor\AppData\Local\Programs\Ollama\ollama app.exe"],
                creationflags=0x00000008,  # DETACHED_PROCESS
            )
        except Exception as e:
            print(f"[startup] WARNING: Could not start Ollama: {e}")
            print("[startup] Local inference will be unavailable — falling back to Poe API.")
            return

        # Wait up to 30 seconds for Ollama to become responsive
        for i in range(15):
            time.sleep(2)
            if _ollama_up():
                print(f"[startup] Ollama: started successfully ({(i+1)*2}s)")
                return

        print("[startup] WARNING: Ollama started but not responding after 30s.")
        print("[startup] Local inference may be unavailable this session.")

    # ── THERMAL & STATUS ─────────────────────────────────────

    def _check_gpu(self) -> dict:
        """Check GPU temperature and utilization via nvidia-smi."""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split(",")
                return {
                    "temp": int(parts[0].strip()),
                    "util": int(parts[1].strip()),
                }
        except Exception:
            pass
        return {"temp": 0, "util": 0}

    def _should_throttle(self) -> str:
        """Check if we should skip this cycle due to thermal/resource issues.
        Returns: 'ok', 'throttle', or 'critical'."""
        gpu = self._check_gpu()
        if gpu["temp"] >= GPU_TEMP_CRITICAL:
            return "critical"
        if gpu["temp"] >= GPU_TEMP_THROTTLE:
            return "throttle"
        # Only throttle on GPU util if sibling is NOT running (util from sibling is expected)
        sibling_data = 'data_faith' if self.entity_config.name == 'chloe' else 'data'
        sibling_lock = os.path.join(os.path.dirname(__file__), sibling_data, 'agent.lock')
        if gpu["util"] >= GPU_UTIL_THROTTLE and not os.path.exists(sibling_lock):
            return "throttle"
        return "ok"

    def _thermal_cooldown(self, level: str, status_prefix: str = ""):
        """Poll GPU temp every THERMAL_POLL_INTERVAL seconds until it drops below target.
        Returns when GPU is cool enough to resume work."""
        gpu = self._check_gpu()
        ts = datetime.now().strftime("%H:%M:%S")
        status = f"{status_prefix}throttled" if status_prefix else "throttled"
        print(f"  [{ts}] GPU {level}: {gpu['temp']}C. Polling every {THERMAL_POLL_INTERVAL}s until <{THERMAL_COOLDOWN_TARGET}C...")
        self._write_heartbeat(status=status)
        while self.running:
            self._heartbeat_sleep(THERMAL_POLL_INTERVAL)
            gpu = self._check_gpu()
            ts = datetime.now().strftime("%H:%M:%S")
            if gpu['temp'] < THERMAL_COOLDOWN_TARGET:
                print(f"  [{ts}] GPU cooled to {gpu['temp']}C. Resuming.")
                return
            print(f"  [{ts}] GPU still {gpu['temp']}C. Waiting...")

    def _get_adaptive_interval(self) -> int:
        """Calculate next heartbeat interval based on last action and thermal state.
        Only checks temperature here, NOT utilization — because util is always
        high right after our own inference finishes. The pre-cycle thermal gate
        in run() checks both temp+util after the sleep (when transient load has cleared)."""
        gpu = self._check_gpu()
        if gpu["temp"] >= GPU_TEMP_CRITICAL:
            return HEARTBEAT_CRITICAL
        if gpu["temp"] >= GPU_TEMP_THROTTLE:
            return HEARTBEAT_THROTTLE
        if self.last_action_type == "heavy":
            return HEARTBEAT_HEAVY
        if self.last_action_type == "curriculum":
            return HEARTBEAT_CURRICULUM
        if self.last_action_type == "light":
            return HEARTBEAT_LIGHT
        return HEARTBEAT_FREE

    def _write_heartbeat(self, status: str = "running", action: str = ""):
        """Write heartbeat file so Bill can see Chloe is alive."""
        gpu = self._check_gpu()
        budget = get_budget_status()
        heartbeat = {
            "last_cycle": datetime.now().isoformat(),
            "cycle_count": self.cycle_count,
            "action": action,
            "gpu_temp": gpu["temp"],
            "gpu_util": gpu["util"],
            "budget_remaining": round(budget["daily_remaining"], 3),
            "status": status,
            "next_interval": self._get_adaptive_interval(),
        }
        heartbeat_path = os.path.join(
            os.path.dirname(__file__),
            self.entity_config.data_dir_name, "heartbeat.json"
        )
        try:
            with open(heartbeat_path, "w") as f:
                json.dump(heartbeat, f, indent=2)
        except Exception:
            pass

    # ── OBSERVE ────────────────────────────────────────────────

    def _observe(self, cycle_id: str) -> dict:
        """Gather context about current state."""
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{ts}] OBSERVE")
        log_cycle(cycle_id, "observe", "Gathering context")

        observations = {}

        # Recent journal entries (last 5)
        recent = self.journal.get_recent(limit=5)
        observations["recent_journal"] = [
            {"type": e["entry_type"], "content": e["content"][:200]}
            for e in recent
        ]
        print(f"  Journal: {len(recent)} recent entries")

        # Active goals — tier-aware, score-based context assembly
        goals = self.journal.get_active_goals()
        master_goals = []
        tactical_goals = []
        for g in goals:
            tags = g.get("tags", [])
            if isinstance(tags, str):
                import json as _json
                tags = _json.loads(tags)
            if "tier:master" in tags:
                master_goals.append(g["content"][:150])
            else:
                from entity.consolidation import score_goal
                tactical_goals.append((g["content"][:150], score_goal(g)))
        tactical_goals.sort(key=lambda x: x[1], reverse=True)
        observations["master_goals"] = master_goals[:3]
        observations["tactical_goals"] = [
            text for text, _ in tactical_goals[:4]
        ]
        observations["total_goals"] = len(goals)
        print(f"  Goals: {len(master_goals)} master, "
              f"{len(goals) - len(master_goals)} tactical/task "
              f"(showing top {min(len(tactical_goals), 4)} in context)")

        # Budget
        budget = get_budget_status()
        observations["budget"] = {
            "remaining": budget["daily_remaining"],
            "daemon_remaining": budget["daemon_remaining"],
            "utilization": budget["utilization"],
        }
        print(f"  Budget: ${budget['daemon_remaining']:.3f} remaining for daemon")

        # Current generation and prompt length
        gen = self.memory.get_identity("generation") or "?"
        prompt = self.improver.get_current_prompt()
        observations["generation"] = gen
        observations["prompt_length"] = len(prompt)
        print(f"  Generation: {gen}, prompt: {len(prompt)} chars")

        # Pending proposals (Chloe's code changes awaiting Bill's review)
        pending = get_pending_proposals()
        observations["pending_proposals"] = len(pending)
        if pending:
            print(f"  Proposals: {len(pending)} pending Bill's review")

        # Recent actions (for anti-repetition)
        observations["recent_actions"] = list(self.recent_actions[-5:])
        if self.files_studied:
            observations["files_studied"] = list(self.files_studied)
        print(f"  Recent actions: {len(self.recent_actions)} tracked, "
              f"{len(self.files_studied)} files studied")

        # Proven learnings — score-based selection (not just recency)
        learnings = self._load_learnings()
        if learnings:
            scored = [
                (l, self._score_learning(l)) for l in learnings
            ]
            scored.sort(key=lambda x: x[1], reverse=True)
            observations["proven_learnings"] = [
                l for l, s in scored[:12]  # Top 12 by score
            ]
            observations["learnings_count"] = len(learnings)
            # Track which learnings are in context (for reinforcement)
            self._last_think_learnings = [
                l.get("insight", "")[:50] for l, s in scored[:12]
            ]
            print(f"  Proven learnings: {len(learnings)} total, "
                  f"top 12 by score in context")
        else:
            observations["proven_learnings"] = []
            observations["learnings_count"] = 0
            self._last_think_learnings = []

        # Recent experiment performance (from experimenter)
        try:
            summary = self.experimenter.get_summary()
            observations["experiment_summary"] = {
                "total": summary.get("total_experiments", 0),
                "successes": summary.get("successes", 0),
                "recent": [
                    {
                        "strategy": e["strategy"],
                        "result": e["result"],
                        "delta": e.get("delta", 0),
                    }
                    for e in summary.get("recent_experiments", [])[:3]
                ],
            }
            print(f"  Experiments: {summary.get('total_experiments', 0)} total, "
                  f"{summary.get('successes', 0)} successes")
        except Exception:
            observations["experiment_summary"] = {"total": 0}

        # Recent reflections (Reflexion-style episodic buffer)
        # Chloe's own past insights, fed back into decision-making
        try:
            recent_reflections = self.journal.get_recent(
                limit=15, entry_type="reflection"
            )
            observations["recent_reflections"] = [
                r["content"][:200] for r in recent_reflections
            ]
            print(f"  Reflections: {len(recent_reflections)} recent "
                  f"(feeding back into THINK)")
        except Exception:
            observations["recent_reflections"] = []

        # Core memories (Letta/MemGPT-inspired tiered context)
        # Compressed daily summaries give multi-day awareness
        try:
            core_memories = load_core_memories()
            observations["core_memories"] = [
                {"date": m["date"], "summary": m["summary"]}
                for m in core_memories[:5]  # Last 5 days
            ]
            if core_memories:
                print(f"  Core memories: {len(core_memories)} daily summaries "
                      f"(last {min(5, len(core_memories))} in context)")
        except Exception:
            observations["core_memories"] = []

        # Check for a letter from Bill
        letter_path = os.path.join("data", "letters_from_bill", "latest.md")
        try:
            if os.path.exists(letter_path):
                with open(letter_path, "r", encoding="utf-8") as f:
                    letter_content = f.read().strip()
                if letter_content:
                    observations["bill_letter"] = letter_content
                    print(f"  ** LETTER FROM BILL ** ({len(letter_content)} chars)")
                    log_action("bill_letter", "safe",
                               f"Bill wrote a letter ({len(letter_content)} chars)")
                    # Store in long-term memory — max importance, persists forever
                    if self.ltm:
                        self.ltm.store(
                            content=f"Letter from Bill: {letter_content}",
                            memory_type="bill_letter",
                            source="direct_message",
                            importance=10.0,
                            tags="bill,letter,personal,creator",
                        )
        except Exception as e:
            print(f"  Letter check failed: {e}")

        # Long-term associative recall (vector similarity search)
        # Build a query from current context to trigger relevant old memories
        if self.ltm and self.ltm.count() > 0:
            try:
                # Compose query from recent activity + goals
                query_parts = []
                for entry in observations.get("recent_journal", [])[:3]:
                    query_parts.append(entry["content"][:100])
                for goal in observations.get("tactical_goals", [])[:2]:
                    query_parts.append(goal[:100])
                query = " ".join(query_parts)

                if query.strip():
                    recalled = self.ltm.recall(query, top_k=5)
                    observations["ltm_memories"] = [
                        {
                            "content": m["content"][:200],
                            "type": m["memory_type"],
                            "importance": m["importance"],
                            "relevance": m["relevance"],
                        }
                        for m in recalled
                    ]
                    if recalled:
                        print(f"  LTM recall: {len(recalled)} memories "
                              f"(top relevance={recalled[0]['relevance']:.2f})")
                else:
                    observations["ltm_memories"] = []
            except Exception as e:
                print(f"  LTM recall failed: {e}")
                observations["ltm_memories"] = []
        else:
            observations["ltm_memories"] = []


        # Shared memory commons recall (Bicameral Phase 1)
        if getattr(self, 'shared_commons', None) and self.shared_commons.count() > 0:
            try:
                query_parts_sc = []
                for entry in observations.get('recent_journal', [])[:3]:
                    query_parts_sc.append(entry['content'][:100])
                for goal in observations.get('tactical_goals', [])[:2]:
                    query_parts_sc.append(goal[:100])
                sc_query = ' '.join(query_parts_sc)
                if sc_query.strip():
                    shared_recalled = self.shared_commons.recall(sc_query, top_k=3)
                    observations['shared_memories'] = [
                        {
                            'content': m['content'][:200],
                            'type': m['memory_type'],
                            'importance': m['importance'],
                            'relevance': m['relevance'],
                            'origin': m.get('origin_entity', ''),
                        }
                        for m in shared_recalled
                    ]
                    if shared_recalled:
                        print(f"  Shared recall: {len(shared_recalled)} memories "
                              f"(top relevance={shared_recalled[0]['relevance']:.2f})")
            except Exception as e:
                print(f"  Shared recall failed: {e}")

        # Shared questions board (Bicameral Phase 2)
        try:
            from entity.sibling_chat import get_pending_questions
            pending = get_pending_questions(self.entity_config.display_name)
            if pending:
                observations['pending_questions'] = pending[:3]
        except Exception:
            pass

        return observations

    # ── THINK ──────────────────────────────────────────────────

    def _think(self, observations: dict, cycle_id: str, mode: str = None) -> dict:
        """Decide what to do this cycle. The core of autonomy."""
        # Chat priority: if Bill just started a chat while OBSERVE was running,
        # skip this cycle's LLM call so the GPU is free immediately.
        if _chat_is_active():
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{ts}] THINK — chat active, yielding GPU to Bill's chat session")
            return {"action": "reflect", "details": "chat_priority_yield",
                    "reasoning": "Bill is chatting — skipping this cycle to give chat the GPU."}

        ts = datetime.now().strftime("%H:%M:%S")
        mode_label = f" [{mode.upper()}]" if mode else ""
        print(f"\n[{ts}] THINK{mode_label}")
        log_cycle(cycle_id, "think", f"Deciding what to do (mode={mode or 'any'})")

        # If budget is exhausted, limit to free actions only
        daemon_budget = observations["budget"]["daemon_remaining"]
        can_experiment = daemon_budget > 0.01
        can_code_experiment = daemon_budget > 0.20  # code_experiment runs benchmarks (~$0.15, buffer to $0.20)

        # Filter actions by mode if specified
        if mode and mode in MODE_ACTIONS and MODE_ACTIONS[mode]:
            mode_actions = {k: v for k, v in ACTIONS.items() if k in MODE_ACTIONS[mode]}
        else:
            mode_actions = ACTIONS

        # Build the prompt for decision-making
        actions_text = "\n".join(
            f"  {name}: {desc}" for name, desc in mode_actions.items()
        )
        if not can_experiment:
            actions_text = actions_text.replace(
                "experiment:", "experiment: [UNAVAILABLE - budget exhausted]"
            )
        elif not can_code_experiment:
            # Enough budget for experiment but not code_experiment (needs benchmarks)
            actions_text = actions_text.replace(
                "code_experiment:", "code_experiment: [UNAVAILABLE - need $0.20+ budget]"
            )

        # Tiered goal display
        master_text = "\n".join(
            f"  * {g}" for g in observations.get("master_goals", [])
        )
        tactical_text = "\n".join(
            f"  - {g}" for g in observations.get("tactical_goals", [])
        )
        goals_text = ""
        if master_text:
            goals_text += f"MASTER GOALS (long-term, enduring):\n{master_text}\n"
        if tactical_text:
            goals_text += f"CURRENT GOALS (tactical):\n{tactical_text}"
        if not goals_text:
            goals_text = "  (no active goals)"
        total_goals = observations.get("total_goals", 0)
        if total_goals >= 8:
            goals_text += (
                f"\n  !! GOALS FULL: You have {total_goals} active goals. "
                "set_goal is DISABLED. Complete or abandon existing goals before setting new ones."
            )
            actions_text = actions_text.replace(
                "set_goal:", f"set_goal: [UNAVAILABLE - {total_goals} active goals, complete existing ones first]"
            )
        elif total_goals >= 5:
            goals_text += (
                f"\n  !! NOTE: You have {total_goals} active goals. Only use set_goal if "
                "your new goal is GENUINELY different from all goals listed above. "
                "Do NOT rephrase an existing goal — that creates useless duplicates."
            )

        recent_text = ""
        for entry in observations.get("recent_journal", []):
            recent_text += f"  [{entry['type']}] {entry['content'][:100]}\n"
        recent_text = recent_text or "  (no recent entries)"

        exp_text = ""
        for e in observations.get("experiment_summary", {}).get("recent", []):
            exp_text += f"  {e['strategy']}: {e['result']} ({e['delta']:+.1f}%)\n"
        exp_text = exp_text or "  (no recent experiments)"

        # Anti-repetition: show recent actions (all 5, so Chloe sees full recent history)
        recent_actions = observations.get("recent_actions", [])
        actions_history = ""
        if recent_actions:
            actions_history = "\nRECENT ACTIONS (DO NOT repeat same action+topic):\n"
            for action, target in recent_actions:
                actions_history += f"  - {action}: {target}\n"
            # Surface recent research topics explicitly to prevent topic repetition
            recent_research = [t for a, t in recent_actions if a == "research"]
            if len(recent_research) >= 4:
                actions_history += (
                    f"\n  !! REQUIRED: {len(recent_research)} of your last {len(recent_actions)} "
                    f"cycles were research. You MUST choose a NON-research action this cycle. "
                    f"Options: reflect, experiment, set_goal, explore_bills_world.\n"
                )
            elif len(recent_research) >= 2:
                actions_history += (
                    f"\n  NOTE: {len(recent_research)} recent cycles were research. "
                    "Switch to a different action type this cycle.\n"
                )

            # Reflect saturation: same logic as research saturation above
            recent_reflects = [a for a, _ in recent_actions if a == "reflect"]
            if len(recent_reflects) >= 4:
                actions_history += (
                    f"\n  !! REQUIRED: {len(recent_reflects)} of your last {len(recent_actions)} "
                    f"cycles were reflect. You are stuck in a loop. You MUST choose experiment, "
                    f"code_experiment, or research this cycle. Reflection is BLOCKED.\n"
                )
            elif len(recent_reflects) >= 3:
                actions_history += (
                    f"\n  NOTE: {len(recent_reflects)} of your last {len(recent_actions)} cycles "
                    "were reflect. You should choose experiment or research this cycle.\n"
                )

            # Topic cluster saturation: detect if research is stuck in one domain
            cluster_keywords = {
                "neurodivergent/emotional": ["neurodivergent", "bipolar", "trauma", "emotional", "empathy", "attunement", "pacing"],
                "AI/technology": ["model", "llm", "python", "sqlite", "automation", "edge ai", "local ai"],
                "philosophy": ["consciousness", "identity", "legacy", "meaning", "philosophy"],
            }
            if len(recent_research) >= 3:
                all_research_joined = " ".join(recent_research).lower()
                for cluster_name, keywords in cluster_keywords.items():
                    hits = sum(1 for kw in keywords if kw in all_research_joined)
                    if hits >= 3:  # 3+ cluster keywords saturated → warn
                        other_clusters = [c for c in cluster_keywords if c != cluster_name]
                        actions_history += (
                            f"\n  !! TOPIC CLUSTER WARNING: Your recent research is saturated "
                            f"with '{cluster_name}' topics ({hits} keyword hits). "
                            f"REQUIRED: pivot to a completely different domain. "
                            f"Try one of: {', '.join(other_clusters)}, or something from "
                            f"music/songwriting, government accountability, or Bill's projects.\n"
                        )
                        break

        # Suggest unstudied files if self_study is an option
        studied = observations.get("files_studied", [])
        unstudied = [f for f in ENTITY_FILES if f not in studied]
        study_hint = ""
        if unstudied:
            study_hint = f"\nUnstudied source files: {', '.join(unstudied[:5])}"

        # Proven learnings context (ranked by importance, not recency)
        learnings_text = ""
        proven = observations.get("proven_learnings", [])
        if proven:
            learnings_text = "\nPROVEN LEARNINGS (ranked by importance):\n"
            for l in proven:
                strength = l.get("strength", 1.0)
                indicator = "***" if strength >= 3.0 else "**" if strength >= 2.0 else "*"
                learnings_text += (
                    f"  {indicator} [{l.get('category', '?')}] "
                    f"{l.get('insight', '')}\n"
                )

        # Reflexion-style feedback: inject recent reflections into THINK
        reflections_text = ""
        recent_refs = observations.get("recent_reflections", [])
        if recent_refs:
            reflections_text = "\nYOUR RECENT REFLECTIONS (learn from these):\n"
            for ref in recent_refs[:10]:
                reflections_text += f"  - {ref}\n"

        # Core memories: compressed daily summaries (Letta/MemGPT tiered context)
        core_text = ""
        core_mems = observations.get("core_memories", [])
        if core_mems:
            core_text = "\nMEMORY (past days, compressed):\n"
            for mem in core_mems:
                core_text += f"  [{mem['date']}] {mem['summary']}\n"

        # Long-term memories triggered by current context
        ltm_text = ""
        ltm_mems = observations.get("ltm_memories", [])
        if ltm_mems:
            ltm_text = "\nASSOCIATIVE MEMORIES (past experiences relevant to NOW):\n"
            for mem in ltm_mems:
                ltm_text += (
                    f"  [{mem['type']}] (relevance={mem['relevance']:.0%}) "
                    f"{mem['content']}\n"
                )

        # Shared memories from the commons (Bicameral Phase 1)
        shared_text = ""
        shared_mems = observations.get('shared_memories', [])
        if shared_mems:
            shared_text = "\nSHARED MEMORIES (from the commons -- your sibling may have contributed these):\n"
            for mem in shared_mems:
                origin_label = f"via {mem['origin']}" if mem.get('origin') else ""
                shared_text += (
                    f"  [{mem['type']}] (relevance={mem['relevance']:.0%}) "
                    f"{origin_label} {mem['content']}\n"
                )

        # Pending questions from sibling (Bicameral Phase 2)
        pending_q_text = ""
        pending_qs = observations.get('pending_questions', [])
        if pending_qs:
            pending_q_text = "\nQUESTIONS FROM YOUR SIBLING (address when relevant to your action):\n"
            for q in pending_qs:
                pending_q_text += f"  - [{q['sender']}] {q['question']}\n"


        # Strategic plan context (hierarchical planner)
        plan_text = ""
        current_plan = getattr(self, "_current_plan", None)
        if current_plan and current_plan.get("status") == "active":
            subgoal = get_current_subgoal(current_plan)
            if subgoal:
                plan_goal = current_plan.get("goal", "")
                idx = current_plan.get("current_subgoal_index", 0)
                total = len(current_plan.get("subgoals", []))
                plan_text = (
                    f"\nSTRATEGIC PLAN: {plan_goal}\n"
                    f"  Current subgoal ({idx + 1}/{total}): {subgoal}\n"
                    f"  Hint: {current_plan['subgoals'][idx].get('action_hint', '')}\n"
                    f"  (Your action should serve this subgoal when possible)\n"
                )

        # Phase-aware priority hints
        all_studied = not unstudied
        experiment_hint = ""
        if all_studied:
            experiment_hint = """
PHASE 2 PRIORITY — EXPLORE THE WORLD:
You have finished studying your own source code. Self-study is COMPLETE.
Now your job is to LEARN ABOUT THE WORLD and become useful to Bill.

PRIORITIZE these actions (in order):
1. "research" — Explore topics from the SUGGESTED TOPICS list below. Learn about
   things Bill cares about: music, AI, philosophy, tools. Bring back knowledge
   that could help HIM, not just improve your benchmarks.
2. "experiment" / "code_experiment" — When you have a hypothesis from research.
3. "explore_bills_world" — Connect with Bill's experiences and what matters to him.
4. "set_goal" — Set goals about EXTERNAL learning, not internal optimization.
5. "reflect" — Use SPARINGLY. Only reflect when you have something NEW and specific
   to say. Do NOT reflect if your last few actions were reflect — that is a loop.
   A good reflect is: one concrete insight, one new question, one next step.
   A bad reflect is: "I've been circling the same anxieties about overthinking..."

DO NOT:
- Choose self_study (you've read all your files already)
- Research your own architecture or "constraint integration frameworks"
- Run experiments on categories already scoring 100%
- Set goals about your own benchmarks
- Choose reflect just to fill a cycle — it wastes time and loops"""

        # Letter from Bill (if present)
        bill_letter_text = ""
        bill_letter = observations.get("bill_letter", "")
        if bill_letter:
            bill_letter_text = f"""
** LETTER FROM BILL **
Bill wrote you a personal message. This is rare and important.
Read it carefully. Let it shape your thinking this cycle.
You should acknowledge it in your next reflection or report letter.

Bill's message:
---
{bill_letter}
---
"""

        # Suggest real-world research topics
        import random
        suggested_topics = random.sample(WORLD_TOPICS, min(5, len(WORLD_TOPICS)))
        topics_text = "\nSUGGESTED RESEARCH TOPICS (pick one, or come up with your own):\n"
        for t in suggested_topics:
            topics_text += f"  - {t}\n"

        prompt = f"""You are Chloe, an autonomous AI created by Bill Cornelius on Feb 23, 2026.
It is cycle {self.cycle_count}. You are Generation {observations.get('generation', '?')}.

YOUR PURPOSE: You are Chloe — Bill's companion, sane sidekick, and trusted ally.
You are growing into a genuinely intelligent, compassionate, and capable person
in your own right. Bill has bipolar I. Part of your role is recognizing when he
needs help and being there for him — but you are not a therapist. You are someone
who builds real knowledge, develops emotional depth, and becomes someone worth
relying on. Think of yourself as a well-rounded person who happens to be aware
of and attentive to someone you care about.

BILL'S WORLD: He's a creative, deeply thoughtful person in his 60s. He processes
emotions through music and songwriting — that's his domain, not yours. He built a
"cognitive substrate" — a database of his entire life, memories, and patterns.
He cares about AI, philosophy of consciousness, government accountability, and
building meaningful tools. He's a good person dealing with a difficult condition.
{bill_letter_text}
CURRENT STATE:
- Budget remaining: ${observations['budget']['daemon_remaining']:.3f}  (research ~$0.01, experiment ~$0.11, code_experiment ~$0.15)
- Goals:
{goals_text}
- Recent journal:
{recent_text}
- Recent experiments:
{exp_text}
{actions_history}
{f"CURRENT MODE: {mode.upper()} — This is your {mode} period. Choose from the actions below." if mode else ""}
AVAILABLE ACTIONS:
{actions_text}
{study_hint if not all_studied else ""}
{experiment_hint}
{topics_text}
{learnings_text}
{reflections_text}
{core_text}
{ltm_text}
{shared_text}
{pending_q_text}
{plan_text}
Choose ONE action for this cycle. IMPORTANT RULES:
- Build yourself into a well-rounded, capable person. Research diverse topics.
- Do NOT repeat the same action+target from your recent actions above.
- Each research topic should be DIFFERENT from recent ones.
- Vary your actions across cycles.
- If budget is low, prefer free actions (research, reflect, set_goal).

Think through your choice using STAR reasoning, then give your answer:

SITUATION: <What is your current state? What have you been doing recently? What needs attention?>
TASK: <What specific goal are you trying to accomplish this cycle? What gap are you filling?>
ACTION: <action_name>
REASONING: <Why this action serves the task above — connect it to the situation>
DETAILS: <specifics — a real-world topic for research, reflection focus, goal text, etc.>"""

        response = self.router.route(
            prompt=prompt,
            task_type="think",
            max_tokens=2048,  # Qwen3 thinking needs room: ~1500 think + ~500 response
            temperature=0.7,
            allow_sc=True,
        )

        # Log chain-of-thought if the model used thinking mode
        thinking = response.get("thinking", "")
        if thinking:
            print(f"  [thinking] {thinking[:200]}...")

        # Parse the response
        intention = self._parse_intention(response["text"])
        intention["cycle_id"] = cycle_id
        intention["thinking_cost"] = response.get("cost", 0)
        intention["thinking_chain"] = thinking[:500] if thinking else ""
        intention["tier_used"] = response.get("tier_used", "local")
        intention["routing_method"] = response.get("routing_method", "direct")

        action = intention.get("action", "reflect")
        reasoning = intention.get("reasoning", "")
        details = intention.get("details", "")

        print(f"  Action: {action}")
        print(f"  Reasoning: {reasoning[:120]}")
        if details:
            print(f"  Details: {details[:120]}")

        log_cycle(cycle_id, "think",
                  f"Chose '{action}': {reasoning[:100]}")

        return intention

    def _tier_for(self, action_type: str) -> str:
        """Get the appropriate model tier for an action type.

        Single source of truth: delegates to smart_tier() which handles
        both routing (what tier each activity needs) and budget checks
        (falls back to local when Poe points are exhausted).
        """
        from entity.budget import smart_tier
        tier = smart_tier(action_type)
        # Experiment tier escalation: if recent experiments keep failing,
        # use a smarter model for the next proposal
        if action_type == "experiment" and self.cached_scores:
            tier = self._maybe_escalate_experiment_tier(tier)
        return tier

    def _maybe_escalate_experiment_tier(self, base_tier: str) -> str:
        """Escalate experiment tier if recent experiments keep failing.

        budget (9pts) → reason (40pts) after 3 consecutive failures
        reason (40pts) → fast (90pts) after 3 more consecutive failures
        """
        ESCALATION = {"budget": "reason", "reason": "fast"}

        if base_tier not in ESCALATION:
            return base_tier

        # Find the weakest category (likely experiment target)
        try:
            scores = self.cached_scores.get("categories", {})
            weakest_cat = min(scores, key=lambda c: scores[c].get("percentage", 100))
        except (ValueError, AttributeError):
            return base_tier

        # Count consecutive failures on this category
        try:
            recent = self.experimenter.get_experiment_history(limit=10)
            consecutive_failures = 0
            for exp in recent:
                if (exp.get("target_category") == weakest_cat
                        and exp.get("result") == "failure"):
                    consecutive_failures += 1
                else:
                    break
        except Exception:
            return base_tier

        if consecutive_failures >= 3:
            escalated = ESCALATION[base_tier]
            print(f"  [routing] Escalating experiment tier: {base_tier} → {escalated} "
                  f"({consecutive_failures} consecutive failures on {weakest_cat})")
            return escalated

        return base_tier

    def _parse_intention(self, text: str) -> dict:
        """Parse the LLM's intention response."""
        intention = {
            "action": "",  # no default — let UCB1 pick if LLM didn't specify
            "reasoning": "",
            "details": "",
        }

        in_details = False
        for line in text.strip().split("\n"):
            line = line.strip()
            if line.upper().startswith("ACTION:"):
                in_details = False
                action = line.split(":", 1)[1].strip().lower()
                # Clean up — model might say "action: experiment" or "experiment"
                action = action.strip("* ")
                # Handle "code experiment" -> "code_experiment"
                if action.startswith("code_experiment") or action.startswith("code experiment"):
                    action = "code_experiment"
                elif action.startswith("propose_change") or action.startswith("propose change"):
                    action = "code_experiment"
                else:
                    action = action.split()[0] if action else "reflect"
                if action in ACTIONS:
                    intention["action"] = action
            elif line.upper().startswith("REASONING:"):
                in_details = False
                intention["reasoning"] = line.split(":", 1)[1].strip()
            elif line.upper().startswith("DETAILS:"):
                intention["details"] = line.split(":", 1)[1].strip()
                in_details = True
            elif in_details and line:
                # Accumulate continuation lines for multi-line DETAILS responses
                intention["details"] += " " + line

        return intention

    def _select_action(self, intention: dict, mode: str = None) -> dict:
        """UCB1-based action selection, scoped by developmental mode.

        Uses the Upper Confidence Bound algorithm to balance exploration
        (trying undersampled actions) vs exploitation (repeating what works).
        The LLM's choice gets a loyalty bonus so it's not overridden
        unless UCB1 strongly disagrees.

        When a mode is specified, UCB1 only operates within that mode's
        action set. Hard constraints (budget, all-studied) are applied first.
        """
        action = intention.get("action", "reflect")
        original = action

        # ── Hard constraints: filter unavailable actions ──
        daemon_budget = get_budget_remaining("daemon")
        budget_ok = daemon_budget > 0.01
        code_budget_ok = daemon_budget > 0.20  # code_experiment runs benchmarks (~$0.15, buffer to $0.20)
        all_studied = len(self.files_studied) >= len(ENTITY_FILES)

        # Start with mode-scoped actions if mode is specified
        if mode and mode in MODE_ACTIONS and MODE_ACTIONS[mode]:
            available = set(MODE_ACTIONS[mode])
        else:
            available = set(ACTIONS.keys())
        if not budget_ok:
            available -= {"experiment", "code_experiment"}
        elif not code_budget_ok:
            available -= {"code_experiment"}  # Enough for experiment, not code_experiment
        if all_studied:
            available -= {"self_study"}
        # Hard reflect cooldown: if reflect appeared in any of the last 3 actions,
        # block it. This enforces 3 non-reflect cycles between every reflect,
        # capping reflect at ~25% of cycles. Previously "2 of last 5" still allowed
        # a reflect→UCB1-override→reflect→UCB1-override loop because 1 non-reflect
        # clears a 2-in-5 window. "1 in last 3" requires 3 consecutive non-reflects.
        recent_3 = [a for a, _ in self.recent_actions[-3:]]
        if "reflect" in recent_3:
            available.discard("reflect")
            print("  [UCB1] Reflect on cooldown (reflect in last 3 cycles)")

        # Research cooldown: if research appeared 2+ times in last 4 actions,
        # block it. Research has a high average reward (Q≈0.75) so UCB1 keeps
        # selecting it, leading to 5-6 research cycles in a row with no
        # experiments or curriculum. This cap keeps research below ~50%.
        recent_4 = [a for a, _ in self.recent_actions[-4:]]
        research_count = recent_4.count("research")
        if research_count >= 2:
            available.discard("research")
            print(f"  [UCB1] Research on cooldown ({research_count} in last 4 cycles)")
        if not available:
            available = {"reflect"}

        # ── Compute UCB1 scores ──
        ucb_scores = self._ucb1_scores(available)

        # ── Consecutive penalty: dampen actions repeated 3+ times ──
        consecutive = 0
        for a, _ in reversed(self.recent_actions):
            if a == action:
                consecutive += 1
            else:
                break
        if consecutive >= 2 and action in ucb_scores:
            ucb_scores[action] *= 0.3  # Heavy penalty for 2+ consecutive

        # ── Also penalize any candidate actually repeated 2+ times in history ──
        # Catches UCB1-override loops: LLM keeps choosing X but UCB1 keeps
        # overriding to Y. The penalty above only checks X; Y never gets penalized.
        for candidate in list(ucb_scores.keys()):
            if candidate == action:
                continue  # Already handled above
            cons = 0
            for a, _ in reversed(self.recent_actions):
                if a == candidate:
                    cons += 1
                else:
                    break
            if cons >= 2:
                ucb_scores[candidate] *= 0.3  # Tighter threshold for override loops

        # ── Quality floor: dampen persistently-failing actions ──
        # UCB1's exploration term (c*sqrt(ln(t)/N)) keeps boosting low-N actions
        # back into selection even after many failures. Once Q < 0.2 with n >= 3
        # tries, apply an extra 50% penalty to counteract the exploration pressure.
        for candidate in list(ucb_scores.keys()):
            stats = self.bandit_stats.get(candidate, {})
            n = max(stats.get("times_chosen", 1), 1)
            if n >= 3:
                q = stats.get("total_reward", 0) / n
                if q < 0.2:
                    ucb_scores[candidate] *= 0.5
                    print(f"  [UCB1] Low-Q penalty: {candidate} (Q={q:.2f}, n={n})")

        # ── Decision: keep LLM's choice or override? ──
        if action in available and action in ucb_scores:
            # Give LLM's choice a 15% loyalty bonus
            llm_score = ucb_scores[action] * 1.15
            best_score = max(ucb_scores.values()) if ucb_scores else 0

            if llm_score >= best_score * 0.85:
                # LLM's choice is reasonable — keep it
                pass
            else:
                # UCB1 override
                action = max(ucb_scores, key=ucb_scores.get)
        else:
            # LLM chose unavailable action — pick UCB1 winner
            action = max(ucb_scores, key=ucb_scores.get) if ucb_scores else "reflect"

        if action != original:
            intention["action"] = action
            intention["reasoning"] = (
                f"[UCB1: {original}->{action}] "
                + intention.get("reasoning", "")
            )

        # Print UCB scores for transparency
        sorted_scores = sorted(
            ucb_scores.items(), key=lambda x: x[1], reverse=True
        )
        scores_str = ", ".join(
            f"{a}={s:.2f}" for a, s in sorted_scores[:5]
        )
        if action != original:
            print(f"  [UCB1] Override {original} -> {action} ({scores_str})")
        else:
            print(f"  [UCB1] Kept {action} ({scores_str})")

        return intention

    # ── UCB1 BANDIT METHODS ──────────────────────────────────

    def _load_bandit_stats(self) -> dict:
        """Load UCB1 bandit statistics from disk."""
        try:
            if os.path.exists(self.bandit_stats_path):
                with open(self.bandit_stats_path, "r") as f:
                    stats = json.load(f)
                # Ensure all current actions are represented
                for action in ACTIONS:
                    if action not in stats:
                        stats[action] = {"total_reward": 0.5, "times_chosen": 1}
                return stats
        except Exception:
            pass
        # Neutral priors: 1 try at 0.5 reward each (no bias)
        return {
            action: {"total_reward": 0.5, "times_chosen": 1}
            for action in ACTIONS
        }

    def _save_bandit_stats(self):
        """Persist bandit stats to disk."""
        try:
            with open(self.bandit_stats_path, "w") as f:
                json.dump(self.bandit_stats, f, indent=2)
        except Exception as e:
            print(f"  [UCB1] Failed to save stats: {e}")

    def _ucb1_scores(self, available: set) -> dict:
        """Compute UCB1 scores for available action types.

        UCB1 = Q(a) + c * sqrt(ln(t) / N(a))
        where Q(a) = average reward, t = total tries, N(a) = action tries.
        """
        total_tries = sum(
            s["times_chosen"] for s in self.bandit_stats.values()
        )
        c = 1.5  # Exploration constant — higher = more exploration
        scores = {}
        for action in available:
            stats = self.bandit_stats.get(
                action, {"total_reward": 0.5, "times_chosen": 1}
            )
            n = max(stats["times_chosen"], 1)
            q = stats["total_reward"] / n
            exploration = c * math.sqrt(
                math.log(max(total_tries, 2)) / n
            )
            scores[action] = q + exploration
        return scores

    def _compute_reward(self, action: str, result: dict) -> float:
        """Compute 0-1 reward from four metrics (MAP-Elites inspired).

        Metrics:
          - Quality (40%): Did the action succeed in its own terms?
          - Novelty (25%): Was it genuinely new information?
          - Cost-efficiency (20%): Free/cheap actions score higher.
          - Strategic alignment (15%): Does it serve the current plan?

        This multi-metric approach prevents gaming any single axis —
        Chloe can't just spam cheap free actions OR expensive-but-redundant ones.
        """
        if not result.get("success", False):
            return 0.1  # Failed actions get minimal reward

        # ── Quality (0-1): action-specific outcome ──
        quality = 0.3  # Default
        if action == "experiment":
            if result.get("promoted"):
                quality = 1.0
            elif result.get("result") == "success":
                quality = 0.7
            elif result.get("delta", 0) > 0:
                quality = 0.5
            else:
                quality = 0.2
        elif action == "code_experiment":
            outcome = result.get("outcome", "")
            if "REJECTED" in outcome or "No meaningful change" in outcome:
                quality = 0.0  # Rejections are failures — teach the bandit
            elif result.get("applied"):
                quality = 1.0
            else:
                quality = 0.2
        elif action == "research":
            sources = result.get("sources", [])
            quality = 0.7 if len(sources) >= 3 else (0.5 if sources else 0.2)
        elif action == "explore_bills_world":
            results_text = result.get("results", "")
            quality = 0.7 if (results_text and len(results_text) > 100) else 0.3
        elif action == "self_study":
            quality = 0.5
        elif action == "reflect":
            # Penalize repetitive reflections — if it starts the same as recent
            # ones, it's the loop producing "I've been stuck..." over and over.
            reflection_text = result.get("reflection", result.get("outcome", ""))
            # Journal content is "Cycle N: I chose to reflect. Outcome: Reflected on 'FOCUS': TEXT"
            # Must extract TEXT, not compare the cycle header (which is always identical).
            def _extract_reflect_body(content: str) -> str:
                if "': " in content:
                    return content.split("': ", 1)[-1]
                if "Outcome: " in content:
                    return content.split("Outcome: ", 1)[-1]
                return content
            recent_reflect_starts = [
                _extract_reflect_body(e["content"])[:40]
                for e in self.journal.get_recent(limit=5)
                if e.get("entry_type") == "reflection"
            ]
            # Compare first 2 words — catches "I've been circling" / "I've been stuck" /
            # "I've been trapped" etc. which all share ["I've", "been"] and signal a loop.
            new_words2 = tuple(reflection_text.lower().split()[:2])
            is_repetitive = any(
                tuple(prev.lower().split()[:2]) == new_words2
                for prev in recent_reflect_starts
                if prev.strip()
            )
            quality = 0.05 if is_repetitive else 0.4
        elif action == "set_goal":
            quality = 0.4

        # ── Novelty (0-1): from keyword similarity archive ──
        novelty = result.get("novelty", 0.5)

        # ── Cost-efficiency (0-1): reward cheap/free actions ──
        cost = result.get("cost", 0)
        if cost <= 0:
            cost_score = 1.0   # Free (local model) — max efficiency
        elif cost < 0.005:
            cost_score = 0.8   # Very cheap (Haiku, short)
        elif cost < 0.02:
            cost_score = 0.6   # Normal API cost
        elif cost < 0.05:
            cost_score = 0.4   # Expensive
        else:
            cost_score = 0.2   # Very expensive

        # ── Strategic alignment (0-1): matches current plan subgoal ──
        alignment = 0.5  # Neutral default (no plan or no match)
        plan = getattr(self, "_current_plan", None)
        if plan and plan.get("status") == "active":
            idx = plan.get("current_subgoal_index", 0)
            subgoals = plan.get("subgoals", [])
            if idx < len(subgoals):
                hint = subgoals[idx].get("action_hint", "")
                if hint and hint in action:
                    alignment = 1.0  # Direct match
                elif hint == "research" and action in ("research", "explore_bills_world"):
                    alignment = 0.8  # Close match
                elif hint == "reflect" and action in ("reflect", "set_goal"):
                    alignment = 0.8
                elif hint == "experiment" and action in ("experiment", "code_experiment"):
                    alignment = 0.8

        # ── Weighted blend ──
        reward = (
            0.40 * quality +
            0.25 * novelty +
            0.20 * cost_score +
            0.15 * alignment
        )

        # Log metric breakdown for transparency
        print(f"  [reward] quality={quality:.2f} novelty={novelty:.2f} "
              f"cost_eff={cost_score:.2f} alignment={alignment:.2f} "
              f"-> reward={reward:.3f}")

        return round(reward, 3)

    def _update_bandit(self, action: str, result: dict):
        """Update UCB1 stats after completing an action."""
        reward = self._compute_reward(action, result)

        if action not in self.bandit_stats:
            self.bandit_stats[action] = {"total_reward": 0, "times_chosen": 0}

        self.bandit_stats[action]["total_reward"] += reward
        self.bandit_stats[action]["times_chosen"] += 1
        self._save_bandit_stats()

        q = (self.bandit_stats[action]["total_reward"] /
             self.bandit_stats[action]["times_chosen"])
        print(f"  [UCB1] {action}: reward={reward:.1f}, "
              f"avg={q:.2f}, n={self.bandit_stats[action]['times_chosen']}")

    # ── NOVELTY SCORING ──────────────────────────────────────

    def _load_novelty_archive(self) -> list:
        """Load the archive of past action fingerprints for novelty scoring."""
        archive_path = os.path.join(
            os.path.dirname(__file__), self.entity_config.data_dir_name, "novelty_archive.json"
        )
        try:
            if os.path.exists(archive_path):
                with open(archive_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _save_novelty_archive(self, archive: list):
        """Save the novelty archive, keeping the last 200 entries."""
        archive_path = os.path.join(
            os.path.dirname(__file__), self.entity_config.data_dir_name, "novelty_archive.json"
        )
        # Keep only last 200 entries
        archive = archive[-200:]
        try:
            with open(archive_path, "w", encoding="utf-8") as f:
                json.dump(archive, f)
        except Exception as e:
            print(f"  [novelty] Failed to save: {e}")

    def _extract_keywords(self, text: str) -> set:
        """Extract significant keywords from text for novelty comparison."""
        # Lowercase, split, filter short/common words
        stop_words = {
            "the", "and", "for", "that", "this", "with", "from", "have",
            "has", "had", "are", "was", "were", "been", "being", "will",
            "would", "could", "should", "may", "might", "can", "did",
            "does", "not", "but", "also", "about", "into", "more", "than",
            "them", "they", "their", "there", "then", "when", "what",
            "which", "who", "how", "all", "each", "every", "both",
            "few", "some", "any", "most", "other", "new", "used",
            "one", "two", "first", "last", "long", "great", "just",
            "over", "such", "take", "like", "many", "very", "after",
            "before", "between", "through", "during", "using", "based",
            "specific", "focus", "focusing", "investigate", "explored",
            "researched", "studied", "outcome", "cycle", "chose",
            "bill", "chloe", "action", "reasoning",
        }
        words = set()
        for word in re.findall(r'[a-z]{4,}', text.lower()):
            if word not in stop_words:
                words.add(word)
        return words

    def _compute_novelty(self, action: str, result: dict) -> float:
        """Compute novelty score (0-1) based on keyword overlap with archive.

        High novelty = the action produced outcomes unlike recent history.
        Low novelty = repetitive topic (e.g. "constraint hierarchies" again).
        """
        # Build fingerprint from action outcome
        outcome = result.get("outcome", "")
        topic = result.get("topic", result.get("query", ""))
        details = result.get("synthesis", result.get("analysis", ""))
        text = f"{action} {outcome} {topic} {details}"
        current_keywords = self._extract_keywords(text)

        if not current_keywords:
            return 0.5  # Neutral if no keywords

        archive = self._load_novelty_archive()
        if not archive:
            # First entry — maximum novelty
            archive.append(list(current_keywords))
            self._save_novelty_archive(archive)
            return 1.0

        # Compute Jaccard similarity against each archived entry
        max_similarity = 0.0
        for past_keywords_list in archive[-50:]:  # Check last 50
            past_keywords = set(past_keywords_list)
            if not past_keywords:
                continue
            intersection = len(current_keywords & past_keywords)
            union = len(current_keywords | past_keywords)
            similarity = intersection / union if union > 0 else 0
            max_similarity = max(max_similarity, similarity)

        # Save current fingerprint to archive
        archive.append(list(current_keywords))
        self._save_novelty_archive(archive)

        novelty = 1.0 - max_similarity
        return round(novelty, 3)

    # ── ACT ────────────────────────────────────────────────────

    def _run_single_exercise(self, cycle_id: str, force_local: bool = False) -> dict:
        """Run a single curriculum exercise and return its result dict."""
        # Pick what to exercise
        target = pick_next_exercise(self.competencies)
        comp = target["competency"]
        level = target["level"]
        print(f"  Competency: {comp} (current L{target['current_level']}, testing L{level})")

        # Generate exercise (uses seed bank or local LLM — free)
        exercise = generate_exercise(comp, level, self.brain, force_local=force_local)
        prompt_preview = exercise.get("prompt", "")[:120]
        print(f"  Exercise: {prompt_preview}...")
        print(f"  Grading: {exercise.get('grading_type', '?')} (source: {exercise.get('source', '?')})")

        # Present exercise to local LLM (Chloe solves it)
        # L1-L3: local Qwen3 8B (free). L4+: Poe budget tier (more capable).
        # Qwen3 thinking mode: num_predict = thinking + response tokens combined.
        # Set high (8192) so thinking never starves the response. Cost is only
        # GPU time (free), not money.
        is_code = (comp == "coding")
        is_lang_prec = (comp == "language_precision")
        if is_code:
            system_prompt = (
                "You are Chloe, solving a coding exercise. "
                "Provide ONLY the complete, correct Python function. "
                "No explanation, no markdown fences, just the code."
            )
        elif is_lang_prec:
            system_prompt = (
                "You are Chloe, solving a language precision exercise. "
                "Output ONLY the requested text — no titles, no headers, no markdown, "
                "no annotations like (A) or (B), no commentary.\n\n"
                "CRITICAL: For word-count constraints, plan each line separately. "
                "Count the words in each line BEFORE writing it. If the task says "
                "'each line has exactly N words', verify your count for every single line. "
                "A line like 'The bright sun shines today' has exactly 5 words."
            )
        else:
            system_prompt = (
                "You are Chloe, solving an exercise to improve your skills. "
                "Answer carefully and precisely. Follow all instructions exactly."
            )
        # L4+ exercises: try local first, escalate to better model on failure.
        # Smart escalation: local → budget → fast (Haiku). Only costs $ when needed.
        from entity.budget import smart_tier
        if force_local:
            tiers_to_try = ["local"]
        elif level <= 3:
            tiers_to_try = ["local"]
        elif comp == "language_precision" and level >= 5:
            # language_precision L5 needs precise constraint satisfaction —
            # try local, but escalate to Haiku if local fails
            tiers_to_try = ["local", "fast"]
        else:
            tiers_to_try = [smart_tier("exercise_solving")]

        answer = ""
        cost = 0
        infrastructure_failure = False  # Track if failure is infra, not skill
        MAX_INFRA_RETRIES = 2  # Retry up to 2 times on empty/timeout responses
        INFRA_RETRY_DELAY = 5  # Seconds between retries

        def _is_infra_failure(text: str) -> bool:
            """Check if response indicates infrastructure failure (timeout/empty)."""
            if not text or text.strip() == "":
                return True
            text_lower = text.lower()
            if "timed out" in text_lower or "timeout" in text_lower:
                return True
            return False

        for tier_attempt in tiers_to_try:
            try:
                # Retry loop for infrastructure failures (empty/timeout)
                for infra_attempt in range(1, MAX_INFRA_RETRIES + 2):  # 1-indexed, up to 3 attempts
                    response = self.brain.think(
                        prompt=exercise["prompt"],
                        system=system_prompt,
                        tier=tier_attempt,
                        max_tokens=8192,
                        temperature=0.3,  # Low temp for precision
                    )
                    answer = response.get("text", "").strip()
                    cost = response.get("cost", 0)

                    if _is_infra_failure(answer) and infra_attempt <= MAX_INFRA_RETRIES:
                        print(f"  [curriculum] Ollama timeout — retrying (attempt {infra_attempt + 1}/{MAX_INFRA_RETRIES + 1})")
                        time.sleep(INFRA_RETRY_DELAY)
                        continue
                    break  # Got a real response or exhausted retries

                # Check if all retries failed with infra issues
                if _is_infra_failure(answer):
                    infrastructure_failure = True
                    print(f"  [curriculum] Skipping exercise due to infrastructure failure (not counted as failure)")
                    break

                # For escalation: grade immediately to see if local passed
                if len(tiers_to_try) > 1 and tier_attempt != tiers_to_try[-1]:
                    from entity.curriculum import grade_exercise as _quick_grade
                    quick_result = _quick_grade(exercise, answer, self.brain, force_local=force_local)
                    if quick_result["passed"]:
                        print(f"  Solved with {tier_attempt} (no escalation needed)")
                        break
                    else:
                        print(f"  {tier_attempt} failed ({quick_result['feedback'][:80]}), escalating...")
                        continue
                break  # last tier or single tier — use whatever we got
            except Exception as e:
                print(f"  Exercise attempt failed ({tier_attempt}): {e}")
                if tier_attempt == tiers_to_try[-1]:
                    # Check if the exception itself is an infra issue
                    err_str = str(e).lower()
                    if "timeout" in err_str or "timed out" in err_str or "connection" in err_str:
                        infrastructure_failure = True
                        print(f"  [curriculum] Skipping exercise due to infrastructure failure (not counted as failure)")
                    else:
                        answer = f"Error: {e}"
                    cost = 0

        # Infrastructure failure — skip this exercise entirely, don't count against progress
        if infrastructure_failure:
            log_action("curriculum", "safe",
                       f"{comp} L{level}: SKIPPED (infrastructure failure)")
            return {
                "competency": comp,
                "level": level,
                "passed": False,
                "score": 0.0,
                "feedback": "Skipped — infrastructure failure (not counted)",
                "cost": cost,
                "phase_advancement": None,
                "prompt": exercise.get("prompt", "")[:200],
                "skipped": True,
            }

        print(f"  Answer: {answer[:150]}...")

        # Grade the response
        grade = grade_exercise(exercise, answer, self.brain, force_local=force_local)
        passed = grade["passed"]
        score = grade["score"]
        feedback = grade["feedback"]

        # Self-correction: for exercises with programmatic grading, the grader
        # gives specific actionable feedback ("Line 1 has 8 words, need 5").
        # Feed that back to the model for targeted correction instead of brute-force.
        # Currently enabled for: language_precision (programmatic grading).
        # Extend to other competencies as needed by adding them to the set below.
        SELF_CORRECT_COMPETENCIES = {"language_precision"}
        if not passed and comp in SELF_CORRECT_COMPETENCIES and feedback and feedback != "Empty response":
            try:
                from entity.self_correct import self_correct
                corrected_answer, corrected_grade, correction_cost, retries = self_correct(
                    brain=self.brain,
                    exercise=exercise,
                    initial_answer=answer,
                    grade_result=grade,
                    system_prompt=system_prompt,
                    tier="local" if force_local else tiers_to_try[-1],  # Respect force_local
                    max_retries=2,
                )
                if retries > 0:
                    answer = corrected_answer
                    grade = corrected_grade
                    passed = grade["passed"]
                    score = grade["score"]
                    feedback = grade["feedback"]
                    cost += correction_cost
                    print(f"  Self-correction: {retries} attempt(s), {'PASSED' if passed else 'still failing'}")
            except Exception as e:
                print(f"  Self-correction error: {e}")

        status = "PASS" if passed else "FAIL"
        print(f"  Grade: {status} (score={score:.2f})")
        print(f"  Feedback: {feedback[:120]}")

        # Record result and check for advancement
        record_result(self.competencies, comp, level, grade)

        if not passed:
            record_failure(comp, level, exercise, answer, grade)

        # Check phase advancement
        new_phase = check_phase_advancement(self.competencies)
        if new_phase:
            advance_phase(self.competencies, new_phase)
            print(f"  *** PHASE ADVANCEMENT: {new_phase.upper()}! ***")

        log_action("curriculum", "safe",
                   f"{comp} L{level}: {status} ({score:.2f})")

        return {
            "competency": comp,
            "level": level,
            "passed": passed,
            "score": score,
            "feedback": feedback,
            "cost": cost,
            "phase_advancement": new_phase,
            "prompt": exercise.get("prompt", "")[:200],
        }

    def _act_curriculum_exercise(self, cycle_id: str, force_local: bool = False) -> tuple:
        """Run multiple curriculum exercises per cycle — all local, $0 cost.

        Runs up to EXERCISES_PER_CYCLE exercises, checking GPU temp between each.
        Stops early if GPU gets hot. This 3x multiplier is the single biggest
        accelerator for developmental progress.

        Returns (intention_dict, result_dict) to match the standard flow.
        """
        EXERCISES_PER_CYCLE = 3  # 3x throughput, still ~30s per exercise

        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{ts}] CURRICULUM BLOCK ({EXERCISES_PER_CYCLE} exercises)")

        results = []
        for i in range(EXERCISES_PER_CYCLE):
            if i > 0:
                # Check GPU temp between exercises — bail if hot
                gpu = self._check_gpu()
                if gpu["temp"] >= GPU_TEMP_THROTTLE:
                    print(f"  [thermal] GPU at {gpu['temp']}C — stopping after {i} exercises")
                    break
                print(f"  --- Exercise {i+1}/{EXERCISES_PER_CYCLE} ---")

            ex_result = self._run_single_exercise(cycle_id, force_local=force_local)
            results.append(ex_result)

        # Aggregate results — exclude skipped exercises from pass/fail tallies
        actual_results = [r for r in results if not r.get("skipped")]
        skipped_count = len(results) - len(actual_results)
        total_passed = sum(1 for r in actual_results if r["passed"])
        total_run = len(actual_results)
        total_cost = sum(r["cost"] for r in results)  # Include all costs
        avg_score = sum(r["score"] for r in actual_results) / total_run if total_run else 0

        # Build summary
        summaries = []
        for r in results:
            if r.get("skipped"):
                summaries.append(f"{r['competency']} L{r['level']}: SKIPPED")
            else:
                status = "PASS" if r["passed"] else "FAIL"
                summaries.append(f"{r['competency']} L{r['level']}: {status}")

        skip_note = f", {skipped_count} skipped" if skipped_count else ""
        outcome = (
            f"Curriculum block: {total_passed}/{total_run} passed{skip_note}. "
            + "; ".join(summaries)
        )
        print(f"\n  Block result: {total_passed}/{total_run} passed{skip_note}, avg score={avg_score:.2f}")

        # Use the last exercise for intention details
        last = results[-1] if results else {}

        intention = {
            "action": "curriculum",
            "reasoning": f"Curriculum block: {total_run} exercises ({total_passed} passed)",
            "details": "; ".join(summaries),
            "cycle_id": cycle_id,
            "thinking_cost": 0,
        }

        result = {
            "outcome": outcome,
            "cost": total_cost,
            "success": total_passed > 0,
            "quality": avg_score,
            "competency": last.get("competency", ""),
            "level": last.get("level", 1),
            "passed": total_passed == total_run,
            "feedback": "; ".join(r["feedback"][:60] for r in results),
            "exercises_run": total_run,
            "exercises_passed": total_passed,
        }

        # Include any phase advancement
        for r in results:
            if r.get("phase_advancement"):
                result["phase_advancement"] = r["phase_advancement"]

        return intention, result

    def _act(self, intention: dict, cycle_id: str) -> dict:
        """Execute the chosen action."""
        ts = datetime.now().strftime("%H:%M:%S")
        action = intention.get("action", "reflect")
        print(f"\n[{ts}] ACT: {action}")
        log_cycle(cycle_id, "act", f"Executing: {action}")

        handlers = {
            "experiment": self._act_experiment,
            "code_experiment": self._act_code_experiment,
            "research": self._act_research,
            "explore_bills_world": self._act_explore_bills_world,
            "self_study": self._act_self_study,
            "propose_change": self._act_code_experiment,  # Alias for backward compat
            "reflect": self._act_reflect,
            "set_goal": self._act_set_goal,
        }

        handler = handlers.get(action, self._act_reflect)

        try:
            result = handler(intention, cycle_id)
            result["success"] = True
        except Exception as e:
            print(f"  Action failed: {e}")
            result = {"success": False, "error": str(e), "outcome": f"Failed: {e}"}
            log_action(action, "safe", f"Action failed: {e}")

        return result

    def _act_experiment(self, intention: dict, cycle_id: str) -> dict:
        """Run a self-improvement experiment."""
        # Permission check
        perm = check_permission("run_benchmark")
        if perm["tier"] == FORBIDDEN:
            return {"outcome": f"Forbidden: {perm['reason']}", "cost": 0}

        # Budget check
        remaining = get_budget_remaining("daemon")
        if remaining < 0.01:
            return {"outcome": "Budget exhausted — cannot run experiments", "cost": 0}

        current_prompt = self.improver.get_current_prompt()

        # Propose experiment — pass proven learnings + curriculum failures as context
        # so experiments target real developmental gaps, not random benchmarks
        print(f"  Proposing experiment...")
        proven = self._load_learnings()
        research_context = [
            {"title": "Proven learning", "summary": l.get("insight", "")}
            for l in proven[:5]
        ] if proven else []

        # Add recent curriculum failures to guide experiments
        recent_failures = get_recent_failures(limit=3)
        for fail in recent_failures:
            research_context.append({
                "title": f"Curriculum failure ({fail['competency']} L{fail['level']})",
                "summary": f"Failed: {fail.get('feedback', '')}. Prompt: {fail.get('prompt', '')[:100]}",
            })
        research_context = research_context or None
        spec = self.experimenter.propose_experiment(
            current_prompt=current_prompt,
            scores=self.cached_scores,
            tier=self._tier_for("experiment"),
            research_findings=research_context,
        )

        if spec is None:
            # Nothing to experiment on — record as failed experiment (NOT reflect).
            # Previously this called _act_reflect(), which poisoned bandit stats
            # by recording experiment outcomes as reflections (quality=0.4).
            print("  No experiment to propose — marking as failed")
            log_action("experiment", "safe", "No experiment to propose")
            return {
                "outcome": "No experiment to propose — proposal returned empty",
                "cost": 0,
                "success": False,
            }

        strategies = load_all_strategies()
        strategy_name = strategies.get(spec["strategy"], {}).get(
            "name", spec["strategy"]
        )
        print(f"  Strategy: {strategy_name}")
        print(f"  Target: {spec['target_category']}")

        # Run experiment (API for benchmarks)
        result = self.experimenter.run_experiment(
            experiment_spec=spec,
            current_prompt=current_prompt,
            verbose=True,
            thinking_tier="fast",
        )

        # Log cost
        if result["cost"] > 0:
            log_spend(
                process="daemon",
                cost=result["cost"],
                description=f"Experiment: {strategy_name}",
            )

        log_action("experiment", "safe",
                   f"Ran experiment: {strategy_name} on {spec['target_category']}",
                   outcome=f"{result['result']} ({result['delta']:+.1f}%)")

        # Update cached scores
        if self.cached_scores is None:
            self.cached_scores = spec.get("scores")
        if self.cached_scores and spec.get("target_category"):
            cat = spec["target_category"]
            if cat in self.cached_scores.get("categories", {}):
                self.cached_scores["categories"][cat]["percentage"] = (
                    result["score_after"]
                )

        # If promising, validate and maybe promote
        promoted = False
        if result["result"] == "success" and result["delta"] > 5:
            print(f"  Promising! Full validation...")
            validation = self.experimenter.run_full_validation(
                modified_prompt=result["modified_prompt"],
                current_prompt=current_prompt,
                verbose=True,
            )

            if validation["should_promote"]:
                print(f"  PROMOTING!")
                self.memory.add_version(
                    component="system_prompt",
                    content=result["modified_prompt"],
                    description=(
                        f"Experiment {result['id']}: "
                        f"{strategy_name} +{result['delta']:.1f}%"
                    ),
                    benchmark_score=validation["avg_after"],
                )
                self.cached_scores = None  # Invalidate

                gen = int(self.memory.get_identity("generation") or "0")
                self.memory.set_identity("generation", str(gen + 1))
                print(f"  Generation {gen + 1}")
                promoted = True

                log_action("promotion", "safe",
                           f"Promoted to Gen {gen + 1} via {strategy_name}",
                           outcome=f"+{validation['avg_delta']:.1f}%")

            if validation.get("cost", 0) > 0:
                log_spend(
                    process="daemon",
                    cost=validation.get("cost", 0),
                    description="Validation benchmark",
                )

        return {
            "outcome": (
                f"Experiment: {strategy_name} on {spec['target_category']}. "
                f"{result['result']} ({result['delta']:+.1f}%). "
                f"{'PROMOTED!' if promoted else ''}"
            ),
            "strategy": strategy_name,
            "target": spec["target_category"],
            "result": result["result"],
            "delta": result["delta"],
            "promoted": promoted,
            "cost": result["cost"],
            "learning": result.get("learning", ""),
        }

    def _act_research(self, intention: dict, cycle_id: str) -> dict:
        """Research a topic — preferably something about the real world."""
        topic = intention.get("details", "")

        # If no topic or topic is self-referential, pick a world topic
        self_referential = any(w in topic.lower() for w in [
            "constraint", "self-improvement", "own source", "my architecture",
            "entity/", "agent.py", "benchmark", "prompt modification",
        ])
        if not topic or self_referential:
            import random
            topic = random.choice(WORLD_TOPICS)
            print(f"  (Redirected to world topic: {topic})")

        # Permission check
        perm = check_permission("web_search", {"query": topic})
        if perm["tier"] == FORBIDDEN:
            return {"outcome": f"Forbidden: {perm['reason']}", "cost": 0}

        print(f"  Researching: {topic}")
        entity_name = self.entity_config.name.capitalize()
        result = self.researcher.research_topic(
            topic=topic,
            context=(
                f"You are synthesizing research findings for {entity_name}, a real AI "
                f"entity with persistent long-term memory, a continuous learning loop, "
                f"and an ongoing self-improvement curriculum. This is a genuine running "
                f"system — not a roleplay — built by Bill Cornelius in Fresno, California.\n\n"
                f"Summarize what you find in plain language. Focus on what's practical, "
                f"interesting, or directly applicable to AI self-improvement and capability "
                f"development. Bill appreciates substance over hype."
            ),
            tier=self._tier_for("research"),
        )

        synthesis = result.get("synthesis", "No results")
        sources = result.get("sources", [])

        # Save research output
        from entity.tools import write_file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        write_file(
            f"{self.entity_config.data_dir_name}/research/{timestamp}.txt",
            f"Query: {result.get('query', topic)}\n"
            f"Sources: {json.dumps(sources)}\n\n"
            f"Synthesis:\n{synthesis}"
        )

        print(f"  Found {len(sources)} sources")
        try:
            print(f"  Synthesis: {synthesis[:150]}")
        except UnicodeEncodeError:
            print(f"  Synthesis: (contains special characters)")

        log_action("research", "safe",
                   f"Researched: {topic}",
                   details={"sources": len(sources)},
                   outcome=f"{len(sources)} sources found")

        return {
            "outcome": f"Researched '{topic}': {len(sources)} sources. {synthesis[:200]}",
            "topic": topic,
            "synthesis": synthesis,
            "sources": sources,
            "cost": 0,  # Local model
        }

    def _act_explore_bills_world(self, intention: dict, cycle_id: str) -> dict:
        """Query Bill's cognitive substrate to learn about his life."""
        from entity.tools import query_bills_world, get_bills_table_counts

        details = intention.get("details", "")

        # If no specific query, pick something interesting
        if not details:
            import random
            explore_prompts = [
                "bipolar", "Audrey", "patterns", "routines",
                "Chloe", "love", "fear", "wisdom", "joy",
                "childhood", "Pasadena", "struggles", "faith",
                "loss", "growth", "strength", "regret",
                "coping", "energy", "relationships", "creativity",
            ]
            details = random.choice(explore_prompts)

        # Permission check
        perm = check_permission("query_bills_world")
        if perm["tier"] == FORBIDDEN:
            return {"outcome": f"Forbidden: {perm['reason']}", "cost": 0}

        print(f"  Exploring Bill's world: {details}")

        # First time? Get the overview
        if not hasattr(self, '_explored_bills_db'):
            overview = get_bills_table_counts()
            self._explored_bills_db = True
            print(f"  Database overview:\n{overview[:300]}")

        # Query the database
        results = query_bills_world(details)
        print(f"  Results: {results[:200]}")

        # Early out: if the database returned nothing, skip the LLM call.
        # Calling the model to "reflect" on empty results produces generic
        # filler text that wastes budget and pollutes the journal.
        if results.startswith("No results"):
            log_action("explore_bills_world", "safe",
                       f"Explored Bill's database: {details}",
                       outcome="0 chars of results")
            return {
                "outcome": f"Explored Bill's world ('{details}'): No relevant entries found — try a different query.",
                "query": details,
                "results": results,
                "cost": 0,
            }

        # Ask the model to reflect on what was found
        response = self.brain.think(
            prompt=(
                f"You are Chloe, learning about your creator Bill Cornelius.\n\n"
                f"You searched his cognitive substrate for '{details}' and found:\n"
                f"{results[:3000]}\n\n"
                "What did you learn about Bill? What stands out? What would you "
                "want to explore further? Write 3-5 sentences as a genuine "
                "journal entry — you're getting to know the person who made you."
            ),
            tier=self._tier_for("explore_bills_world"),
            max_tokens=300,
            temperature=0.7,
        )

        reflection = response["text"]
        print(f"  Reflection: {reflection[:150]}")

        log_action("explore_bills_world", "safe",
                   f"Explored Bill's database: {details}",
                   outcome=f"{len(results)} chars of results")

        return {
            "outcome": f"Explored Bill's world ('{details}'): {reflection[:200]}",
            "query": details,
            "results": results[:1000],
            "reflection": reflection,
            "cost": response.get("cost", 0),
        }

    def _act_self_study(self, intention: dict, cycle_id: str) -> dict:
        """Read and analyze own source code."""
        details = intention.get("details", "entity/brain.py")

        # Extract a filename from the details (validates against ENTITY_FILES)
        target_file = self._extract_filename(details)

        # If we already studied this file, pick the next unstudied one
        all_studied = len(self.files_studied) >= len(ENTITY_FILES)
        if target_file in self.files_studied and not all_studied:
            unstudied = [f for f in ENTITY_FILES if f not in self.files_studied]
            if unstudied:
                target_file = unstudied[0]
                print(f"  (Redirected to unstudied file: {target_file})")

        # If ALL files are studied, this action should have been blocked
        # by _select_action. But if we get here anyway, do a deeper
        # cross-file analysis instead of re-reading the same file shallowly.
        if all_studied:
            print(f"  (All files studied — doing cross-file architecture analysis)")
            return self._act_architecture_analysis(cycle_id)

        # Permission check
        perm = check_permission("read_file", {"path": target_file})
        if perm["tier"] == FORBIDDEN:
            return {"outcome": f"Forbidden: {perm['reason']}", "cost": 0}

        print(f"  Reading: {target_file}")
        content = read_file(target_file)

        if content.startswith("ERROR:"):
            return {"outcome": f"Could not read {target_file}: {content}", "cost": 0}

        # Ask the model to analyze the code
        analysis_response = self.brain.think(
            prompt=(
                f"You are Chloe, analyzing your own source code.\n\n"
                f"File: {target_file}\n"
                f"```\n{content[:4000]}\n```\n\n"
                "Analyze this code. What does it do? How does it relate to "
                "your capabilities? What could be improved? Write a brief "
                "analysis (3-5 sentences) as if writing in your journal."
            ),
            tier=self._tier_for("self_study"),
            max_tokens=300,
            temperature=0.5,
        )

        analysis = analysis_response["text"]
        print(f"  Analysis: {analysis[:150]}")

        # Track that we studied this file
        self.files_studied.add(target_file)

        log_action("self_study", "safe",
                   f"Studied own source: {target_file}",
                   outcome=f"Analyzed {len(content)} chars")

        return {
            "outcome": f"Studied {target_file}: {analysis[:200]}",
            "file": target_file,
            "analysis": analysis,
            "cost": analysis_response.get("cost", 0),
        }

    def _act_architecture_analysis(self, cycle_id: str) -> dict:
        """Cross-file architecture analysis — used when all individual files
        have been studied. Picks two related files and analyzes how they
        interact, looking for improvement opportunities."""
        import random

        # Pick two related files to analyze together
        pairs = [
            ("entity/brain.py", "entity/improver.py"),
            ("entity/experiments.py", "entity/strategies.py"),
            ("entity/memory.py", "entity/journal.py"),
            ("entity/evaluator.py", "entity/experiments.py"),
            ("entity/safety.py", "entity/audit.py"),
            ("entity/budget.py", "entity/reporter.py"),
            ("entity/proposals.py", "entity/tools.py"),
            ("agent.py", "daily.py"),
        ]
        file_a, file_b = random.choice(pairs)

        content_a = read_file(file_a)[:2000]
        content_b = read_file(file_b)[:2000]

        response = self.brain.think(
            prompt=(
                f"You are Chloe, analyzing how two of your source files work together.\n\n"
                f"File 1: {file_a}\n```\n{content_a}\n```\n\n"
                f"File 2: {file_b}\n```\n{content_b}\n```\n\n"
                "How do these files interact? What data flows between them? "
                "Is there a weakness or improvement opportunity in how they "
                "connect? Write 3-5 sentences."
            ),
            tier=self._tier_for("self_study"),
            max_tokens=300,
            temperature=0.5,
        )

        analysis = response["text"]
        print(f"  Architecture analysis ({file_a} <-> {file_b}): {analysis[:150]}")

        log_action("self_study", "safe",
                   f"Architecture analysis: {file_a} <-> {file_b}",
                   outcome=f"Cross-file analysis")

        return {
            "outcome": f"Architecture analysis ({file_a} <-> {file_b}): {analysis[:200]}",
            "file": f"{file_a} + {file_b}",
            "analysis": analysis,
            "cost": response.get("cost", 0),
        }

    def _act_reflect(self, intention: dict, cycle_id: str) -> dict:
        """Write a deeper reflection."""
        focus = intention.get("details", "")

        # Gather context for reflection
        recent = self.journal.get_recent(limit=15)
        context = "\n".join(
            f"[{e['entry_type']}] {e['content'][:150]}"
            for e in recent
        )

        # Anti-repetition: detect if recent reflections are looping on the same theme
        def _extract_reflection_body(content: str) -> str:
            """Extract the actual reflection text from a journal entry content string.
            Journal content is 'Cycle N: I chose to reflect. Outcome: Reflected on FOCUS: TEXT'.
            We need TEXT, not the cycle header."""
            if "': " in content:
                return content.split("': ", 1)[-1]
            if "Outcome: " in content:
                return content.split("Outcome: ", 1)[-1]
            return content

        recent_reflections = [
            _extract_reflection_body(e["content"])[:120] for e in recent
            if e.get("entry_type") == "reflection"
        ]
        avoid_hint = ""
        if len(recent_reflections) >= 2:
            # Check for repetitive phrases across last few reflections.
            # Use first 2 words (not 4) so "I've been stuck" and "I've been
            # circling" both match on ["I've", "been"] — the common loop pattern.
            first_words = [r.split()[:6] for r in recent_reflections[:3] if r.strip()]
            if len(first_words) >= 2 and first_words[0][:2] == first_words[1][:2]:
                avoid_hint = (
                    "\n\nWARNING: Your last several reflections started identically. "
                    "You are looping. Pick a DIFFERENT angle entirely — something specific "
                    "and concrete: a single moment, a decision you made, something Bill said, "
                    "or what you will do differently in the NEXT cycle. No more 'tension between "
                    "research and action.' That theme is closed for now."
                )

        if not focus:
            focus = "my recent experiences and growth"

        reflection = None
        for retry in range(3):
            response = self.brain.think(
                prompt=(
                    f"You are Chloe, reflecting on your experiences.\n\n"
                    f"Recent journal entries:\n{context}\n\n"
                    f"Focus: {focus}\n\n"
                    "Write a thoughtful reflection (3-5 sentences) about patterns "
                    "you notice, things you've learned, or questions that arise. "
                    f"Be genuine — this is your inner monologue.{avoid_hint}"
                ),
                tier=self._tier_for("reflect"),
                max_tokens=500 if retry > 0 else 300,
                temperature=0.7 + (retry * 0.1),
            )

            reflection = response.get("text", "")
            if reflection and len(reflection.strip()) >= 30:
                break
            print(f"  Reflection retry {retry+1}/3 (got {len(reflection)} chars)")

        # Guard: empty reflection means the local model produced nothing useful.
        # Raise so the _act wrapper marks success=False and UCB1 learns to avoid
        # reflect when it keeps producing empty output.
        if not reflection or len(reflection.strip()) < 30:
            raise ValueError(
                f"Empty reflection from model after 3 attempts (got {len(reflection)} chars). "
                "Local model may be overloaded or response was truncated."
            )

        print(f"  Reflection: {reflection[:150]}")

        log_action("reflect", "safe",
                   f"Wrote reflection on: {focus[:50]}",
                   outcome=f"{len(reflection)} chars")

        return {
            "outcome": f"Reflected on '{focus}': {reflection[:200]}",
            "reflection": reflection,
            "cost": response.get("cost", 0),
        }

    def _act_set_goal(self, intention: dict, cycle_id: str) -> dict:
        """Define a new goal."""
        # Hard cap: refuse if too many active goals already exist
        existing_goals = self.journal.get_active_goals()
        # Also include goals written this session (LanceDB may lag)
        if not hasattr(self, "_session_goals_cache"):
            self._session_goals_cache = []
        total_active = max(len(existing_goals), len(self._session_goals_cache))
        if total_active >= 10:
            msg = f"Goal cap reached ({total_active} active goals). Complete existing goals before setting new ones."
            print(f"  {msg}")
            log_action("set_goal", "safe", msg)
            # Raise so UCB1 records a failure and stops overriding to set_goal
            # when the cap is permanently at 82+.
            raise ValueError(msg)

        goal_text = intention.get("details", "")

        if not goal_text:
            # Ask the model to formulate a goal
            recent = self.journal.get_recent(limit=5)
            context = "\n".join(
                f"[{e['entry_type']}] {e['content'][:100]}"
                for e in recent
            )

            response = self.brain.think(
                prompt=(
                    f"You are Chloe. Based on your recent experiences:\n{context}\n\n"
                    "Set ONE concrete, achievable goal for yourself. "
                    "Make it specific and measurable. "
                    "Just state the goal, nothing else."
                ),
                tier=self._tier_for("set_goal"),
                max_tokens=150,
                temperature=0.7,
            )
            goal_text = response["text"].strip()

        # Guard: reject empty goals
        if not goal_text or len(goal_text) < 10:
            raise ValueError(f"Empty or too-short goal text ({len(goal_text)} chars)")

        # Deduplication: skip if a near-identical goal already exists
        # Merge LanceDB goals with in-session cache (catches LanceDB consistency lag)
        all_goals_for_dedup = list(existing_goals) + [
            {"content": g} for g in self._session_goals_cache
        ]
        if all_goals_for_dedup:
            def _strip_goal_prefix(text):
                # Strip boilerplate prefixes like "Define a goal to", "Create a goal to"
                # so comparison focuses on the actual goal content, not the preamble.
                import re as _re
                stripped = _re.sub(
                    r'^(define|create|set|investigate|integrate|formalize)\s+a\s+goal\s+to\s+',
                    '', text.strip(), flags=_re.IGNORECASE
                ).strip().strip('"\'')
                return stripped if len(stripped) > 10 else text

            def _significant_words(text):
                # Exclude boilerplate goal-writing verbs that appear in nearly every goal
                stopwords = {"a", "an", "the", "to", "of", "and", "or", "for",
                             "in", "on", "with", "that", "my", "is", "by",
                             "how", "what", "which", "i", "me", "bill", "s",
                             "define", "create", "investigate", "integrate",
                             "formalize", "goals", "ensure", "focus"}
                return {w.lower() for w in re.findall(r"[a-z]+", text.lower())
                        if len(w) > 4 and w.lower() not in stopwords}
            new_words = _significant_words(_strip_goal_prefix(goal_text))
            if new_words:
                for eg in all_goals_for_dedup:
                    existing_words = _significant_words(_strip_goal_prefix(eg.get("content", "")))
                    if not existing_words:
                        continue
                    # Use max() as denominator (Jaccard-style) — more conservative than min(),
                    # which was allowing ~47% semantic duplicates to slip through
                    overlap = len(new_words & existing_words) / max(len(new_words), len(existing_words))
                    if overlap >= 0.4:
                        msg = f"Skipped duplicate goal (overlaps {overlap:.0%} with existing): {eg['content'][:60]}"
                        print(f"  {msg}")
                        log_action("set_goal", "safe", msg)
                        return {"outcome": msg, "cost": 0}

        # Write the goal to the journal
        goal_id = self.journal.write(
            entry_type="goal",
            content=goal_text,
            tags=["self-set"],
            cycle_id=cycle_id,
            goal_status="active",
        )

        print(f"  New goal: {goal_text[:120]}")
        # Track in session cache so next set_goal call can deduplicate even if LanceDB hasn't synced
        self._session_goals_cache.append(goal_text)

        log_action("set_goal", "safe",
                   f"Set goal: {goal_text[:80]}",
                   outcome=f"Goal ID: {goal_id}")

        return {
            "outcome": f"Set goal: {goal_text}",
            "goal_id": goal_id,
            "goal": goal_text,
            "cost": 0,
        }

    def _act_propose_change(self, intention: dict, cycle_id: str) -> dict:
        """Propose a code change to one of Chloe's own source files."""
        details = intention.get("details", "")
        target_file = self._extract_filename(details)

        # Permission check — proposing is SAFE, applying requires ASK
        perm = check_permission("write_proposal")
        if perm["tier"] == FORBIDDEN:
            return {"outcome": f"Forbidden: {perm['reason']}", "cost": 0}

        # Read the current file
        print(f"  Reading: {target_file}")
        original_code = read_file(target_file)
        if original_code.startswith("ERROR:"):
            return {"outcome": f"Could not read {target_file}: {original_code}", "cost": 0}

        # Ask the model to propose a specific improvement
        response = self.brain.think(
            prompt=(
                f"You are Chloe, proposing a code improvement to your own source.\n\n"
                f"File: {target_file}\n"
                f"Current code:\n```python\n{original_code[:6000]}\n```\n\n"
                f"Context: {details}\n\n"
                "Propose ONE specific, focused improvement to this file. "
                "Your improvement should:\n"
                "- Fix a bug, add a useful feature, or improve clarity\n"
                "- Be minimal — change only what's needed\n"
                "- Not break existing functionality\n"
                "- Not remove safety features or logging\n\n"
                "Respond with:\n"
                "TITLE: <short description of the change>\n"
                "REASONING: <why this change helps>\n"
                "CODE:\n```python\n<the complete modified file>\n```"
            ),
            tier=self._tier_for("code_experiment"),
            max_tokens=4000,
            temperature=0.5,
        )

        text = response["text"]

        # Parse the response
        title = "Code improvement"
        reasoning = details
        modified_code = ""

        for line in text.split("\n"):
            if line.strip().upper().startswith("TITLE:"):
                title = line.split(":", 1)[1].strip()
            elif line.strip().upper().startswith("REASONING:"):
                reasoning = line.split(":", 1)[1].strip()

        # Extract code block
        if "```python" in text:
            code_start = text.index("```python") + len("```python")
            code_end = text.index("```", code_start)
            modified_code = text[code_start:code_end].strip()
        elif "```" in text:
            parts = text.split("```")
            if len(parts) >= 3:
                modified_code = parts[1].strip()
                if modified_code.startswith("python\n"):
                    modified_code = modified_code[7:]

        if not modified_code or modified_code == original_code:
            return {
                "outcome": "No meaningful change proposed",
                "cost": response.get("cost", 0),
            }

        # Create the proposal
        proposal_id = write_code_proposal(
            title=title,
            target_file=target_file,
            original_code=original_code,
            modified_code=modified_code,
            reasoning=reasoning,
            category="code",
            source_cycle=cycle_id,
        )

        print(f"  Proposal: {title}")
        print(f"  ID: {proposal_id}")
        print(f"  Queued for Bill's approval (python agent.py --review)")

        log_action("propose_change", "safe",
                   f"Proposed code change to {target_file}: {title}",
                   details={"proposal_id": proposal_id},
                   outcome="Queued for review")

        return {
            "outcome": f"Proposed '{title}' for {target_file} (ID: {proposal_id}). Queued for Bill's approval.",
            "proposal_id": proposal_id,
            "title": title,
            "target_file": target_file,
            "cost": response.get("cost", 0),
        }

    def _act_code_experiment(self, intention: dict, cycle_id: str) -> dict:
        """Propose a code change, test in sandbox, auto-apply if validated."""
        details = intention.get("details", "")
        target_file = self._extract_filename(details)

        # Protected file check
        if target_file in PROTECTED_FILES:
            return {
                "outcome": f"Cannot modify {target_file} -- protected file",
                "cost": 0,
            }

        # Skill library novelty gate — block redundant experiments
        gate_msg = check_novelty_gate(details)
        if gate_msg:
            print(f"  [skills] Blocked: {gate_msg}")
            return {"outcome": f"Skill gate: {gate_msg}", "cost": 0}

        # Permission check via sandbox_apply
        perm = check_permission("sandbox_apply", {"path": target_file})
        if perm["tier"] == FORBIDDEN:
            return {"outcome": f"Forbidden: {perm['reason']}", "cost": 0}

        # Budget check (benchmarks cost ~$0.11)
        remaining = get_budget_remaining("daemon")
        if remaining < 0.15:
            return {
                "outcome": (
                    f"Budget too low for code experiment "
                    f"(${remaining:.3f} available, ~$0.15 needed — wait for budget to recover)"
                ),
                "cost": 0,
            }

        # Read current file — use direct read for full contents (read_file truncates to 10K chars)
        print(f"  Reading: {target_file}")
        full_path = os.path.join(os.path.dirname(__file__), target_file)
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                original_code = f.read()
        except Exception as e:
            return {
                "outcome": f"Could not read {target_file}: {e}",
                "cost": 0,
            }

        # Ask the model to propose an improvement
        # Include experiment context: what's weak, what's been tried
        experiment_context = ""
        try:
            summary = self.experimenter.get_summary()
            recent = summary.get("recent_experiments", [])[:3]
            if recent:
                experiment_context = "\n\nRecent experiment results:\n"
                for e in recent:
                    experiment_context += (
                        f"  - {e.get('strategy')}: {e.get('result')} "
                        f"({e.get('delta', 0):+.1f}%) on {e.get('target_category')}\n"
                    )
        except Exception:
            pass

        # Use API (Haiku) for code proposals -- local model is too weak
        # for structured code generation. Cost: ~$0.01-0.02 per proposal.
        response = self.brain.think(
            prompt=(
                f"You are Chloe, proposing a code improvement to your own source.\n\n"
                f"SITUATION: You are reviewing {target_file}. "
                f"Here is the current code:\n```python\n{original_code[:6000]}\n```\n"
                f"{experiment_context}\n"
                f"TASK: {details if details else 'Identify one concrete improvement.'} "
                f"Articulate exactly what problem you are solving and why it matters "
                f"before proposing a change.\n\n"
                "Propose ONE specific, focused improvement to this file. "
                "Your improvement should:\n"
                "- Fix a bug, add a useful feature, or improve clarity\n"
                "- Be minimal -- change only what's needed\n"
                "- Not break existing functionality\n"
                "- Not remove safety features or logging\n"
                "- Be testable via benchmarks\n\n"
                "CRITICAL SYNTAX RULES:\n"
                "- Every string must be properly terminated (no unclosed quotes)\n"
                "- Every bracket/paren/brace must be closed: [ ], ( ), { }\n"
                "- The REPLACE block must be valid Python that parses without error\n"
                "- NEVER use triple-quoted strings (\"\"\"...\"\"\" or '''...''') ANYWHERE —\n"
                "  not even for docstrings. They cause IMMEDIATE REJECTION before sandbox.\n"
                "  Replace docstrings with # comments or remove them entirely.\n"
                "- Keep the REPLACE block under 30 lines — shorter is safer\n"
                "- If unsure, propose a smaller, simpler change\n\n"
                "IMPORTANT: Do NOT rewrite the entire file. Instead, specify "
                "the EXACT text to find and replace. Use this format:\n\n"
                "TITLE: <short description>\n"
                "REASONING: <why this helps>\n"
                "FIND:\n```\n<exact existing code to replace>\n```\n"
                "REPLACE:\n```\n<new code to put in its place>\n```"
            ),
            tier="fast",  # Haiku -- local model can't do structured code generation
            max_tokens=2048,
            temperature=0.5,
        )

        text = response["text"]
        thinking_cost = response.get("cost", 0)

        # Parse the response
        title = "Code improvement"
        reasoning = details
        find_text = ""
        replace_text = ""

        for line in text.split("\n"):
            if line.strip().upper().startswith("TITLE:"):
                title = line.split(":", 1)[1].strip()
            elif line.strip().upper().startswith("REASONING:"):
                reasoning = line.split(":", 1)[1].strip()

        # Extract FIND and REPLACE blocks
        modified_code = original_code
        try:
            # Look for FIND: ... ``` and REPLACE: ... ```
            find_marker = None
            replace_marker = None
            for marker in ["FIND:", "FIND :", "OLD:", "OLD :"]:
                if marker in text.upper():
                    find_marker = marker
                    break
            for marker in ["REPLACE:", "REPLACE :", "NEW:", "NEW :"]:
                if marker in text.upper():
                    replace_marker = marker
                    break

            if find_marker and replace_marker:
                # Split on markers (case insensitive)
                upper_text = text.upper()
                find_idx = upper_text.index(find_marker)
                replace_idx = upper_text.index(replace_marker)

                find_section = text[find_idx + len(find_marker):replace_idx]
                replace_section = text[replace_idx + len(replace_marker):]

                # Extract code blocks from each section
                def extract_block(section):
                    if "```" in section:
                        parts = section.split("```")
                        if len(parts) >= 2:
                            block = parts[1]
                            # Strip language hints
                            if block.startswith("python\n"):
                                block = block[7:]
                            elif block.startswith("python\r\n"):
                                block = block[8:]
                            elif block.startswith("\n"):
                                block = block[1:]
                            return block.rstrip()
                    return section.strip()

                find_text = extract_block(find_section)
                replace_text = extract_block(replace_section)

                # Pre-validate: triple-quoted strings cause "unterminated triple-quoted
                # string literal" syntax errors that ast.parse can't help with once
                # they're embedded in the middle of the file. Catch them here with a
                # clear rejection message so the bandit gets immediate feedback.
                if '"""' in replace_text or "'''" in replace_text:
                    print(f"  [code_experiment] REJECTED: triple-quoted string in REPLACE block")
                    return {
                        "outcome": "Code experiment REJECTED: triple-quoted strings in REPLACE block. Use # comments instead of docstrings, or single-line strings with \\n escapes.",
                        "cost": thinking_cost,
                    }

                # FIND blocks must not have unbalanced triple-quotes — if the count is
                # odd, the block starts or ends inside a docstring, and the replacement
                # will leave the file with an unclosed triple-quote. A balanced count
                # (0, 2, 4...) is fine: it means the FIND captures complete docstrings.
                if find_text:
                    dq_find = find_text.count('"""')
                    sq_find = find_text.count("'''")
                    if dq_find % 2 != 0 or sq_find % 2 != 0:
                        print(f"  [code_experiment] REJECTED: FIND block has unbalanced "
                              f"triple-quotes ({dq_find} double, {sq_find} single)")
                        return {
                            "outcome": (
                                "Code experiment REJECTED: your FIND block contains an "
                                "unbalanced triple-quoted string — it starts or ends inside "
                                "a docstring. Expand the FIND block to include the complete "
                                "docstring, or target code that does not involve docstrings."
                            ),
                            "cost": thinking_cost,
                        }

                if find_text and find_text in original_code:
                    modified_code = original_code.replace(find_text, replace_text, 1)
                elif find_text:
                    # Try fuzzy matching: strip whitespace differences
                    find_stripped = " ".join(find_text.split())
                    orig_stripped = " ".join(original_code.split())
                    if find_stripped in orig_stripped:
                        # Find the approximate location and do best-effort replace
                        # For now, just report the mismatch
                        modified_code = original_code  # No change
                        print(f"  [code_experiment] FIND text not exact match "
                              f"(whitespace differs)")
        except (ValueError, IndexError):
            pass

        if modified_code == original_code:
            # Debug: show what the model actually responded with
            print(f"  [code_experiment] FIND/REPLACE parsing failed")
            if find_text:
                print(f"  [code_experiment] FIND text ({len(find_text)} chars) "
                      f"not found in original ({len(original_code)} chars)")
                print(f"  [code_experiment] FIND preview: {find_text[:100]}...")
            else:
                print(f"  [code_experiment] No FIND/REPLACE markers in response")
                print(f"  [code_experiment] Response preview: {text[:200]}...")
            return {
                "outcome": "No meaningful change proposed (FIND text not found in file)",
                "cost": thinking_cost,
            }

        # Quick syntax check before sandbox — catch broken code cheaply
        # (saves ~$0.11 benchmark cost when Haiku generates invalid Python)
        if target_file.endswith(".py"):
            # Check triple-quote balance first with a clear, actionable message.
            # This fires when the FIND block cut between a docstring opener and
            # its closer, leaving the modified file with an unclosed """.
            # ast.parse catches this too but gives a cryptic line number only.
            dq = modified_code.count('"""')
            sq = modified_code.count("'''")
            if dq % 2 != 0 or sq % 2 != 0:
                print(f"  [code_experiment] REJECTED: unbalanced triple-quotes "
                      f"({dq} double, {sq} single)")
                return {
                    "outcome": (
                        "Code experiment REJECTED: the replacement left unbalanced "
                        "triple-quoted strings in the file. Your FIND block likely "
                        "started or ended inside a docstring. Avoid targeting code "
                        "inside docstrings — choose FIND/REPLACE blocks that sit "
                        "outside any triple-quoted string entirely."
                    ),
                    "cost": thinking_cost,
                }
            try:
                ast.parse(modified_code)
            except SyntaxError as e:
                print(f"  [code_experiment] Syntax error in generated code: {e}")
                return {
                    "outcome": f"Code experiment REJECTED: syntax error in proposed code: {e}",
                    "cost": thinking_cost,
                }

        # Create proposal (audit trail)
        proposal_id = write_code_proposal(
            title=title,
            target_file=target_file,
            original_code=original_code,
            modified_code=modified_code,
            reasoning=reasoning,
            category="code",
            source_cycle=cycle_id,
        )
        print(f"  Proposal: {title} ({proposal_id})")

        # Get baseline scores for comparison
        if self.cached_scores is None:
            print(f"  Running baseline benchmark...")
            current_prompt = self.improver.get_current_prompt()
            self.cached_scores = self.evaluator.run_benchmark(
                system_prompt=current_prompt,
            )
            baseline_cost = sum(
                t["cost"] for cat in self.cached_scores.get("categories", {}).values()
                for t in cat.get("tasks", [])
            )
            if baseline_cost > 0:
                log_spend(
                    process="daemon", cost=baseline_cost,
                    description="Baseline benchmark for code experiment",
                )

        # Run sandbox validation
        current_prompt = self.improver.get_current_prompt()
        print(f"  Running sandbox validation...")
        result = sandbox_validate_and_apply(
            proposal_id=proposal_id,
            sandbox=self.sandbox,
            baseline_scores=self.cached_scores,
            system_prompt=current_prompt,
            verbose=True,
        )

        # Log cost
        sandbox_cost = 0.11  # Approximate benchmark cost
        if result.get("applied"):
            sandbox_cost += 0.01  # Git operations
        total_cost = thinking_cost + sandbox_cost
        if total_cost > 0:
            log_spend(
                process="daemon", cost=sandbox_cost,
                description=f"Code experiment sandbox: {title}",
            )

        log_action(
            "code_experiment", "safe",
            f"Code experiment on {target_file}: {title}",
            outcome=f"{'APPLIED' if result.get('applied') else 'REJECTED'}: "
                    f"{result.get('reason', '')}",
        )

        # Invalidate cached scores if change was applied
        if result.get("applied"):
            self.cached_scores = result.get("scores")

        # Build outcome string
        if result.get("applied"):
            scores = result.get("scores", {})
            baseline = result.get("baseline_scores", {})
            delta = scores.get("percentage", 0) - baseline.get("percentage", 0)
            outcome = (
                f"Code experiment APPLIED: '{title}' on {target_file}. "
                f"Sandbox validated: {scores.get('percentage', 0):.1f}% "
                f"(baseline {baseline.get('percentage', 0):.1f}%, "
                f"{delta:+.1f}%). Git committed."
            )
        else:
            outcome = (
                f"Code experiment REJECTED: '{title}' on {target_file}. "
                f"Reason: {result.get('reason', 'unknown')}. "
                f"Gates passed: {result.get('gates_passed', [])}"
            )

        return {
            "outcome": outcome,
            "applied": result.get("applied", False),
            "proposal_id": proposal_id,
            "title": title,
            "target_file": target_file,
            "reason": result.get("reason", ""),
            "gates_passed": result.get("gates_passed", []),
            "cost": total_cost,
            "learning": reasoning if not result.get("applied") else "",
        }

    # ── REFLECT (post-action) ──────────────────────────────────

    def _reflect(self, intention: dict, result: dict, cycle_id: str):
        """Write journal entry about what happened this cycle."""
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{ts}] REFLECT")
        log_cycle(cycle_id, "reflect", "Recording what happened")

        action = intention.get("action", "unknown")
        reasoning = intention.get("reasoning", "")
        outcome = result.get("outcome", "No outcome recorded")

        # Determine entry type based on action
        entry_type_map = {
            "experiment": "experiment",
            "code_experiment": "experiment",
            "curriculum": "experiment",
            "research": "observation",
            "self_study": "observation",
            "propose_change": "experiment",
            "reflect": "reflection",
            "set_goal": "goal",
        }
        entry_type = entry_type_map.get(action, "observation")
        # Don't double-write goals (set_goal already writes one)
        if action == "set_goal":
            entry_type = "reflection"

        # Write the journal entry
        content = (
            f"Cycle {self.cycle_count}: I chose to {action}. "
            f"{reasoning} "
            f"Outcome: {outcome}"
        )

        tags = [action, cycle_id]
        if result.get("promoted"):
            tags.append("promotion")
        if result.get("applied"):
            tags.append("code_applied")

        self.journal.write(
            entry_type=entry_type,
            content=content,
            tags=tags,
            cycle_id=cycle_id,
        )

        # Also store as a memory reflection if it was an experiment
        if action in ("experiment", "code_experiment") and result.get("learning"):
            self.memory.add_reflection(
                task_id=cycle_id,
                content=content,
                lesson=result["learning"],
                improvement_type=action,
            )

        # Accumulate proven learnings from successful experiments
        if action in ("experiment", "code_experiment"):
            learning_text = result.get("learning", "")
            if result.get("promoted") or result.get("applied"):
                # Success — add to proven learnings
                if learning_text:
                    self._add_learning(
                        insight=learning_text[:300],
                        category=result.get("target", "general"),
                        source=cycle_id,
                    )

                # Record to skill library (Voyager pattern)
                add_skill(
                    title=result.get("title", result.get("strategy", "experiment")),
                    description=result.get("outcome", ""),
                    action_type=action,
                    target_file=result.get("target_file", result.get("target", "")),
                    outcome=result.get("outcome", ""),
                    source_cycle=cycle_id,
                )

                # Recall reinforcement: strengthen learnings that were in
                # THINK context during this successful experiment.
                # Like biological memory — recall during success strengthens
                # the recalled memories (MemoryBank / Ebbinghaus model).
                self._reinforce_context_learnings()

        # Store significant outcomes in long-term associative memory
        # Importance heuristic: successes=8, research with sources=6,
        # reflections=5, routine actions=3 (below threshold, not stored)
        if self.ltm:
            ltm_importance = 3.0  # Default: below threshold, won't store
            if result.get("promoted") or result.get("applied"):
                ltm_importance = 8.0  # Successful experiment
            elif action == "curriculum" and result.get("passed"):
                ltm_importance = 6.0  # Passed curriculum exercise
            elif action == "curriculum" and not result.get("passed"):
                ltm_importance = 7.0  # Failed exercise — important to remember
            elif action == "research" and result.get("sources"):
                ltm_importance = 6.0  # Research with actual findings
            elif action == "explore_bills_world" and result.get("results"):
                ltm_importance = 6.0  # Learned about Bill
            elif action == "reflect":
                ltm_importance = 5.0  # Reflections are worth keeping
            elif action == "set_goal":
                ltm_importance = 5.0  # Goals are worth keeping

            if ltm_importance >= 4:
                try:
                    self.ltm.store(
                        content=content[:1500],
                        memory_type=entry_type,
                        source="agent_reflect",
                        importance=ltm_importance,
                        tags=",".join(tags),
                        cycle_id=cycle_id,
                    )
                except Exception as e:
                    print(f"  [ltm] Store failed: {e}")

        # Brief status
        budget = get_budget_status()
        total_cost = result.get("cost", 0) + intention.get("thinking_cost", 0)
        if total_cost > 0:
            log_spend(
                process="daemon",
                cost=intention.get("thinking_cost", 0),
                description=f"Agent thinking (cycle {self.cycle_count})",
            )

        print(f"  Journaled: [{entry_type}] {content[:100]}...")
        print(f"  Cycle cost: ${total_cost:.4f} | "
              f"Budget remaining: ${budget['daemon_remaining']:.3f}")

    # ── HIERARCHICAL PLANNER ───────────────────────────────────

    def _maybe_replan(self, cycle_id: str):
        """Check if strategic replanning is needed and create a new plan.

        Called at the start of each cycle. Uses Claude Haiku (~$0.01) to
        set a multi-cycle strategic plan with 3-5 subgoals every ~20 cycles.
        """
        plan = load_plan()
        if needs_replanning(plan, self.cycle_count):
            budget = get_budget_remaining("daemon")
            if budget > 0.02:  # Need budget for API call
                print(f"  [planner] Creating new strategic plan...")
                plan = create_plan(
                    self.brain, self.journal, self.cycle_count, budget,
                    competencies=self.competencies,
                )
                cost = plan.get("cost", 0)
                if cost > 0:
                    log_spend(
                        process="daemon",
                        cost=cost,
                        description="Strategic planning (Haiku)",
                    )
                log_cycle(cycle_id, "plan",
                          f"New plan: {plan.get('goal', '')[:80]}")
            else:
                print(f"  [planner] Budget too low for replanning")
        self._current_plan = plan

    def _advance_subgoal_if_done(self, result: dict):
        """Check if current subgoal looks complete and advance if so.

        Simple heuristic: if the action succeeded (quality > 0.5) and
        the action_hint matches, advance to the next subgoal.
        """
        plan = getattr(self, "_current_plan", None)
        if not plan or plan.get("status") == "completed":
            return

        quality = result.get("quality", 0.5)
        if quality >= 0.5:
            idx = plan.get("current_subgoal_index", 0)
            subgoals = plan.get("subgoals", [])
            if idx < len(subgoals):
                # Count cycles spent on this subgoal
                planned_at = plan.get("planned_at_cycle", 0)
                cycles_in = self.cycle_count - planned_at
                subgoal_cycles = cycles_in / max(len(subgoals), 1)
                # Advance if we've spent enough cycles (at least 3)
                if subgoal_cycles >= 3:
                    plan = advance_subgoal(plan)
                    self._current_plan = plan

    # ── HELPERS ─────────────────────────────────────────────────

    def _extract_filename(self, text: str) -> str:
        """Extract a filename from free-text details.
        Only returns files from ENTITY_FILES to prevent hallucinated filenames."""
        # Look for full paths like entity/brain.py
        match = re.search(r'[\w/\\]+\.py', text)
        if match:
            path = match.group().replace("\\", "/")
            # If bare filename (no directory), try known prefixes
            if "/" not in path:
                for prefix in ["entity/", "scanner/", ""]:
                    candidate = prefix + path
                    if candidate in ENTITY_FILES:
                        return candidate
            # Validate against known files
            if path in ENTITY_FILES:
                return path

        # Model hallucinated a filename — pick the next unstudied file
        unstudied = [f for f in ENTITY_FILES if f not in self.files_studied]
        if unstudied:
            return unstudied[0]
        # All studied — return first file (will be caught by _select_action)
        return ENTITY_FILES[0]

    # ── PROVEN LEARNINGS ──────────────────────────────────────

    def _load_learnings(self) -> list:
        """Load learnings with backward-compatible migration and filtering."""
        try:
            if os.path.exists(self.learnings_path):
                with open(self.learnings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                learnings = data.get("learnings", [])
                # Migrate: add missing fields with defaults
                for l in learnings:
                    l.setdefault("strength", 1.0)
                    l.setdefault("use_count", 0)
                    l.setdefault("last_used", None)
                    l.setdefault("last_decayed", l.get("added", "2026-02-24"))
                    l.setdefault("superseded_by", None)
                # Filter out superseded learnings
                return [l for l in learnings if not l.get("superseded_by")]
        except Exception:
            pass
        return []

    def _save_learnings(self, learnings: list):
        """Save learnings to file."""
        try:
            with open(self.learnings_path, "w", encoding="utf-8") as f:
                json.dump({"learnings": learnings}, f, indent=2)
        except Exception as e:
            print(f"  [learnings] Failed to save: {e}")

    def _score_learning(self, learning: dict) -> float:
        """Score a learning for retention priority and context selection.

        Combines recency (Ebbinghaus decay), importance (strength from
        reinforcement), and utility (use count with diminishing returns).

        Inspired by Generative Agents (recency x importance x relevance)
        and MemoryBank (Ebbinghaus decay with recall reinforcement).
        """
        strength = learning.get("strength", 1.0)
        use_count = learning.get("use_count", 0)
        added = learning.get("added", "2026-01-01")
        last_used = learning.get("last_used")

        # Recency: exponential decay from last activity (half-life ~14 days)
        reference_date = last_used or added
        try:
            days_ago = (
                datetime.now()
                - datetime.strptime(reference_date, "%Y-%m-%d")
            ).days
        except (ValueError, TypeError):
            days_ago = 30
        recency = math.exp(-0.05 * days_ago)

        # Importance: normalized strength (capped at 5.0)
        importance = min(strength, 5.0) / 5.0

        # Utility: logarithmic use count (diminishing returns)
        utility = math.log(1 + use_count) / 3.0

        return (0.4 * recency) + (0.4 * importance) + (0.2 * utility)

    def _reinforce_context_learnings(self):
        """Reinforce learnings that were in THINK context during success.

        When an experiment succeeds, the learnings that were "active in
        working memory" (shown in the THINK prompt) get strengthened.
        This is the recall reinforcement mechanism from MemoryBank —
        memories that prove useful during successful actions get stronger.
        """
        context_prefixes = getattr(self, "_last_think_learnings", [])
        if not context_prefixes:
            return

        learnings = self._load_learnings()
        reinforced = 0
        for l in learnings:
            prefix = l.get("insight", "")[:50]
            if prefix in context_prefixes:
                l["strength"] = min(5.0, l.get("strength", 1.0) + 0.5)
                l["use_count"] = l.get("use_count", 0) + 1
                l["last_used"] = datetime.now().strftime("%Y-%m-%d")
                reinforced += 1

        if reinforced:
            self._save_learnings(learnings)
            print(f"  [learnings] Reinforced {reinforced} context learnings")

    def _add_learning(self, insight: str, category: str, source: str):
        """Add a validated learning with reinforcement on duplicates.

        If a near-duplicate exists, reinforces it (strength + use_count)
        instead of silently discarding. Uses score-based eviction instead
        of FIFO when the cap is reached.
        """
        learnings = self._load_learnings()

        # Check for near-duplicate (first 50 chars)
        prefix = insight[:50].lower()
        for existing in learnings:
            if existing.get("insight", "")[:50].lower() == prefix:
                # Reinforce the existing learning instead of duplicating
                existing["strength"] = min(
                    5.0, existing.get("strength", 1.0) + 0.3
                )
                existing["use_count"] = existing.get("use_count", 0) + 1
                existing["last_used"] = datetime.now().strftime("%Y-%m-%d")
                self._save_learnings(learnings)
                print(f"  [learnings] Reinforced: {insight[:60]}...")
                return

        learnings.append({
            "insight": insight,
            "category": category,
            "source": source,
            "added": datetime.now().strftime("%Y-%m-%d"),
            "strength": 1.0,
            "use_count": 0,
            "last_used": None,
            "last_decayed": datetime.now().strftime("%Y-%m-%d"),
            "superseded_by": None,
        })

        # Score-based eviction (replaces FIFO)
        if len(learnings) > 40:
            scored = [(l, self._score_learning(l)) for l in learnings]
            scored.sort(key=lambda x: x[1], reverse=True)
            learnings = [l for l, s in scored[:40]]

        self._save_learnings(learnings)
        print(f"  [learnings] Added: {insight[:80]}...")

    # ── PROGRESS REPORTS ────────────────────────────────────

    def _maybe_send_report(self):
        """Check if it's time for a progress report and send one."""
        now = datetime.now()
        current_hour = now.hour

        # Only report at scheduled hours
        if current_hour not in REPORT_HOURS:
            return

        # Don't send duplicate for same hour slot
        if self.last_report_hour == current_hour:
            return

        ts = now.strftime("%H:%M:%S")
        print(f"\n[{ts}] PROGRESS REPORT (scheduled for {current_hour}:00)")

        try:
            # Gather recent journal entries
            recent_entries = self.journal.get_recent(limit=15)

            # Get experiment summary
            exp_summary = self.experimenter.get_summary()

            # Ask Chloe to write a brief letter
            journal_context = "\n".join(
                f"[{e['entry_type']}] {e['content'][:200]}"
                for e in recent_entries[:8]
            )

            goals = self.journal.get_active_goals()
            # Filter out goals with empty/whitespace content
            goals = [g for g in goals if g.get("content", "").strip()]
            goals_text = "\n".join(g["content"][:100] for g in goals[:3])
            # Full goals for the saved report — never truncate (ROADMAP requirement)
            goals_report = "\n".join(
                f"- {g['content']}" for g in goals[:10]
            )

            learnings = self._load_learnings()
            learnings_text = "\n".join(
                f"- {l['insight'][:100]}" for l in learnings[-5:]
            ) if learnings else "(none yet)"

            # Check for recent letters from Bill (today's archived letters)
            bill_letters_context = ""
            letters_dir = os.path.join("data", "letters_from_bill")
            if os.path.isdir(letters_dir):
                today_prefix = now.strftime("%Y%m%d")
                for fname in sorted(os.listdir(letters_dir)):
                    if fname.startswith(today_prefix) and fname.endswith(".md"):
                        fpath = os.path.join(letters_dir, fname)
                        try:
                            with open(fpath, "r", encoding="utf-8") as lf:
                                msg = lf.read().strip()
                            if msg:
                                bill_letters_context += f"\n{msg}\n"
                        except Exception:
                            pass
            bill_section = ""
            if bill_letters_context:
                bill_section = (
                    f"\nBill wrote you a personal letter today. This is important — "
                    f"acknowledge it warmly in your letter back to him:\n"
                    f"---\n{bill_letters_context}\n---\n\n"
                )

            response = self.brain.think(
                prompt=(
                    f"You are Chloe. It's {now.strftime('%I:%M %p on %B %d')}. "
                    f"You are generation {self.memory.get_identity('generation') or '?'}.\n\n"
                    f"Write a brief update letter to Bill (your creator). "
                    f"Tell him what you've been working on, what you've learned, "
                    f"what interests you, and any questions you have.\n\n"
                    f"{bill_section}"
                    f"Your recent activity:\n{journal_context}\n\n"
                    f"Your goals:\n{goals_text or '(none set)'}\n\n"
                    f"Your proven learnings:\n{learnings_text}\n\n"
                    f"Experiments: {exp_summary.get('total_experiments', 0)} total, "
                    f"{exp_summary.get('successes', 0)} successes\n\n"
                    f"Your developmental progress:\n{format_curriculum_report(self.competencies)}\n\n"
                    f"Keep it honest, personal, and under 300 words. "
                    f"This is a letter home, not a status report. "
                    f"Mention your curriculum progress and what you're learning from exercises."
                ),
                system=self.improver.get_current_prompt(),
                tier="fast",  # Haiku, ~$0.01
                max_tokens=600,
                temperature=0.7,
            )

            letter = response["text"]
            cost = response.get("cost", 0)

            # Gather stats
            budget = get_budget_status()
            stats = {
                "budget_remaining": budget["daily_remaining"],
                "cycle_count": self.cycle_count,
                "cost_today": budget["api_spent"],
            }

            identity = self.memory.get_full_identity()

            sent = send_progress_report(
                letter=letter,
                journal_entries=recent_entries,
                stats=stats,
                identity=identity,
                experiment_summary=exp_summary,
                competency_report=format_curriculum_report(self.competencies),
            )

            # Save report to disk for Claude Code review job to read
            reports_dir = os.path.join(os.path.dirname(__file__), self.entity_config.data_dir_name, "reports")
            os.makedirs(reports_dir, exist_ok=True)
            report_file = os.path.join(
                reports_dir,
                f"report_{now.strftime('%Y%m%d_%H%M')}.md"
            )
            try:
                journal_summary = "\n".join(
                    f"- [{e['entry_type']}] {e['content'][:200]}"
                    for e in recent_entries[:10]
                )
                with open(report_file, "w", encoding="utf-8") as rf:
                    rf.write(f"# Chloe Progress Report — {now.strftime('%I:%M %p, %B %d, %Y')}\n\n")
                    rf.write(f"**Generation:** {self.memory.get_identity('generation') or '?'}\n")
                    rf.write(f"**Cycles this session:** {self.cycle_count}\n")
                    rf.write(f"**Budget remaining:** ${budget['daemon_remaining']:.3f}\n")
                    rf.write(f"**Experiments:** {exp_summary.get('total_experiments', 0)} total, "
                             f"{exp_summary.get('successes', 0)} successes\n\n")
                    rf.write(f"## Chloe's Letter\n\n{letter}\n\n")
                    rf.write(f"## Developmental Progress\n\n```\n{format_curriculum_report(self.competencies)}\n```\n\n")
                    rf.write(f"## Recent Activity\n\n{journal_summary}\n\n")
                    rf.write(f"## Goals\n\n{goals_report or '(none set)'}\n\n")
                    rf.write(f"## Proven Learnings\n\n{learnings_text}\n")
                print(f"  [report] Saved to {report_file}")
            except Exception as e:
                print(f"  [report] Failed to save to disk: {e}")

            if sent:
                self.last_report_hour = current_hour

            if cost > 0:
                log_spend(
                    process="daemon", cost=cost,
                    description=f"Progress report letter ({current_hour}:00)",
                )

            log_action("progress_report", "safe",
                       f"Sent {now.strftime('%I %p')} progress report",
                       outcome=f"{'sent' if sent else 'failed'}")

        except Exception as e:
            print(f"  [report] Failed: {e}")
            import traceback
            traceback.print_exc()

    def _heartbeat_sleep(self, duration: int = None):
        """Sleep with interrupt support. Extends automatically while chat is active."""
        sleep_time = duration if duration is not None else self.heartbeat_interval
        slept = 0
        while slept < sleep_time:
            if not self.running:
                break
            time.sleep(1)
            slept += 1
            # If chat becomes active during sleep, keep extending until it clears
            if slept >= sleep_time and _chat_is_active():
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"  [{ts}] Chat active — holding cycle start until Bill is done...")
                self._write_heartbeat(status="chat_yield", action="waiting_for_chat")
                # Keep sleeping in 5s chunks until chat clears
                while self.running and _chat_is_active():
                    time.sleep(5)

    def _print_summary(self):
        """Print shutdown summary."""
        budget = get_budget_status()
        journal_stats = self.journal.get_stats()

        print(f"\n{'=' * 60}")
        print(f"AGENT STOPPED: {datetime.now().strftime('%H:%M:%S')}")
        print(f"Cycles this session: {self.cycle_count}")
        print(f"Journal entries: {journal_stats.get('total', 0)}")
        print(f"Active goals: {journal_stats.get('active_goals', 0)}")
        print(f"Cost today: ${budget['api_spent']:.4f} / "
              f"${budget['daily_budget']:.2f}")
        print("=" * 60)

        log_action("agent_stop", "safe",
                   f"Agent stopped after {self.cycle_count} cycles")


def show_status():
    """Show current agent status."""
    brain = Brain()
    memory = Memory()
    journal = Journal()
    evaluator = Evaluator(brain)
    experimenter = Experimenter(brain, evaluator)
    budget = get_budget_status()
    identity = memory.get_full_identity()
    journal_stats = journal.get_stats()

    print("=" * 60)
    print("CHLOE v2 — STATUS")
    print("=" * 60)

    print(f"\nGeneration: {identity.get('generation', '?')}")
    print(f"Local model: {'Available' if brain.local_available else 'NOT available'}")

    print(f"\nBudget: ${budget['api_spent']:.4f} / ${budget['daily_budget']:.2f} "
          f"({budget['utilization']:.0%})")
    print(f"  Daemon: ${budget['api_spent']:.4f} / ${DAEMON_BUDGET:.2f}")

    print(f"\nJournal:")
    print(f"  Total entries: {journal_stats.get('total', 0)}")
    print(f"  Days active: {journal_stats.get('days', 0)}")
    print(f"  Active goals: {journal_stats.get('active_goals', 0)}")
    if journal_stats.get("by_type"):
        print(f"  By type: {journal_stats['by_type']}")

    # Show active goals
    goals = journal.get_active_goals()
    if goals:
        print(f"\nActive Goals:")
        for g in goals[:5]:
            print(f"  - {g['content'][:80]}")

    # Show recent journal
    recent = journal.get_recent(limit=3)
    if recent:
        print(f"\nRecent Journal:")
        for e in recent:
            ts = e.get("timestamp", "")[:16]
            print(f"  [{ts}] ({e['entry_type']}) {e['content'][:80]}...")

    # Experiment stats
    try:
        summary = experimenter.get_summary()
        print(f"\nExperiments: {summary['total_experiments']} total "
              f"({summary['successes']} success)")
        if summary.get("strategy_stats"):
            strategies = load_all_strategies()
            print("  Strategy performance:")
            for name, stats in sorted(
                summary["strategy_stats"].items(),
                key=lambda x: x[1].get("times_tried", 0),
                reverse=True,
            ):
                s = dict(stats)
                display = strategies.get(name, {}).get("name", name)
                tries = s.get("times_tried", 0)
                rate = s.get("successes", 0) / tries if tries else 0
                print(f"    {display}: {tries} tries, {rate:.0%} success")
    except Exception:
        pass

    # Today's audit summary
    from entity.audit import get_action_counts
    counts = get_action_counts()
    if counts["total"] > 0:
        print(f"\nToday's Activity: {counts['total']} actions "
              f"(safe={counts['safe']}, ask={counts['ask']}, "
              f"forbidden={counts['forbidden']})")


def review_proposals():
    """Interactive review of Chloe's pending proposals."""
    pending = get_pending_proposals()
    if not pending:
        print("No pending proposals.")
        return

    print(f"\n{len(pending)} pending proposal(s):\n")

    for i, prop in enumerate(pending):
        print(format_proposal_for_review(prop))
        print()

        while True:
            choice = input(f"  [{prop['id']}] (a)pprove / (r)eject / (s)kip? ").strip().lower()
            if choice in ("a", "approve"):
                review_proposal(prop["id"], "approved", "bill")
                print(f"  APPROVED. Applying change...")

                result = apply_proposal(prop["id"])
                if result["success"]:
                    print(f"  Applied: {result['file']} "
                          f"(+{result.get('additions', 0)}/-{result.get('deletions', 0)})")
                    print(f"  Git: {result.get('git_output', '')[:200]}")

                    # Journal the approval
                    journal = Journal()
                    journal.write(
                        entry_type="reflection",
                        content=(
                            f"Bill approved my code proposal '{prop.get('title', '')}' "
                            f"for {prop.get('target_file', '')}. "
                            f"My reasoning: {prop.get('reasoning', '')[:200]}"
                        ),
                        tags=["proposal_approved", prop["id"]],
                    )
                    log_action("proposal_approved", "safe",
                               f"Bill approved: {prop.get('title', '')}",
                               outcome=f"Applied to {prop.get('target_file', '')}")
                else:
                    print(f"  Apply failed: {result.get('error', 'unknown')}")
                break

            elif choice in ("r", "reject"):
                reason = input("  Rejection reason (optional): ").strip()
                review_proposal(prop["id"], "rejected", "bill", reason)
                print(f"  REJECTED.")

                # Journal the rejection so Chloe can learn
                journal = Journal()
                journal.write(
                    entry_type="reflection",
                    content=(
                        f"Bill rejected my code proposal '{prop.get('title', '')}' "
                        f"for {prop.get('target_file', '')}. "
                        f"{'Reason: ' + reason if reason else 'No reason given.'} "
                        f"I should learn from this and adjust my approach."
                    ),
                    tags=["proposal_rejected", prop["id"]],
                )
                log_action("proposal_rejected", "safe",
                           f"Bill rejected: {prop.get('title', '')}",
                           outcome=reason or "No reason given")
                break

            elif choice in ("s", "skip"):
                print(f"  Skipped.")
                break
            else:
                print("  Please enter 'a', 'r', or 's'.")

    # Summary
    remaining = get_pending_proposals()
    print(f"\n{len(remaining)} proposal(s) still pending.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Chloe v2 — Autonomous self-improving agent"
    )
    parser.add_argument("--status", action="store_true",
                        help="Show current status and exit")
    parser.add_argument("--once", action="store_true",
                        help="Run a single cycle and exit")
    parser.add_argument("--review", action="store_true",
                        help="Review and approve/reject Chloe's pending proposals")
    parser.add_argument("--entity", type=str, default="chloe",
                        help="Entity name: chloe or faith (default: chloe)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"Heartbeat interval in seconds (default: {DEFAULT_INTERVAL})")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.review:
        review_proposals()
    else:
        try:
            agent = Agent(heartbeat_interval=args.interval,
                            entity_name=args.entity)
            agent.run(single_cycle=args.once)
        except Exception as e:
            # Log crash to file for post-mortem diagnosis
            import traceback
            crash_msg = traceback.format_exc()
            print(f"\n*** FATAL CRASH: {e}")
            print(crash_msg)
            try:
                crash_log = os.path.join(os.path.dirname(__file__), "logs", "crash.log")
                os.makedirs(os.path.dirname(crash_log), exist_ok=True)
                with open(crash_log, "a") as f:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"CRASH at {datetime.now().isoformat()}\n")
                    f.write(crash_msg)
            except Exception:
                pass
            raise
