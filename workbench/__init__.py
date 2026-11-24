"""司仪/礼宾协同工作台。

把报名名单、亲友留言、司仪稿、舞台时段与已分发卡片接成一条
可审计链路，覆盖指代消歧、同意管理、行动对账、跨岗更正与公开存档。
"""
from .workbench import Workbench

__all__ = ["Workbench"]
