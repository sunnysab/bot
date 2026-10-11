""" 用真实群聊记录回放中期记忆：压缩质量 + 关键词召回。

拿一段真实聊天，按 batch 切成连续窗口，模拟真实运行：

    for 每个窗口:
        把窗口压成一条回想存下来     ← 运行时是「消息滑出上下文窗口」触发
        拿下一个窗口的第一条当探针，看能不能捞回刚存的那条

**顺序很重要**：必须压一个、探一个。早期版本先把所有窗口压完再统一探测，
于是「命中多优先、其次取新」的排序会把正确答案（紧邻探针的那个窗口）排到最后。

## 这个工具能测什么、不能测什么

能测：
  - **覆盖率**：每条探针能不能捞出东西。运行时「一条也没捞到」就是没记忆。
     捞出来的相关性需要人看——下面每次都会打印出来。
  - **误召回率**：捞出来的回想里有多少根本不共享关键词（应当为 0，是 LIKE 的底线）。
  - **自召回**（只在真压缩模式下有意义）：拿某窗口里的一条当探针，
     看它自己那一窗的回想能不能排第一。真回想是改写过的话，不共享原文子串，
     所以这是真考验；离线模式存的是原文，白送分，所以不算。

不能测：「正确答案排第几」。窗口是固定 20 条切开的，而群里话题跳得很快，
相邻窗口经常本来就没关系——探针没捞到上一窗是**正确的沉默**。

用法：
  python replay_memory.py "有福同享（重生版）.jsonl" --offline
  python replay_memory.py "有福同享（重生版）.jsonl" --windows 6 --model deepseek-chat
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

from loguru import logger

from ai import OpenAI
from build_profiles import BRACKET, PLACEHOLDER, TEXT_TYPES, iter_records
from context import SingleRecord
from moments import Moment, MomentKeeper, MomentStore, extract_keywords

SKIP_HEAD = 3000        # 开头的入群寒暄没营养
SLICE = 600             # 取多少条文字消息做回放


def load_slice(path: str | Path, skip: int, size: int) -> list[SingleRecord]:
    """ 截一段连续的文字消息，跳过媒体/系统消息。 """
    records: list[SingleRecord] = []
    seen = 0
    for record in iter_records(path):
        if record.get('_type') != 'message' or record.get('type') not in TEXT_TYPES:
            continue
        content = str(record.get('content') or '').strip()
        if not content or PLACEHOLDER.match(content):
            continue
        content = BRACKET.sub('', content).strip()
        if not content:
            continue
        seen += 1
        if seen <= skip:
            continue
        sender = record.get('accountName') or record.get('sender') or ''
        records.append(SingleRecord(sender, content, int(record.get('timestamp') or 0)))
        if len(records) >= size:
            break
    return records


def chunks_of(records: list[SingleRecord], windows: int, batch: int) -> list[list[SingleRecord]]:
    return [records[i:i + batch] for i in range(0, len(records), batch)][:windows + 1]


async def replay(records: list[SingleRecord], windows: int, batch: int, model: str = '',
                 limit: int = 2, verbose: bool = True) -> None:
    """ 逐窗口压缩 + 探测，打印覆盖率和每次捞回来的东西。 """
    chunks = chunks_of(records, windows, batch)
    if len(chunks) < 2:
        print('消息太少，切不出两个窗口')
        return
    keeper = None
    if not model:
        print(f'离线模式：拿窗口原文当回想文本，共 {len(chunks)} 个窗口，每个 {batch} 条\n')
    else:
        from config import Config

        config = Config.load()
        ai = OpenAI(url=config.deepseek_url, key=config.deepseek_key, model=model, temperature=0.3)
        print(f'真压缩：{len(chunks)} 个窗口，每个 {batch} 条，模型 {model}\n')

    covered = total = 0
    with tempfile.TemporaryDirectory() as tmp:
        store = MomentStore(Path(tmp) / 'replay.db')
        keeper = MomentKeeper(ai, store, batch=batch) if model else None

        for index, chunk in enumerate(chunks[:-1]):
            # 1) 先压这一窗（运行时就是「消息滑出上下文窗口」时做的）
            if keeper:
                await keeper.compress('replay', chunk)
                stored = _summaries(store)[-1]
            else:
                # 离线代餐：窗口原文当回想文本。真实回想更短、还有 cues，所以这只是下界
                stored = '\n'.join(record.text for record in chunk)
                store.add(Moment('replay', chunk[0].t, chunk[-1].t, stored, ()))
            if verbose:
                print(f'--- 窗口 {index + 1}（{len(chunk)} 条）压成：{stored[:70]}')

            # 2) 再拿下一窗的第一条当探针
            probe = chunks[index + 1][0]
            keywords = extract_keywords(probe.text)
            found = store.search('replay', keywords, limit=limit)
            total += 1
            covered += bool(found)
            if verbose:
                print(f'    探针：{probe.text[:50]}')
                print(f'    关键词：{keywords}')
                for i, moment in enumerate(found):
                    # 捞回来的必须真的共享关键词，否则就是 LIKE 漏了
                    shared = [word for word in keywords if word in moment.summary + '\n'.join(moment.cues)]
                    mark = '✓' if shared else '✗误召回'
                    print(f'    {mark} 第{i + 1}名 {moment.summary[:60]}')
                if not found:
                    print('    – 没捞到（若探针本身没信息量，这就是对的）')
                print()

        self_recall = _self_recall(store, chunks, limit) if model else None
        store.close()

    if keeper:
        await keeper.stop()
    print(f'覆盖率（探针捞出≥1 条）：{covered}/{total} = {covered / max(total, 1):.0%}')
    print('  ↑ 低不等于召回差：群里每条消息经常就是新话题，捞不到才是对的。'
          '自己拿下面的输出看相关性。')
    if self_recall:
        print(f'自召回（该窗口的回想能进前 {limit} 名）：{self_recall}')


def _self_recall(store: MomentStore, chunks: list[list[SingleRecord]], limit: int) -> str:
    """ 拿每个窗口的末条当探针，看它自己那一窗的回想能进前几名。

    只在真压缩模式下有意义：回想是改写过的话，不共享探针的原文子串。
    离线模式存的就是原文，探针字面就在里面，测了也是白测，所以不调这个。
    """
    hits = 0
    total = 0
    for chunk in chunks[:-1]:
        if not chunk:
            continue
        total += 1
        found = store.search('replay', extract_keywords(chunk[-1].text), limit=limit)
        hits += any(moment.t_start == chunk[0].t for moment in found)
    return f'{hits}/{total}'


def _summaries(store: MomentStore) -> list[str]:
    rows = store._conn.execute('SELECT summary FROM moments ORDER BY id').fetchall()
    return [row[0] for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description='回放中期记忆')
    parser.add_argument('source', help='聊天记录 jsonl')
    parser.add_argument('--windows', type=int, default=8, help='压缩多少个窗口')
    parser.add_argument('--batch', type=int, default=20, help='每个窗口多少条消息')
    parser.add_argument('--limit', type=int, default=2, help='每次召回几条')
    parser.add_argument('--model', default='deepseek-chat', help='压缩用的模型')
    parser.add_argument('--offline', action='store_true', help='不调模型，只测检索这一半')
    parser.add_argument('--skip', type=int, default=SKIP_HEAD, help='跳过开头多少条文字消息')
    parser.add_argument('--quiet', action='store_true', help='只打最后的汇总')
    args = parser.parse_args()

    logger.remove()
    records = load_slice(args.source, args.skip, SLICE)
    print(f'截取 {len(records)} 条真实文字消息\n')
    asyncio.run(replay(records, args.windows, args.batch,
                       model='' if args.offline else args.model,
                       limit=args.limit, verbose=not args.quiet))


if __name__ == '__main__':
    main()
