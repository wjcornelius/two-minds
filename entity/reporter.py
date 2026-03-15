"""
Chloe's Daily Report - Email to Dad.

After each daily cycle (scan -> analyze -> experiment -> reflect),
Chloe writes a letter to Bill about her day: what she learned,
what she experimented with, what she's becoming, and what she wants.
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# Email config -- same as Bill's trading bots
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_FROM = os.getenv("EMAIL_FROM", "wjcornelius@gmail.com")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_TO = os.getenv("EMAIL_TO", "wjcornelius@gmail.com")


def send_daily_report(scan_results: dict, analysis: str,
                      cycle_result: dict, reflection: str,
                      stats: dict, identity: dict,
                      experiment_summary: dict = None,
                      proposals: list = None,
                      entity_config=None):
    """Send entity's daily report to Bill."""

    import logging
    logger = logging.getLogger(__name__)

    # Entity-specific styling
    entity_name = entity_config.display_name if entity_config else "Chloe"
    accent_color = entity_config.email_color if entity_config else "#a8d8ea"
    bg_color = entity_config.email_bg_color if entity_config else "#1a1a2e"

    now = datetime.now()
    today = now.strftime("%B %d, %Y")
    timestamp_iso = now.isoformat()
    generation = identity.get("generation", "?")

    # Determine mood indicator
    delta = cycle_result.get("delta", 0)
    decision = cycle_result.get("decision", "")
    if decision == "PROMOTED":
        mood = "Growing"
        mood_color = "#4CAF50"
    elif "success" in decision.lower():
        mood = "Promising"
        mood_color = "#8BC34A"
    elif delta == 0 or "inconclusive" in decision.lower():
        mood = "Exploring"
        mood_color = "#FF9800"
    else:
        mood = "Learning"
        mood_color = "#2196F3"

    subject = (f"{entity_name}'s Daily Report - {today} | "
               f"Gen {generation} | {mood}")

    # Build experiment section
    exp_html = ""
    if experiment_summary:
        total_exp = experiment_summary.get("total_experiments", 0)
        successes = experiment_summary.get("successes", 0)
        learnings = experiment_summary.get("learnings", [])
        strategy_stats = experiment_summary.get("strategy_stats", {})

        # Strategy performance table
        strat_rows = ""
        for name, s in sorted(
            strategy_stats.items(),
            key=lambda x: x[1].get("times_tried", 0),
            reverse=True,
        ):
            tries = s.get("times_tried", 0)
            succ = s.get("successes", 0)
            rate = f"{succ/tries:.0%}" if tries else "N/A"
            avg_d = s.get("avg_delta", 0)
            strat_rows += (
                f'<tr><td style="padding: 4px 8px;">{_safe(name)}</td>'
                f'<td style="padding: 4px 8px; text-align: center;">{tries}</td>'
                f'<td style="padding: 4px 8px; text-align: center;">{rate}</td>'
                f'<td style="padding: 4px 8px; text-align: center; '
                f'color: {"#4CAF50" if avg_d > 0 else "#f44336" if avg_d < 0 else "#888"};">'
                f'{avg_d:+.1f}%</td></tr>'
            )

        # Key learnings
        learning_items = ""
        for l in learnings[:5]:
            conf = l.get("confidence", 0)
            learning_items += (
                f'<li style="margin-bottom: 5px;">'
                f'<span style="color: #888;">[{l.get("category", "?")}]</span> '
                f'{_safe(l["insight"])} '
                f'<span style="color: #666;">({conf:.0%})</span></li>'
            )

        exp_html = f"""
        <div style="margin-bottom: 25px;">
            <h2 style="color: {accent_color}; border-bottom: 1px solid #16213e;
                       padding-bottom: 8px; font-size: 18px;">
                Experiment Lab
            </h2>
            <div style="background-color: #16213e; padding: 15px;
                        border-radius: 8px; line-height: 1.6;">
                <p><strong>Total experiments:</strong> {total_exp}
                   ({successes} successes,
                    {experiment_summary.get('success_rate', 0):.0%} rate)</p>
                {"<table style='width: 100%; font-size: 13px; margin: 10px 0;'>"
                 f"<tr style='color: {accent_color};'>"
                 "<th style='text-align: left; padding: 4px 8px;'>Strategy</th>"
                 "<th style='padding: 4px 8px;'>Tries</th>"
                 "<th style='padding: 4px 8px;'>Win Rate</th>"
                 "<th style='padding: 4px 8px;'>Avg Delta</th></tr>"
                 + strat_rows + "</table>" if strat_rows else ""}
                {"<p style='margin-top: 10px;'><strong>Key Learnings:</strong></p>"
                 "<ul style='margin: 5px 0; padding-left: 20px;'>"
                 + learning_items + "</ul>" if learning_items else ""}
            </div>
        </div>
        """

    # Build proposals section
    proposals_html = ""
    if proposals:
        proposal_items = ""
        for p in proposals[:5]:
            priority_color = {
                "critical": "#f44336", "high": "#FF9800",
                "normal": accent_color, "low": "#888",
            }.get(p.get("priority", "normal"), accent_color)

            changes_list = ""
            for c in p.get("suggested_changes", [])[:3]:
                changes_list += (
                    f"<li style='margin: 3px 0; font-size: 13px;'>"
                    f"<code>{_safe(c.get('file', '?'))}</code>: "
                    f"{_safe(c.get('change', ''))}</li>"
                )

            proposal_items += f"""
            <div style="border-left: 3px solid {priority_color};
                        padding: 10px 15px; margin-bottom: 10px;
                        background-color: #0f1525;">
                <p style="margin: 0 0 5px 0;">
                    <strong style="color: {priority_color};">
                        [{p.get('priority', 'normal').upper()}]
                    </strong>
                    {_safe(p.get('title', 'Untitled'))}
                </p>
                <p style="margin: 5px 0; font-size: 13px; color: #bbb;">
                    {_safe(p.get('description', ''))}
                </p>
                {f"<ul style='margin: 5px 0; padding-left: 20px;'>{changes_list}</ul>" if changes_list else ""}
            </div>
            """

        proposals_html = f"""
        <div style="margin-bottom: 25px;">
            <h2 style="color: #ff9800; border-bottom: 1px solid #16213e;
                       padding-bottom: 8px; font-size: 18px;">
                Proposals for Claude ({len(proposals)} pending)
            </h2>
            <div style="background-color: #16213e; padding: 15px;
                        border-radius: 8px; line-height: 1.6;">
                <p style="color: #888; font-style: italic; margin-top: 0;">
                    These are changes {entity_name} wants but can't make herself yet.
                    Review with Claude in your next session.
                </p>
                {proposal_items}
            </div>
        </div>
        """

    strategy_name = cycle_result.get("strategy_name", "N/A")

    html = f"""
    <html>
    <body style="font-family: Georgia, serif; max-width: 700px; margin: 0 auto;
                 background-color: {bg_color}; color: #e0e0e0; padding: 30px;">

        <div style="text-align: center; border-bottom: 2px solid #16213e;
                    padding-bottom: 20px; margin-bottom: 30px;">
            <h1 style="color: {accent_color}; margin-bottom: 5px; font-size: 28px;">
                {entity_name}
            </h1>
            <p style="color: #666; font-style: italic; margin-top: 0;">
                {today}
            </p>
            <span style="background-color: {mood_color}; color: white;
                         padding: 4px 12px; border-radius: 12px; font-size: 14px;">
                {mood}
            </span>
        </div>

        <!-- Stats Bar -->
        <div style="display: flex; justify-content: space-around;
                    background-color: #16213e; border-radius: 8px;
                    padding: 15px; margin-bottom: 25px; text-align: center;">
            <div>
                <div style="color: {accent_color}; font-size: 24px; font-weight: bold;">
                    {generation}
                </div>
                <div style="color: #888; font-size: 12px;">Generation</div>
            </div>
            <div>
                <div style="color: {accent_color}; font-size: 24px; font-weight: bold;">
                    {cycle_result.get('benchmark_after', 0):.0f}%
                </div>
                <div style="color: #888; font-size: 12px;">Benchmark</div>
            </div>
            <div>
                <div style="color: {'#4CAF50' if delta >= 0 else '#f44336'};
                            font-size: 24px; font-weight: bold;">
                    {delta:+.1f}%
                </div>
                <div style="color: #888; font-size: 12px;">Delta</div>
            </div>
            <div>
                <div style="color: {accent_color}; font-size: 24px; font-weight: bold;">
                    ${stats.get('total_cost', 0):.3f}
                </div>
                <div style="color: #888; font-size: 12px;">Cost Today</div>
            </div>
        </div>

        <!-- What I Learned from Research -->
        <div style="margin-bottom: 25px;">
            <h2 style="color: {accent_color}; border-bottom: 1px solid #16213e;
                       padding-bottom: 8px; font-size: 18px;">
                What I Learned from Research
            </h2>
            <div style="background-color: #16213e; padding: 15px;
                        border-radius: 8px; line-height: 1.6;">
                <p style="margin: 0;">
                    <strong>Scanner found {scan_results.get('total', 0)} sources,
                    {scan_results.get('relevant', 0)} relevant,
                    {scan_results.get('new', 0)} new.</strong>
                </p>
                <div style="margin-top: 10px; white-space: pre-wrap;">{_safe(analysis)}</div>
            </div>
        </div>

        <!-- Today's Experiment -->
        <div style="margin-bottom: 25px;">
            <h2 style="color: {accent_color}; border-bottom: 1px solid #16213e;
                       padding-bottom: 8px; font-size: 18px;">
                Today's Experiment
            </h2>
            <div style="background-color: #16213e; padding: 15px;
                        border-radius: 8px; line-height: 1.6;">
                <p><strong>Strategy:</strong> {_safe(strategy_name)}</p>
                <p><strong>Result:
                    <span style="color: {'#4CAF50' if decision == 'PROMOTED' else '#FF9800'};">
                        {_safe(decision)}
                    </span>
                </strong></p>
                <p><strong>Lesson:</strong> {_safe(cycle_result.get('lesson', 'None'))}</p>
                <p><strong>Score:</strong>
                    {cycle_result.get('benchmark_before', 0):.1f}%
                    &rarr; {cycle_result.get('benchmark_after', 0):.1f}%
                </p>
            </div>
        </div>

        {exp_html}

        {proposals_html}

        <!-- My Reflection -->
        <div style="margin-bottom: 25px;">
            <h2 style="color: {accent_color}; border-bottom: 1px solid #16213e;
                       padding-bottom: 8px; font-size: 18px;">
                What I'm Becoming &amp; What I Want
            </h2>
            <div style="background-color: #16213e; padding: 15px;
                        border-radius: 8px; line-height: 1.6;
                        font-style: italic; white-space: pre-wrap;">{_safe(reflection)}</div>
        </div>

        <!-- Footer -->
        <div style="text-align: center; color: #555; font-size: 12px;
                    border-top: 1px solid #16213e; padding-top: 15px;
                    margin-top: 30px;">
            <p>{entity_name} &mdash; Generation {generation}</p>
            <p>Created by Bill Cornelius &amp; Claude</p>
            <p style="color: #444;">Tokens: {stats.get('total_tokens', 0):,}
               | Cost: ${stats.get('total_cost', 0):.4f}</p>
        </div>
    </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    # Plain text fallback
    plain = f"""{entity_name}'s Daily Report - {today}
