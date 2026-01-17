"""
Scheduler模块
提供全局scheduler实例访问
"""

_scheduler_instance = None

def set_scheduler(scheduler):
    """设置全局scheduler实例"""
    global _scheduler_instance
    _scheduler_instance = scheduler

def get_scheduler():
    """获取全局scheduler实例"""
    return _scheduler_instance
