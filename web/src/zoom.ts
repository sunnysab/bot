/** 图片查看器的缩放数学。纯函数，vitest 直接测。 */

export interface View {
  s: number;
  x: number;
  y: number;
}

export const RESET: View = { s: 1, x: 0, y: 0 };

/** 以光标为中心缩放：鼠标底下那一点在缩放前后必须落在同一个屏幕位置。
 *
 * view 对应 `translate(x,y) scale(s)`，(mx,my) 是光标，(cx,cy) 是容器中心。
 * 下限 1 = 适应窗口——再缩下去只是把图变小，没意义。
 */
export function zoomAt(view: View, k: number, mx: number, my: number, cx: number, cy: number): View {
  const s = Math.min(20, Math.max(1, view.s * k));
  const r = s / view.s;
  const u = mx - cx - view.x; // 光标相对「图片中心」的偏移
  const v = my - cy - view.y;
  return { s, x: view.x - u * (r - 1), y: view.y - v * (r - 1) };
}
