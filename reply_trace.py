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
    # 触发这一轮的微信消息 ID（relay 的 server_id）。管理后台的「为什么没回」
    # 抽屉就靠它把气泡和这一轮对上；老记录没有这个字段，读出来是空串
    msg_id: str = ''


class TraceLogger:
    """ 往 JSONL 追加回复轨迹，超过 max_bytes 就转存成 .1。 """

    def __init__(self, path: str | Path, max_bytes: int = 8 * 1024 * 1024):
        self.path = Path(path)
        self.max_bytes = max_bytes

    def log(self, record: TraceRecord) -> None:
        try:
            self._rotate()
            with self.path.open('a', encoding='utf-8') as file:
                file.write(json.dumps(asdict(record), ensure_ascii=False) + '\n')
        except OSError as e:
            logger.warning(f'写回复轨迹失败 {self.path}: {e}')

    def _rotate(self) -> None:
        # ponytail: 只留一份 .1，够查最近的问题就行；真要长期归档再说
        if self.max_bytes > 0 and self.path.exists() and self.path.stat().st_size >= self.max_bytes:
            self.path.replace(self.path.parent / (self.path.name + '.1'))


def find(path: str | Path, msg_id) -> dict | None:
    """ 按 msg_id 找一条轨迹，最新的一份和它的备份都找。

    只看这两份（8MB 上限），整体读进来倒着扫一遍就够了。
    老记录没有 msg_id 字段，扫不到就返回 None——面板会显示「无 trace」，
    因为被门控拦下的轮次本来就走不到模型。
    """
    target = '' if msg_id in (None, '') else str(msg_id)
    if not target:
        return None
    for candidate in (Path(path), Path(f'{path}.1')):
        if not candidate.exists():
            continue
        try:
            lines = candidate.read_text(encoding='utf-8').splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if str(record.get('msg_id') or '') == target:
                return record
    return None


def _self_check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'trace.jsonl'
        tracer = TraceLogger(path)
        tracer.log(TraceRecord(ts=1.0, contact='g@chatroom', contact_name='某群', sender='Alice',
                               trigger='proactive', score=65, reasons=('问句', '活跃=10条'),
                               transcript='[09-14 14:32] Alice: 这个怎么弄', reply=('这样弄',),
                               latency_ms=800, msg_id='7100000000000000123'))
        tracer.log(TraceRecord(ts=2.0, contact='g@chatroom', contact_name='某群', sender='Bob',
                               trigger='at', score=100, reasons=(), transcript='...', reply=(),
                               latency_ms=500))

        # 64 位 msg_id 存下来要能原样找回
        hit = find(path, 7100000000000000123)
        assert hit and hit['reply'] == ['这样弄'], hit
        assert find(path, '7100000000000000123') == hit
        assert find(path, '7100000000000000124') is None

        lines = path.read_text(encoding='utf-8').strip().split('\n')
        assert len(lines) == 2, lines
        first = json.loads(lines[0])
        assert first['trigger'] == 'proactive' and first['score'] == 65
        assert first['reasons'] == ['问句', '活跃=10条']
        assert first['reply'] == ['这样弄']
        assert json.loads(lines[1])['reply'] == []

        # 按 msg_id 找：抽屉靠它把气泡和这一轮对上
        assert find(path, '') is None and find(path, None) is None
        assert find(path, '没这个') is None
        assert find('/no/such/dir/trace.jsonl', '1') is None

        # 超过上限就转存，新的从头写
        rotated = Path(tmp) / 'big.jsonl'
        roller = TraceLogger(rotated, max_bytes=100)
        for i in range(5):
            roller.log(TraceRecord(ts=float(i), contact='c', contact_name='n', sender='s',
                                   trigger='at', score=1, reasons=(), transcript='t' * 50,
                                   reply=(), latency_ms=0))
        assert Path(tmp, 'big.jsonl.1').exists()
        assert len(rotated.read_text(encoding='utf-8').strip().split('\n')) == 1

    # 路径不可写时不能抛出去
    TraceLogger('/no/such/dir/trace.jsonl').log(
        TraceRecord(ts=1.0, contact='c', contact_name='n', sender='s', trigger='at', score=1,
                    reasons=(), transcript='t', reply=(), latency_ms=0))
    print('trace self-check ok')

if __name__ == '__main__':
    _self_check()
