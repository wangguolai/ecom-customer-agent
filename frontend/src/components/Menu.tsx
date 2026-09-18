import { MENU_ITEMS } from '../menuItems'
import type { MenuItem } from '../types'

interface Props {
  onPick: (item: MenuItem) => void
  onReset: () => void
  disabled: boolean
}

export function Menu({ onPick, onReset, disabled }: Props) {
  return (
    <div className="menu">
      <span className="menu-label">快捷：</span>
      {MENU_ITEMS.map((item) => (
        <button key={item.label} disabled={disabled} onClick={() => onPick(item)}>
          {item.label}
        </button>
      ))}
      <button
        className="menu-reset"
        disabled={disabled}
        onClick={onReset}
        title="清空对话，与后端历史断联后重开一个会话"
      >
        🔄 新会话
      </button>
    </div>
  )
}
