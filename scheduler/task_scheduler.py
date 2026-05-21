"""
定时任务调度器
在指定时间自动运行分析并发送通知
"""

import logging
import sys
from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.config_manager import ConfigManager
from account_signal.config import get_runtime_config
from account_signal.engine import run_account_signal
from watchlist_boll_filter import run_analysis_and_notify
from scheduler.token_refresher import refresh_longbridge_token

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TaskScheduler:
    """定时任务调度器"""
    
    def __init__(self, config_manager: ConfigManager, instance_id: str = "", is_active_instance=None):
        """
        初始化调度器
        
        Args:
            config_manager: 配置管理器实例
            instance_id: 当前运行实例ID，用于排查重复实例
            is_active_instance: 回调函数，返回当前实例是否仍是最新实例
        """
        self.config_manager = config_manager
        self.instance_id = instance_id
        self.is_active_instance = is_active_instance
        self.scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Shanghai'))
        self._setup_job()
    
    def _setup_job(self):
        """设置定时任务"""
        schedule_config = self.config_manager.get_schedule_config()
        
        hour = schedule_config.get("hour", 11)
        minute = schedule_config.get("minute", 0)
        timezone_str = schedule_config.get("timezone", "Asia/Shanghai")
        tz = pytz.timezone(timezone_str)
        
        # 添加每天定时任务，明确指定时区
        self.scheduler.add_job(
            func=self._run_analysis_job,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=tz),
            id='daily_boll_analysis',
            name='每日BOLL指标分析',
            replace_existing=True
        )
        
        logger.info(f"定时任务已设置: 每天 {hour:02d}:{minute:02d} ({timezone_str}时区) 执行分析")
        self._setup_account_signal_jobs()

    def _setup_account_signal_jobs(self):
        """设置真实账户提醒任务。"""
        account_config = get_runtime_config(self.config_manager)
        if not account_config.enabled:
            logger.info("真实账户提醒定时任务未启用")
            return
        tz = pytz.timezone(account_config.timezone)
        for time_text in account_config.schedule_hours:
            try:
                hour_raw, minute_raw = str(time_text).split(":", 1)
                hour = int(hour_raw)
                minute = int(minute_raw)
            except (TypeError, ValueError):
                logger.warning("跳过无效 account_signal 调度时间: %s", time_text)
                continue
            self.scheduler.add_job(
                func=self._run_account_signal_job,
                trigger=CronTrigger(hour=hour, minute=minute, timezone=tz),
                id=f"account_signal_{hour:02d}_{minute:02d}",
                name=f"真实账户提醒 {hour:02d}:{minute:02d}",
                replace_existing=True,
            )
        logger.info("真实账户提醒定时任务已设置: %s (%s)", ", ".join(account_config.schedule_hours), account_config.timezone)
    
    def _run_analysis_job(self):
        """执行分析任务"""
        if self.is_active_instance and not self.is_active_instance():
            logger.warning(f"跳过定时分析任务: 当前实例不是最新实例 ({self.instance_id})")
            return

        logger.info("开始执行定时分析任务...")

        lb_config = self.config_manager.get_longbridge_config()
        client_id = lb_config.get("oauth_client_id", "")
        if client_id:
            if not refresh_longbridge_token(client_id):
                logger.warning("Token refresh failed, proceeding anyway")

        try:
            result = run_analysis_and_notify(
                config_manager=self.config_manager,
                send_email=True,  # 定时任务自动发送邮件
                save_html=True
            )
            
            if result:
                logger.info(f"分析完成: 找到 {result.total_found} 只需要关注的股票")
            else:
                logger.error("分析失败")
        except Exception as e:
            logger.error(f"执行分析任务时出错: {e}", exc_info=True)

    def _run_account_signal_job(self):
        """执行真实账户提醒任务。"""
        if self.is_active_instance and not self.is_active_instance():
            logger.warning(f"跳过真实账户提醒任务: 当前实例不是最新实例 ({self.instance_id})")
            return
        logger.info("开始执行真实账户提醒任务...")
        try:
            result = run_account_signal(
                config_manager=self.config_manager,
                dry_run=False,
                send_email=True,
                symbols=["GOOGL.US", "TSLA.US"],
                include_debug=False,
            )
            logger.info(
                "真实账户提醒完成: status=%s new_signals=%s email=%s",
                result.get("status"),
                len(result.get("new_signals", []) or []),
                (result.get("email") or {}).get("sent"),
            )
        except Exception as e:
            logger.error(f"执行真实账户提醒任务时出错: {e}", exc_info=True)
    
    def start(self):
        """启动调度器"""
        self.scheduler.start()
        logger.info("定时任务调度器已启动")
    
    def stop(self):
        """停止调度器"""
        self.scheduler.shutdown()
        logger.info("定时任务调度器已停止")
    
    def get_next_run_time(self):
        """获取下次运行时间"""
        job = self.scheduler.get_job('daily_boll_analysis')
        if job:
            return job.next_run_time
        return None
    
    def update_schedule(self, hour: int, minute: int, timezone: str = "Asia/Shanghai") -> bool:
        """
        动态更新定时任务时间
        
        Args:
            hour: 执行小时（0-23）
            minute: 执行分钟（0-59）
            timezone: 时区，默认Asia/Shanghai
        
        Returns:
            是否更新成功
        """
        try:
            tz = pytz.timezone(timezone)
            
            # 更新配置
            if self.config_manager.update_schedule_config(hour, minute, timezone):
                # 重新调度任务
                job = self.scheduler.get_job('daily_boll_analysis')
                if job:
                    # 使用reschedule更新触发器
                    new_trigger = CronTrigger(hour=hour, minute=minute, timezone=tz)
                    self.scheduler.reschedule_job('daily_boll_analysis', trigger=new_trigger)
                    logger.info(f"定时任务已更新: 每天 {hour:02d}:{minute:02d} ({timezone}时区) 执行分析")
                    return True
                else:
                    # 如果任务不存在，创建新任务
                    self.scheduler.add_job(
                        func=self._run_analysis_job,
                        trigger=CronTrigger(hour=hour, minute=minute, timezone=tz),
                        id='daily_boll_analysis',
                        name='每日BOLL指标分析',
                        replace_existing=True
                    )
                    logger.info(f"定时任务已创建: 每天 {hour:02d}:{minute:02d} ({timezone}时区) 执行分析")
                    return True
            return False
        except Exception as e:
            logger.error(f"更新定时任务失败: {e}", exc_info=True)
            return False
