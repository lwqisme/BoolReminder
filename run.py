"""
主启动脚本
统一入口，同时启动Flask和调度器
"""

import signal
import sys
import threading
import logging
from pathlib import Path

from config.config_manager import ConfigManager
from web.app import app, init_app
from scheduler.task_scheduler import TaskScheduler

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
    
    # 启动定时任务调度器
    scheduler = TaskScheduler(config_manager)
    scheduler.start()
    
    next_run = scheduler.get_next_run_time()
    if next_run:
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
