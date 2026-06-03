"""Tests for intraday signal scheduler."""
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from apscheduler.triggers.cron import CronTrigger

from scheduler.signal_scheduler import SignalScheduler


class SignalSchedulerLeapsTest(unittest.TestCase):
    """LEAPS signal detection in scheduler email generation."""

    def setUp(self):
        self.email_sender = MagicMock()
        self.email_sender.send_report_simple = MagicMock()

    def _make_scheduler(self):
        return SignalScheduler(
            self.email_sender, ["test@test.com"],
            hours=[11, 12], minutes=[0, 0],
        )

    @patch("scheduler.signal_scheduler.generate_all_signals")
    def test_leaps_entry_signal_included_in_email(self, mock_gen):
        """LEAPS entry_signals should appear in email body."""
        mock_gen.return_value = [
            {
                "symbol": "GOOGL",
                "entry_signals": [
                    {
                        "date": "2026-06-03",
                        "underlying": "GOOGL",
                        "stock_price": 361.85,
                        "drawdown_pct": 10.13,
                        "bollinger_score": 1.5,
                        "reason": "回撤10.1% 布林1.50",
                    },
                ],
                "sell_signals": [],
                "errors": [],
            },
        ]
        sched = self._make_scheduler()
        with patch("datetime.date") as mock_date:
            mock_date.today.return_value = date(2026, 6, 3)  # Wednesday
            mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
            sched._run()

        self.email_sender.send_report_simple.assert_called_once()
        call_args = self.email_sender.send_report_simple.call_args
        subject = call_args[0][0]
        body = call_args[0][1]

        self.assertIn("GOOGL", subject)
        self.assertIn("GOOGL", body)
        self.assertIn("361.85", body)

    @patch("scheduler.signal_scheduler.generate_all_signals")
    def test_leaps_sell_signal_included_in_email(self, mock_gen):
        """LEAPS sell_signals should appear in email body."""
        mock_gen.return_value = [
            {
                "symbol": "AMZN",
                "entry_signals": [],
                "sell_signals": [
                    {
                        "date": "2026-06-03",
                        "stock_price": 240.0,
                        "option_roi_pct": 85.0,
                        "pct_to_sell": 47.0,
                        "stage": 1,
                        "reason": "S1 持有30天 ROA85%≥80% 建议卖47%",
                    },
                ],
                "errors": [],
            },
        ]
        sched = self._make_scheduler()
        with patch("datetime.date") as mock_date:
            mock_date.today.return_value = date(2026, 6, 3)
            mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
            sched._run()

        self.email_sender.send_report_simple.assert_called_once()
        body = self.email_sender.send_report_simple.call_args[0][1]
        self.assertIn("AMZN", body)
        self.assertIn("S1", body)
        self.assertIn("47%", body)

    @patch("scheduler.signal_scheduler.generate_all_signals")
    def test_mixed_stock_and_leaps_signals(self, mock_gen):
        """Stock signals and LEAPS signals coexist."""
        mock_gen.return_value = [
            {
                "symbol": "AMZN",
                "signals": [
                    {"action": "buy", "shares": 10, "price": 200.0, "reason": "test"},
                ],
            },
            {
                "symbol": "GOOGL",
                "entry_signals": [
                    {
                        "date": "2026-06-03",
                        "underlying": "GOOGL",
                        "stock_price": 361.85,
                        "drawdown_pct": 10.13,
                        "bollinger_score": 1.5,
                        "reason": "回撤10.1% 布林1.50",
                    },
                ],
                "sell_signals": [],
                "errors": [],
            },
        ]
        sched = self._make_scheduler()
        with patch("datetime.date") as mock_date:
            mock_date.today.return_value = date(2026, 6, 3)
            mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
            sched._run()

        self.email_sender.send_report_simple.assert_called_once()
        body = self.email_sender.send_report_simple.call_args[0][1]
        self.assertIn("AMZN", body)
        self.assertIn("GOOGL", body)

    @patch("scheduler.signal_scheduler.generate_all_signals")
    def test_no_email_when_no_signals(self, mock_gen):
        """No signals → no email sent."""
        mock_gen.return_value = [
            {"symbol": "AAPL", "entry_signals": [], "sell_signals": [], "errors": []},
            {"symbol": "AMZN", "signals": []},
        ]
        sched = self._make_scheduler()
        with patch("datetime.date") as mock_date:
            mock_date.today.return_value = date(2026, 6, 3)
            mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
            sched._run()

        self.email_sender.send_report_simple.assert_not_called()

    @patch("scheduler.signal_scheduler.generate_all_signals")
    def test_error_email_when_generation_fails(self, mock_gen):
        """Signal generation exception → send error email."""
        mock_gen.side_effect = RuntimeError("Longbridge API timeout")
        sched = self._make_scheduler()
        with patch("datetime.date") as mock_date:
            mock_date.today.return_value = date(2026, 6, 3)
            mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
            sched._run()

        self.email_sender.send_report_simple.assert_called_once()
        body = self.email_sender.send_report_simple.call_args[0][1]
        self.assertIn("失败", body)


class SignalSchedulerIntradayTest(unittest.TestCase):
    """Intraday scheduling: 11:00 and 12:00 US/Eastern."""

    def setUp(self):
        self.email_sender = MagicMock()

    def test_cron_trigger_us_eastern(self):
        """Scheduler should use US/Eastern timezone with 11:00 and 12:00."""
        sched = SignalScheduler(
            self.email_sender, ["test@test.com"],
            hours=[11, 12], minutes=[0, 0],
        )
        jobs = sched.scheduler.get_jobs()
        self.assertEqual(len(jobs), 2, "Expected 2 jobs (11:00 and 12:00)")

        triggers = [j.trigger for j in jobs]
        hours = set()
        for t in triggers:
            self.assertIsInstance(t, CronTrigger)
            for field in t.fields:
                if field.name == "hour":
                    hours.add(str(field))
        self.assertIn("11", hours)
        self.assertIn("12", hours)


if __name__ == "__main__":
    unittest.main()