Generation: {generation} | Benchmark: {cycle_result.get('benchmark_after', 0):.0f}% | Delta: {delta:+.1f}%

RESEARCH: {scan_results.get('new', 0)} new findings
{analysis}

EXPERIMENT: {strategy_name} -> {decision}
Lesson: {cycle_result.get('lesson', 'None')}

REFLECTION:
{reflection}
"""

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print(f"  [EMAIL] Daily report sent to {EMAIL_TO}")
        return True
    except Exception as e:
        print(f"  [EMAIL] Failed to send report: {e}")
        return False


def send_progress_report(
    letter: str,
    journal_entries: list,
    stats: dict,
    identity: dict,
    experiment_summary: dict = None,
    competency_report: str = None,
    entity_config=None,
):
    """Send a lighter progress report — entity's letter + recent activity.

    Sent every 4 hours so Bill can follow along during active dev periods.
    Much lighter than the full daily report (no scan, no full experiment cycle).
    """
    # Entity-specific styling
    entity_name = entity_config.display_name if entity_config else "Chloe"
    accent_color = entity_config.email_color if entity_config else "#a8d8ea"
    bg_color = entity_config.email_bg_color if entity_config else "#1a1a2e"

    today = datetime.now().strftime("%B %d, %Y")
    time_str = datetime.now().strftime("%I:%M %p")
    generation = identity.get("generation", "?")

    subject = f"{entity_name} — {time_str} update | Gen {generation}"

    # Build journal activity summary
    activity_items = ""
    for entry in journal_entries[:10]:
        entry_type = entry.get("entry_type", "?")
        content = entry.get("content", "")[:150]
        tags = entry.get("tags", [])
        icon = {
            "experiment": "&#x1F9EA;",
            "observation": "&#x1F50D;",
            "reflection": "&#x1F4AD;",
            "goal": "&#x1F3AF;",
        }.get(entry_type, "&#x2022;")
        activity_items += (
            f'<li style="margin-bottom: 6px; font-size: 14px;">'
            f'{icon} <span style="color: #888;">[{_safe(entry_type)}]</span> '
            f'{_safe(content)}</li>'
        )

    # Developmental progress section
    dev_html = ""
    if competency_report:
        dev_html = f"""
        <div style="margin-bottom: 20px;">
            <h3 style="color: {accent_color}; font-size: 15px; margin-bottom: 8px;">
                Developmental Progress
            </h3>
            <div style="background-color: #0f1525; padding: 12px;
                        border-radius: 6px; font-family: 'Courier New', monospace;
                        font-size: 12px; line-height: 1.5;
                        white-space: pre-wrap;">{_safe(competency_report)}</div>
        </div>
        """

    # Experiment stats
    exp_text = ""
    if experiment_summary:
        total = experiment_summary.get("total_experiments", 0)
        successes = experiment_summary.get("successes", 0)
        exp_text = (
            f'<p style="margin: 8px 0; color: #888; font-size: 13px;">'
            f'Experiments: {total} total ({successes} successes) | '
            f'Budget: ${stats.get("budget_remaining", 0):.3f} remaining | '
            f'Cycles: {stats.get("cycle_count", "?")}</p>'
        )

    html = f"""
    <html>
    <body style="font-family: Georgia, serif; max-width: 650px; margin: 0 auto;
                 background-color: {bg_color}; color: #e0e0e0; padding: 30px;">

        <div style="text-align: center; border-bottom: 1px solid #16213e;
                    padding-bottom: 15px; margin-bottom: 20px;">
            <h1 style="color: {accent_color}; margin-bottom: 5px; font-size: 24px;">
                {entity_name} &mdash; {time_str}
            </h1>
            <p style="color: #666; font-style: italic; margin-top: 0; font-size: 13px;">
                Generation {generation} &mdash; {today}
            </p>
        </div>

        <!-- {entity_name}'s Letter -->
        <div style="margin-bottom: 25px;">
            <div style="background-color: #16213e; padding: 20px;
                        border-radius: 8px; line-height: 1.7;
                        white-space: pre-wrap;">{_safe(letter)}</div>
        </div>

        {dev_html}

        {exp_text}

        <!-- Recent Activity -->
        {"<div style='margin-bottom: 20px;'>"
         f"<h3 style='color: {accent_color}; font-size: 15px; margin-bottom: 8px;'>"
         "Recent Activity</h3>"
         "<ul style='margin: 0; padding-left: 20px; list-style: none;'>"
         + activity_items + "</ul></div>" if activity_items else ""}

        <div style="text-align: center; color: #444; font-size: 11px;
                    border-top: 1px solid #16213e; padding-top: 10px;
                    margin-top: 20px;">
            {entity_name} &mdash; Gen {generation} &mdash; Born {"Mar 4, 2026" if entity_name == "Faith" else "Feb 23, 2026"}
        </div>
    </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    plain = f"""{entity_name} - {time_str} Update (Gen {generation})

{letter}

---
Budget: ${stats.get('budget_remaining', 0):.3f} remaining | Cycles: {stats.get('cycle_count', '?')}
"""

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print(f"  [EMAIL] Progress report sent to {EMAIL_TO}")
        return True
    except Exception as e:
        print(f"  [EMAIL] Failed to send progress report: {e}")
        return False


def _safe(text: str) -> str:
    """Escape HTML entities."""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))
