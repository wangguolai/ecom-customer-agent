/** 检索命中的商品图（后端随 SSE 单独下发，不进 LLM 文本） */
export interface ProductImage {
  image: string
  title?: string
  intro?: string
}

/**
 * 执行步骤。后端只下发 `text`（见 src/agent.py 的 `("step", {...})`），
 * `done` 由前端推导：**只有最后一条是进行中**，流结束全部置完成。
 * ⚠️ 必须有终态，否则最后一步永久转圈——`streaming` 变 false 是唯一可靠的终点信号
 * （不能指望后端补发一个「结束」步骤，它不知道前端什么时候渲染完）。
 */
export interface Step {
  text: string
  done: boolean
}

export interface Message {
  id: number
  role: 'user' | 'assistant'
  text: string
  images: ProductImage[]
  /**
   * 本轮的 trace_id（SSE 首帧下发）。
   *
   * ⚠️ 必须挂在**消息**上，不能是组件级单值 state：
   * 多轮之后给**旧消息**打分时，单值 state 会把评分挂到**最新一轮**的 trace 上。
   * 那不是显示问题——后端按 trace_id 存反馈，挂错 trace 等于往
   * 「用来复现失败」的表里写别人的数据，整条 trace↔feedback 线就废了。
   */
  traceId?: string
  /** 已提交的评分（本地记住，避免重复提示「谢谢反馈」） */
  rated?: 1 | -1
}

/**
 * SSE 单条事件体。四种帧（与后端 main.py 的 event_gen 一一对应）：
 *   { trace_id }  首帧，前端存到当前消息上供评分用
 *   { step }      执行步骤
 *   { delta }     逐 token 文本
 *   { images }    本轮命中的商品图
 */
export interface StreamEvent {
  trace_id?: string
  delta?: string
  step?: { text: string }
  images?: ProductImage[]
}

/** 伪菜单项：点一下把 text 作为意图句直接发出去 */
export interface MenuItem {
  label: string
  text: string
  /**
   * 后端白名单里的**菜单 id**（`orders` / `logistics` / …），**不是工具名**。
   *
   * 服务端只拿它决定路由，**不信任 `text` 做路由判断**——`menu_intent` 与 `message`
   * 一样是客户端可控输入，传工具名等于开放任意工具调用入口。
   * id 的单一真源在 `src/config/rules.py` 的 `MENU_INTENTS`。
   */
  intent: string
}
