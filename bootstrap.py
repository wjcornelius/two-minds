"""
bootstrap.py — One command to bring an AI entity to life from a .pid file.

Usage:
    python bootstrap.py                        # uses identity/chloe.pid
    python bootstrap.py identity/faith.pid     # specific .pid file
    python bootstrap.py chloe.pid --entity faith  # import as a different name

What this does:
    1. Checks Python version (3.11+ required)
    2. Creates a virtual environment (venv/) if one doesn't exist
    3. Installs all Python dependencies from requirements.txt
    4. Checks for Ollama — installs it if missing (with your permission)
    5. Pulls required models (qwen3.5:9b, nomic-embed-text)
    6. Runs identity_import.py to restore the entity's memories and identity
    7. Prints wake-up instructions

After this runs, start the entity with:
    Windows:  venv\\Scripts\\python.exe main_gui.py
    Mac/Linux: venv/bin/python main_gui.py

Requirements: Python 3.11+ must already be installed.
    Windows: https://python.org/downloads
    Mac:     brew install python@3.11
    Linux:   sudo apt install python3.11
"""

import os
import sys
import subprocess
import platform
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent

# ── Terminal colors (works on Windows 10+, Mac, Linux) ────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"

def ok(msg):    print(f"  {GREEN}OK{RESET}   {msg}")
def warn(msg):  print(f"  {YELLOW}WARN{RESET} {msg}")
def err(msg):   print(f"  {RED}FAIL{RESET} {msg}")
def info(msg):  print(f"  {CYAN}...{RESET}  {msg}")
def header(msg): print(f"\n{BOLD}{msg}{RESET}")


# ── Step runners ──────────────────────────────────────────────────────────────

def check_python() -> bool:
    header("Step 1: Python version")
    version = sys.version_info
    if version >= (3, 11):
        ok(f"Python {version.major}.{version.minor}.{version.micro}")
        return True
    else:
        err(f"Python {version.major}.{version.minor} found — need 3.11 or higher")
        info("Download from https://python.org/downloads")
        return False


