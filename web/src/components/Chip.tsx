import { GROUP, LABEL, SPIN } from '../status';

/** 一条消息的状态章。status 为空时什么都不画——省得留一个空壳 */
export function Chip({ status, detail }: { status?: string; detail?: string }) {
  if (!status) return null;
  return (
    <>
      <span className={`chip ${GROUP[status] ?? ''}`}>
        {SPIN.has(status) ? <><span className="spin">◌</span>{' '}</> : null}
        {LABEL[status] ?? status}
      </span>
      {detail ? <span className="detail">{detail}</span> : null}
    </>
  );
}
