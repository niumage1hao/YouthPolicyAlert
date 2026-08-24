"""
notifiers/base.py
通知推送器统一基类
"""
from abc import ABC, abstractmethod
from typing import List

from core.models import PolicyItem


class BaseNotifier(ABC):
    """通知器统一抽象基类"""

    name: str = "notifier"

    @abstractmethod
    def send(self, items: List[PolicyItem]) -> bool:
        """
        推送一批政策通知。
        :return: True 表示确认送达；False 表示失败（调用方会安排下一轮重试）
        """
        raise NotImplementedError

    def send_plain(self, title: str, lines: List[str]) -> bool:
        """
        推送一条纯文本系统消息（启动确认、数据源失效告警等）。
        子类未实现时默认返回 False，不影响政策推送主流程。
        """
        return False
