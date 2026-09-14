""" 回复必要性打分：用便宜的确定性规则决定一条消息值不值得交给模型。

思路改编自 MaiBot 的 reply_necessity —— 沉默是默认，开口要挣分。
被 @ / 被叫到名字 / 私聊不走评分，直接满分触发；其余群消息按
「内容 + 活跃度压力 - 存在感惩罚」打分，过线才交给模型。

模型仍可用「本轮不发言」做内容层的最终否决。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

NECESSITY_THRESHOLD = 60

# 存在感：最近这段时间内自己发言占比越高，越不该再插话
PRESENCE_WINDOW_SECONDS = 300.0
PRESENCE_FREE_RATIO = 0.25
PRESENCE_FULL_RATIO = 0.60
PRESENCE_PENALTY_MAX = 25

# 活跃度压力：群里聊得越热，主动参与的压力越大
PRESSURE_MAX = 45
PRESSURE_FULL_ACTIVITY = 10  # 活动窗口内 10 条别人的消息即拿满

QUESTION_TERMS = ('怎么', '如何', '为什么', '有没有', '是不是', '哪里有')
REQUEST_TERMS = ('帮我', '帮忙', '谁能', '谁会', '有没有人', '求个', '救命', '教我')
OPINION_TERMS = ('你觉得', '你们觉得', '咋看', '怎么看', '有什么建议', '说说看')
SHORT_REACTIONS = frozenset((
    '哈哈', '哈哈哈', '哈哈哈哈', '草', '笑死', '6', '666', '？', '?',
    '好', '嗯', '啊', '哦', '确实', '牛的', 'nb', '可以'))
MEDIA_PREFIXES = ('[图片', '[表情', '[语音', '[视频', '[位置', '[链接', '[文件', '[合并转发')

TriggerKind = Literal['at', 'mention', 'private', 'proactive']


@dataclass(frozen=True)
class NecessityInput:
    text: str
    is_group: bool
    has_at: bool
    has_mention: bool
    activity: int = 0          # 活动窗口内别人的消息数
    self_replies: int = 0      # 存在感窗口内自己的发言数
    recent_messages: int = 0   # 存在感窗口内总消息数


@dataclass(frozen=True)
class NecessityScore:
    score: int
    kind: TriggerKind
    reasons: tuple[str, ...] = ()

    @property
    def detail(self) -> str:
        suffix = f'（{"，".join(self.reasons)}）' if self.reasons else ''
        return f'{self.kind} {self.score}分{suffix}'


def score_necessity(inp: NecessityInput) -> NecessityScore:
    """ 打分。调用方自己跟阈值比较。 """
    if inp.has_at:
        return NecessityScore(100, 'at')
    if inp.has_mention:
        return NecessityScore(80, 'mention')
    if not inp.is_group:
        return NecessityScore(100, 'private')

    content, reasons = _score_content(inp.text)
    score = content + _pressure(inp.activity)
    reasons.append(f'活跃={inp.activity}条')

    penalty, penalty_reason = _presence_penalty(inp.self_replies, inp.recent_messages)
    if penalty_reason:
        score -= penalty
        reasons.append(penalty_reason)

    return NecessityScore(max(0, score), 'proactive', tuple(reasons))


def _score_content(text: str) -> tuple[int, list[str]]:
    cleaned = (text or '').strip()
    if not cleaned:
        return 0, []
    if cleaned.startswith(MEDIA_PREFIXES):
        return 0, ['媒体占位']
    if _is_short_reaction(cleaned):
        return -30, ['短反应']

    score = 0
    reasons: list[str] = []
    if _is_question(cleaned):
        score += 20
        reasons.append('问句')
    if any(term in cleaned for term in REQUEST_TERMS):
        score += 25
        reasons.append('请求')
    if any(term in cleaned for term in OPINION_TERMS):
        score += 25
        reasons.append('征询')
    if '@all' in cleaned.lower():
        score += 10
        reasons.append('@all')
    length = len(cleaned)
    if length >= 120:
        score += 10
        reasons.append('长文')
    elif length >= 40:
        score += 5
        reasons.append('较长')
    return score, reasons


def _is_short_reaction(text: str) -> bool:
    """ 纯短反应（哈哈/6/？）没什么可接的。 """
    return len(text) <= 8 and text in SHORT_REACTIONS


def _is_question(text: str) -> bool:
    """ 判断是否像真问题，尽量避开「什么」当语气词、单个问号之类。 """
    if any(term in text for term in QUESTION_TERMS):
        return True
    if re.search(r'(?<![这那没])什么', text):
        return True
    if 4 <= len(text) <= 80 and re.search(r'[吗呢][？?。！!~～…]*$', text):
        return True
    return 4 <= len(text) <= 120 and bool(re.search(r'[？?](?:$|[。！!~～…])', text))


def _pressure(activity: int) -> int:
    """ 活动窗口内别人消息越多，压力分越高，封顶 PRESSURE_MAX。 """
    if activity <= 0:
        return 0
    return min(PRESSURE_MAX, activity * PRESSURE_MAX // PRESSURE_FULL_ACTIVITY)


def _presence_penalty(self_replies: int, recent_messages: int) -> tuple[int, str]:
    """ 最近窗口里自己发言占比超过 25% 开始扣分，60% 扣满。 """
    if self_replies <= 0 or recent_messages <= 0:
        return 0, ''
    ratio = min(1.0, self_replies / recent_messages)
    if ratio <= PRESENCE_FREE_RATIO:
        return 0, ''
    span = PRESENCE_FULL_RATIO - PRESENCE_FREE_RATIO
    progress = min(1.0, (ratio - PRESENCE_FREE_RATIO) / span)
    return int(round(PRESENCE_PENALTY_MAX * progress)), f'存在感={self_replies}/{recent_messages}'


def _self_check() -> None:
    def score(text, **kwargs) -> NecessityScore:
        return score_necessity(NecessityInput(text=text, is_group=True, has_at=False, has_mention=False, **kwargs))

    # @ 和私聊直接满分
    assert score_necessity(NecessityInput('在吗', True, True, False)).score == 100
    assert score_necessity(NecessityInput('在吗', True, False, True)).score == 80
    assert score_necessity(NecessityInput('在吗', False, False, False)).kind == 'private'

    # 冷清群里说句闲话，不够格
    assert score('大家好啊', activity=1).score < NECESSITY_THRESHOLD
    # 热聊群里抛问题，够格
    assert score('这个怎么弄啊', activity=10).score >= NECESSITY_THRESHOLD
    # 热聊群里纯短反应，仍不够
    assert score('哈哈哈哈', activity=10).score < NECESSITY_THRESHOLD
    # 请求类直接命中
    assert score('谁能帮我看看', activity=8).score >= NECESSITY_THRESHOLD
    # 自己刷屏后，同样的问题被压回阈值下
    assert score('这个怎么弄啊', activity=10, self_replies=6, recent_messages=10).score < NECESSITY_THRESHOLD
    # 媒体占位不加分
    assert score('[图片：一只橘猫]', activity=10).score < NECESSITY_THRESHOLD

    print('necessity self-check ok')


if __name__ == '__main__':
    _self_check()
