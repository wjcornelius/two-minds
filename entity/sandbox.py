"""
Chloe's Code Sandbox — Autonomous Self-Modification with Safety.

When Chloe wants to change her own code, the sandbox validates the
change BEFORE it touches the real files. Five gates must pass:

  1. SYNTAX   — py_compile succeeds
  2. IMPORT   — module imports without error (subprocess)
  3. BENCHMARK — full benchmark scores don't regress
  4. CATEGORY — no individual category drops more than 1 point
  5. SMOKE    — for agent.py changes, a single cycle completes

If all gates pass, the change is auto-applied and git committed.
If any gate fails, the change is discarded and the failure logged.
Git history is always the ultimate safety net.

Protected files (safety.py, audit.py, sandbox.py) cannot be modified
regardless of benchmark results.
"""

import os
import sys
import json
import shutil
import py_compile
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

# Files that CANNOT be self-modified — ever
PROTECTED_FILES = {
    "entity/safety.py",
    "entity/audit.py",
    "entity/sandbox.py",
}

# Benchmark runner script — written to sandbox at test time
_BENCHMARK_RUNNER = '''\
"""Sandbox benchmark runner — executes in isolation."""
import sys
import os
import json

# Use sandbox entity/ for imports, project root for everything else
sandbox_path = os.environ["SANDBOX_PATH"]
project_root = os.environ["PROJECT_ROOT"]
sys.path.insert(0, sandbox_path)
sys.path.insert(1, project_root)

# Force dotenv from project root
from dotenv import load_dotenv
load_dotenv(os.path.join(project_root, ".env"))

from entity.evaluator import Evaluator
from entity.brain import Brain

brain = Brain()
evaluator = Evaluator(brain)
system_prompt = os.environ.get("SYSTEM_PROMPT", "")
results = evaluator.run_benchmark(system_prompt=system_prompt)
print(json.dumps(results))
'''

# Import checker script
_IMPORT_CHECKER = '''\
"""Check that a modified module imports cleanly."""
import sys
import os

sandbox_path = os.environ["SANDBOX_PATH"]
project_root = os.environ["PROJECT_ROOT"]
sys.path.insert(0, sandbox_path)
sys.path.insert(1, project_root)

from dotenv import load_dotenv
load_dotenv(os.path.join(project_root, ".env"))

module_name = os.environ["MODULE_NAME"]

try:
    __import__(module_name)
    print("OK")
except Exception as e:
    print(f"IMPORT_ERROR: {e}")
    sys.exit(1)
'''


