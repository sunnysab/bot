""" 用真实群聊记录回放中期记忆：压缩质量 + 关键词召回率。

拿一段真实聊天，按 batch 切成连续窗口，每个窗口当成「刚滑出上下文」去压缩；
再用紧随其后的真实消息当探针，看能不能把相关的回想捞回来。

目的是在接入之前暴露「LIKE + 二元组」的召回率问题。

用法：
  python replay_memory.py "有福同享（重生版）.jsonl" [--windows 6] [--batch 20]

离线校验工具，不参与机器人运行。
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

from loguru import logger

from ai import Deepseek
from build_profiles import BRACKET, PLACEHOLDER, TEXT_TYPES, iter_records
from context import SingleRecord
from moments import MomentKeeper, MomentStore, extract_keywords

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


async def replay(records: list[SingleRecord], windows: int, batch: int, model: str) -> None:
    import os

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / '.env')
    ai = Deepseek(key=os.getenv('DEEPSEEK_KEY', ''), model=model, temperature=0.3)

    chunks = [records[i:i + batch] for i in range(0, len(records), batch)][:windows + 1]
    print(f'共 {len(chunks)} 个窗口，每个 {batch} 条\n')

    with tempfile.TemporaryDirectory() as tmp:
        store = MomentStore(Path(tmp) / 'replay.db')
        keeper = MomentKeeper(ai, store, batch=batch)

        for index, chunk in enumerate(chunks):
            await keeper.compress('replay', chunk)
            summaries = _summaries(store)
            if index < len(summaries):
                print(f'--- 窗口 {index + 1}（{chunk[0].t} 起 {len(chunk)} 条）压成：')
                print(f'    {summaries[index]}')
                print(f'    原文开头：{chunk[0].text[:60]}')
            print()

        print('=' * 72)
        print('召回测试：拿每个窗口的第一条当探针（它紧跟在上一段之后），看捞回什么\n')

        hits = 0
        for index, chunk in enumerate(chunks[1:], start=1):
            probe = chunk[0].text
            keywords = extract_keywords(probe)
            found = store.search('replay', keywords, limit=2)
            if found:
                hits += 1
            print(f'探针[{index}] {probe[:50]}')
            print(f'   关键词：{keywords}')
            for moment in found:
                print(f'   → 命中 {moment.summary[:70]}')
            if not found:
                print('   → （没捞到）')
            print()

        total = len(chunks) - 1
        print(f'召回率：{hits}/{total} = {hits / max(total, 1):.0%}')
        store.close()


def _summaries(store: MomentStore) -> list[str]:
    rows = store._conn.execute('SELECT summary FROM moments ORDER BY id').fetchall()
    return [row[0] for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description='回放中期记忆')
    parser.add_argument('source', help='聊天记录 jsonl')
    parser.add_argument('--windows', type=int, default=6, help='压缩多少个窗口')
    parser.add_argument('--batch', type=int, default=20, help='每个窗口多少条消息')
    parser.add_argument('--model', default='deepseek-chat', help='压缩用的模型')
    parser.add_argument('--skip', type=int, default=SKIP_HEAD, help='跳过开头多少条文字消息')
    args = parser.parse_args()

    logger.remove()
    records = load_slice(args.source, args.skip, SLICE)
    print(f'截取 {len(records)} 条真实文字消息\n')
    asyncio.run(replay(records, args.windows, args.batch, args.model))


if __name__ == '__main__':
    main()
