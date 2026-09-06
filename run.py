"""
主启动脚本
统一入口，同时启动Flask和调度器
"""

import signal
import sys
import threading
import logging
import json
import os
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config.config_manager import ConfigManager
from web.app import app, init_app
from scheduler.task_scheduler import TaskScheduler
from scheduler.signal_scheduler import SignalScheduler
from scheduler import set_scheduler, set_signal_scheduler

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('app.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


scheduler = None  # 全局调度器变量
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
INSTANCE_MARKER_PATH = Path(os.getenv("BOOL_REMINDER_INSTANCE_FILE", "data/active_instance.json"))


def register_active_instance():
    """标记当前进程为最新实例，旧实例的定时任务会自动跳过。"""
    INSTANCE_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "instance_id": INSTANCE_ID,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp_path = INSTANCE_MARKER_PATH.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_path.replace(INSTANCE_MARKER_PATH)
    logger.info(f"当前实例已登记为最新实例: {INSTANCE_ID}")


def is_active_instance() -> bool:
    """检查当前进程是否仍是最新实例。"""
    try:
        with INSTANCE_MARKER_PATH.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        active_instance_id = payload.get("instance_id")
        return not active_instance_id or active_instance_id == INSTANCE_ID
    except FileNotFoundError:
        return True
    except Exception as e:
        logger.warning(f"检查最新实例标记失败，继续执行当前任务: {e}")
        return True

def get_scheduler():
    """获取全局调度器实例，供web应用使用"""
    return scheduler

def signal_handler(sig, frame):
    """处理退出信号"""
    global scheduler
    logger.info("收到退出信号，正在关闭服务...")
    if scheduler is not None:
        scheduler.stop()
    sys.exit(0)


def run_flask_app():
    """在单独线程中运行Flask应用"""
    # 初始化Flask应用
    init_app()
    
    # 获取Web配置
    config_manager = ConfigManager()
    web_config = config_manager.get_web_config()
    
    app.run(
        host=web_config.get("host", "0.0.0.0"),
        port=web_config.get("port", 5000),
        debug=False,
        use_reloader=False  # 禁用reloader，避免多进程问题
    )


def main():
    """主函数"""
    global scheduler
    
    logger.info("=" * 60)
    logger.info("BOLL指标筛选系统启动中...")
    logger.info("=" * 60)
    
    # 初始化配置管理器
    config_manager = ConfigManager()
    
    # 检查配置
    lb_config = config_manager.get_longbridge_config()
    if not lb_config.get("app_key") or not lb_config.get("app_secret") or not lb_config.get("access_token"):
        logger.error("错误: LongBridge配置不完整，请检查config/config.yaml")
        logger.error("请复制 config/config.yaml.example 为 config/config.yaml 并填写配置")
        sys.exit(1)
    
    # 初始化Flask应用
    init_app()
    logger.info("Flask应用已初始化")

    # 标记当前进程为最新实例，防止旧容器/旧进程继续发送重复邮件
    register_active_instance()
    
    from notify.email_sender import EmailSender
    
    # 启动定时任务调度器
    scheduler = TaskScheduler(config_manager, instance_id=INSTANCE_ID, is_active_instance=is_active_instance)
    scheduler.start()
    # 设置全局scheduler实例，供web应用使用
    set_scheduler(scheduler)

    # 启动策略信号调度器 (每晚 23:30，北京时间)
    email_config = config_manager.get_email_config()
    smtp_host = email_config.get("smtp_host", "")
    if smtp_host:
        from_email = email_config.get("from_email", "")
        email_sender_instance = EmailSender(
            smtp_host=smtp_host,
            smtp_port=int(email_config.get("smtp_port", 587)),
            smtp_user=email_config.get("smtp_user", ""),
            smtp_password=email_config.get("smtp_password", ""),
            from_email=from_email,
        )
        signal_scheduler = SignalScheduler(
            email_sender=email_sender_instance,
            to_emails=email_config.get("to_emails", []),
        )
        signal_scheduler.start()
        set_signal_scheduler(signal_scheduler)
        logger.info("盘中策略信号调度器已启动 (23:30 Asia/Shanghai)")
    else:
        logger.warning("SMTP未配置，策略信号调度器未启动")
    
    next_run = scheduler.get_next_run_time()
    if next_run:
        # 如果next_run是UTC时间，需要转换为北京时间显示
        import pytz
        if next_run.tzinfo is None or next_run.tzinfo.utcoffset(next_run).total_seconds() == 0:
            # 如果是UTC时间，转换为北京时间
            beijing_tz = pytz.timezone('Asia/Shanghai')
            next_run_beijing = next_run.replace(tzinfo=pytz.UTC).astimezone(beijing_tz)
            logger.info(f"下次分析时间: {next_run_beijing.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
        else:
            logger.info(f"下次分析时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
    
    # 注册信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 在单独线程中启动Flask
    web_config = config_manager.get_web_config()
    flask_thread = threading.Thread(target=run_flask_app, daemon=True)
    flask_thread.start()
    
    logger.info(f"Flask Web服务已启动: http://{web_config.get('host', '0.0.0.0')}:{web_config.get('port', 5000)}")
    logger.info("=" * 60)
    logger.info("系统运行中，按 Ctrl+C 退出")
    logger.info("=" * 60)
    
    # 保持主线程运行
    try:
        flask_thread.join()
    except KeyboardInterrupt:
        signal_handler(None, None)


if __name__ == '__main__':
    main()
