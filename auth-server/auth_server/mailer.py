"""邮件发送：Mailer 接口 + SMTPMailer / ConsoleMailer，按 AUTH_MAIL_MODE 选择。"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SMTP_SSL_PORT = 465


class Mailer:
    """邮件发送接口。"""

    def send(self, to: str, subject: str, body: str) -> None:
        raise NotImplementedError


class ConsoleMailer(Mailer):
    """开发/测试用：把邮件内容打日志，并保留最近发送记录供测试读取。"""

    def __init__(self) -> None:
        self.sent: List[Dict[str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})
        logger.info("[ConsoleMailer] to=%s subject=%s body=%s", to, subject, body)


class SMTPMailer(Mailer):
    """smtplib 实现：端口 465 走 SMTP_SSL（隐式 SSL），其余端口按 use_tls 走 STARTTLS。"""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        use_tls: bool,
        mail_from: str,
    ) -> None:
        self._host = host
        self._port = int(port)
        self._username = username
        self._password = password
        self._use_tls = bool(use_tls)
        self._mail_from = mail_from

    def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self._mail_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)

        context = ssl.create_default_context()
        if self._port == SMTP_SSL_PORT:
            with smtplib.SMTP_SSL(self._host, self._port, context=context) as smtp:
                self._login_and_send(smtp, message)
        else:
            with smtplib.SMTP(self._host, self._port) as smtp:
                if self._use_tls:
                    smtp.starttls(context=context)
                self._login_and_send(smtp, message)

    def _login_and_send(self, smtp: smtplib.SMTP, message: EmailMessage) -> None:
        if self._username:
            smtp.login(self._username, self._password)
        smtp.send_message(message)


def create_mailer(config: Any) -> Optional[Mailer]:
    """按配置创建 Mailer；AUTH_MAIL_MODE=smtp 但 SMTP 未配置时返回 None（注册端点 503）。"""
    if config.mail_mode == "console":
        return ConsoleMailer()
    if not config.smtp_host:
        return None
    return SMTPMailer(
        host=config.smtp_host,
        port=config.smtp_port,
        username=config.smtp_username,
        password=config.smtp_password,
        use_tls=config.smtp_use_tls,
        mail_from=config.mail_from,
    )
