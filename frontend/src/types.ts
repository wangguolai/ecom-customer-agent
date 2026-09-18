/** 检索命中的商品图（后端随 SSE 单独下发，不进 LLM 文本） */
export interface ProductImage {
  image: string
  title?: string
  intro?: string
}

export interface Message {
  id: number
  role: 'user' | 'assistant'
  text: string
  images: ProductImage[]
}

/** SSE 单条事件体：{ delta } 逐 token 文本 / { images } 本轮命中商品图 */
export interface StreamEvent {
  delta?: string
  images?: ProductImage[]
}

/** 伪菜单项：点一下把 text 作为意图句直接发出去 */
export interface MenuItem {
  label: string
  text: string
}
