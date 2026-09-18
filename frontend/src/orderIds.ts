/**
 * 可测试的订单号 —— 供聊天页左侧「测试订单」栏使用。
 *
 * ⚠️ 这是 `src/backend/seed.py` 的 `_SEED_ORDERS` 的**硬编码副本**，改 seed 时要一起改。
 *
 * 为什么不从后端拉：聊天页的用户没有登录，拉订单列表需要一个**公开**接口——
 * 为了一个演示辅助列表新增公开数据面不划算（管理后台那套 /api/admin/* 有鉴权，这里用不了）。
 *
 * 订单号必须满足 `20\d{9}`（11 位、20 开头），否则 `intent_router` 的正则命中不了，
 * 会掉进 LLM ReAct（慢且不确定）。
 */
export interface TestOrder {
  id: string
  status: string
  /** 这个订单适合测什么（帮使用者挑样本） */
  hint: string
}

export const TEST_ORDERS: TestOrder[] = [
  { id: '20240818001', status: '已发货', hint: '有完整物流轨迹' },
  { id: '20240817002', status: '待付款', hint: '无物流（测「暂无物流」）' },
  { id: '20240816003', status: '已完成', hint: '已签收' },
  { id: '20240818004', status: '运输中', hint: '派送中' },
  { id: '20240818005', status: '已签收', hint: '三段轨迹' },
]
