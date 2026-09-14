""" 错别字：让发出去的话不那么像印刷品。

群里没人打对字，一个错都没有反而像机器人。这里只做**单字同音/形近替换**，
一条消息最多抖一个，概率很低（默认 8%）。

红线：
- 数字、@ 人、链接、[图片]/[表情] 这类占位标记**绝不碰**——改错了就是要命
- 不改专有名词，不做整词替换（「应该」→「因该」这种要另配词典，先不做）
- 只在出站那一刻抖，模型看不到自己「打错字」了，跟真人一致
"""

from __future__ import annotations

import random
import re

# 同音 / 形近，双向都配：反着错也是错
TYPOS = {
    '的': '得', '得': '的',
    '在': '再', '再': '在',
    '做': '作', '作': '做',
    '那': '哪', '哪': '那',
    '已': '以', '以': '已',
    '象': '像', '像': '象',
    '需': '须',
    '吗': '嘛',
    '末': '未',
    '己': '已',
}

# 这些片段里的字一个都不动
PROTECTED = re.compile(r'\[[^\]]*\]|https?://\S+|@\S+')


def apply_typos(text: str, rate: float = 0.0, rng: random.Random | None = None) -> str:
    """ 按概率给这句话抖一个错别字；rate <= 0 或没得抖就原样返回。 """
    if not text or rate <= 0:
        return text
    rng = rng or random
    if rng.random() >= rate:
        return text

    blocked = {i for match in PROTECTED.finditer(text) for i in range(match.start(), match.end())}
    spots = [i for i, ch in enumerate(text) if i not in blocked and ch in TYPOS]
    if not spots:
        return text

    at = rng.choice(spots)
    return text[:at] + TYPOS[text[at]] + text[at + 1:]


def _self_check() -> None:
    text = '这个我觉得不太行，应该在改改'
    assert apply_typos(text, 0.0) == text             # 关掉就完全不抖
    assert apply_typos('', 1.0) == ''
    assert apply_typos('abcd', 1.0) == 'abcd'         # 没一个字在表里
    assert apply_typos(text, 1.0, random.Random(0)) != text

    # 只抖一个，而且抖出来的一定是表里的替换
    rng = random.Random(1)
    for _ in range(50):
        out = apply_typos(text, 1.0, rng)
        diff = [i for i, (a, b) in enumerate(zip(text, out, strict=True)) if a != b]
        assert len(diff) == 1, out
        assert TYPOS[text[diff[0]]] == out[diff[0]], out
        assert len(out) == len(text)

    # 红线：占位标记 / 链接里的字一个都不能动
    assert apply_typos('[图片：我的在]', 1.0, random.Random(0)) == '[图片：我的在]'
    assert apply_typos('看 https://a.b再/c', 1.0, random.Random(0)) == '看 https://a.b再/c'
    # 保护段外面的照抖（@ 的那个人名保住了，后面该错还是错）
    guarded = apply_typos('@再某人 在吗', 1.0, random.Random(0))
    assert guarded.startswith('@再某人 ') and guarded != '@再某人 在吗', guarded

    print('typos self-check ok')


if __name__ == '__main__':
    _self_check()
