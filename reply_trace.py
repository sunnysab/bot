""" 回复轨迹：结构化记录每轮「触发原因 → 上下文 → 回复」。

一行一条 JSONL，用来给以后的效果评估（reply_effect）攒数据：
事后可以回放当时看到了什么、为什么开口、说了什么，再对照群里后续反应。

只记录真正走到模型的那一轮（被必要性门控拦下的不算，否则全是噪声）。
写日志失败绝不能影响聊天，所以整体吞掉异常。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from loguru import logger


@dataclass(frozen=True)
class TraceRecord:
    """ 一轮回复的完整快照。 """

    ts: float
    contact: str                # 会话 id（群 id 或 wxid）
    contact_name: str           # 会话显示名
    sender: str                 # 触发这轮的人
    trigger: str                # at / mention / private / proactive
    score: int                  # 必要性得分
    reasons: tuple[str, ...]
    transcript: str             # 送进模型的聊天记录
    reply: tuple[str, ...]      # 实际发出的内容，空 = 模型选择不发言
    latency_ms: int
    plan: str = ''              # Planner 的决策（两段式时才有）


class TraceLogger:
    """ 往 JSONL 追加回复轨迹。 """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def log(self, record: TraceRecord) -> None:
        try:
            with self.path.open('a', encoding='utf-8') as file:
                file.write(json.dumps(asdict(record), ensure_ascii=False) + '\n')
        except OSError as e:
            logger.warning(f'写回复轨迹失败 {self.path}: {e}')


def _self_check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'trace.jsonl'
        tracer = TraceLogger(path)
        tracer.log(TraceRecord(ts=1.0, contact='g@chatroom', contact_name='某群', sender='Alice',
                               trigger='proactive', score=65, reasons=('问句', '活跃=10条'),
                               transcript='[09-14 14:32] Alice: 这个怎么弄', reply=('这样弄',),
                               latency_ms=800))
        tracer.log(TraceRecord(ts=2.0, contact='g@chatroom', contact_name='某群', sender='Bob',
                               trigger='at', score=100, reasons=(), transcript='...', reply=(),
                               latency_ms=500))

        lines = path.read_text(encoding='utf-8').strip().split('\n')
        assert len(lines) == 2, lines
        first = json.loads(lines[0])
        assert first['trigger'] == 'proactive' and first['score'] == 65
        assert first['reasons'] == ['问句', '活跃=10条']
        assert first['reply'] == ['这样弄']
        assert json.loads(lines[1])['reply'] == []

    # 路径不可写时不能抛出去
    TraceLogger('/no/such/dir/trace.jsonl').log(
        TraceRecord(ts=1.0, contact='c', contact_name='n', sender='s', trigger='at', score=1,
                    reasons=(), transcript='t', reply=(), latency_ms=0))
    print('trace self-check ok')


if __name__ == '__main__':
    _self_check()
