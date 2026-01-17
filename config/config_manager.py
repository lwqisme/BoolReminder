"""
配置管理模块
从YAML文件读取配置，支持环境变量覆盖，提供token更新功能
"""

import os
import yaml
from typing import Dict, Any, Optional
from pathlib import Path


class ConfigManager:
    """配置管理器"""
    
    def __init__(self, config_path: Optional[str] = None):
        """
        初始化配置管理器
        
        Args:
            config_path: 配置文件路径，默认为 config/config.yaml
        """
        if config_path is None:
            # 默认配置文件路径
            base_dir = Path(__file__).parent.parent
            config_path = base_dir / "config" / "config.yaml"
        
        self.config_path = Path(config_path)
        self.config: Dict[str, Any] = {}
        self.load_config()
    
    def load_config(self) -> None:
        """从文件加载配置，支持环境变量覆盖"""
        # 如果配置文件不存在，使用默认配置
        if not self.config_path.exists():
            self.config = self._get_default_config()
            return
        
        # 读取YAML文件
        with open(self.config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f) or {}
        
        # 应用环境变量覆盖
        self._apply_env_overrides()
    
    def _get_default_config(self) -> Dict[str, Any]:
        """获取默认配置"""
        return {
            "longbridge": {
                "app_key": "",
                "app_secret": "",
                "access_token": ""
            },
            "email": {
                "smtp_host": "",
                "smtp_port": 587,
                "smtp_user": "",
                "smtp_password": "",
                "from_email": "",
                "to_emails": []
            },
            "web": {
                "host": "0.0.0.0",
                "port": 5000,
                "secret_key": "",
                "update_password": ""
            },
            "schedule": {
                "timezone": "Asia/Shanghai",
                "hour": 11,
                "minute": 0
            }
        }
    
    def _apply_env_overrides(self) -> None:
        """应用环境变量覆盖"""
        # LongBridge配置
        if os.getenv("LONGBRIDGE_APP_KEY"):
            self.config.setdefault("longbridge", {})["app_key"] = os.getenv("LONGBRIDGE_APP_KEY")
        if os.getenv("LONGBRIDGE_APP_SECRET"):
            self.config.setdefault("longbridge", {})["app_secret"] = os.getenv("LONGBRIDGE_APP_SECRET")
        if os.getenv("LONGBRIDGE_ACCESS_TOKEN"):
            self.config.setdefault("longbridge", {})["access_token"] = os.getenv("LONGBRIDGE_ACCESS_TOKEN")
        
        # 邮件配置
        if os.getenv("SMTP_HOST"):
            self.config.setdefault("email", {})["smtp_host"] = os.getenv("SMTP_HOST")
        if os.getenv("SMTP_PORT"):
            self.config.setdefault("email", {})["smtp_port"] = int(os.getenv("SMTP_PORT"))
        if os.getenv("SMTP_USER"):
            self.config.setdefault("email", {})["smtp_user"] = os.getenv("SMTP_USER")
        if os.getenv("SMTP_PASSWORD"):
            self.config.setdefault("email", {})["smtp_password"] = os.getenv("SMTP_PASSWORD")
        if os.getenv("FROM_EMAIL"):
            self.config.setdefault("email", {})["from_email"] = os.getenv("FROM_EMAIL")
        if os.getenv("TO_EMAILS"):
            self.config.setdefault("email", {})["to_emails"] = os.getenv("TO_EMAILS").split(",")
        
        # Web配置
        if os.getenv("WEB_HOST"):
            self.config.setdefault("web", {})["host"] = os.getenv("WEB_HOST")
        if os.getenv("WEB_PORT"):
            self.config.setdefault("web", {})["port"] = int(os.getenv("WEB_PORT"))
        if os.getenv("WEB_SECRET_KEY"):
            self.config.setdefault("web", {})["secret_key"] = os.getenv("WEB_SECRET_KEY")
        if os.getenv("UPDATE_PASSWORD"):
            self.config.setdefault("web", {})["update_password"] = os.getenv("UPDATE_PASSWORD")
    
    def get(self, key_path: str, default: Any = None) -> Any:
        """
        获取配置值，支持点号分隔的路径
        
        Args:
            key_path: 配置路径，如 "longbridge.app_key"
            default: 默认值
        
        Returns:
            配置值
        """
        keys = key_path.split(".")
        value = self.config
        
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
                if value is None:
                    return default
            else:
                return default
        
        return value if value is not None else default
    
    def update_token(self, access_token: str) -> bool:
        """
        更新access_token
        
        Args:
            access_token: 新的token
        
        Returns:
            是否更新成功
        """
        try:
            if "longbridge" not in self.config:
                self.config["longbridge"] = {}
            
            self.config["longbridge"]["access_token"] = access_token
            self.save_config()
            return True
        except Exception as e:
            print(f"更新token失败: {e}")
            return False
    
    def save_config(self) -> None:
        """保存配置到文件"""
        # 确保目录存在
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 保存到文件
        with open(self.config_path, 'w', encoding='utf-8') as f:
            yaml.dump(self.config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    
    def get_longbridge_config(self) -> Dict[str, str]:
        """获取LongBridge配置"""
        lb_config = self.get("longbridge", {})
        return {
            "app_key": lb_config.get("app_key", ""),
            "app_secret": lb_config.get("app_secret", ""),
            "access_token": lb_config.get("access_token", "")
        }
    
    def get_email_config(self) -> Dict[str, Any]:
        """获取邮件配置"""
        email_config = self.get("email", {})
        return {
            "smtp_host": email_config.get("smtp_host", ""),
            "smtp_port": email_config.get("smtp_port", 587),
            "smtp_user": email_config.get("smtp_user", ""),
            "smtp_password": email_config.get("smtp_password", ""),
            "from_email": email_config.get("from_email", ""),
            "to_emails": email_config.get("to_emails", [])
        }
    
    def get_web_config(self) -> Dict[str, Any]:
        """获取Web配置"""
        web_config = self.get("web", {})
        return {
            "host": web_config.get("host", "0.0.0.0"),
            "port": web_config.get("port", 5000),
            "secret_key": web_config.get("secret_key", ""),
            "update_password": web_config.get("update_password", "")
        }
    
    def get_schedule_config(self) -> Dict[str, Any]:
        """获取调度配置"""
        schedule_config = self.get("schedule", {})
        return {
            "timezone": schedule_config.get("timezone", "Asia/Shanghai"),
            "hour": schedule_config.get("hour", 11),
            "minute": schedule_config.get("minute", 0)
        }
    
    def get_report_cleanup_config(self) -> Dict[str, Any]:
        """获取报告清理配置"""
        cleanup_config = self.get("report_cleanup", {})
        return {
            "enabled": cleanup_config.get("enabled", True),
            "keep_days": cleanup_config.get("keep_days", 30),
            "keep_count": cleanup_config.get("keep_count", 100)
        }
    
    def update_schedule_config(self, hour: int, minute: int, timezone: str = "Asia/Shanghai") -> bool:
        """
        更新定时任务配置
        
        Args:
            hour: 执行小时（0-23）
            minute: 执行分钟（0-59）
            timezone: 时区，默认Asia/Shanghai
        
        Returns:
            是否更新成功
        """
        try:
            if "schedule" not in self.config:
                self.config["schedule"] = {}
            
            self.config["schedule"]["hour"] = hour
            self.config["schedule"]["minute"] = minute
            self.config["schedule"]["timezone"] = timezone
            self.save_config()
            return True
        except Exception as e:
            print(f"更新定时任务配置失败: {e}")
            return False