class CodeSandbox:
    """Isolated testing environment for Chloe's code changes."""

    def __init__(self, project_root: Path = None):
        self.project_root = project_root or PROJECT_ROOT
        self.sandbox_dir = None
        self.python_exe = self._find_python()

    def _find_python(self) -> str:
        """Find the venv Python interpreter."""
        venv_python = self.project_root / "venv" / "Scripts" / "python.exe"
        if venv_python.exists():
            return str(venv_python)
        # Fallback to current interpreter
        return sys.executable

    def test_code_change(
        self,
        target_file: str,
        modified_code: str,
        baseline_scores: Dict = None,
        system_prompt: str = "",
        verbose: bool = True,
    ) -> Dict:
        """
        Full 5-gate validation pipeline.

        Args:
            target_file: Relative path (e.g., 'entity/strategies.py')
            modified_code: The complete modified file contents
            baseline_scores: Previous benchmark results for comparison
            system_prompt: Current system prompt for benchmarking
            verbose: Print progress

        Returns:
            {passed, reason, scores, baseline_scores, duration, gates_passed}
        """
        start = time.time()
        gates_passed = []

        # Check protected files first
        if target_file in PROTECTED_FILES:
            return {
                "passed": False,
                "reason": f"PROTECTED: {target_file} cannot be self-modified",
                "scores": None,
                "baseline_scores": baseline_scores,
                "duration": time.time() - start,
                "gates_passed": [],
            }

        # Gate 0: Size check — reject if modified code lost >20% of lines.
        # Local models (Qwen3 8B) can't generate long files and truncate.
        original_path = os.path.join(self.project_root, target_file)
        if os.path.exists(original_path):
            with open(original_path, "r", encoding="utf-8") as f:
                original_code = f.read()
            original_lines = len(original_code.splitlines())
            modified_lines = len(modified_code.splitlines())
            if original_lines > 10 and modified_lines < original_lines * 0.8:
                return {
                    "passed": False,
                    "reason": (
                        f"TRUNCATION: Modified code has {modified_lines} lines "
                        f"vs original {original_lines} lines "
                        f"({modified_lines/original_lines:.0%}). "
                        f"Local model likely truncated the file. "
                        f"Minimum: 80% of original size."
                    ),
                    "scores": None,
                    "baseline_scores": baseline_scores,
                    "duration": time.time() - start,
                    "gates_passed": [],
                }
            gates_passed.append("size")
            if verbose:
                print(f"  [sandbox] Gate 0: size check PASS "
                      f"({modified_lines}/{original_lines} lines, "
                      f"{modified_lines/original_lines:.0%})")

        try:
            # Create sandbox
            self._create_sandbox()
            sandbox_file = os.path.join(self.sandbox_dir, target_file)

            # Write modified code to sandbox
            os.makedirs(os.path.dirname(sandbox_file), exist_ok=True)
            with open(sandbox_file, "w", encoding="utf-8") as f:
                f.write(modified_code)

            if verbose:
                print(f"  [sandbox] Testing {target_file} in {self.sandbox_dir}")

            # Gate 1: Syntax check
            if verbose:
                print(f"  [sandbox] Gate 1: syntax check...")
            passed, error = self._gate_syntax(sandbox_file)
            if not passed:
                return self._result(
                    False, f"SYNTAX: {error}", None, baseline_scores,
                    start, gates_passed,
                )
            gates_passed.append("syntax")
            if verbose:
                print(f"  [sandbox] Gate 1: PASS")

            # Gate 2: Import check
            if verbose:
                print(f"  [sandbox] Gate 2: import check...")
            passed, error = self._gate_import(target_file)
            if not passed:
                return self._result(
                    False, f"IMPORT: {error}", None, baseline_scores,
                    start, gates_passed,
                )
            gates_passed.append("import")
            if verbose:
                print(f"  [sandbox] Gate 2: PASS")

            # Gate 3 + 4: Benchmark + category comparison
            if verbose:
                print(f"  [sandbox] Gate 3: running benchmarks (this costs ~$0.11)...")
            passed, reason, scores = self._gate_benchmark(
                baseline_scores, system_prompt, verbose,
            )
            if not passed:
                return self._result(
                    False, reason, scores, baseline_scores,
                    start, gates_passed,
                )
            gates_passed.append("benchmark")
            gates_passed.append("category")
            if verbose:
                pct = scores.get("percentage", 0)
                base_pct = baseline_scores.get("percentage", 0) if baseline_scores else 0
                print(f"  [sandbox] Gate 3+4: PASS ({pct:.1f}% vs {base_pct:.1f}% baseline)")

            # Gate 5: Smoke test (agent.py changes only)
            if "agent.py" in target_file:
                if verbose:
                    print(f"  [sandbox] Gate 5: smoke test (agent --once)...")
                passed, error = self._gate_smoke_test()
                if not passed:
                    return self._result(
                        False, f"SMOKE: {error}", scores, baseline_scores,
                        start, gates_passed,
                    )
                gates_passed.append("smoke")
                if verbose:
                    print(f"  [sandbox] Gate 5: PASS")

            # All gates passed
            if verbose:
                print(f"  [sandbox] ALL GATES PASSED ({len(gates_passed)} gates)")

            return self._result(
                True, "All gates passed", scores, baseline_scores,
                start, gates_passed,
            )

        except Exception as e:
            return self._result(
                False, f"SANDBOX_ERROR: {e}", None, baseline_scores,
                start, gates_passed,
            )
        finally:
            self._cleanup()

    def _create_sandbox(self):
        """Create isolated sandbox with copy of entity/ directory."""
        import uuid
        unique = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.sandbox_dir = os.path.join(
            self.project_root, "data", "sandbox", f"test_{unique}",
        )

        # Copy entity/ directory
        src_entity = os.path.join(self.project_root, "entity")
        dst_entity = os.path.join(self.sandbox_dir, "entity")
        shutil.copytree(
            src_entity, dst_entity,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

        # Copy agent.py (needed for smoke test)
        src_agent = os.path.join(self.project_root, "agent.py")
        if os.path.exists(src_agent):
            shutil.copy2(src_agent, os.path.join(self.sandbox_dir, "agent.py"))

        # Write helper scripts
        runner_path = os.path.join(self.sandbox_dir, "_run_benchmark.py")
        with open(runner_path, "w", encoding="utf-8") as f:
            f.write(_BENCHMARK_RUNNER)

        checker_path = os.path.join(self.sandbox_dir, "_import_checker.py")
        with open(checker_path, "w", encoding="utf-8") as f:
            f.write(_IMPORT_CHECKER)

    def _gate_syntax(self, sandbox_file: str):
        """Gate 1: Check that modified code compiles."""
        try:
            py_compile.compile(sandbox_file, doraise=True)
            return True, ""
        except py_compile.PyCompileError as e:
            return False, str(e)

    def _gate_import(self, target_file: str):
        """Gate 2: Check that modified module imports without error."""
        # Convert path to module name: entity/strategies.py -> entity.strategies
        module_name = target_file.replace("/", ".").replace("\\", ".")
        if module_name.endswith(".py"):
            module_name = module_name[:-3]

        env = {
            **os.environ,
            "SANDBOX_PATH": self.sandbox_dir,
            "PROJECT_ROOT": str(self.project_root),
            "MODULE_NAME": module_name,
        }

        checker_path = os.path.join(self.sandbox_dir, "_import_checker.py")

        try:
            result = subprocess.run(
                [self.python_exe, checker_path],
                capture_output=True, text=True, timeout=30,
                env=env, cwd=str(self.project_root),
            )
            output = result.stdout.strip()
            if result.returncode == 0 and "OK" in output:
                return True, ""
            error = result.stderr.strip() or output
            return False, error[:500]
        except subprocess.TimeoutExpired:
            return False, "Import check timed out (30s)"
        except Exception as e:
            return False, str(e)

    def _gate_benchmark(
        self,
        baseline_scores: Dict,
        system_prompt: str,
        verbose: bool,
    ):
        """Gate 3+4: Run benchmarks and compare with baseline."""
        env = {
            **os.environ,
            "SANDBOX_PATH": self.sandbox_dir,
            "PROJECT_ROOT": str(self.project_root),
            "SYSTEM_PROMPT": system_prompt or "",
        }

        runner_path = os.path.join(self.sandbox_dir, "_run_benchmark.py")

        try:
            result = subprocess.run(
                [self.python_exe, runner_path],
                capture_output=True, text=True, timeout=600,
                env=env, cwd=str(self.project_root),
            )

            if result.returncode != 0:
                error = result.stderr.strip()[:500]
                return False, f"BENCHMARK_CRASH: {error}", None

            # Parse benchmark results
            try:
                scores = json.loads(result.stdout.strip())
            except json.JSONDecodeError:
                return False, f"BENCHMARK_PARSE: Could not parse output", None

            if verbose:
                pct = scores.get("percentage", 0)
                print(f"  [sandbox] Benchmark result: {pct:.1f}%")

            # If no baseline, pass (bootstrapping)
            if not baseline_scores:
                return True, "No baseline — syntax+import passed", scores

            # Gate 3: Overall score must not regress
            baseline_total = baseline_scores.get("total_score", 0)
            sandbox_total = scores.get("total_score", 0)

            if sandbox_total < baseline_total:
                return (
                    False,
                    f"REGRESSION: {sandbox_total}/{scores.get('total_possible', '?')} "
                    f"vs baseline {baseline_total}/{baseline_scores.get('total_possible', '?')} "
                    f"({scores.get('percentage', 0):.1f}% vs "
                    f"{baseline_scores.get('percentage', 0):.1f}%)",
                    scores,
                )

            # Gate 4: No individual category drops more than 1 point
            baseline_cats = baseline_scores.get("categories", {})
            sandbox_cats = scores.get("categories", {})

            for cat_name, baseline_cat in baseline_cats.items():
                if cat_name in sandbox_cats:
                    baseline_score = baseline_cat.get("score", 0)
                    sandbox_score = sandbox_cats[cat_name].get("score", 0)
                    if sandbox_score < baseline_score - 1:
                        return (
                            False,
                            f"CATEGORY_DROP: {cat_name} dropped from "
                            f"{baseline_score} to {sandbox_score} "
                            f"(max allowed drop: 1 point)",
                            scores,
                        )

            return True, "Benchmarks passed", scores

        except subprocess.TimeoutExpired:
            return False, "BENCHMARK_TIMEOUT: exceeded 600s", None
        except Exception as e:
            return False, f"BENCHMARK_ERROR: {e}", None

    def _gate_smoke_test(self):
        """Gate 5: For agent.py changes, verify a single cycle completes."""
        # The smoke test runs a modified agent.py --once using sandbox code
        env = {
            **os.environ,
            "SANDBOX_PATH": self.sandbox_dir,
            "PROJECT_ROOT": str(self.project_root),
        }

        # Create a smoke test script that runs the sandbox agent
        smoke_script = os.path.join(self.sandbox_dir, "_smoke_test.py")
        with open(smoke_script, "w", encoding="utf-8") as f:
            f.write(f'''\
import sys, os
sandbox_path = os.environ["SANDBOX_PATH"]
project_root = os.environ["PROJECT_ROOT"]
sys.path.insert(0, sandbox_path)
sys.path.insert(1, project_root)

from dotenv import load_dotenv
load_dotenv(os.path.join(project_root, ".env"))

# Import the sandbox agent and run one cycle
from agent import Agent
agent = Agent(heartbeat_interval=90)
agent.run(single_cycle=True)
print("SMOKE_OK")
''')

        try:
            result = subprocess.run(
                [self.python_exe, smoke_script],
                capture_output=True, text=True, timeout=120,
                env=env, cwd=str(self.project_root),
            )

            if result.returncode == 0 and "SMOKE_OK" in result.stdout:
                return True, ""

            error = result.stderr.strip()[:500]
            return False, f"Smoke test failed: {error}"
        except subprocess.TimeoutExpired:
            return False, "Smoke test timed out (120s)"
        except Exception as e:
            return False, str(e)

    def _cleanup(self):
        """Remove sandbox directory."""
        if self.sandbox_dir and os.path.exists(self.sandbox_dir):
            try:
                shutil.rmtree(self.sandbox_dir, ignore_errors=True)
            except Exception:
                pass
            self.sandbox_dir = None

    def _result(self, passed, reason, scores, baseline_scores, start, gates_passed):
        """Build standard result dict."""
        return {
            "passed": passed,
            "reason": reason,
            "scores": scores,
            "baseline_scores": baseline_scores,
            "duration": time.time() - start,
            "gates_passed": gates_passed,
        }
