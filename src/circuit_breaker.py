# -*- coding: utf-8 -*-
"""熔断器 —— 三态状态机（模块 5）

下游（后端）连续失败 → 快速失败（不调坏的下游，保护自己）→ 冷却后半开试探 → 恢复。

三态：
  closed（正常，直接放行）
  open（连续失败达阈值，快速失败，不真调下游）
  half_open（冷却到后放一个试探请求，成功回 closed，失败回 open）

失败判定（调用方自己判断后调 record_*，本类不判断「什么算失败」）：
  算失败：ConnectError / Timeout / HTTP 5xx / 200 但响应垃圾（解析失败/缺字段）
  不算失败（record_success）：HTTP 4xx（业务正常响应——订单不存在/参数非法/409/429）

原子性：allow()/record_*() 无锁，安全仅因当前所有 HTTP 工具是纯 async、在事件循环里
执行、不进 to_thread（search_products 才走 to_thread）。allow() 同步无 await → 状态机
转换原子。半开单飞靠状态机转换天然保证：第一个请求转 half_open 放行，await 期间其他
协程看到 half_open 被拒，单探针在飞。若将来把 HTTP 工具丢 to_thread，无锁假设崩；
多进程部署每个进程一个熔断器，半开单飞退化为多探针——demo 单进程无此问题。

计时用 time.monotonic()（单调时钟，不受系统时间调整影响）。
"""

import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class CircuitBreaker:
    """三态熔断器。demo 单后端一个全局实例；生产按下游服务粒度分（订单/物流/库存各一个）。"""

    def __init__(self, fail_threshold: int = 5, cooldown: float = 30.0):
        self.fail_threshold = fail_threshold  # 连续失败多少次打开
        self.cooldown = cooldown  # 打开后多久进入半开（秒）
        self._fail_count = 0  # closed 状态下连续失败计数
        self._state = "closed"  # closed / open / half_open
        self._opened_at = 0.0  # 打开时刻（monotonic）

    def allow(self) -> bool:
        """是否允许发起调用（同步无 await，asyncio 单线程下原子）。

        open 且冷却到 → 转 half_open 放行（单探针）；half_open 已在飞 → 拒；closed → 放行。
        """
        if self._state == "closed":
            return True
        if self._state == "open":
            if time.monotonic() - self._opened_at >= self.cooldown:
                self._state = "half_open"
                return True
            return False
        # half_open：试探请求已放行，在结果回来前不再放行
        return False

    def record_success(self):
        """调用成功（含 4xx 业务正常响应）——重置失败计数（连续失败语义），半开试探成功回 closed。

        为什么成功必须重置（连续失败 vs 累计失败）：若成功不重置，失败次数会跨「恢复期」累加，
        后端早就恢复、偶发几次失败仍会累计到阈值误熔断。熔断本意是「下游近期持续不可用」才触发，
        不是「历史累计失败」，所以成功即清零。

        4xx 必须走这里而不是 no-op：否则「失败失败 404 失败」会被 404 打断连续计数，
        导致熔断永远不触发。4xx 语义上后端健康，应重置连续失败计数。
        """
        self._fail_count = 0
        if self._state == "half_open":
            self._state = "closed"

    def record_failure(self):
        """调用失败——closed 下计数达阈值打开；half_open 试探失败重新打开。"""
        self._fail_count += 1
        if self._state == "half_open":
            self._state = "open"
            self._opened_at = time.monotonic()
            self._fail_count = 0
        elif self._fail_count >= self.fail_threshold:
            self._state = "open"
            self._opened_at = time.monotonic()
            self._fail_count = 0

    def reset(self):
        """重置熔断器到初始 closed 状态（评测/故障注入后清理用，避免注入超时把熔断器打挂污染后续请求）"""
        self._fail_count = 0
        self._state = "closed"
        self._opened_at = 0.0

    @property
    def state(self) -> str:
        return self._state
