"""
Scheduler模块
提供全局scheduler实例访问
"""

_scheduler_instance = None
_signal_scheduler_instance = None

def set_scheduler(scheduler):
    """设置全局scheduler实例"""
    global _scheduler_instance
    _scheduler_instance = scheduler

def get_scheduler():
    """获取全局scheduler实例"""
    return _scheduler_instance

def set_signal_scheduler(scheduler):
    """设置全局signal scheduler实例"""
    global _signal_scheduler_instance
    _signal_scheduler_instance = scheduler

def get_signal_scheduler():
    """获取全局signal scheduler实例"""
    return _signal_scheduler_instance
