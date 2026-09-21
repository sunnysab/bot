/** 状态 → 中文 / 颜色分组。
 *
 * 这两张表必须覆盖 events.py 的每一个 Status，否则气泡上会显示裸状态名（`express_failed`）。
 * admin.py 的自检会读这个文件断言覆盖率——新加状态忘了在这里登记，自检就红。
 * 分组名对应 admin.css 里的 `.chip.<group>`。
 */

export const LABEL: Record<string, string> = {
  received: '已接收', stale: '积压·只记不回', phone: '手机发的', muted: '已静音',
  gated: '跳过·门控', backoff: '跳过·退避', rate_limited: '跳过·太频繁',
  deciding: '决策中', planner_silent: '不发言', planner_failed: '决策调用失败',
  expressing: '生成回复中', express_failed: '表达失败', empty_reply: '回复为空',
  placeholder_blocked: '占位符已拦', manual: '手动触发',
  queued: '已排队', typing: '输入中',
  sent: '已回复', dry_run: '演习回复', failed: '发送报错',
};

export const GROUP: Record<string, string> = {
  received: 'pending', deciding: 'pending', expressing: 'pending', queued: 'pending',
  typing: 'pending', manual: 'pending',
  sent: 'replied', phone: 'phone', dry_run: 'dry_run',
  stale: 'skipped', muted: 'skipped', gated: 'skipped', backoff: 'skipped',
  rate_limited: 'skipped',
  planner_silent: 'skipped', empty_reply: 'skipped', placeholder_blocked: 'skipped',
  planner_failed: 'warn', express_failed: 'failed', failed: 'failed',
};

/** 还在进行中的状态：chip 上带一个转圈，让「卡住了」和「在跑」分得开 */
export const SPIN = new Set(['deciding', 'expressing', 'queued', 'typing']);
