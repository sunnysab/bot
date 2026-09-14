""" 从聊天记录里挖「这个群用过哪些表情」——只挖 md5 和热度。

导出里的表情消息长这样：

    {"type": 5, "content": "[动画表情] emojis/20260213/1770997053_a56b036e6416b4206c6e14eb3264de53.gif"}

文件名最后一段就是微信表情的 md5。**但导出里没有图片、也没有 cdnurl/aeskey**，
所以这批记录发不出去，只能当「本群的梗」的热度权重：

  - 热门的表情，等 relay 真收到一次（那时才拿到完整 XML）就该优先用
  - 冷门的，以后从库里挤掉

能不能发由 Emoji.sendable 决定，跟热度无关。这跟 build_profiles/build_habits 一样
是纯统计、可回溯的，不调模型。

用法： python build_emojis.py 有福同享（重生版）.jsonl memory/emojis.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from build_profiles import iter_records  # noqa: E402
from emojis import Emoji  # noqa: E402

EMOJI_TYPE = 5
# emojis/20260213/1770997053_a56b036e6416b4206c6e14eb3264de53.gif
MD5_IN_PATH = re.compile(r'/(\d+)_([0-9a-fA-F]{32})\.\w+$')

MAX_ENTRIES = 400        # 跟 EmojiLibrary.LIBRARY_LIMIT 对齐
MIN_COUNT = 2            # 只用过一次的别收，噪声太大


def mine(path: str | Path) -> Counter:
    """ md5 -> 在群里出现过多少次。 """
    counts: Counter = Counter()
    for record in iter_records(path):
        if record.get('_type') != 'message' or record.get('type') != EMOJI_TYPE:
            continue
        match = MD5_IN_PATH.search(str(record.get('content') or ''))
        if match:
            counts[match.group(2).lower()] += 1
    return counts


def build(path: str | Path, output: str | Path) -> list[Emoji]:
    counts = mine(path)
    kept = [(md5, count) for md5, count in counts.most_common(MAX_ENTRIES * 2) if count >= MIN_COUNT]
    kept = kept[:MAX_ENTRIES]
    # 只有 md5 和热度：导出里既没有图，也没有使用上下文，场景得等运行时补
    emojis = [Emoji(md5=md5, count=count) for md5, count in kept]
    Path(output).write_text(
        json.dumps([item.to_dict() for item in emojis], ensure_ascii=False, indent=1),
        encoding='utf-8')
    return emojis


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__.strip())
        raise SystemExit(1)
    emojis = build(sys.argv[1], sys.argv[2])
    print(f'挖到 {len(emojis)} 张表情，写入 {sys.argv[2]}')
    for item in emojis[:10]:
        print(f'  {item.count:>4} 次  {item.md5}')
    print(f'（只有 {sum(1 for item in emojis if item.sendable)} 张能发——'
          f'其余要等 relay 真收到一次才有下载地址）')


def _self_check() -> None:
    import tempfile

    lines = [
        '{"_type": "message", "type": 5, "timestamp": 100, "accountName": "甲",'
        ' "content": "[动画表情] emojis/20260213/100_' + 'a' * 32 + '.gif"}',
        '{"_type": "message", "type": 5, "timestamp": 200, "accountName": "乙",'
        ' "content": "[动画表情] emojis/20260213/200_' + 'a' * 32 + '.png"}',
        '{"_type": "message", "type": 5, "timestamp": 300, "accountName": "丙",'
        ' "content": "[动画表情] emojis/20260213/300_' + 'A' * 32 + '.gif"}',
        # 只出现一次的不收；不是表情类型的不看；路径不含 md5 的忽略
        '{"_type": "message", "type": 5, "timestamp": 400, "accountName": "丁",'
        ' "content": "[动画表情] emojis/20260213/400_' + 'b' * 32 + '.gif"}',
        '{"_type": "message", "type": 1, "timestamp": 500, "accountName": "戊",'
        ' "content": "emojis/20260213/500_' + 'c' * 32 + '.png"}',
        '{"_type": "message", "type": 5, "timestamp": 600, "accountName": "己",'
        ' "content": "[动画表情] 没有路径"}',
        '{"_type": "message", "type": 0, "timestamp": 700, "accountName": "庚", "content": "说话"}',
    ]
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / 'chat.jsonl'
        source.write_text('\n'.join(lines), encoding='utf-8')
        output = Path(tmp) / 'emojis.json'

        counts = mine(source)
        # 大小写统一：'A'*32 和 'a'*32 是同一张（mine 不过滤，MIN_COUNT 在 build 里）
        assert counts == {'a' * 32: 3, 'b' * 32: 1}, counts

        emojis = build(source, output)
        assert len(emojis) == 1 and emojis[0].count == 3
        assert emojis[0].scene == ''            # 导出里没有使用上下文，不能编
        assert not emojis[0].sendable           # 没下载地址，发不出去

        # 落盘格式要能被 EmojiLibrary 直接读
        from emojis import EmojiLibrary

        assert len(EmojiLibrary.load(output)) == 1
        assert json.loads(output.read_text(encoding='utf-8'))[0]['count'] == 3

    print('build_emojis self-check ok')


if __name__ == '__main__':
    if len(sys.argv) > 1:
        main()
    else:
        _self_check()
