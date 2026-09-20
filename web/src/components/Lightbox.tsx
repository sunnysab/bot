import { useEffect, useRef, useState } from 'react';

import { RESET, zoomAt } from '../zoom';
import type { View } from '../zoom';

/** 就地看原图：src 就是 /api/media 的原始字节，面板只把浏览器的缩放权交回来。
 *
 * 滚轮缩放以光标为锚点（wheel 必须 preventDefault，所以只能自己 addEventListener，
 * 不能上 React 的 onWheel——那个是被动监听）。
 */
export function Lightbox({ src, onClose }: { src: string; onClose: () => void }) {
  const [view, setView] = useState<View>(RESET);
  const [dragging, setDragging] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const img = useRef<HTMLImageElement>(null);
  const from = useRef({ x: 0, y: 0, view: RESET });

  // 每次打开都从头开始：同一张图再点一次也该是「适应窗口」
  useEffect(() => { setView(RESET); }, [src]);

  useEffect(() => {
    const el = img.current;
    if (!el || !src) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = box.current?.getBoundingClientRect();
      if (!r) return;
      setView(v => zoomAt(v, e.deltaY < 0 ? 1.2 : 1 / 1.2, e.clientX, e.clientY,
        r.left + r.width / 2, r.top + r.height / 2));
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [src]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  if (!src) return null;
  return (
    <div
      id="lightbox" ref={box}
      onClick={e => { if (e.target === e.currentTarget) onClose(); }}
    >
      <img
        id="lightbox-img" ref={img} src={src} alt="" draggable={false}
        className={dragging ? 'dragging' : ''}
        style={{ transform: `translate(${view.x}px,${view.y}px) scale(${view.s})` }}
        onDoubleClick={() => setView(RESET)}
        onPointerDown={e => {
          e.preventDefault();
          from.current = { x: e.clientX, y: e.clientY, view };
          setDragging(true);
          e.currentTarget.setPointerCapture(e.pointerId);
        }}
        onPointerMove={e => {
          if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
          const start = from.current;
          setView({
            ...start.view,
            x: start.view.x + e.clientX - start.x,
            y: start.view.y + e.clientY - start.y,
          });
        }}
        onPointerUp={() => setDragging(false)}
      />
      <span className="hint">滚轮缩放 · 拖动平移 · 双击还原 · Esc 关闭</span>
    </div>
  );
}
