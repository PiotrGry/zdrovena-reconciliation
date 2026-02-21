"""
zdrovena.month_closing.email_service – Email Service (Zoho SMTP)
==================================================================
Sends the monthly accounting package to the accountant via Zoho Mail
SMTP (SSL, port 465).
"""

from __future__ import annotations

import logging
import mimetypes
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from zdrovena.month_closing.config import ZOHO_EMAIL, ZOHO_SMTP_HOST, ZOHO_SMTP_PORT

logger = logging.getLogger("zdrovena.month_closing.email")


class EmailService:
    def __init__(
        self,
        smtp_password: str,
        sender_email: str = ZOHO_EMAIL,
        smtp_host: str = ZOHO_SMTP_HOST,
        smtp_port: int = ZOHO_SMTP_PORT,
    ) -> None:
        self.sender_email = sender_email
        self.smtp_password = smtp_password
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port

    def send_report(
        self,
        to_email: str,
        subject: str,
        body: str,
        attachments: list[Path] | None = None,
    ) -> None:
        msg = MIMEMultipart()
        msg["From"] = self.sender_email
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        for file_path in attachments or []:
            if not file_path.exists():
                logger.warning("Attachment not found, skipping: %s", file_path)
                continue
            self._attach_file(msg, file_path)

        logger.info("Connecting to %s:%d …", self.smtp_host, self.smtp_port)
        try:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=30) as smtp:
                smtp.login(self.sender_email, self.smtp_password)
                smtp.send_message(msg)
            logger.info("Email sent → %s  (subject=%r)", to_email, subject)
        except smtplib.SMTPException as exc:
            logger.error("Failed to send email: %s", exc)
            raise RuntimeError(f"SMTP send failed: {exc}") from exc

    @staticmethod
    def _attach_file(msg: MIMEMultipart, file_path: Path) -> None:
        mime_type, _ = mimetypes.guess_type(str(file_path))
        if mime_type is None:
            mime_type = "application/octet-stream"
        main_type, sub_type = mime_type.split("/", 1)
        with open(file_path, "rb") as fh:
            part = MIMEBase(main_type, sub_type)
            part.set_payload(fh.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=file_path.name)
        msg.attach(part)
        logger.debug("Attached: %s (%.1f KB)", file_path.name, file_path.stat().st_size / 1024)