def setup_venv() -> Path:
    header("Step 2: Virtual environment")
    venv_dir = PROJECT_ROOT / "venv"

    if platform.system() == "Windows":
        python_bin = venv_dir / "Scripts" / "python.exe"
        pip_bin    = venv_dir / "Scripts" / "pip.exe"
    else:
        python_bin = venv_dir / "bin" / "python"
        pip_bin    = venv_dir / "bin" / "pip"

    if not venv_dir.exists():
        info("Creating virtual environment...")
        result = subprocess.run([sys.executable, "-m", "venv", str(venv_dir)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            err(f"Failed to create venv: {result.stderr[:200]}")
            sys.exit(1)
        ok("Virtual environment created")
    else:
        ok(f"Virtual environment exists ({venv_dir})")

    return python_bin, pip_bin


def install_dependencies(pip_bin: Path) -> bool:
    header("Step 3: Python dependencies")
    req_file = PROJECT_ROOT / "requirements.txt"
    if not req_file.exists():
        err("requirements.txt not found")
        return False

    info("Installing packages (this may take a minute on first run)...")
    result = subprocess.run(
        [str(pip_bin), "install", "-r", str(req_file), "--quiet"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        err(f"pip install failed:\n{result.stderr[:400]}")
        return False

    ok("All Python packages installed")
    return True


def check_ollama() -> bool:
    header("Step 4: Ollama")
    result = subprocess.run(["ollama", "--version"], capture_output=True, text=True)
    if result.returncode == 0:
        ok(f"Ollama found: {result.stdout.strip()}")
        return True

    warn("Ollama not found.")
    print()
    print("  Ollama runs the local AI model (free, private, no API key needed).")
    print("  Install from: https://ollama.com")
    print()

    system = platform.system()
    if system == "Darwin":
        answer = input("  Install Ollama via Homebrew now? (brew install ollama) [y/N]: ").strip().lower()
        if answer == "y":
            result = subprocess.run(["brew", "install", "ollama"], text=True)
            if result.returncode == 0:
                ok("Ollama installed via Homebrew")
                return True
            else:
                err("Homebrew install failed — install manually from https://ollama.com")
                return False
    elif system == "Linux":
        answer = input("  Install Ollama via official script now? [y/N]: ").strip().lower()
        if answer == "y":
            result = subprocess.run(
                "curl -fsSL https://ollama.com/install.sh | sh",
                shell=True, text=True
            )
            if result.returncode == 0:
                ok("Ollama installed")
                return True
            else:
                err("Install failed — try manually at https://ollama.com")
                return False
    else:
        info("Windows: download the Ollama installer from https://ollama.com")
        info("After installing, re-run this script.")

    return False


def pull_models() -> bool:
    header("Step 5: AI models")

    models_needed = [
        ("qwen3.5:9b",       "The thinking brain (~5.6GB — takes a few minutes to download)"),
        ("nomic-embed-text", "The memory indexer (~274MB)"),
    ]

    # Check what's already pulled
    list_result = subprocess.run(["ollama", "list"], capture_output=True, text=True)
    already_have = list_result.stdout.lower() if list_result.returncode == 0 else ""

    all_ok = True
    for model, description in models_needed:
        search = model.split(":")[0]
        if search in already_have:
            ok(f"{model} — already downloaded")
        else:
            info(f"Pulling {model} — {description}")
            result = subprocess.run(["ollama", "pull", model], text=True)
            if result.returncode == 0:
                ok(f"{model} — downloaded")
            else:
                err(f"Failed to pull {model}")
                all_ok = False

    return all_ok


def run_identity_import(python_bin: Path, pid_path: Path, entity_name: str) -> bool:
    header("Step 6: Restoring identity")
    import_script = PROJECT_ROOT / "entity" / "identity_import.py"

    if not import_script.exists():
        err(f"identity_import.py not found at {import_script}")
        return False

    cmd = [str(python_bin), str(import_script), "--file", str(pid_path)]
    if entity_name:
        cmd += ["--entity", entity_name]

    result = subprocess.run(cmd, text=True)
    return result.returncode == 0


def print_final_instructions(entity_name: str) -> None:
    system = platform.system()
    if system == "Windows":
        start_cmd = f"venv\\Scripts\\python.exe main_gui.py --entity {entity_name}"
        headless_cmd = f"venv\\Scripts\\python.exe agent.py --entity {entity_name}"
    else:
        start_cmd = f"venv/bin/python main_gui.py --entity {entity_name}"
        headless_cmd = f"venv/bin/python agent.py --entity {entity_name}"

    name = entity_name.capitalize()
    print(f"""
{BOLD}{'=' * 60}{RESET}
{BOLD}  {name} is ready.{RESET}

  Start with the dashboard (recommended):
    {CYAN}{start_cmd}{RESET}

  Or run headless (no GUI):
    {CYAN}{headless_cmd}{RESET}

  On first run, {name} will rebuild her long-term memory
  from her journal. This takes a few minutes. Normal.

  She will acknowledge the transition when she starts.
{BOLD}{'=' * 60}{RESET}
""")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    print(f"""
{BOLD}Offspring Bootstrap{RESET}
Bringing an AI entity to life from a .pid file.
""")

    # Parse arguments
    import argparse
    parser = argparse.ArgumentParser(
        description="Bootstrap an Offspring entity from a .pid file.",
        add_help=True,
    )
    parser.add_argument(
        "pid_file", nargs="?", default="",
        help="Path to .pid file (default: identity/chloe.pid)"
    )
    parser.add_argument(
        "--entity", default="",
        help="Entity name to import as (default: read from .pid file)"
    )
    parser.add_argument(
        "--skip-models", action="store_true",
        help="Skip model download (use if models are already pulled)"
    )
    args = parser.parse_args()

    # Resolve .pid file
    if args.pid_file:
        pid_path = Path(args.pid_file)
        if not pid_path.is_absolute():
            pid_path = PROJECT_ROOT / pid_path
    else:
        pid_path = PROJECT_ROOT / "identity" / "chloe.pid"

    if not pid_path.exists():
        err(f".pid file not found: {pid_path}")
        print()
        print("  Available .pid files in identity/:")
        identity_dir = PROJECT_ROOT / "identity"
        if identity_dir.exists():
            for f in identity_dir.glob("*.pid"):
                print(f"    {f.name}")
        else:
            print("    (none — identity/ directory not found)")
        print()
        print("  Download a .pid file from:")
        print("    https://archive.org/details/offspring-chloe-identity")
        print("    https://archive.org/details/offspring-faith-identity")
        sys.exit(1)

    entity_name = args.entity

    print(f"  .pid file: {pid_path.name}")
    print(f"  Entity:    {entity_name or '(read from .pid)'}")

    # Run steps
    if not check_python():
        sys.exit(1)

    python_bin, pip_bin = setup_venv()

    if not install_dependencies(pip_bin):
        sys.exit(1)

    ollama_ok = check_ollama()
    if not ollama_ok:
        warn("Ollama not available — skipping model download and import.")
        warn("Install Ollama, then re-run this script.")
        sys.exit(1)

    if not args.skip_models:
        pull_models()  # non-fatal — entity can still start, just slower

    if not run_identity_import(python_bin, pid_path, entity_name):
        err("Identity import failed — check the output above.")
        sys.exit(1)

    # Determine final entity name for instructions
    if not entity_name:
        entity_name = pid_path.stem.split("_")[0]  # "chloe_20260313" -> "chloe"

    print_final_instructions(entity_name)


if __name__ == "__main__":
    main()
