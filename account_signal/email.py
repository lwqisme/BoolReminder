"""HTML email rendering and SMTP sending for account signals."""

from __future__ import annotations

import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Any

from config.config_manager import ConfigManager


def render_account_signal_email(run: dict[str, Any], signals: list[dict[str, Any]]) -> str:
    status = escape(str(run.get("status", "")))
    generated_at = escape(str(run.get("generated_at", "")))
    rows = "\n".join(_signal_row(signal) for signal in signals)
    if not rows:
        rows = '<tr><td colspan="7" class="empty">本次没有新的账户提醒。</td></tr>'
    errors = run.get("errors") if isinstance(run.get("errors"), list) else []
    error_block = ""
    if errors:
        error_block = "<div class=\"errors\">" + "<br>".join(escape(str(item)) for item in errors) + "</div>"
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ margin:0; padding:24px; background:#f4f1ea; color:#1f2933; font-family: Georgia, 'Times New Roman', serif; }}
    .wrap {{ max-width: 920px; margin:0 auto; background:#fffdf8; border:1px solid #d6c8ad; }}
    .head {{ padding:22px 24px; border-bottom:3px solid #111827; }}
    h1 {{ margin:0; font-size:28px; letter-spacing:0; }}
    .meta {{ margin-top:8px; color:#5f6b7a; font-size:13px; }}
    table {{ width:100%; border-collapse:collapse; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:13px; }}
    th {{ text-align:left; background:#1f2933; color:#fffdf8; padding:10px; }}
    td {{ padding:10px; border-top:1px solid #e4dccd; vertical-align:top; }}
    .buy {{ color:#0f766e; font-weight:700; }}
    .sell {{ color:#b42318; font-weight:700; }}
    .empty {{ text-align:center; color:#667085; }}
    .errors {{ margin:16px 24px; padding:12px; background:#fff1f0; border:1px solid #f2b8b5; color:#9f1c1c; }}
    .note {{ color:#667085; font-size:12px; margin-top:4px; font-family: Georgia, 'Times New Roman', serif; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="head">
      <h1>真实账户提醒</h1>
      <div class="meta">状态 {status} · 生成时间 {generated_at}</div>
    </div>
    {error_block}
    <table>
      <thead><tr><th>标的</th><th>动作</th><th>策略</th><th>阶段</th><th>价格</th><th>金额/股数</th><th>理由</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</body>
</html>"""


def send_account_signal_email(
    run: dict[str, Any],
    signals: list[dict[str, Any]],
    config_manager: ConfigManager | None = None,
) -> tuple[bool, str]:
    manager = config_manager or ConfigManager()
    email_config = manager.get_email_config()
    to_emails = email_config.get("to_emails") or []
    if not email_config.get("smtp_host") or not to_emails:
        return False, "SMTP 未配置或缺少收件人"

    subject = f"真实账户提醒 - {len(signals)} 个新信号"
    msg = MIMEMultipart("alternative")
    msg["From"] = email_config.get("from_email") or email_config.get("smtp_user")
    msg["To"] = ", ".join(to_emails)
    msg["Subject"] = subject
    msg.attach(MIMEText(render_account_signal_email(run, signals), "html", "utf-8"))

    try:
        port = int(email_config.get("smtp_port", 587) or 587)
        if port == 465:
            context = ssl.create_default_context()
            server = smtplib.SMTP_SSL(email_config["smtp_host"], port, context=context)
            try:
                if email_config.get("smtp_user"):
                    server.login(email_config.get("smtp_user"), email_config.get("smtp_password"))
                server.sendmail(msg["From"], to_emails, msg.as_string())
            finally:
                server.quit()
        else:
            context = ssl.create_default_context()
            server = smtplib.SMTP(email_config["smtp_host"], port)
            try:
                server.starttls(context=context)
                if email_config.get("smtp_user"):
                    server.login(email_config.get("smtp_user"), email_config.get("smtp_password"))
                server.sendmail(msg["From"], to_emails, msg.as_string())
            finally:
                server.quit()
    except Exception as exc:
        return False, f"邮件发送失败: {exc}"
    return True, "邮件已发送"


def _signal_row(signal: dict[str, Any]) -> str:
    action = escape(str(signal.get("action", "")))
    css = "buy" if action == "buy" else "sell"
    amount = signal.get("amount_usd")
    shares = signal.get("shares")
    size = f"${float(amount):,.2f}" if amount is not None else f"{float(shares or 0):,.4f} 股"
    reasons = "<br>".join(escape(str(item)) for item in signal.get("rationale", [])[:4])
    leaps = signal.get("leaps")
    if isinstance(leaps, dict) and leaps.get("enabled"):
        trigger_count = int(leaps.get("trigger_count") or 1)
        triggers = leaps.get("triggers") if isinstance(leaps.get("triggers"), list) else []
        trigger_stages = ", ".join(
            escape(str(item.get("stage", "")))
            for item in triggers
            if isinstance(item, dict) and item.get("stage")
        )
        trigger_text = f"，档位触发 {trigger_count} 次"
        if trigger_stages:
            trigger_text += f" ({trigger_stages})"
        reasons += (
            "<div class=\"note\">可人工检查 LEAPS: "
            f"DTE {escape(str(leaps.get('target_dte', '')))}, "
            f"买点 {escape(str(leaps.get('stock_entry', '')))}"
            f"{trigger_text}</div>"
        )
    return (
        "<tr>"
        f"<td>{escape(str(signal.get('symbol', '')))}</td>"
        f"<td class=\"{css}\">{action}</td>"
        f"<td>{escape(str(signal.get('strategy', '')))}</td>"
        f"<td>{escape(str(signal.get('stage', '')))}</td>"
        f"<td>{float(signal.get('price') or 0):,.2f}<div class=\"note\">回撤 {float(signal.get('drawdown_pct') or 0):.2f}%</div></td>"
        f"<td>{escape(size)}</td>"
        f"<td>{reasons}</td>"
        "</tr>"
    )
