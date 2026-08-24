"""
collectors/base.py
所有城市采集器的通用抽象基类与接口契约
"""
from abc import ABC, abstractmethod
from typing import List, Optional
import logging
from core.models import PolicyItem, PolicyCategory
from core.requester import BaseRequester, default_requester

logger = logging.getLogger("YouthPolicyAlert.Collector")


class BaseCollector(ABC):
    """
    城市采集器抽象基类
    每个自定义代码级采集器均继承自本类
    """
    # 插件元数据规范 (子类必须显式声明)
    city: str = ""                         # 负责的城市，如 "深圳"
    district: str = "全市"                 # 负责的区县，如 "南山区"
    source_name: str = ""                  # 来源部门，如 "深圳市住建局"
    category: PolicyCategory = PolicyCategory.OTHER
    target_url: str = ""                   # 目标网址

    def __init__(self, requester: Optional[BaseRequester] = None):
        self.requester = requester or default_requester

    @abstractmethod
    def fetch(self) -> List[PolicyItem]:
        """
        核心抓取方法：由子类实现具体的页面抓取与解析逻辑。
        必须返回标准规范的 List[PolicyItem]
        """
        raise NotImplementedError("每个采集器必须实现 fetch() 方法")

    def health_check(self) -> bool:
        """
        健康检查接口（用于测试官方网站是否存活或改版）
        默认尝试抓取，无异常且能返回数据或正常结束即为健康
        """
        try:
            items = self.fetch()
            logger.info(f"[{self.city}-{self.source_name}] 健康检查通过，抓取到 {len(items)} 条数据")
            return True
        except Exception as e:
            logger.error(f"[{self.city}-{self.source_name}] 健康检查失败: {e}")
            return False
