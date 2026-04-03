# services/admin_notify_service.py
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from html import escape

import config


def _is_email_config_ready() -> bool:
    required = [
        config.ADMIN_ALERT_EMAIL,
        config.SMTP_HOST,
        config.SMTP_PORT,
        config.SMTP_USER,
        config.SMTP_PASSWORD,
    ]
    return all(required)


def notify_admin_email(sender_id: str, merged_text: str, response_text: str, decision: dict):
    """
    Send escalation alert email to admin.
    """
    if not _is_email_config_ready():
        print("❌ Admin email config is incomplete. Cannot send escalation email.")
        return False

    subject = f"[Chatbot Escalation] sender_id={sender_id} score={decision['score']}"

    reasons_html = "".join(
        f"<li>{escape(str(reason))}</li>" for reason in decision.get("reasons", [])
    )

    html_body = f"""
    <html>
      <body>
        <h2>🚨 Chatbot Escalation Alert</h2>
        <p><strong>Sender ID:</strong> {escape(str(sender_id))}</p>
        <p><strong>Score:</strong> {escape(str(decision.get('score')))}</p>

        <p><strong>Reasons:</strong></p>
        <ul>
          {reasons_html}
        </ul>

        <p><strong>Latest User Message:</strong></p>
        <blockquote>{escape(merged_text).replace(chr(10), '<br>')}</blockquote>

        <p><strong>Bot Reply Draft / Last Reply:</strong></p>
        <blockquote>{escape(response_text).replace(chr(10), '<br>')}</blockquote>
      </body>
    </html>
    """

    text_body = f"""
[Chatbot Escalation Alert]

Sender ID: {sender_id}
Score: {decision.get('score')}
Reasons: {', '.join(decision.get('reasons', []))}

Latest User Message:
{merged_text}

Bot Reply Draft / Last Reply:
{response_text}
""".strip()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.SMTP_USER
    msg["To"] = config.ADMIN_ALERT_EMAIL

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as server:
            if config.SMTP_USE_TLS:
                server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.sendmail(
                config.SMTP_USER,
                [config.ADMIN_ALERT_EMAIL],
                msg.as_string()
            )

        print(f"✅ Escalation email sent to admin: {config.ADMIN_ALERT_EMAIL}")
        return True

    except Exception as e:
        print(f"❌ Failed to send escalation email: {e}")
        return False