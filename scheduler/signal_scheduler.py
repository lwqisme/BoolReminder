"""Strategy signal intraday scheduler."""

import logging
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from drawdown.strategy_signal import generate_all_signals
from notify.email_sender import EmailSender

logger = logging.getLogger(__name__)


class SignalScheduler:
    """Intraday strategy signal scheduler (23:30 Asia/Shanghai)."""

    def __init__(
        self,
        email_sender: EmailSender,
        to_emails: list[str],
        hours: list[int] | None = None,
        minutes: list[int] | None = None,
    ):
        self.email_sender = email_sender
        self.to_emails = to_emails
        hours = hours or [23]
        minutes = minutes or [30]
        tz = pytz.timezone("Asia/Shanghai")
        self.scheduler = BackgroundScheduler(timezone=tz)
        for h, m in zip(hours, minutes):
            self.scheduler.add_job(
                func=self._run,
                trigger=CronTrigger(hour=h, minute=m, timezone=tz),
                id=f"intraday_signal_{h:02d}{m:02d}",
                name=f"盘中策略信号 {h:02d}:{m:02d} Asia/Shanghai",
                replace_existing=True,
            )

    def _run(self):
        logger.info("开始生成盘中策略信号...")
        try:
            results = generate_all_signals(dry_run=False)
        except Exception as e:
            logger.error(f"信号生成失败: {e}", exc_info=True)
            try:
                self.email_sender.send_report_simple(
                    "策略信号生成失败",
                    f"信号生成失败: {e}",
                    to_emails=self.to_emails,
                )
            except Exception as ee:
                logger.error(f"错误邮件发送失败: {ee}", exc_info=True)
            return

        # Collect active signals: stock signals + LEAPS entry/sell signals
        active: list[dict[str, object]] = []
        for r in results:
            if r.get("error"):
                continue
            has_stock = any(s.get("status") == "signal" for s in r.get("signals", []))
            has_leaps = bool(r.get("entry_signals") or r.get("sell_signals"))
            if has_stock or has_leaps:
                active.append(r)

        if not active:
            logger.info("无有效信号，跳过邮件发送")
            return

        subject_parts: list[str] = []
        body_lines: list[str] = []
        for r in active:
            symbol = str(r["symbol"])
            preset_name = str(r.get("preset_name") or r.get("preset_id", ""))
            preset_tag = f" [{preset_name}]" if preset_name else ""
            # Stock signals
            has_actionable = False
            for sig in r.get("signals", []):
                status = sig.get("status", "signal")
                if status == "covered":
                    body_lines.append(
                        f"{symbol}{preset_tag}: [已覆盖] {sig.get('reason', '')}"
                    )
                    continue
                has_actionable = True
                action = sig.get("action", "?")
                price = sig.get("price", "?")
                reason = sig.get("reason", "")
                shares = sig.get("shares", "?")
                line = f"{symbol}{preset_tag}: {action} {shares}股 @ ${price}"
                if reason:
                    line += f" ({reason})"
                body_lines.append(line)
                subject_parts.append(f"{symbol} {action}")
            # LEAPS entry signals
            for es in r.get("entry_signals", []):
                price = es.get("stock_price", "?")
                reason = es.get("reason", "")
                line = f"{symbol}{preset_tag}: LEAPS买入 @ ${price}"
                if reason:
                    line += f" ({reason})"
                body_lines.append(line)
                subject_parts.append(f"{symbol} LEAPS买入")
            # LEAPS sell signals
            for ss in r.get("sell_signals", []):
                price = ss.get("stock_price", "?")
                stage = ss.get("stage", "?")
                pct = ss.get("pct_to_sell", "?")
                reason = ss.get("reason", "")
                line = f"{symbol}{preset_tag}: LEAPS卖出 S{stage} {pct}% @ ${price}"
                if reason:
                    line += f" ({reason})"
                body_lines.append(line)
                subject_parts.append(f"{symbol} LEAPS卖出")

        subject = "策略信号: " + ", ".join(subject_parts)

        try:
            from drawdown.strategy_signal import build_signal_email_html
            _, html = build_signal_email_html(results)
            self.email_sender.send_html_email(subject, html, to_emails=self.to_emails)
            logger.info(f"策略信号邮件已发送: {subject}")
        except Exception as e:
            logger.error(f"策略信号邮件发送失败: {e}", exc_info=True)

    def start(self):
        self.scheduler.start()
        logger.info("盘中策略信号调度器已启动: %s", "; ".join(str(job) for job in self.scheduler.get_jobs()))

    def stop(self):
        self.scheduler.shutdown()
        logger.info("策略信号调度器已停止")
