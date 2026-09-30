"""
services/notifier.py
────────────────────
Build-completion notifications for BuildBot.

Public API
──────────
    send_gchat_build_result(...)  → bool   rich Google Chat card  (v1 cards format)
    send_gchat(message)           → bool   plain-text fallback
    send_email(subject, body)     → bool   SMTP (MailHog or real server)

All functions return True on success, False on any failure, and never raise.
Failures are logged as WARNING with the HTTP status / error text so you can
see exactly what went wrong in the app console.

Why v1 cards (not cardsV2)?
────────────────────────────
Incoming webhook URLs  (https://chat.googleapis.com/v1/spaces/.../messages?key=…)
only accept the **v1** card format.  The `cardsV2` schema is exclusively for
Chat Bot API calls authenticated with a service-account token.  Sending
`cardsV2` to a webhook always returns HTTP 400 — silently dropped here
previously.
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

logger = logging.getLogger(__name__)


# ─── Google Chat — v1 Card (webhook-compatible) ───────────────────────────────────

def send_gchat_build_result(
    job_name:           str,
    build_number:       int,
    result:             str,        # "SUCCESS" | "FAILURE" | "UNSTABLE" | "ABORTED"
    duration_str:       str,
    build_url:          str         = "",
    artifact_path:      str         = "",
    staged_files:       list        = None,
    not_found_files:    list        = None,
    failure_cause:      str         = "",
    failure_suggestion: str         = "",
    triggered_by:       str         = "unknown",
    build_params:       dict        = None,
) -> bool:
    """
    Post a rich v1-cards notification to the configured Google Chat webhook.

    Returns True if Google Chat accepted it (HTTP 200), False otherwise.
    Uses the v1 card format — the only format accepted by incoming webhooks.
    """
    webhook_url = os.getenv("GCHAT_WEBHOOK", "").strip()
    if not webhook_url:
        logger.warning("GCHAT_WEBHOOK not set — skipping Google Chat notification.")
        return False

    staged_files    = staged_files    or []
    not_found_files = not_found_files or []
    build_params    = build_params    or {}

    is_success  = result == "SUCCESS"
    is_unstable = result == "UNSTABLE"
    is_aborted  = result == "ABORTED"
    is_failure  = result == "FAILURE"

    status_icon = (
        "✅" if is_success  else
        "⚠️" if is_unstable else
        "🚫" if is_aborted  else "❌"
    )
    status_label = {
        "SUCCESS":  "Build Succeeded",
        "FAILURE":  "Build Failed",
        "UNSTABLE": "Build Unstable",
        "ABORTED":  "Build Aborted",
    }.get(result, f"Build {result}")

    console_url = (build_url.rstrip("/") + "/console") if build_url else ""

    # ── Section 1: summary key-value rows ────────────────────────────────────────
    widgets = []

    widgets.append({"keyValue": {
        "topLabel": "Job",
        "content":  f"<b>{job_name}</b>",
        "icon":     "BOOKMARK",
    }})
    widgets.append({"keyValue": {
        "topLabel": "Build",
        "content":  f"<b>#{build_number}</b>  ·  {duration_str or '—'}",
        "icon":     "INVITE",
    }})
    widgets.append({"keyValue": {
        "topLabel": "Triggered by",
        "content":  triggered_by,
        "icon":     "PERSON",
    }})

    # Build parameters (branch + repo + extras)
    if build_params:
        parts: list[str] = []
        if build_params.get("BRANCH"):
            parts.append(f"Branch: <b>{build_params['BRANCH']}</b>")
        if build_params.get("GITHUB_URL"):
            repo = build_params["GITHUB_URL"].replace("https://github.com/", "")
            parts.append(f"Repo: <b>{repo}</b>")
        for k, v in build_params.items():
            if k not in ("GITHUB_URL", "BRANCH") and v not in (None, "", [], True, False):
                parts.append(f"{k}: <b>{v}</b>")
        if parts:
            widgets.append({"keyValue": {
                "topLabel":    "Parameters",
                "content":     "  ·  ".join(parts),
                "contentMultiline": True,
                "icon":        "DESCRIPTION",
            }})

    # Artifacts (SUCCESS / UNSTABLE)
    if artifact_path and (is_success or is_unstable):
        if staged_files:
            file_list = "\n".join(
                f"  • {f['name']}" for f in staged_files[:8]
            )
            if len(staged_files) > 8:
                file_list += f"\n  … and {len(staged_files)-8} more"
            art_content = (
                f"<b>{len(staged_files)} file(s)</b> staged to:\n"
                f"{artifact_path}\n{file_list}"
            )
        else:
            art_content = artifact_path

        if not_found_files:
            missing = ", ".join(not_found_files[:5])
            art_content += f"\n⚠️ Not found in build: {missing}"

        widgets.append({"keyValue": {
            "topLabel":         "Artifacts",
            "content":          art_content,
            "contentMultiline": True,
            "icon":             "DESCRIPTION",
        }})

    # Failure details
    if (is_failure or is_unstable) and failure_cause:
        widgets.append({"textParagraph": {
            "text": f"<b>❌ Root Cause:</b> {failure_cause}"
        }})
    if (is_failure or is_unstable) and failure_suggestion:
        widgets.append({"textParagraph": {
            "text": f"<b>💡 Suggestion:</b> {failure_suggestion}"
        }})

    # Action buttons
    buttons: list[dict] = []
    if build_url:
        buttons.append({"textButton": {
            "text":    "Open in Jenkins",
            "onClick": {"openLink": {"url": build_url}},
        }})
    if console_url and (is_failure or is_unstable):
        buttons.append({"textButton": {
            "text":    "View Console",
            "onClick": {"openLink": {"url": console_url}},
        }})
    if buttons:
        widgets.append({"buttons": buttons})

    # ── Assemble v1 card payload ──────────────────────────────────────────────────
    payload = {
        "cards": [
            {
                "header": {
                    "title":     f"{status_icon}  {status_label}",
                    "subtitle":  f"{job_name}  •  Build #{build_number}  •  {duration_str or '—'}",
                    "imageUrl":  "https://www.jenkins.io/images/logos/jenkins/jenkins.svg",
                    "imageStyle": "IMAGE",
                },
                "sections": [
                    {"widgets": widgets}
                ],
            }
        ]
    }

    return _post_gchat(payload)


def send_gchat(message: str) -> bool:
    """Post a plain-text message to Google Chat (simple alerts / tests)."""
    webhook_url = os.getenv("GCHAT_WEBHOOK", "").strip()
    if not webhook_url:
        logger.warning("GCHAT_WEBHOOK not set — skipping Google Chat notification.")
        return False
    return _post_gchat({"text": message})


def _post_gchat(payload: dict) -> bool:
    """
    POST payload to the configured Google Chat webhook.
    Returns True on HTTP 200, False on any error.
    Logs the HTTP status code and response body on failure so you can debug.
    """
    webhook_url = os.getenv("GCHAT_WEBHOOK", "").strip()
    if not webhook_url:
        return False
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if not resp.ok:
            # Log the full response so operators can see exactly what Google rejected
            logger.warning(
                "GChat notification rejected — HTTP %d: %s",
                resp.status_code,
                resp.text[:400],
            )
            return False
        logger.info("GChat notification sent successfully (HTTP %d).", resp.status_code)
        return True
    except requests.exceptions.Timeout:
        logger.warning("GChat notification timed out after 10s.")
        return False
    except requests.exceptions.ConnectionError as exc:
        logger.warning("GChat notification connection error: %s", exc)
        return False
    except requests.exceptions.RequestException as exc:
        logger.warning("GChat notification failed: %s", exc)
        return False


# ─── Email (MailHog / SMTP) ───────────────────────────────────────────────────────

def send_email(subject: str, body: str) -> bool:
    """
    Send a notification email via SMTP.
    Defaults to MailHog on localhost:1025.  Returns True on success.

    Common failures:
      ConnectionRefusedError → MailHog / SMTP not running on the configured port.
      SMTPException          → auth or relay error on a real SMTP server.
    """
    smtp_host   = os.getenv("SMTP_HOST",        "localhost")
    smtp_port   = int(os.getenv("SMTP_PORT",    "1025"))
    from_addr   = os.getenv("NOTIFY_EMAIL_FROM","buildbot@local")
    to_addr_raw = os.getenv("NOTIFY_EMAIL_TO",  "dev@local")

    to_addrs = [a.strip() for a in to_addr_raw.split(",") if a.strip()]
    if not to_addrs:
        logger.warning("NOTIFY_EMAIL_TO is empty — skipping email notification.")
        return False

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(to_addrs)
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(_plain_to_html(body), "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.sendmail(from_addr, to_addrs, msg.as_string())
        logger.info("Email notification sent to %s  subject=%r", to_addrs, subject)
        return True
    except ConnectionRefusedError:
        logger.warning(
            "Email notification failed — nothing is listening on %s:%d. "
            "Is MailHog running? Start it with: mailhog  (or: go run github.com/mailhog/MailHog)",
            smtp_host, smtp_port,
        )
        return False
    except (smtplib.SMTPException, OSError) as exc:
        logger.warning("Email notification error (%s:%d): %s", smtp_host, smtp_port, exc)
        return False


# ─── HTML email formatter ─────────────────────────────────────────────────────────

def _plain_to_html(plain: str) -> str:
    """Convert a plain-text notification body to a simple styled HTML email."""
    import re as _re
    _label_re = _re.compile(r"^([\w #]+):\s")
    lines = []
    for line in plain.splitlines():
        esc = (
            line
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        m = _label_re.match(esc)
        if m:
            key = m.group(1)
            rest = esc[m.end():]
            esc = f"<b>{key}:</b> {rest}"
        lines.append(esc or "&nbsp;")

    body_html = "<br>\n".join(lines)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body  {{ font-family: Segoe UI, Arial, sans-serif; font-size: 14px;
         color: #222; background: #f9f9f9; padding: 24px; }}
.box  {{ background: #fff; border-left: 4px solid #4f46e5;
         padding: 16px 20px; border-radius: 6px;
         box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
.foot {{ margin-top: 16px; font-size: 11px; color: #999; }}
</style></head><body>
<div class="box">{body_html}</div>
<p class="foot">Sent by BuildBot · Exterro DevOps AI</p>
</body></html>"""
