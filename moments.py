""" 中期记忆：把滑出上下文窗口的消息压成「聊天回想」，需要时按关键词回忆起来。

短期上下文只有 100 条（ChatWindow.max_history），再往前的会被挤掉。这里在挤掉的
那一刻把它攒起来，够一批就交给模型压成 `summary + cues` 存进 sqlite；回复时按当前
消息的关键词去捞，命中才注入 prompt。

和 MaiBot 的 mid_term 是同一个思路。存储用 stdlib sqlite3，不加依赖。
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from ai import AiProvider
from context import SingleRecord, _format_time

# 压缩用的提示词：强调「不许编」，因为记忆失真比没有记忆更糟
SUMMARY_PROMPT = '''请把下面这段群聊压缩成一条「聊天回想」，供以后回忆。

要求：
1. 只根据给定消息总结，绝对不要编造没有出现的信息。
2. summary 保留话题脉络、人物立场、已达成的结论、待办和重要细节，一段话即可。
3. cues 输出 3-5 句「什么情景下会需要这段信息」的自然语言短句，用于以后匹配。
4. 只输出一个 JSON 对象，不要 Markdown 代码块，格式必须是 {"summary":"...","cues":["...","..."]}。'''

BRACKET = re.compile(r'\[[^\]]{0,16}\]')
SPLIT = re.compile(r'[，。！？!?~～…、,;；:：\s"\'“”‘’()（）\[\]【】]+')
STOPWORDS = frozenset((
    '什么', '这个', '那个', '怎么', '不是', '没有', '可以', '一个', '自己', '就是',
    '这样', '这里', '时候', '东西', '问题', '还是', '因为', '所以', '但是', '然后',
    '我们', '你们', '他们', '现在', '今天', '明天', '昨天', '真的', '感觉', '应该'))

DEFAULT_BATCH = 20
DEFAULT_TIMEOUT = 60.0


def extract_keywords(text: str, limit: int = 8) -> list[str]:
    """ 粗粒度关键词：按标点切短句，长句再切成 2 字滑窗。

    中文没分词器，这里用「短句 + 二元组」近似召回。
    # ponytail: 粗召回，个人群聊够用；漏召回了再上分词或 embedding
    """
    cleaned = BRACKET.sub('', text or '')
    keywords: list[str] = []
    for part in SPLIT.split(cleaned):
        part = part.strip()
        if len(part) < 2 or part in STOPWORDS:
            continue
        pieces = [part] if len(part) <= 4 else [part[i:i + 2] for i in range(len(part) - 1)]
        for piece in pieces:
            if piece not in STOPWORDS and piece not in keywords:
                keywords.append(piece)
    return keywords[:limit]


@dataclass(frozen=True)
class Moment:
    """ 一条聊天回想。 """

    chat: str
    t_start: int
    t_end: int
    summary: str
    cues: tuple[str, ...] = ()

    def render(self) -> str:
        return f'（{_format_time(self.t_end)}）{self.summary}'


def parse_summary(raw: str) -> tuple[str, tuple[str, ...]]:
    """ 解析模型输出的 JSON；解析不了就整段当 summary，不丢信息。 """
    text = raw.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\s*|\s*```$', '', text).strip()
    try:
        data = json.loads(text)
    except ValueError:
        return raw.strip(), ()
    if not isinstance(data, dict):
        return raw.strip(), ()
    summary = str(data.get('summary') or '').strip()
    cues = data.get('cues')
    cue_list = tuple(str(cue).strip() for cue in cues if str(cue).strip()) if isinstance(cues, list) else ()
    return (summary or raw.strip()), cue_list


def transcript(records: Sequence[SingleRecord]) -> str:
    """ 把滑出的消息拼成给模型看的文本。 """
    lines = []
    for record in records:
        who = '我' if record.is_self else record.sender
        lines.append(f'[{_format_time(record.t)}] {who}: {record.text}')
    return '\n'.join(lines)


class MomentStore:
    """ 中期记忆的 sqlite 存储。 """

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.execute('''CREATE TABLE IF NOT EXISTS moments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat TEXT NOT NULL,
            t_start INTEGER NOT NULL,
            t_end INTEGER NOT NULL,
            summary TEXT NOT NULL,
            cues TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL)''')
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_moments_chat_end ON moments(chat, t_end DESC)')
        self._conn.commit()

    def __len__(self) -> int:
        return self._conn.execute('SELECT COUNT(*) FROM moments').fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    def last_end(self, chat: str) -> int:
        """ 这个会话已经记住的最后一刻；用于跳过重启后重复淘汰的老消息。 """
        row = self._conn.execute('SELECT MAX(t_end) FROM moments WHERE chat=?', (chat,)).fetchone()
        return row[0] or 0

    def add(self, moment: Moment) -> None:
        self._conn.execute(
            'INSERT INTO moments(chat, t_start, t_end, summary, cues, created_at) VALUES (?,?,?,?,?,?)',
            (moment.chat, moment.t_start, moment.t_end, moment.summary, '\n'.join(moment.cues), int(time.time())))
        self._conn.commit()

    def search(self, chat: str, keywords: Sequence[str], limit: int = 2) -> list[Moment]:
        """ 按关键词捞回想，命中词多的优先，其次取新的。 """
        keywords = [keyword for keyword in keywords if keyword]
        if not keywords:
            return []
        where = ' OR '.join(['summary LIKE ? OR cues LIKE ?'] * len(keywords))
        params: list[object] = [chat]
        for keyword in keywords:
            params.extend((f'%{keyword}%', f'%{keyword}%'))

        rows = self._conn.execute(
            f'SELECT t_start, t_end, summary, cues FROM moments WHERE chat=? AND ({where}) '
            'ORDER BY t_end DESC LIMIT 50', params).fetchall()

        scored = []
        for t_start, t_end, summary, cues in rows:
            blob = summary + cues
            hits = sum(1 for keyword in keywords if keyword in blob)
            scored.append((hits, t_end, t_start, summary, cues))
        scored.sort(key=lambda item: (-item[0], -item[1]))

        return [Moment(chat=chat, t_start=row[2], t_end=row[1], summary=row[3],
                       cues=tuple(cue for cue in row[4].split('\n') if cue))
                for row in scored[:limit]]


class MomentKeeper:
    """ 攒够一批滑出窗口的消息就压成一条回想。 """

    def __init__(self, ai: AiProvider, store: MomentStore, batch: int = DEFAULT_BATCH,
                 timeout: float = DEFAULT_TIMEOUT, max_tokens: int = 400):
        self.ai = ai
        self.store = store
        self.batch = batch
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._buffers: dict[str, list[SingleRecord]] = defaultdict(list)
        self._tasks: set[asyncio.Task] = set()

    def schedule(self, chat: str, record: SingleRecord) -> None:
        """ 记录一条刚滑出窗口的消息。 """
        buffer = self._buffers[chat]
        buffer.append(record)
        if len(buffer) >= self.batch:
            batch, self._buffers[chat] = list(buffer), []
            self._spawn(self._compress(chat, batch))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _compress(self, chat: str, records: list[SingleRecord]) -> None:
        t_end = records[-1].t
        if t_end <= self.store.last_end(chat):  # 重启后重新淘汰的老消息，不要重复记
            return
        try:
            raw = await asyncio.wait_for(self.ai.summarize(SUMMARY_PROMPT, transcript(records),
                                                           max_tokens=self.max_tokens), self.timeout)
        except Exception as e:  # 压缩失败不能影响聊天，丢一批就算了
            logger.warning(f'压缩聊天回想失败 {chat}: {e}')
            return
        if not raw:
            return
        summary, cues = parse_summary(raw)
        self.store.add(Moment(chat=chat, t_start=records[0].t, t_end=t_end, summary=summary, cues=cues))
        logger.debug(f'已记住 {chat} 的 {len(records)} 条消息: {summary[:40]}')

    async def join(self) -> None:
        """ 等待当前在跑的压缩任务结束（关闭时用）。 """
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()


def _self_check() -> None:
    import tempfile

    assert extract_keywords('这个怎么弄啊') == ['这个怎么弄啊'] or '怎么' not in extract_keywords('这个怎么弄啊')
    assert '上班' in extract_keywords('我明天还要去上班，烦死了')
    assert extract_keywords('') == []

    summary, cues = parse_summary('{"summary":"在聊加班","cues":["提到加班时"]}')
    assert summary == '在聊加班' and cues == ('提到加班时',)
    assert parse_summary('```json\n{"summary":"x"}\n```')[0] == 'x'
    assert parse_summary('模型胡言乱语')[0] == '模型胡言乱语'

    with tempfile.TemporaryDirectory() as tmp:
        store = MomentStore(Path(tmp) / 'moments.db')
        store.add(Moment('g', 1, 10, '大家在聊明天上班要不要请假', ('提到请假时',)))
        store.add(Moment('g', 11, 20, '讨论了周末去看电影', ('提到电影时',)))
        store.add(Moment('other', 1, 10, '别群的上班话题', ()))

        assert store.last_end('g') == 20
        assert store.last_end('nobody') == 0
        assert len(store) == 3

        found = store.search('g', extract_keywords('明天上班怎么办'))
        assert found and '上班' in found[0].summary, found
        assert store.search('g', extract_keywords('完全不相关的内容')) == []
        assert store.search('g', []) == []
        assert [m.summary for m in store.search('other', ['上班'])] == ['别群的上班话题']  # 不串群
        store.close()

    print('moments self-check ok')


if __name__ == '__main__':
    _self_check()
