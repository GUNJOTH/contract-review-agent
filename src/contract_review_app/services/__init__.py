"""异步任务服务导出。"""

from .task_dispatcher import TaskDispatchConfig, get_dispatch_config
from .task_service import TaskService, task_service

__all__ = [
    "TaskDispatchConfig",
    "TaskService",
    "get_dispatch_config",
    "task_service",
]
