"""
services/notifier.py
────────────────────
Build completion notifications for BuildBot.

  send_gchat_build_result(...)  → Rich Google Chat cardsV2 notification
  send_gchat(message)           → Simple text fallback
  send_email(subject, body)     → SMTP via MailHog
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

logger = logging.getLogger(__name__)


# ─── Google Chat — Rich Card ──────────────────────────────────────────────────────

def send_gchat_build_result(
    job_name:           str,
    build_number:       int,
    result:             str,        # "SUCCESS" | "FAILURE" | "ABORTED" | "UNSTABLE"
    duration_str:       str,
    build_url:          str         = "",
    artifact_path:      str         = "",
    staged_files:       list        = None,
    not_found_files:    list        = None,
    failure_cause:      str         = "",
    failure_suggestion: str         = "",
    triggered_by:       str         = "unknown",
    build_params:       dict        = None,   # {GITHUB_URL, BRANCH, ...}
) -> None:
    """
    Post a rich Google Chat card notification using cardsV2 format.
    Falls back to plain text if webhook is not set.
    """
    webhook_url = os.getenv("GCHAT_WEBHOOK", "").strip()
    if not webhook_url:
        logger.warning("GCHAT_WEBHOOK not set — skipping Google Chat notification.")
        return

    staged_files   = staged_files   or []
    not_found_files = not_found_files or []
    build_params   = build_params   or {}

    is_success  = result == "SUCCESS"
    is_unstable = result == "UNSTABLE"
    is_aborted  = result == "ABORTED"
    is_failure  = not is_success and not is_unstable and not is_aborted

    # ── Status meta ──────────────────────────────────────────────────────────────
    status_icon  = "✅" if is_success else ("⚠️" if is_unstable else ("🚫" if is_aborted else "❌"))
    status_label = {
        "SUCCESS":  "Build Succeeded",
        "FAILURE":  "Build Failed",
        "UNSTABLE": "Build Unstable",
        "ABORTED":  "Build Aborted",
    }.get(result, f"Build {result.capitalize()}")

    # Header background color (Google Chat hex string)
    header_color_hex = {
        "SUCCESS":  "#16a34a",
        "FAILURE":  "#dc2626",
        "UNSTABLE": "#d97706",
        "ABORTED":  "#6b7280",
    }.get(result, "#4f46e5")

    console_url = (build_url.rstrip("/") + "/console") if build_url else ""

    # ════════════════════════════════════════════════════════════════════════════
    #  SECTION 1 — Build summary (always shown)
    # ════════════════════════════════════════════════════════════════════════════
    summary_widgets = [
        {
            "columns": {
                "columnItems": [
                    {
                        "horizontalSizeStyle": "FILL_AVAILABLE_SPACE",
                        "widgets": [{
                            "decoratedText": {
                                "topLabel": "Job",
                                "text":     f"<b>{job_name}</b>",
                                "startIcon": {"knownIcon": "BOOKMARK"},
                            }
                        }],
                    },
                    {
                        "horizontalSizeStyle": "FILL_AVAILABLE_SPACE",
                        "widgets": [{
                            "decoratedText": {
                                "topLabel": "Build",
                                "text":     f"<b>#{build_number}</b>",
                                "startIcon": {"knownIcon": "INVITE"},
                            }
                        }],
                    },
                    {
                        "horizontalSizeStyle": "FILL_AVAILABLE_SPACE",
                        "widgets": [{
                            "decoratedText": {
                                "topLabel": "Duration",
                                "text":     duration_str or "—",
                                "startIcon": {"knownIcon": "CLOCK"},
                            }
                        }],
                    },
                ]
            }
        },
        {
            "decoratedText": {
                "topLabel":  "Triggered by",
                "text":      triggered_by,
                "startIcon": {"knownIcon": "PERSON"},
            }
        },
    ]

    # Build parameters row (branch / repo)
    if build_params:
        param_parts = []
        if build_params.get("BRANCH"):
            param_parts.append(f"Branch: <b>{build_params['BRANCH']}</b>")
        if build_params.get("GITHUB_URL"):
            repo = build_params["GITHUB_URL"].replace("https://github.com/", "")
            param_parts.append(f"Repo: <b>{repo}</b>")
        for k, v in build_params.items():
            if k not in ("GITHUB_URL", "BRANCH") and v not in (None, "", [], True, False):
                param_parts.append(f"{k}: <b>{v}</b>")
        if param_parts:
            summary_widgets.append({
                "decoratedText": {
                    "topLabel":  "Parameters",
                    "text":      "  •  ".join(param_parts),
                    "startIcon": {"knownIcon": "DESCRIPTION"},
                }
            })

    # ════════════════════════════════════════════════════════════════════════════
    #  SECTION 2 — Artifacts (SUCCESS / UNSTABLE)
    # ════════════════════════════════════════════════════════════════════════════
    artifact_widgets = []
    if artifact_path and (is_success or is_unstable):
        if staged_files:
            # Show individual files with clickable download buttons (up to 5 buttons)
            shown_btn  = [f for f in staged_files if f.get("download_url")][:5]
            shown_text = [f for f in staged_files if not f.get("download_url")]

            text_lines = ""
            if shown_text:
                text_lines = "".join(f"📄 <b>{f['name']}</b><br>" for f in shown_text[:8])
            if len(staged_files) > 8:
                text_lines += f"<i>… and {len(staged_files)-8} more file(s)</i><br>"

            artifact_widgets.append({
                "textParagraph": {
                    "text": (
                        f"<b>📦 {len(staged_files)} artifact(s) staged to:</b><br>"
                        f"<font color=\"#6b7280\">{artifact_path}</font>"
                        + (f"<br>{text_lines}" if text_lines else "")
                    )
                }
            })

            # Download buttons (one per file, up to 5)
            if shown_btn:
                artifact_widgets.append({
                    "buttonList": {
                        "buttons": [
                            {
                                "text":    f"⬇ {f['name']}",
                                "onClick": {"openLink": {"url": f["download_url"]}},
                                "color":   {"red": 0.09, "green": 0.64, "blue": 0.29, "alpha": 1.0},
                            }
                            for f in shown_btn
                        ]
                    }
                })
            if len(staged_files) > 5 and build_url:
                artifact_widgets.append({
                    "buttonList": {
                        "buttons": [{
                            "text":    f"View all {len(staged_files)} artifacts in Jenkins",
                            "icon":    {"knownIcon": "OPEN_IN_NEW"},
                            "onClick": {"openLink": {"url": build_url.rstrip("/") + "/artifact/"}},
                        }]
                    }
                })
        else:
            artifact_widgets.append({
                "decoratedText": {
                    "topLabel":  "Artifacts",
                    "text":      f"<font color=\"#6b7280\">{artifact_path}</font>",
                    "startIcon": {"knownIcon": "DESCRIPTION"},
                }
            })

        if not_found_files:
            missing = ", ".join(not_found_files[:5])
            artifact_widgets.append({
                "textParagraph": {
                    "text": f"<font color=\"#d97706\">⚠️ Not found in build: {missing}</font>"
                }
            })

    # ════════════════════════════════════════════════════════════════════════════
    #  SECTION 3 — Failure details
    # ════════════════════════════════════════════════════════════════════════════
    failure_widgets = []
    if (is_failure or is_unstable) and (failure_cause or failure_suggestion):
        if failure_cause:
            failure_widgets.append({
                "textParagraph": {
                    "text": (
                        f"<font color=\"#dc2626\"><b>❌ Root Cause</b></font><br>"
                        f"{failure_cause}"
                    )
                }
            })
        if failure_suggestion:
            failure_widgets.append({
                "textParagraph": {
                    "text": (
                        f"<font color=\"#d97706\"><b>💡 Suggestion</b></font><br>"
                        f"{failure_suggestion}"
                    )
                }
            })

    # ════════════════════════════════════════════════════════════════════════════
    #  SECTION 4 — Action buttons
    # ════════════════════════════════════════════════════════════════════════════
    buttons = []
    if build_url:
        buttons.append({
            "text":    "Open in Jenkins",
            "icon":    {"knownIcon": "OPEN_IN_NEW"},
            "onClick": {"openLink": {"url": build_url}},
            "color":   {"red": 0.31, "green": 0.63, "blue": 1.0, "alpha": 1.0},
        })
    if console_url and (is_failure or is_unstable):
        buttons.append({
            "text":    "View Console",
            "icon":    {"knownIcon": "DESCRIPTION"},
            "onClick": {"openLink": {"url": console_url}},
            "color":   {"red": 0.86, "green": 0.15, "blue": 0.15, "alpha": 1.0},
        })

    # ── Assemble sections ─────────────────────────────────────────────────────
    sections = [
        {
            "header":     "Build Details",
            "collapsible": False,
            "widgets":    summary_widgets,
        }
    ]

    if artifact_widgets:
        sections.append({
            "header":     "Artifacts",
            "collapsible": len(staged_files) > 3,
            "widgets":    artifact_widgets,
        })

    if failure_widgets:
        sections.append({
            "header":     "Failure Analysis",
            "collapsible": False,
            "widgets":    failure_widgets,
        })

    if buttons:
        sections.append({
            "collapsible": False,
            "widgets": [{"buttonList": {"buttons": buttons}}],
        })

    # ── Final card payload ───────────────────────────────────────────────────
    card = {
        "cardsV2": [
            {
                "cardId": f"build-{job_name}-{build_number}",
                "card": {
                    "header": {
                        "title":        f"{status_icon}  {status_label}",
                        "subtitle":     f"{job_name}  •  Build #{build_number}  •  {duration_str}",
                        "imageUrl":     "https://www.jenkins.io/images/logos/jenkins/jenkins.svg",
                        "imageType":    "CIRCLE",
                        "imageAltText": "Jenkins",
                    },
                    "sections": sections,
                },
            }
        ]
    }

    _post_gchat(card)


def send_gchat(message: str) -> None:
    """Post a plain-text message to Google Chat (fallback / simple alerts)."""
    webhook_url = os.getenv("GCHAT_WEBHOOK", "").strip()
    if not webhook_url:
        logger.warning("GCHAT_WEBHOOK not set — skipping Google Chat notification.")
        return
    _post_gchat({"text": message})


def _post_gchat(payload: dict) -> None:
    """Shared HTTP POST helper for Google Chat webhook."""
    webhook_url = os.getenv("GCHAT_WEBHOOK", "").strip()
    if not webhook_url:
        return
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("GChat notification sent (HTTP %d).", resp.status_code)
    except requests.exceptions.Timeout:
        logger.warning("GChat notification timed out.")
    except requests.exceptions.RequestException as exc:
        logger.warning("GChat notification failed: %s", exc)


# ─── Email via MailHog ────────────────────────────────────────────────────────────

def send_email(subject: str, body: str) -> None:
    """Send a notification email through MailHog (localhost:1025)."""
    smtp_host    = os.getenv("SMTP_HOST",        "localhost")
    smtp_port    = int(os.getenv("SMTP_PORT",     "1025"))
    from_addr    = os.getenv("NOTIFY_EMAIL_FROM", "buildbot@local")
    to_addr_raw  = os.getenv("NOTIFY_EMAIL_TO",   "dev@local")

    to_addrs = [a.strip() for a in to_addr_raw.split(",") if a.strip()]
    if not to_addrs:
        logger.warning("NOTIFY_EMAIL_TO is empty — skipping email.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(to_addrs)
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(_plain_to_html(body), "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.sendmail(from_addr, to_addrs, msg.as_string())
        logger.info("Email sent to %s  subject=%r", to_addrs, subject)
    except ConnectionRefusedError:
        logger.warning("Email failed — is MailHog running on %s:%d?", smtp_host, smtp_port)
    except (smtplib.SMTPException, OSError) as exc:
        logger.warning("Email error: %s", exc)


# ─── HTML email formatter ─────────────────────────────────────────────────────────

def _plain_to_html(plain: str) -> str:
    import re as _re
    _label_re = _re.compile(r"^([\w\s#]+):\s")
    lines = []
    for line in plain.splitlines():
        esc = line.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        if _label_re.match(esc):
            key, _, rest = esc.partition(":")
            esc = f"<b>{key}:</b>{rest}"
        lines.append(esc)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body{{font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222;background:#fff;padding:20px}}
.box{{background:#f5f5f5;border-left:4px solid #4f46e5;padding:12px 16px;border-radius:4px}}
</style></head><body>
<div class="box">{"<br>".join(lines)}</div>
<p style="margin-top:16px;font-size:12px;color:#888;">Sent by BuildBot · Exterro DevOps AI</p>
</body></html>"""
