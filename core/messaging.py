from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

import requests

from core import delivery
from core.config import AppConfig
from core.persistence import Database
from core.utils import EmailQueueStatus, EmailSendError, OutlookUnavailableError

logger = logging.getLogger(__name__)


# Outlook


class Sender(Protocol):
    def send(
        self,
        *,
        to_address: str,
        subject: str,
        body_html: str,
        attachment_path: str,
        attachment_name: str,
        from_on_behalf: str = "",
    ) -> None: ...


class DraftSender:
    def __init__(self, inner) -> None:
        self._inner = inner

    def send(self, **kwargs) -> None:
        self._inner.send(save_only=True, **kwargs)


if sys.platform == "win32":
    import win32com.client

    class OutlookSender:
        def __init__(self) -> None:
            self._app = None

        def _app_or_attach(self):
            if self._app is not None:
                return self._app
            try:
                self._app = win32com.client.GetActiveObject("Outlook.Application")
            except Exception:
                try:
                    self._app = win32com.client.gencache.EnsureDispatch("Outlook.Application")
                except Exception as exc:
                    raise OutlookUnavailableError(
                        f"Could not start or attach to Outlook: {exc}"
                    ) from exc
            return self._app

        def send(
            self,
            *,
            to_address: str,
            subject: str,
            body_html: str,
            attachment_path: str,
            attachment_name: str,
            from_on_behalf: str = "",
            save_only: bool = False,
        ) -> None:
            if not Path(attachment_path).exists():
                raise EmailSendError(f"Attachment missing: {attachment_path}")
            app = self._app_or_attach()
            mail = app.CreateItem(0)
            mail.To = to_address
            mail.Subject = subject
            mail.HTMLBody = body_html
            mail.Attachments.Add(str(attachment_path), DisplayName=attachment_name)
            if from_on_behalf:
                mail.SentOnBehalfOfName = from_on_behalf
            if save_only:
                mail.Save()
            else:
                mail.Send()

else:

    class OutlookSender:
        def __init__(self) -> None:
            raise OutlookUnavailableError("Outlook COM automation is only available on Windows")


# Email worker


CompletionCallback = Callable[[str, int, int, int], None]


class EmailWorker(threading.Thread):
    def __init__(
        self,
        db: Database,
        config: AppConfig,
        sender: Sender,
        batch_id: str,
        webhook_success: str = "",
        webhook_error: str = "",
        on_complete: CompletionCallback | None = None,
        success_status: EmailQueueStatus = EmailQueueStatus.SENT,
    ) -> None:
        super().__init__(daemon=True, name="email-worker")
        self.db = db
        self.config = config
        self.sender = sender
        self.batch_id = batch_id
        self.webhook_success = webhook_success
        self.webhook_error = webhook_error
        self.on_complete = on_complete
        self.success_status = success_status
        self._pause_event = threading.Event()
        self._stop_event = threading.Event()

    def pause(self) -> None:
        self._pause_event.set()

    def resume(self) -> None:
        self._pause_event.clear()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        is_windows = sys.platform == "win32"
        if is_windows:
            import pythoncom

            pythoncom.CoInitialize()
        try:
            self._loop()
        finally:
            if is_windows:
                import pythoncom

                pythoncom.CoUninitialize()

    def _loop(self) -> None:
        sent = failed = 0
        while not self._stop_event.is_set():
            if self._pause_event.is_set():
                time.sleep(0.2)
                continue

            item = delivery.claim_next(self.db, self.batch_id)
            if item is None:
                break

            try:
                self.sender.send(
                    to_address=item["to_address"],
                    subject=item["subject"],
                    body_html=item["body_html"],
                    attachment_path=item["attachment_path"],
                    attachment_name=item["attachment_name"],
                    from_on_behalf=item["from_on_behalf"],
                )
                if self.success_status == EmailQueueStatus.SENT:
                    delivery.mark_sent(self.db, item["id"])
                    sent += 1
                else:
                    self.db.execute(
                        "UPDATE email_queue SET status = ? WHERE id = ?",
                        (self.success_status.value, item["id"]),
                    )
            except OutlookUnavailableError as exc:

                self.db.execute(
                    "UPDATE email_queue SET status = ? WHERE id = ?",
                    (EmailQueueStatus.QUEUED.value, item["id"]),
                )
                logger.error("Outlook unavailable, pausing worker: %s", exc)
                notify(
                    self.webhook_error,
                    "Email worker paused",
                    {"batch_id": self.batch_id, "reason": str(exc)},
                )
                self.pause()
                continue
            except Exception as exc:
                status = delivery.mark_retry_or_failed(
                    self.db, item["id"], str(exc), self.config.email.max_attempts
                )
                if status == EmailQueueStatus.FAILED.value:
                    failed += 1
                else:
                    time.sleep(self.config.email.retry_backoff_seconds)

            time.sleep(self.config.email.throttle_seconds)

        counts = delivery.queue_counts(self.db, self.batch_id)
        cancelled = counts.get(EmailQueueStatus.CANCELLED.value, 0)
        if self.success_status == EmailQueueStatus.SENT:
            notify(
                self.webhook_success,
                "Send run completed",
                {"batch_id": self.batch_id, "sent": sent, "failed": failed, "cancelled": cancelled},
            )
        if self.on_complete:
            self.on_complete(self.batch_id, sent, failed, cancelled)


# Teams notifications


logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10


def _adaptive_card(title: str, facts: dict) -> dict:
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": title,
                            "weight": "Bolder",
                            "size": "Medium",
                            "wrap": True,
                        },
                        {
                            "type": "FactSet",
                            "facts": [{"title": str(k), "value": str(v)} for k, v in facts.items()],
                        },
                    ],
                },
            }
        ],
    }


def notify(webhook_url: str, title: str, facts: dict) -> bool:
    if not webhook_url:
        return False
    payload = _adaptive_card(title, facts)
    for attempt in range(2):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Teams webhook failed (attempt %d): %s", attempt + 1, exc)
    return False
