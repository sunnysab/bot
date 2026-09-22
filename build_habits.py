""" 从聊天记录里挖「本群特色用语」——梗、口头禅、缩写。

和 build_profiles.py 一样是纯统计、可回溯：每条都附真实用例。
刻意不让 LLM 生成「当 X 时可以说 Y」那类规则（会编、难核对、每跑一次要花 API）。

用法： python build_habits.py 有福同享（重生版）.jsonl memory/habits.json
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from build_profiles import (
    BRACKET,
    PHRASE_STOPLIST,
    PLACEHOLDER,
    TEXT_TYPES,
    iter_clauses,
    iter_records,
    keep_phrases,
    load_members,
    normalize_name,
)

MIN_COUNT = 20            # 至少出现这么多次才算「群里的习惯」
MAX_HABITS = 80
MAX_SAMPLES = 3

# 群里高频但没有「群味」的词，注入了只会污染 prompt
GENERIC = frozenset((
    *PHRASE_STOPLIST,
    # 附和、确认
    '是的', '是的是的', '我也是', '我也', '确实', '对啊', '对的', '没错', '好吧', '行吧',
    '哦哦', '噢噢', '嗯嗯', '可以的', '好的', '好嘞', '好好好', '真的吗', '这样', '那样',
    '知道', '了解', '明白', '对了', '支持', '了解了解',
    # 礼貌
    '谢谢', '感谢', '麻烦', '抱歉', '不好意思',
    # 笑：只反映情绪，不带群信息
    '哈哈', '哈哈哈', '哈哈哈哈', '哈哈哈哈哈', '哈哈哈哈哈哈', '哈哈哈哈哈哈哈',
    '嘿嘿', '嘻嘻', 'hh', 'hhh', 'hhhh',
    # 通用动词 / 差语气
    '没事', '没有', '不是', '就是', '可以', '不行', '算了', '算了算了', '好看', '好厉害',
    '厉害', '真好', '是啊', '是真的', '真的',
    # 时间、程度、通用疑问
    '今天', '明天', '昨天', '现在', '刚刚', '有点', '感觉', '应该', '可能', '真的', '好像',
    '一定', '一起', '大家', '各位', '不知', '咋办',
    '不知道', '为什么', '怎么办', '怎么样', '多少', '什么时候', '什么意思',
))


def build(path: str | Path) -> dict:
    members, chat = load_members(path)
    banned = GENERIC | {name for name in members.values()} | {normalize_name(name) for name in members.values()}

    counts: Counter[str] = Counter()
    samples: dict[str, list[str]] = defaultdict(list)

    for record in iter_records(path):
        if record.get('_type') != 'message' or record.get('type') not in TEXT_TYPES:
            continue
        content = str(record.get('content') or '').strip()
        if not content or PLACEHOLDER.match(content) or content.startswith('@'):
            continue
        body = BRACKET.sub('', content).strip()
        if not body:
            continue
        for clause in set(iter_clauses(content)):
            counts[clause] += 1
            bucket = samples[clause]
            if len(bucket) < MAX_SAMPLES and len(body) > len(clause) + 1 and len(body) <= 60:
                bucket.append(body)

    habits = []
    for phrase in keep_phrases(counts, min_count=MIN_COUNT, limit=MAX_HABITS, banned=banned):
        habits.append({'phrase': phrase, 'count': counts[phrase], 'samples': samples[phrase]})

    return {
        'version': 1,
        'generated_at': datetime.now(UTC).isoformat(timespec='seconds'),
        'source': Path(path).name,
        'chat': chat,
        'habits': habits,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description='从聊天记录挖掘本群特色用语')
    parser.add_argument('source', help='聊天记录 jsonl')
    parser.add_argument('output', nargs='?', default='memory/habits.json', help='输出路径')
    args = parser.parse_args()

    data = build(args.source)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'写出 {len(data["habits"])} 条群用语 -> {output}')


if __name__ == '__main__':
    main()
