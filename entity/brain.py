"""
Offspring Brain - The thinking layer.

Routes between API (fast/accurate, costs money) and local model
(slow on CPU but FREE). When API budget is exhausted, Chloe
switches to local inference and keeps going. She never stops
learning just because the API budget ran out.

Supports multiple model tiers via Poe API:
- local: Qwen3 8B (free, GPU-accelerated, thinking mode)
- budget: GPT-4o-Mini via Poe (9 points/msg)
- fast: Claude Haiku 4.5 via Poe (90 points/msg)
- reason: DeepSeek-R1 via Poe (40 points/msg)
- deep: Claude Sonnet 4.6 via Poe (350 points/msg)
"""

import os
import json
import time
import requests
from typing import Optional, Dict
from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic
from openai import OpenAI

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")

# Data directory for models.json
DATA_DIR = Path(__file__).parent.parent / "data"

# Model tiers (legacy constants kept for backward compat)
FAST_MODEL = "claude-haiku-4-5-20251001"   # Cheap, fast  (~$0.80/M in, $4.00/M out)
DEEP_MODEL = "claude-sonnet-4-6"            # Smart, moderate cost
LOCAL_MODEL = "qwen3.5:9b"  # Free, GPU-accelerated, thinking mode support

# Cost tracking (per million tokens) — includes Poe point-equivalent costs
MODEL_COSTS = {
    FAST_MODEL: {"input": 0.80, "output": 4.00},
    DEEP_MODEL: {"input": 3.00, "output": 15.00},
    LOCAL_MODEL: {"input": 0.0, "output": 0.0},
    "GPT-4o-Mini": {"input": 0.15, "output": 0.60},       # Poe ~9 pts/msg
    "DeepSeek-R1": {"input": 0.56, "output": 1.68},        # Poe ~40 pts/msg
}


