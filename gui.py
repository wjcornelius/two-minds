"""
Chloe's Dashboard — GUI for managing the autonomous agent.

Single window with tabs:
  Status   — Generation, budget, goals, at-a-glance stats
  Journal  — Read Chloe's daily journal entries
  Audit    — Plain-English activity log
  Proposals — Review and approve/reject code changes
  Agent    — Start/stop the agent loop, configure heartbeat

Launch: python gui.py (or double-click desktop shortcut)
"""

import sys
import os
import json
import threading
import subprocess
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

import customtkinter as ctk

from entity.journal import Journal
from entity.memory import Memory
from entity.audit import get_today_log, get_action_counts
from entity.budget import get_budget_status, DAEMON_BUDGET
from entity.proposals import (
    get_pending_proposals, get_all_proposals,
    format_proposal_for_review, review_proposal, apply_proposal,
)
from entity.safety import get_pending_approvals

# Theme
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

PROJECT_ROOT = os.path.dirname(__file__)
ICON_PATH = os.path.join(PROJECT_ROOT, "chloe.ico")

# ── TV-optimized font sizes ──────────────────────────────────────
FONT_HEADER = 40
FONT_SECTION = 30
FONT_STAT_LABEL = 22
FONT_STAT_VALUE = 34
FONT_BODY = 24
FONT_MONO = 22
FONT_BUTTON = 22
FONT_TAB = 24
FONT_STATUS = 26


class ChloeGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Chloe — Autonomous Agent Dashboard")
        self.geometry("1800x1000")
        self.minsize(1200, 800)
        self.state("zoomed")  # Start maximized

        # Set icon if available
        if os.path.exists(ICON_PATH):
            self.iconbitmap(ICON_PATH)

        self.agent_process = None

        # Tab view with larger tabs
        self.tabview = ctk.CTkTabview(
            self, anchor="nw",
            segmented_button_fg_color=("gray75", "gray28"),
            segmented_button_selected_color=("green", "#2FA572"),
        )
        self.tabview.pack(fill="both", expand=True, padx=15, pady=15)

        self._build_status_tab()
        self._build_journal_tab()
        self._build_audit_tab()
        self._build_proposals_tab()
        self._build_agent_tab()

        # Scale up tab button fonts
        try:
            self.tabview._segmented_button.configure(
                font=ctk.CTkFont(size=FONT_TAB, weight="bold"))
        except Exception:
            pass

        # Auto-refresh status on launch
        self.after(200, self._refresh_status)

    # ── STATUS TAB ──────────────────────────────────────────────

    def _build_status_tab(self):
        tab = self.tabview.add("Status")

        # Top bar: header + action buttons
        top_bar = ctk.CTkFrame(tab, fg_color="transparent")
        top_bar.pack(fill="x", padx=30, pady=(10, 5))

        header = ctk.CTkLabel(top_bar, text="Chloe v2 — Status",
                              font=ctk.CTkFont(size=FONT_HEADER, weight="bold"))
        header.pack(side="left")

        # Run Cycle button — prominent on Status tab
        self.status_cycle_btn = ctk.CTkButton(
            top_bar, text="Run Cycle", height=50, width=200,
            font=ctk.CTkFont(size=FONT_BUTTON, weight="bold"),
            fg_color="#2FA572",
            command=self._run_single_cycle_from_status)
        self.status_cycle_btn.pack(side="right", padx=(10, 0))

        refresh_btn = ctk.CTkButton(top_bar, text="Refresh", height=50, width=160,
                                    font=ctk.CTkFont(size=FONT_BUTTON),
                                    fg_color="gray35",
                                    command=self._refresh_status)
        refresh_btn.pack(side="right", padx=(10, 0))

        # Stats frame
        stats_frame = ctk.CTkFrame(tab)
        stats_frame.pack(fill="x", padx=30, pady=10)

        self.status_labels = {}
        stats = [
            ("generation", "Generation"),
            ("budget", "Budget Today"),
            ("journal_entries", "Journal Entries"),
            ("active_goals", "Active Goals"),
            ("experiments", "Experiments"),
            ("agent_status", "Agent"),
        ]

        for i, (key, label) in enumerate(stats):
            row, col = divmod(i, 3)
            frame = ctk.CTkFrame(stats_frame)
            frame.grid(row=row, column=col, padx=15, pady=12, sticky="ew")
            stats_frame.columnconfigure(col, weight=1)

            lbl = ctk.CTkLabel(frame, text=label,
                               font=ctk.CTkFont(size=FONT_STAT_LABEL),
                               text_color="gray70")
            lbl.pack(pady=(10, 0))

            val = ctk.CTkLabel(frame, text="—",
                               font=ctk.CTkFont(size=FONT_STAT_VALUE, weight="bold"))
            val.pack(pady=(0, 10))
            self.status_labels[key] = val

        # Lower half: Goals (left) + Journal (right)
        lower = ctk.CTkFrame(tab, fg_color="transparent")
        lower.pack(fill="both", expand=True, padx=30, pady=(10, 10))
        lower.columnconfigure(0, weight=1)
        lower.columnconfigure(1, weight=2)
        lower.rowconfigure(0, weight=1)

        # ── Goals panel (left) ──
        goals_panel = ctk.CTkFrame(lower)
        goals_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        ctk.CTkLabel(goals_panel, text="Goals",
                     font=ctk.CTkFont(size=FONT_SECTION, weight="bold"),
                     anchor="w").pack(fill="x", padx=15, pady=(10, 5))

        self.goals_scroll = ctk.CTkScrollableFrame(
            goals_panel, fg_color="transparent")
        self.goals_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._goals_data = []

        # ── Journal panel (right) ──
        journal_panel = ctk.CTkFrame(lower)
        journal_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        ctk.CTkLabel(journal_panel, text="Journal",
                     font=ctk.CTkFont(size=FONT_SECTION, weight="bold"),
                     anchor="w").pack(fill="x", padx=15, pady=(10, 5))

        self.journal_scroll = ctk.CTkScrollableFrame(
            journal_panel, fg_color="transparent")
        self.journal_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._journal_data = []

    def _refresh_status(self):
        try:
            memory = Memory()
            journal = Journal()
            budget = get_budget_status()
            journal_stats = journal.get_stats()
            identity = memory.get_full_identity()

            self.status_labels["generation"].configure(
                text=identity.get("generation", "?"))
            self.status_labels["budget"].configure(
                text=f"API ${budget.get('api_spent', 0):.2f}/${budget.get('api_cap', 1):.2f} | Poe {budget.get('poe_points_remaining', 0):,}pts")
            self.status_labels["journal_entries"].configure(
                text=str(journal_stats.get("total", 0)))
            self.status_labels["active_goals"].configure(
                text=str(journal_stats.get("active_goals", 0)))

            exp_total = "—"
            try:
                from entity.experiments import Experimenter
                from entity.evaluator import Evaluator
                from entity.brain import Brain
                brain = Brain()
                evaluator = Evaluator(brain)
                experimenter = Experimenter(brain, evaluator)
                summary = experimenter.get_summary()
                exp_total = f"{summary.get('total_experiments', 0)} ({summary.get('successes', 0)} wins)"
            except Exception:
                pass
            self.status_labels["experiments"].configure(text=exp_total)

            agent_status = "Running" if self.agent_process and self.agent_process.poll() is None else "Stopped"
            self.status_labels["agent_status"].configure(
                text=agent_status,
                text_color="green" if agent_status == "Running" else "gray70")

            # ── Populate Goals list ──
            self._goals_data = journal.get_active_goals()
            for w in self.goals_scroll.winfo_children():
                w.destroy()
            if self._goals_data:
                for i, g in enumerate(self._goals_data):
                    self._make_card(
                        self.goals_scroll, g["content"],
                        on_click=lambda idx=i: self._show_detail(
                            "Goal", self._goals_data[idx]["content"]))
            else:
                ctk.CTkLabel(self.goals_scroll, text="  No active goals.",
                             font=ctk.CTkFont(size=FONT_BODY - 2),
                             text_color="gray60").pack(pady=10)

            # ── Populate Journal list ──
            self._journal_data = journal.get_recent(limit=9999)
            for w in self.journal_scroll.winfo_children():
                w.destroy()
            if self._journal_data:
                for i, e in enumerate(self._journal_data):
                    ts = e.get("timestamp", "")[:16]
                    etype = e.get("entry_type", "")
                    self._make_card(
                        self.journal_scroll, e["content"],
                        header=f"{ts}  —  {etype}",
                        wraplength=1000,
                        on_click=lambda idx=i: self._show_detail(
                            f"Journal — {self._journal_data[idx].get('entry_type', '')}",
                            self._journal_data[idx]["content"]))
            else:
                ctk.CTkLabel(self.journal_scroll, text="  No journal entries yet.",
                             font=ctk.CTkFont(size=FONT_BODY - 2),
                             text_color="gray60").pack(pady=10)

        except Exception as e:
            self.status_labels["generation"].configure(text=f"Error: {e}")

    def _make_card(self, parent, content, header=None, wraplength=600,
                   on_click=None):
        """Create a card-style entry with wrapping text inside a scrollable frame."""
        card = ctk.CTkFrame(parent, fg_color="gray22", corner_radius=8)
        card.pack(fill="x", pady=4, padx=2)

        if header:
            hdr = ctk.CTkLabel(
                card, text=header,
                font=ctk.CTkFont(size=FONT_BODY - 6),
                text_color="#2FA572", anchor="w")
            hdr.pack(fill="x", padx=12, pady=(8, 2))
            if on_click:
                hdr.bind("<Button-1>", lambda e: on_click())

        body = ctk.CTkLabel(
            card, text=content,
            font=ctk.CTkFont(size=FONT_BODY - 2),
            anchor="w", justify="left", wraplength=wraplength)
        body.pack(fill="x", padx=12, pady=(4 if header else 8, 8))

        if on_click:
            card.bind("<Button-1>", lambda e: on_click())
            body.bind("<Button-1>", lambda e: on_click())
            # Hover effect
            for widget in [card, body] + ([hdr] if header else []):
                widget.bind("<Enter>", lambda e, c=card: c.configure(fg_color="gray30"))
                widget.bind("<Leave>", lambda e, c=card: c.configure(fg_color="gray22"))

    def _show_detail(self, title, content):
        """Pop up a window showing the full text of a journal entry or goal."""
        popup = ctk.CTkToplevel(self)
        popup.title(title)
        popup.geometry("900x500")
        popup.transient(self)
        popup.grab_set()

        if os.path.exists(ICON_PATH):
            popup.after(200, lambda: popup.iconbitmap(ICON_PATH))

        ctk.CTkLabel(popup, text=title,
                     font=ctk.CTkFont(size=FONT_SECTION, weight="bold")).pack(
                         padx=20, pady=(15, 5), anchor="w")

        text = ctk.CTkTextbox(popup, font=ctk.CTkFont(size=FONT_BODY), wrap="word")
        text.pack(fill="both", expand=True, padx=20, pady=(5, 10))
        text.insert("1.0", content)
        text.configure(state="disabled")

        ctk.CTkButton(popup, text="Close", width=160, height=45,
                       font=ctk.CTkFont(size=FONT_BUTTON),
                       command=popup.destroy).pack(pady=(0, 15))

    def _run_single_cycle_from_status(self):
        """Run a single cycle and switch to Agent tab to show output."""
        self.tabview.set("Agent")
        self._run_single_cycle()

    # ── JOURNAL TAB ─────────────────────────────────────────────

    def _build_journal_tab(self):
        tab = self.tabview.add("Journal")

        toolbar = ctk.CTkFrame(tab)
        toolbar.pack(fill="x", padx=15, pady=10)

        ctk.CTkLabel(toolbar, text="Date:",
                     font=ctk.CTkFont(size=FONT_BODY)).pack(side="left", padx=8)

        self.journal_date = ctk.CTkEntry(toolbar, width=220, height=45,
                                         font=ctk.CTkFont(size=FONT_BODY),
                                         placeholder_text="YYYY-MM-DD")
        self.journal_date.pack(side="left", padx=8)
        self.journal_date.insert(0, datetime.now().strftime("%Y-%m-%d"))

        ctk.CTkButton(toolbar, text="Load", width=140, height=45,
                       font=ctk.CTkFont(size=FONT_BUTTON),
                       command=self._load_journal).pack(side="left", padx=8)

        self.journal_text = ctk.CTkTextbox(tab, font=ctk.CTkFont(size=FONT_BODY))
        self.journal_text.pack(fill="both", expand=True, padx=15, pady=8)

    def _load_journal(self):
        date_str = self.journal_date.get().strip()
        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")
        journal = Journal()
        content = journal.read_markdown(date_str)

        self.journal_text.configure(state="normal")
        self.journal_text.delete("1.0", "end")
        self.journal_text.insert("1.0", content)
        self.journal_text.configure(state="disabled")

    # ── AUDIT TAB ───────────────────────────────────────────────

    def _build_audit_tab(self):
        tab = self.tabview.add("Audit")

        toolbar = ctk.CTkFrame(tab)
        toolbar.pack(fill="x", padx=15, pady=10)

        ctk.CTkButton(toolbar, text="Refresh", width=140, height=45,
                       font=ctk.CTkFont(size=FONT_BUTTON),
                       command=self._load_audit).pack(side="left", padx=8)

        self.audit_counts = ctk.CTkLabel(toolbar, text="",
                                          font=ctk.CTkFont(size=FONT_BODY))
        self.audit_counts.pack(side="right", padx=15)

        self.audit_text = ctk.CTkTextbox(tab,
                                          font=ctk.CTkFont(family="Consolas", size=FONT_MONO))
        self.audit_text.pack(fill="both", expand=True, padx=15, pady=8)

    def _load_audit(self):
        content = get_today_log()
        counts = get_action_counts()

        self.audit_counts.configure(
            text=f"Today: {counts['total']} actions "
                 f"(safe={counts['safe']}, ask={counts['ask']}, "
                 f"forbidden={counts['forbidden']})")

        self.audit_text.configure(state="normal")
        self.audit_text.delete("1.0", "end")
        self.audit_text.insert("1.0", content)
        self.audit_text.configure(state="disabled")

    # ── PROPOSALS TAB ───────────────────────────────────────────

    def _build_proposals_tab(self):
        tab = self.tabview.add("Proposals")

        toolbar = ctk.CTkFrame(tab)
        toolbar.pack(fill="x", padx=15, pady=10)

        ctk.CTkButton(toolbar, text="Refresh", width=140, height=45,
                       font=ctk.CTkFont(size=FONT_BUTTON),
                       command=self._load_proposals).pack(side="left", padx=8)

        self.proposal_count_label = ctk.CTkLabel(toolbar, text="",
                                                  font=ctk.CTkFont(size=FONT_BODY))
        self.proposal_count_label.pack(side="left", padx=15)

        btn_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        btn_frame.pack(side="right", padx=8)

        self.approve_btn = ctk.CTkButton(btn_frame, text="Approve",
                                          width=150, height=45,
                                          font=ctk.CTkFont(size=FONT_BUTTON),
                                          fg_color="green",
                                          command=self._approve_selected)
        self.approve_btn.pack(side="left", padx=5)

        self.reject_btn = ctk.CTkButton(btn_frame, text="Reject",
                                         width=150, height=45,
                                         font=ctk.CTkFont(size=FONT_BUTTON),
                                         fg_color="red",
                                         command=self._reject_selected)
        self.reject_btn.pack(side="left", padx=5)

        # Proposal list (left) and detail (right)
        panes = ctk.CTkFrame(tab)
        panes.pack(fill="both", expand=True, padx=15, pady=8)
        panes.columnconfigure(1, weight=1)
        panes.rowconfigure(0, weight=1)

        # Left: proposal list
        self.proposal_list = ctk.CTkTextbox(panes, width=450,
                                             font=ctk.CTkFont(size=FONT_BODY))
        self.proposal_list.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.proposal_list.bind("<ButtonRelease-1>", self._on_proposal_click)

        # Right: proposal detail
        self.proposal_detail = ctk.CTkTextbox(panes,
                                               font=ctk.CTkFont(family="Consolas", size=FONT_MONO))
        self.proposal_detail.grid(row=0, column=1, sticky="nsew")

        self.proposals_data = []
        self.selected_proposal_idx = None

    def _load_proposals(self):
        self.proposals_data = get_all_proposals(limit=20)
        pending = [p for p in self.proposals_data if p.get("status") == "pending"]
        self.proposal_count_label.configure(
            text=f"{len(pending)} pending / {len(self.proposals_data)} total")

        self.proposal_list.configure(state="normal")
        self.proposal_list.delete("1.0", "end")
        for i, p in enumerate(self.proposals_data):
            status = p.get("status", "?").upper()
            marker = {"PENDING": "●", "APPROVED": "✓", "REJECTED": "✗",
                      "IMPLEMENTED": "◆"}.get(status, "?")
            title = p.get("title", "Untitled")[:40]
            self.proposal_list.insert("end", f"{marker} [{status}] {title}\n")
        self.proposal_list.configure(state="disabled")

        self.proposal_detail.configure(state="normal")
        self.proposal_detail.delete("1.0", "end")
        if pending:
            self.proposal_detail.insert("1.0",
                format_proposal_for_review(pending[0]))
            self.selected_proposal_idx = self.proposals_data.index(pending[0])
        else:
            self.proposal_detail.insert("1.0", "No pending proposals.\n\n"
                                        "When Chloe proposes code changes, they'll appear here.")
            self.selected_proposal_idx = None
        self.proposal_detail.configure(state="disabled")

    def _on_proposal_click(self, event):
        try:
            line = self.proposal_list.index("insert").split(".")[0]
            idx = int(line) - 1
            if 0 <= idx < len(self.proposals_data):
                self.selected_proposal_idx = idx
                prop = self.proposals_data[idx]
                self.proposal_detail.configure(state="normal")
                self.proposal_detail.delete("1.0", "end")
                self.proposal_detail.insert("1.0", format_proposal_for_review(prop))
                self.proposal_detail.configure(state="disabled")
        except Exception:
            pass

    def _approve_selected(self):
        if self.selected_proposal_idx is None:
            return
        prop = self.proposals_data[self.selected_proposal_idx]
        if prop.get("status") != "pending":
            return

        review_proposal(prop["id"], "approved", "bill")
        result = apply_proposal(prop["id"])

        if result["success"]:
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
            from entity.audit import log_action
            log_action("proposal_approved", "safe",
                       f"Bill approved: {prop.get('title', '')}",
                       outcome=f"Applied to {prop.get('target_file', '')}")

        self._load_proposals()

    def _reject_selected(self):
        if self.selected_proposal_idx is None:
            return
        prop = self.proposals_data[self.selected_proposal_idx]
        if prop.get("status") != "pending":
            return

        # Simple rejection dialog
        dialog = ctk.CTkInputDialog(
            text="Rejection reason (optional):",
            title=f"Reject: {prop.get('title', '')}")
        reason = dialog.get_input() or ""

        review_proposal(prop["id"], "rejected", "bill", reason)

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
        from entity.audit import log_action
        log_action("proposal_rejected", "safe",
                   f"Bill rejected: {prop.get('title', '')}",
                   outcome=reason or "No reason given")

        self._load_proposals()

    # ── AGENT TAB ───────────────────────────────────────────────

    def _build_agent_tab(self):
        tab = self.tabview.add("Agent")

        # Controls
        controls = ctk.CTkFrame(tab)
        controls.pack(fill="x", padx=30, pady=15)

        ctk.CTkLabel(controls, text="Heartbeat (seconds):",
                     font=ctk.CTkFont(size=FONT_BODY)).pack(side="left", padx=8)

        self.interval_entry = ctk.CTkEntry(controls, width=130, height=45,
                                            font=ctk.CTkFont(size=FONT_BODY))
        self.interval_entry.pack(side="left", padx=8)
        self.interval_entry.insert(0, "90")

        self.start_btn = ctk.CTkButton(controls, text="Start Agent",
                                        fg_color="green", width=200, height=50,
                                        font=ctk.CTkFont(size=FONT_BUTTON, weight="bold"),
                                        command=self._start_agent)
        self.start_btn.pack(side="left", padx=15)

        self.stop_btn = ctk.CTkButton(controls, text="Stop Agent",
                                       fg_color="red", width=200, height=50,
                                       font=ctk.CTkFont(size=FONT_BUTTON, weight="bold"),
                                       command=self._stop_agent,
                                       state="disabled")
        self.stop_btn.pack(side="left", padx=8)

        self.agent_status_label = ctk.CTkLabel(controls, text="Stopped",
                                                text_color="gray70",
                                                font=ctk.CTkFont(size=FONT_STATUS, weight="bold"))
        self.agent_status_label.pack(side="right", padx=15)

        # Agent output
        self.agent_output = ctk.CTkTextbox(tab,
                                            font=ctk.CTkFont(family="Consolas", size=FONT_MONO))
        self.agent_output.pack(fill="both", expand=True, padx=30, pady=(0, 15))

        # Single cycle button
        single_frame = ctk.CTkFrame(tab, fg_color="transparent")
        single_frame.pack(fill="x", padx=30, pady=(0, 15))

        self.single_btn = ctk.CTkButton(single_frame, text="Run Single Cycle",
                                         width=260, height=50,
                                         font=ctk.CTkFont(size=FONT_BUTTON),
                                         command=self._run_single_cycle)
        self.single_btn.pack(side="left")

        ctk.CTkLabel(single_frame,
                     text="  Run one OBSERVE → THINK → ACT → REFLECT cycle",
                     font=ctk.CTkFont(size=FONT_BODY - 4),
                     text_color="gray60").pack(side="left", padx=15)

    def _start_agent(self):
        if self.agent_process and self.agent_process.poll() is None:
            return  # Already running

        interval = self.interval_entry.get().strip() or "90"

        self.agent_output.configure(state="normal")
        self.agent_output.delete("1.0", "end")
        self.agent_output.insert("1.0", f"Starting agent (heartbeat={interval}s)...\n")
        self.agent_output.configure(state="disabled")

        python = os.path.join(PROJECT_ROOT, "venv", "Scripts", "python.exe")
        agent_script = os.path.join(PROJECT_ROOT, "agent.py")

        self.agent_process = subprocess.Popen(
            [python, agent_script, "--interval", interval],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=PROJECT_ROOT,
        )

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.single_btn.configure(state="disabled")
        self.agent_status_label.configure(text="Running", text_color="green")

        # Read output in background thread
        threading.Thread(target=self._read_agent_output, daemon=True).start()

    def _stop_agent(self):
        if self.agent_process and self.agent_process.poll() is None:
            self.agent_process.terminate()
            self.agent_output.configure(state="normal")
            self.agent_output.insert("end", "\n[Stopping agent...]\n")
            self.agent_output.configure(state="disabled")

        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.single_btn.configure(state="normal")
        self.agent_status_label.configure(text="Stopped", text_color="gray70")

    def _run_single_cycle(self):
        self.single_btn.configure(state="disabled")
        self.agent_output.configure(state="normal")
        self.agent_output.delete("1.0", "end")
        self.agent_output.insert("1.0", "Running single cycle...\n")
        self.agent_output.configure(state="disabled")

        python = os.path.join(PROJECT_ROOT, "venv", "Scripts", "python.exe")
        agent_script = os.path.join(PROJECT_ROOT, "agent.py")

        self.agent_process = subprocess.Popen(
            [python, agent_script, "--once"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=PROJECT_ROOT,
        )

        threading.Thread(target=self._read_agent_output,
                         kwargs={"single": True}, daemon=True).start()

    def _read_agent_output(self, single=False):
        try:
            for line in self.agent_process.stdout:
                self.agent_output.configure(state="normal")
                self.agent_output.insert("end", line)
                self.agent_output.see("end")
                self.agent_output.configure(state="disabled")
        except Exception:
            pass

        # Process ended
        self.after(100, lambda: self._on_agent_done(single))

    def _on_agent_done(self, single=False):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.single_btn.configure(state="normal")
        self.agent_status_label.configure(text="Stopped", text_color="gray70")

        self.agent_output.configure(state="normal")
        self.agent_output.insert("end", "\n[Agent stopped]\n")
        self.agent_output.configure(state="disabled")

        # Auto-refresh status
        self._refresh_status()

    def destroy(self):
        # Kill agent process on window close
        if self.agent_process and self.agent_process.poll() is None:
            self.agent_process.terminate()
        super().destroy()


if __name__ == "__main__":
    app = ChloeGUI()
    app.mainloop()
