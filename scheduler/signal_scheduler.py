"""Strategy signal nightly scheduler."""

import logging
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from drawdown.strategy_signal import generate_all_signals
from notify.email_sender import EmailSender

logger = logging.getLogger(__name__)


class SignalScheduler:
    """Nightly strategy signal scheduler (default 23:00 Asia/Shanghai)."""

    def __init__(self, email_sender: EmailSender, to_emails: list[str], hour: int = 23, minute: int = 0):
        self.email_sender = email_sender
        self.to_emails = to_emails
        self.scheduler = BackgroundScheduler(timezone=pytz.timezone("Asia/Shanghai"))
        self.scheduler.add_job(
            func=self._run,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=pytz.timezone("Asia/Shanghai")),
            id="nightly_signal",
            name="每晚策略信号",
            replace_existing=True,
        )

    def _run(self):
        from datetime import date
        today = date.today()
        if today.weekday() >= 5:
            logger.info(f"周末 ({today.isoformat()})，跳过策略信号邮件发送")
            return

        logger.info("开始生成每晚策略信号...")
        try:
            results = generate_all_signals(dry_run=False)
        except Exception as e:
            logger.error(f"信号生成失败: {e}", exc_info=True)
            return

        active = [r for r in results if "error" not in r and r.get("signals")]
        if not active:
            logger.info("无有效信号，跳过邮件发送")
            return

        subject_parts: list[str] = []
        body_lines: list[str] = []
        for r in active:
            symbol = r["symbol"]
            signals = r["signals"]
            for sig in signals:
                action = sig["action"]
                price = sig.get("price", "?")
                reason = sig.get("reason", "")
                shares = sig.get("shares", "?")
                line = f"{symbol}: {action} {shares}股 @ ${price}"
                if reason:
                    line += f" ({reason})"
                body_lines.append(line)
                subject_parts.append(f"{symbol} {action}")

        subject = "策略信号: " + ", ".join(subject_parts)
        body = "\n".join(body_lines)

        try:
            self.email_sender.send_report_simple(subject, body, to_emails=self.to_emails)
            logger.info(f"策略信号邮件已发送: {subject}")
        except Exception as e:
            logger.error(f"策略信号邮件发送失败: {e}", exc_info=True)

    def start(self):
        self.scheduler.start()
        logger.info("策略信号调度器已启动 (每晚 23:00)")

    def stop(self):
        self.scheduler.shutdown()
        logger.info("策略信号调度器已停止")