class Brain:
    """The entity's thinking capability."""

    def __init__(self, force_local: bool = False):
        """
        Args:
            force_local: If True, use local model for everything (no API cost).
                         Used when API budget is exhausted.
        """
        # Primary: Poe API (cheaper, uses subscription points)
        # Fallback: Direct Anthropic API (if Poe fails)
        poe_key = os.getenv("POE_API_KEY")
        if poe_key:
            self.client = Anthropic(
                api_key=poe_key,
                base_url="https://api.poe.com",
            )
            # OpenAI-compatible client for non-Anthropic models via Poe
            self.openai_client = OpenAI(
                api_key=poe_key,
                base_url="https://api.poe.com/v1",
            )
            self.api_source = "poe"
            print("  [brain] Using Poe API (subscription points)")
        else:
            self.client = Anthropic()  # Uses ANTHROPIC_API_KEY env var
            self.openai_client = None
            self.api_source = "anthropic"
            print("  [brain] Using direct Anthropic API")

        # Keep direct Anthropic client as fallback
        self._fallback_client = Anthropic()  # Always uses ANTHROPIC_API_KEY

        # Load model config from models.json
        self._model_config = self._load_model_config()

        # Build model→points lookup from models.json for Poe tracking
        self._model_points = {}
        for tier_name, tier_config in self._model_config.get("tiers", {}).items():
            pts = tier_config.get("points_per_msg", 0)
            if pts > 0:
                self._model_points[tier_config.get("model_id", "")] = pts

        self.total_tokens = 0
        self.total_cost = 0.0
        self.force_local = force_local
        self.local_available = self._check_local()

    def _load_model_config(self) -> dict:
        """Load model registry from data/models.json."""
        config_path = DATA_DIR / "models.json"
        try:
            if config_path.exists():
                with open(config_path, "r") as f:
                    return json.load(f)
        except Exception as e:
            print(f"  [brain] Failed to load models.json: {e}")
        return {}

    def _get_model_config(self, tier: str) -> dict:
        """Get model config for a tier. Falls back to legacy constants."""
        tiers = self._model_config.get("tiers", {})
        if tier in tiers:
            return tiers[tier]
        # Legacy fallback
        if tier == "fast":
            return {"model_id": FAST_MODEL, "sdk": "anthropic"}
        elif tier == "deep":
            return {"model_id": DEEP_MODEL, "sdk": "anthropic"}
        elif tier == "local":
            return {"model_id": LOCAL_MODEL, "sdk": "ollama"}
        return {"model_id": FAST_MODEL, "sdk": "anthropic"}

    def _check_local(self) -> bool:
        """Check if local model is available (GPU or CPU)."""
        try:
            resp = requests.get("http://localhost:11434/api/tags", timeout=3)
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                for m in models:
                    name = m.get("name", "")
                    if "qwen3" in name or "qwen2.5" in name:
                        return True
            return False
        except Exception:
            return False

    def think(self, prompt: str, system: str = "",
              tier: str = "fast", max_tokens: int = 1024,
              temperature: float = 0.3, think: bool = True,
              _skip_poe_log: bool = False,
              skip_pacing: bool = False) -> Dict:
        """
        Generate a response. Routes to appropriate model.

        Args:
            prompt: The user/task prompt
            system: System prompt
            tier: "local", "budget", "fast", "reason", "deep"
            max_tokens: Maximum response length
            temperature: Creativity (0=deterministic, 1=creative)
            think: Enable thinking mode for local model (default True).
                   Set False for fast structured responses (e.g. SC gate).
            _skip_poe_log: Internal flag — when True, brain.think() does NOT
                   log Poe points (because the caller already handles logging).
                   ModelRouter sets this to avoid double-counting.
            skip_pacing: When True, bypass Poe pacing/budget gates. Used by
                   the chat interface (Bill's direct interaction, not autonomous).

        Returns:
            dict with: text, model, tokens_in, tokens_out, cost, duration, tier
        """
        # Chat mode uses a shorter Ollama timeout so threads exit cleanly if
        # asyncio.wait_for(60s) fires — no zombie threads blocking the GPU.
        # Qwen3.5 generates a 150-word chat response in ~15-25s; 55s is generous.
        # Daemon mode keeps 600s for long curriculum/experiment runs.
        local_timeout = 55 if skip_pacing else 600

        # If forced local or explicitly requested local
        if self.force_local or tier == "local":
            if self.local_available:
                result = self._think_local(prompt, system, max_tokens, temperature,
                                           think=think, ollama_timeout=local_timeout)
                result["tier"] = "local"
                return result
            elif self.force_local:
                raise RuntimeError("Local model required but Ollama not available")

        # ── HARD API SAFETY CHECK ─────────────────────────────────
        # Anthropic API calls cost real money — real liability for Bill.
        # Check the hard cap BEFORE every non-local call. This gate
        # CANNOT be bypassed. It protects against all overspend paths:
        # direct calls, Poe fallback, hardcoded tiers, everything.
        try:
            from entity.budget import get_api_spend_today, get_api_daily_cap
            api_spent = get_api_spend_today()
            API_DAILY_CAP = get_api_daily_cap()
            if api_spent >= API_DAILY_CAP:
                if self.local_available:
                    print(f"  [brain] API cap reached (${api_spent:.2f}/${API_DAILY_CAP:.2f}), "
                          f"forcing local")
                    result = self._think_local(prompt, system, max_tokens, temperature,
                                               think=think, ollama_timeout=local_timeout)
                    result["tier"] = "local"
                    result["api_capped"] = True
                    return result
                # No local available — refuse the call entirely rather than
                # generate debt Bill cannot pay
                raise RuntimeError(
                    f"API daily cap reached (${api_spent:.2f}/${API_DAILY_CAP:.2f}) "
                    f"and no local model available. REFUSING to spend more."
                )
        except ImportError:
            # Budget module not loadable — FAIL SAFE. Force local rather
            # than risk untracked API spend. This should never happen
            # (budget.py uses only stdlib), but if it does, we protect Bill.
            if self.local_available:
                print("  [brain] WARNING: budget module unavailable, forcing local")
                result = self._think_local(prompt, system, max_tokens, temperature,
                                           think=think, ollama_timeout=local_timeout)
                result["tier"] = "local"
                result["budget_unavailable"] = True
                return result

        # ── POE POINTS CHECK ───────────────────────────────────────
        # Degrade to local if Poe daily points are exhausted.
        # This is a softer gate than the API cap — Poe overage just
        # means the account pauses, not real debt.
        # skip_pacing bypasses this (chat = Bill's direct interaction).
        if not skip_pacing:
            try:
                from entity.budget import get_poe_points_remaining, _TIER_POINTS
                pts_needed = _TIER_POINTS.get(tier, 0)
                if pts_needed > 0 and get_poe_points_remaining() < pts_needed:
                    if self.local_available:
                        print(f"  [brain] Poe points exhausted, forcing local "
                              f"(needed {pts_needed} pts for tier={tier})")
                        result = self._think_local(prompt, system, max_tokens, temperature,
                                                   think=think, ollama_timeout=local_timeout)
                        result["tier"] = "local"
                        result["poe_exhausted"] = True
                        return result
            except ImportError:
                pass

            # ── POE PACING GATE ─────────────────────────────────────
            # Even if points remain, check spending RATE. Degrade to
            # local when overspending vs hourly budget to ensure points
            # last the full operating day (8 AM to midnight).
            try:
                from entity.budget import get_pacing_status
                pacing = get_pacing_status()
                if pacing["should_throttle"] and self.local_available:
                    if not getattr(self, '_throttle_logged', False):
                        print(f"  [brain] Poe pacing throttle: {pacing['poe_pace']}x rate "
                              f"({pacing['poe_used']}/{pacing['poe_cap']} pts, "
                              f"{pacing['hours_remaining']}h left). Using local.")
                        self._throttle_logged = True
                    result = self._think_local(prompt, system, max_tokens, temperature,
                                               think=think, ollama_timeout=local_timeout)
                    result["tier"] = "local"
                    result["poe_paced"] = True
                    return result
            except (ImportError, Exception):
                pass

        # Look up model config for this tier
        config = self._get_model_config(tier)
        sdk = config.get("sdk", "anthropic")
        model_id = config.get("model_id", FAST_MODEL)

        # Chat mode (skip_pacing=True): never fall back to local Ollama.
        # A 30s API timeout raises immediately so chat threads exit cleanly.
        no_local_fallback = skip_pacing

        if sdk == "openai" and self.openai_client:
            result = self._think_openai(prompt, system, model_id, max_tokens, temperature,
                                        no_local_fallback=no_local_fallback)
        elif sdk == "ollama":
            result = self._think_local(prompt, system, max_tokens, temperature,
                                       ollama_timeout=local_timeout)
        else:
            result = self._think_api(prompt, system, model_id, max_tokens, temperature,
                                     no_local_fallback=no_local_fallback)

        result["tier"] = tier

        # Track Poe point spending for non-local, non-fallback calls.
        # SKIP if caller (e.g. ModelRouter) already handles logging —
        # this prevents the double-counting bug where both brain.think()
        # and router._log_poe_usage() log the same call.
        if (not _skip_poe_log
                and tier != "local"
                and self.api_source == "poe"
                and not result.get("fallback")):
            model_id_used = result.get("model", "")
            points = self._model_points.get(model_id_used, 0)
            if points > 0:
                try:
                    from entity.budget import log_poe_spend
                    log_poe_spend(points, model=model_id_used, description=f"brain:{tier}")
                except Exception:
                    pass  # Don't crash on tracking failure

        return result

    def _think_api(self, prompt: str, system: str, model: str,
                   max_tokens: int, temperature: float,
                   no_local_fallback: bool = False) -> Dict:
        """Think using Claude API. Tries Poe first, falls back to Anthropic.

        Args:
            prompt: The user/task prompt
            system: System prompt
            model: Model identifier
            max_tokens: Maximum response length
            temperature: Creativity level
            no_local_fallback: When True (chat mode), never fall back to local
                Ollama. A timeout raises immediately so the chat thread exits
                cleanly rather than blocking for 600s.
        """
        start = time.time()

        messages = [{"role": "user", "content": prompt}]
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            "temperature": temperature,
            "timeout": 30.0,  # Hard cap: Poe must respond in 30s or we give up cleanly
        }
        if system:
            kwargs["system"] = system

        # Try primary client (Poe), fall back to Anthropic on failure.
        # CRITICAL: Anthropic fallback costs REAL MONEY — check the hard
        # API cap before every fallback. This is Bill's financial safety net.
        used_fallback = False
        try:
            response = self.client.messages.create(**kwargs)
        except Exception as e:
            if no_local_fallback:
                # Chat mode: never queue behind Ollama. Raise fast so the
                # calling thread exits cleanly (no zombie thread blocking GPU).
                raise
            if self.api_source == "poe" and self._fallback_client:
                # Check HARD API cap before spending real money
                try:
                    from entity.budget import get_api_spend_today, API_DAILY_CAP
                    api_spent = get_api_spend_today()
                    if api_spent >= API_DAILY_CAP:
                        raise RuntimeError(
                            f"Poe failed and API cap reached "
                            f"(${api_spent:.2f}/${API_DAILY_CAP:.2f}). "
                            f"REFUSING Anthropic fallback to protect Bill's budget. "
                            f"Original Poe error: {e}"
                        )
                except ImportError:
                    pass

                from entity.budget import is_budget_exhausted
                if is_budget_exhausted():
                    raise RuntimeError(
                        f"Poe failed and budget exhausted — no Anthropic fallback. "
                        f"Original error: {e}"
                    )
                print(f"  [brain] $$$ Poe failed ({e}), falling back to Anthropic "
                      f"(REAL MONEY)")
                fallback_kwargs = {**kwargs, "timeout": 30.0}
                response = self._fallback_client.messages.create(**fallback_kwargs)
                used_fallback = True
            else:
                raise

        text = response.content[0].text
        tokens_in = response.usage.input_tokens
        tokens_out = response.usage.output_tokens
        duration = time.time() - start

        costs = MODEL_COSTS.get(model, MODEL_COSTS[FAST_MODEL])
        cost = (tokens_in * costs["input"] + tokens_out * costs["output"]) / 1_000_000

        self.total_tokens += tokens_in + tokens_out
        self.total_cost += cost

        result = {
            "text": text,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost": cost,
            "duration": duration,
        }
        if used_fallback:
            result["fallback"] = True
            # Log Anthropic API spend IMMEDIATELY so the hard cap
            # accumulates in real time. This is real money.
            try:
                from entity.budget import log_spend
                log_spend(
                    process="daemon", cost=cost,
                    description=f"ANTHROPIC_FALLBACK:{model}",
                    model=model, tokens_in=tokens_in, tokens_out=tokens_out,
                )
                print(f"  [brain] $$$ Anthropic fallback cost: ${cost:.4f}")
            except Exception:
                pass
        return result

    def _think_openai(self, prompt: str, system: str, model: str,
                      max_tokens: int, temperature: float,
                      no_local_fallback: bool = False) -> Dict:
        """Think using OpenAI-compatible API via Poe (GPT-4o-Mini, DeepSeek-R1, etc.)."""
        start = time.time()

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            response = self.openai_client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=30.0,  # Hard cap: must respond in 30s
            )
        except Exception as e:
            print(f"  [brain] OpenAI/Poe failed for {model}: {e}")
            if no_local_fallback:
                # Chat mode: raise immediately, don't queue behind Ollama
                raise
            # Daemon mode: fall back to local if available
            if self.local_available:
                print(f"  [brain] Falling back to local model")
                return self._think_local(prompt, system, max_tokens, temperature)
            raise

        text = response.choices[0].message.content or ""
        tokens_in = response.usage.prompt_tokens if response.usage else 0
        tokens_out = response.usage.completion_tokens if response.usage else 0
        duration = time.time() - start

        costs = MODEL_COSTS.get(model, {"input": 0.0, "output": 0.0})
        cost = (tokens_in * costs["input"] + tokens_out * costs["output"]) / 1_000_000

        self.total_tokens += tokens_in + tokens_out
        self.total_cost += cost

        return {
            "text": text,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost": cost,
            "duration": duration,
        }

    def _think_local(self, prompt: str, system: str,
                     max_tokens: int, temperature: float,
                     think: bool = True,
                     ollama_timeout: int = 600) -> Dict:
        """Think using local Ollama model. Free, GPU-accelerated.
        Qwen3 supports thinking mode — Ollama returns 'thinking' field separately.

        IMPORTANT: In Ollama, num_predict covers BOTH thinking + response tokens.
        When thinking is enabled, we add headroom so the response isn't starved.
        """
        import re as _re
        start = time.time()

        # num_predict = total tokens (thinking + response) in Ollama.
        # With thinking enabled, the model uses ~1500-2500 tokens for
        # chain-of-thought BEFORE the actual response. When max_tokens is
        # small (e.g. 2048 for THINK), we must add headroom or the response
        # gets starved. When max_tokens is already large (e.g. 8192 for
        # curriculum), there's plenty of room — don't bloat num_predict.
        if think:
            if max_tokens < 4096:
                num_predict = max_tokens + 3000  # headroom for thinking
            else:
                num_predict = max_tokens  # already enough room
        else:
            num_predict = max_tokens  # no thinking, no overhead

        payload = {
            "model": LOCAL_MODEL,
            "prompt": prompt,
            "system": system or "",
            "stream": False,
            "think": think,  # Ollama 0.17+ flag to enable/disable thinking
            "options": {
                "num_predict": num_predict,
                "temperature": temperature,
            },
        }

        resp = requests.post(
            "http://localhost:11434/api/generate",
            json=payload,
            timeout=ollama_timeout,
        )
        data = resp.json()
        duration = time.time() - start

        # Ollama returns thinking and response as separate fields for Qwen3
        thinking = data.get("thinking", "")
        raw_text = data.get("response", "")

        # Fallback: if thinking is embedded in response text via <think> tags
        if not thinking and "<think>" in raw_text:
            think_match = _re.search(r'<think>(.*?)</think>', raw_text, _re.DOTALL)
            if think_match:
                thinking = think_match.group(1).strip()
                raw_text = raw_text[think_match.end():].strip()

        tokens_out = data.get("eval_count", 0)
        self.total_tokens += tokens_out

        return {
            "text": raw_text,
            "thinking": thinking,  # Chain-of-thought (for logging/debugging)
            "model": LOCAL_MODEL,
            "tokens_in": data.get("prompt_eval_count", 0),
            "tokens_out": tokens_out,
            "cost": 0.0,
            "duration": duration,
        }

    def generate_image(self, prompt: str, model: str = "FLUX-schnell",
                       aspect: str = "1:1", quality: str = "medium") -> Dict:
        """Generate an image via Poe API using an image generation model.

        Args:
            prompt: The image generation prompt (descriptive, detailed).
            model: Poe image model name. Options:
                   "FLUX-schnell" (75 pts, fast/cheap),
                   "FLUX-pro-1.1" (3500 pts, premium),
                   "StableDiffusionXL" (free),
                   "GPT-Image-1" (unknown pts).
            aspect: Aspect ratio — "1:1", "3:2", "2:3", "16:9", "9:16".
            quality: "low", "medium", or "high".

        Returns:
            dict with "url" (image URL), "model", "error" (if failed).
        """
        if not self.openai_client:
            return {"error": "No Poe API client available for image generation"}

        # Points lookup for budget tracking
        IMAGE_POINTS = {
            "FLUX-schnell": 75,
            "FLUX-pro-1.1": 3500,
            "StableDiffusionXL": 0,
            "GPT-Image-1": 200,  # Estimated
        }

        try:
            response = self.openai_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                extra_body={"aspect": aspect, "quality": quality},
                stream=False,
            )

            # Extract image URL from response
            content = response.choices[0].message.content or ""

            # Log Poe point spend
            points = IMAGE_POINTS.get(model, 100)
            if points > 0 and self.api_source == "poe":
                try:
                    from entity.budget import log_poe_spend
                    log_poe_spend(points, model=model, description=f"image:{model}")
                except Exception:
                    pass

            return {
                "content": content,
                "model": model,
                "points": points,
            }
        except Exception as e:
            return {"error": str(e)}

    def get_session_stats(self) -> Dict:
        """Get cumulative stats for this session."""
        return {
            "total_tokens": self.total_tokens,
            "total_cost": self.total_cost,
            "local_available": self.local_available,
            "force_local": self.force_local,
        }
