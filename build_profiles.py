""" 群友画像：从离线导出的聊天记录里提取每个群友的风格，供 prompt 注入。

画像是纯数据，不入库（见 .gitignore 里的 memory/）；机器人运行时按发送者
昵称查出对应的人，让回复「认识人」。

旧版提取的几个问题，这里都改了：
- 不再把昵称里的括注当外号；外号只保留能在 @ 里对上的
- 统一口径：图片/表情/语音/位置占位不算文本，方括号里的表情名不参与词频
- 口头禅改成整句短句词频，避免「破涕为笑」被切成「破涕/涕为/为笑」
- 去掉 LLM 生成的「故事与梗」（容易编），只留能从数据核对的东西
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path

TEXT_TYPES = (0, 25)          # 普通文字 / 引用文字
STICKER_TYPE = 5              # 动画表情
PLACEHOLDER = re.compile(r'^\[[^\]]{0,16}\][^\[\]]*$')
BRACKET = re.compile(r'\[[^\]]{1,16}\]')
MENTION = re.compile(r'@([^\s@\u2005]{1,24})[\u2005\s]')
CLAUSE_SPLIT = re.compile(r'[，。！？!?~～…、,;；:：\s"\'“”‘’()（）\[\]【】]+')
SUFFIX = re.compile(r'[（(\[【][^）)\]】]{0,20}[）)\]】]\s*$')

MIN_TEXT_COUNT = 30           # 少于这么多条文字消息的人不进画像
MIN_PHRASE_COUNT = 5
MAX_PHRASES = 6
MAX_ALIASES = 5
MAX_SAMPLES = 3

TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    '上班': ('上班', '打工', '摸鱼', '加班', '通勤', '下班'),
    '工作': ('工作', '项目', '老板', '同事', '裁员', '面试', 'offer'),
    '工资': ('工资', '薪水', '薪资', '奖金', '涨薪', '年薪'),
    '对象': ('对象', '女朋友', '男朋友', '女友', '男友'),
    '相亲': ('相亲', '征婚', '介绍对象'),
    '恋爱': ('恋爱', '暧昧', '告白', '分手', '前任', '喜欢的人'),
    '结婚': ('结婚', '婚礼', '彩礼', '领证', '离婚'),
    '朋友': ('朋友', '兄弟', '姐妹', '哥们', '闺蜜'),
    '游戏': ('游戏', 'steam', '开黑', '上分', '单机', '原神'),
    '王者': ('王者', '排位', '打野', '辅助', '峡谷'),
    '吃喝': ('外卖', '奶茶', '咖啡', '吃饭', '宵夜', '食堂'),
    '论文': ('论文', '综述', '答辩', '导师', '期刊', '投稿'),
    '学习': ('学习', '考研', '考试', '上课', '网课', '雅思'),
    '毕业': ('毕业', '大学', '研究生', '读研'),
    '猫狗': ('猫', '狗', '铲屎', '遛狗'),
    '回家': ('回家', '爸妈', '老妈', '老爸', '过年'),
}

# 纯语气/功能词，不算「口头禅」
PHRASE_STOPLIST = frozenset((
    '什么', '这个', '那个', '怎么', '不是', '没有', '可以', '一个', '自己', '就是',
    '这样', '这里', '时候', '东西', '问题', '还是', '因为', '所以', '但是', '然后'))


def normalize_name(name: str) -> str:
    """ 去掉群昵称尾部的括注和空格，得到匹配用的基名。 """
    cleaned = (name or '').strip()
    while cleaned:
        stripped = SUFFIX.sub('', cleaned).strip()
        if stripped == cleaned:
            break
        cleaned = stripped
    return cleaned


def iter_records(path: str | Path) -> Iterator[dict]:
    with open(path, encoding='utf-8') as file:
        for line in file:
            if '"_type"' not in line:
                continue
            yield json.loads(line)


def load_members(path: str | Path) -> tuple[dict[str, str], dict]:
    """ 返回 (账号 -> 当前群昵称, 群信息)。 """
    members: dict[str, str] = {}
    chat: dict = {}
    for record in iter_records(path):
        kind = record.get('_type')
        if kind == 'member':
            members[record['platformId']] = record.get('accountName') or ''
        elif kind == 'header':
            meta = record.get('meta') or {}
            chat = {'name': meta.get('name', ''), 'group_id': meta.get('groupId', '')}
    return members, chat


def build_name_index(members: dict[str, str]) -> dict[str, str]:
    """ 基名 -> 账号。带括注的群昵称会额外注册一个基名。 """
    index: dict[str, str] = {}
    for wxid, name in members.items():
        for candidate in (name, normalize_name(name)):
            if candidate:
                index.setdefault(candidate, wxid)
    return index


def iter_clauses(text: str) -> Iterator[str]:
    """ 把一句消息切成短句，并去掉方括号里的表情名。 """
    for part in CLAUSE_SPLIT.split(BRACKET.sub('', text)):
        part = part.strip()
        if 2 <= len(part) <= 8:
            yield part


def keep_phrases(counter: Counter[str]) -> list[str]:
    """ 取高频短句，顺带去掉互为子串的变体（哈哈 / 哈哈哈 / 哈哈哈哈）。 """
    picked: list[str] = []
    for phrase, count in counter.most_common():
        if count < MIN_PHRASE_COUNT:
            break
        if phrase in PHRASE_STOPLIST:
            continue
        if any(phrase in other or other in phrase for other in picked):
            continue
        picked.append(phrase)
        if len(picked) >= MAX_PHRASES:
            break
    return picked


def pick_samples(texts: list[str], phrases: Iterable[str]) -> list[str]:
    """ 每个口头禅挑一条真实发言当样例。 """
    samples: list[str] = []
    for phrase in phrases:
        for text in texts:
            if phrase in text and 4 <= len(text) <= 40 and text not in samples:
                samples.append(text)
                break
        if len(samples) >= MAX_SAMPLES:
            break
    return samples


def build(path: str | Path) -> dict:
    members, chat = load_members(path)
    name_index = build_name_index(members)

    total = Counter()
    stickers = Counter()
    texts: dict[str, list[str]] = defaultdict(list)
    clauses: dict[str, Counter[str]] = defaultdict(Counter)
    mentions: dict[str, Counter[str]] = defaultdict(Counter)
    alias_votes: dict[str, Counter[str]] = defaultdict(Counter)

    for record in iter_records(path):
        if record.get('_type') != 'message':
            continue
        sender = record.get('sender') or ''
        if not sender or sender.endswith('@chatroom'):
            continue
        total[sender] += 1
        msg_type = record.get('type')
        if msg_type == STICKER_TYPE:
            stickers[sender] += 1

        content = str(record.get('content') or '').strip()
        for token in MENTION.findall(content):
            target = name_index.get(token) or name_index.get(normalize_name(token))
            if target and target != sender:
                mentions[sender][target] += 1
                alias_votes[target][token] += 1

        if msg_type not in TEXT_TYPES or not content:
            continue
        if PLACEHOLDER.match(content) or content.startswith('@'):
            continue
        stripped = BRACKET.sub('', content).strip()
        if not stripped:
            continue
        texts[sender].append(stripped)
        clauses[sender].update(set(iter_clauses(content)))

    people: dict[str, dict] = {}
    for wxid, messages in texts.items():
        if len(messages) < MIN_TEXT_COUNT:
            continue
        phrases = keep_phrases(clauses[wxid])
        counts = [len(text) for text in messages]
        laugh = sum(1 for text in messages if '哈哈' in text)
        aliases = [token for token, _ in alias_votes[wxid].most_common(MAX_ALIASES)]
        names = [members.get(wxid, wxid)]
        base = normalize_name(names[0])
        if base and base != names[0]:
            names.append(base)
        names.extend(alias for alias in aliases if alias not in names)

        topics = Counter()
        for text in messages:
            for topic, keywords in TOPIC_KEYWORDS.items():
                if any(keyword in text for keyword in keywords):
                    topics[topic] += 1

        people[wxid] = {
            'wxid': wxid,
            'names': names,
            'message_count': total[wxid],
            'text_count': len(messages),
            'sticker_ratio': round(stickers[wxid] / max(total[wxid], 1), 3),
            'avg_len': round(sum(counts) / len(counts), 1),
            'short_ratio': round(sum(1 for count in counts if count <= 3) / len(counts), 3),
            'laugh_ratio': round(laugh / len(messages), 3),
            'phrases': phrases,
            'topics': [topic for topic, _ in topics.most_common(5)],
            'samples': pick_samples(messages, phrases),
        }

    ordered = dict(sorted(people.items(), key=lambda item: -item[1]['text_count']))
    return {
        'version': 1,
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'source': Path(path).name,
        'chat': chat,
        'people': ordered,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description='从聊天记录导出群友画像')
    parser.add_argument('source', help='聊天记录 jsonl')
    parser.add_argument('output', nargs='?', default='memory/profiles.json', help='输出路径')
    args = parser.parse_args()

    profiles = build(args.source)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'写出 {len(profiles["people"])} 个人的画像 -> {output}')


if __name__ == '__main__':
    main()
