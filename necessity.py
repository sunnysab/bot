""" 回复必要性打分：用便宜的确定性规则决定一条消息值不值得交给模型。

思路改编自 MaiBot 的 reply_necessity —— 沉默是默认，开口要挣分。
被 @ / 被叫到名字 / 私聊不走评分，直接满分触发；其余群消息按
「内容 + 活跃度压力 - 存在感惩罚」打分，过线才交给模型。

模型仍可用「本轮不发言」做内容层的最终否决。
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Literal

NECESSITY_THRESHOLD = 60

# 存在感：最近这段时间内自己发言占比越高，越不该再插话。
# 实测：老参数（0.25/0.60、最多扣 25）压不住热群里的刷屏——bot 占到群消息的
# 36% 时也只扣 7 分，而分数线是 60+。于是收紧起算点、放大量级
PRESENCE_WINDOW_SECONDS = 300.0
PRESENCE_FREE_RATIO = 0.15
PRESENCE_FULL_RATIO = 0.45
PRESENCE_PENALTY_MAX = 40

# 活跃度压力：群里聊得越热，主动参与的压力越大。
# **封顶必须低于阈值**：老参数封顶 60 = 阈值，意味着「群够热」本身就能触发，
# 内容一分不加也过线——实测热群里 75/131 轮都是这种纯压力分，bot 每 34 秒一条。
# 现在封顶 40 < 阈值 60，热群也需要内容贡献（问句 +20 / 请求 +25）才够格。
PRESSURE_MAX = 40
PRESSURE_FULL_ACTIVITY = 10  # 活动窗口内 10 条别人的消息即拿满

QUESTION_TERMS = ('怎么', '如何', '为什么', '有没有', '是不是', '哪里有')
REQUEST_TERMS = ('帮我', '帮忙', '谁能', '谁会', '有没有人', '求个', '救命', '教我')
OPINION_TERMS = ('你觉得', '你们觉得', '咋看', '怎么看', '有什么建议', '说说看')
SHORT_REACTIONS = frozenset((
    '哈哈', '哈哈哈', '哈哈哈哈', '草', '笑死', '6', '666', '？', '?',
    '好', '嗯', '啊', '哦', '确实', '牛的', 'nb', '可以'))
MEDIA_PREFIXES = ('[图片', '[表情', '[语音', '[视频', '[位置', '[链接', '[文件', '[合并转发')

# 连续沉默退避：没人点名的消息每条都要花一次决策模型调用，连着几轮都判
# 「不说」就说明这会儿不需要它，那就歇一会儿再听。
IDLE_START_AFTER = 2       # 连着几轮没开口才开始退避
IDLE_BASE_SECONDS = 15.0   # 第一档冷却
IDLE_MAX_SECONDS = 300.0   # 冷却上限
IDLE_BYPASS_MESSAGES = 6   # 中间又来了这么多条消息，就当话题翻篇了，立刻放行

# 发言频率闸：阈值 + 内容分能过门，但人不会一分钟说五句。这是最后一道闸，
# 也是「话密」的直接解。只拦主动参与，被点名 / 私聊不走这里。
SPEECH_WINDOW_SECONDS = 300.0   # 滑动窗口
SPEECH_MAX_IN_WINDOW = 5        # 窗口内最多主动说几条
SPEECH_MIN_GAP_SECONDS = 45.0   # 同一会话两条之间至少隔多久

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
    focused: bool = False      # 专注模式里的一轮：门控和沉默退避都不拦


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
    if cleaned.startswith('[表情'):
        # 表情是有人在表达情绪，接一下比假装没看见自然；图片/语音那种还得自己去看才有意义
        return 20, ['表情']
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


@dataclass
class _IdleState:
    silences: int = 0
    until: float = 0.0


class IdleBackoff:
    """ 连续几轮没开口就歇一会儿（冷却 15s → 30s → … → 最多 5 分钟）。

    只该用在「主动参与」上：被 @ / 被叫名字 / 私聊是必须回应的，不能退避。
    群里又刷了 IDLE_BYPASS_MESSAGES 条消息就当作话题翻篇，立刻放行。
    """

    def __init__(self, start_after: int = IDLE_START_AFTER, base: float = IDLE_BASE_SECONDS,
                 maximum: float = IDLE_MAX_SECONDS, bypass: int = IDLE_BYPASS_MESSAGES):
        self.start_after = start_after
        self.base = base
        self.maximum = maximum
        self.bypass = bypass
        self._states: dict[str, _IdleState] = {}
        self._newest: dict[str, float] = {}   # 上次判断时看到的最新消息时间

    def allows(self, contact: str, now: float, history=()) -> bool:
        state = self._states.get(contact)
        if state is None or state.until <= now:
            return True
        # 还在冷却里，但话题已经翻篇了就不等了
        seen = self._newest.get(contact, 0.0)
        return sum(1 for r in history if r.t > seen) >= self.bypass

    def record(self, contact: str, spoke: bool, now: float, history=()) -> None:
        """ 记下这一轮的结果：开口就清零，没开口就攒着。 """
        newest = max([now, *(r.t for r in history)])
        self._newest[contact] = newest
        state = self._states.setdefault(contact, _IdleState())
        if spoke:
            state.silences = 0
            state.until = 0.0
            return
        state.silences += 1
        if state.silences >= self.start_after:
            steps = state.silences - self.start_after
            state.until = now + min(self.base * (2 ** steps), self.maximum)

    def remaining(self, contact: str, now: float) -> float:
        """ 冷却还剩多少秒（0 = 没有冷却）。

        注意它只说冷却是多久，不说**拦不拦**：冷却中也可能因为话题翻篇
        （见 allows 的 bypass）而放行。要判断拦不拦用 allows()。
        """
        state = self._states.get(contact)
        if state is None or state.until <= now:
            return 0.0
        return state.until - now

    def pending(self, now: float) -> dict[str, float]:
        """ 还在冷却里的会话 -> 剩余秒数。管理后台的健康卡要列出来。 """
        return {contact: state.until - now
                for contact, state in self._states.items() if state.until > now}


class SpeechLimiter:
    """ 每个会话的发言频率闸。

    记的是**自己真正开口的时刻**（`ReplyPolicy.record(spoke=True)` 时 note），
    所以窗口内的读数就是「我刚说了几条」，不依赖历史里自己那几条还在不在窗口内。
    """

    def __init__(self, window: float = SPEECH_WINDOW_SECONDS, maximum: int = SPEECH_MAX_IN_WINDOW,
                 min_gap: float = SPEECH_MIN_GAP_SECONDS):
        self.window = window
        self.maximum = maximum
        self.min_gap = min_gap
        self._sent: dict[str, deque[float]] = {}

    def refusal(self, contact: str, now: float) -> str:
        """ 该闭嘴就返回原因（给面板看），可以开口返回空串。 """
        times = self._prune(contact, now)
        if times and now - times[-1] < self.min_gap:
            return f'刚说过（{int(now - times[-1])}s 前）'
        if len(times) >= self.maximum:
            return f'{int(self.window // 60)} 分钟内已说 {len(times)} 条'
        return ''

    def note(self, contact: str, now: float) -> None:
        self._prune(contact, now).append(now)

    def counts(self, now: float) -> dict[str, int]:
        """ 各会话窗口内已说几条。健康卡要显示。 """
        return {contact: len(self._prune(contact, now)) for contact in list(self._sent)
                if self._prune(contact, now)}

    def _prune(self, contact: str, now: float) -> deque[float]:
        times = self._sent.setdefault(contact, deque())
        while times and now - times[0] > self.window:
            times.popleft()
        return times


def _self_check() -> None:
    def score(text, **kwargs) -> NecessityScore:
        return score_necessity(NecessityInput(text=text, is_group=True, has_at=False, has_mention=False, **kwargs))

    # @ 和私聊直接满分
    assert score_necessity(NecessityInput('在吗', True, True, False)).score == 100
    assert score_necessity(NecessityInput('在吗', True, False, True)).score == 80
    assert score_necessity(NecessityInput('在吗', False, False, False)).kind == 'private'

    # 热群本身不再够格：闲话（无内容分）过不了线
    assert score('今天真冷', activity=10).score < NECESSITY_THRESHOLD
    # 热聊群里抛问题，够格
    assert score('这个怎么弄啊', activity=10).score >= NECESSITY_THRESHOLD
    # 冷清群里说句闲话，不够格
    assert score('大家好啊', activity=1).score < NECESSITY_THRESHOLD
    # 热聊群里纯短反应，仍不够
    assert score('哈哈哈哈', activity=10).score < NECESSITY_THRESHOLD
    # 请求类命中；热度不够时也得等热度
    assert score('谁能帮我看看', activity=10).score >= NECESSITY_THRESHOLD
    # 自己刷屏后，同样的问题被压回阈值下
    assert score('这个怎么弄啊', activity=10, self_replies=6, recent_messages=10).score < NECESSITY_THRESHOLD
    # 媒体占位不算内容分（但热聊群靠活跃度照样能触发）
    assert score('[图片：一只橘猫]', activity=10).score == score('一句平平无奇的话', activity=10).score
    assert score('[图片：一只橘猫]', activity=5).score < NECESSITY_THRESHOLD

    # 连续沉默退避
    from types import SimpleNamespace

    def feed(history, times):
        for t in times:
            history.append(SimpleNamespace(t=t))

    backoff = IdleBackoff()
    history: list = []
    feed(history, [1.0])
    assert backoff.allows('g', 1.0, history)          # 新会话不拦
    backoff.record('g', spoke=False, now=1.0, history=history)
    assert backoff.allows('g', 2.0, history)          # 才一次，还没到 start_after

    feed(history, [2.0])
    backoff.record('g', spoke=False, now=2.0, history=history)
    assert not backoff.allows('g', 3.0, history)      # 连着两轮没开口，开始歇
    assert backoff._states['g'].until == 17.0         # 第一档 15s

    # 冷却到期就放行
    assert backoff.allows('g', 17.0, history)

    # 再沉默一轮，冷却变成 30s（直到 48.0）
    feed(history, [18.0])
    backoff.record('g', spoke=False, now=18.0, history=history)
    assert backoff._states['g'].until == 48.0

    # 冷却中，但群里又刷够 6 条就立刻放行
    feed(history, [19.0 + i for i in range(IDLE_BYPASS_MESSAGES - 1)])
    assert not backoff.allows('g', 20.0, history)     # 才 5 条
    assert backoff.remaining('g', 20.0) == 28.0       # 面板要显示「还剩多少秒」
    feed(history, [30.0])
    assert backoff.allows('g', 30.0, history)         # 第 6 条，话题翻篇了
    # 翻篇只是放行，不重置冷却：剩下 18s 还在，面板要显示的是「拦不拦」不是「剩多少」
    assert backoff.remaining('g', 30.0) == 18.0
    assert backoff.remaining('从没见过的会话', 30.0) == 0.0
    assert backoff.pending(30.0) == {'g': 18.0}

    # 连败越久退避越久，封顶 IDLE_MAX_SECONDS
    capped = IdleBackoff()
    for i in range(12):
        capped.record('g', spoke=False, now=float(i), history=[])
    assert capped._states['g'].until == 11.0 + IDLE_MAX_SECONDS

    # 开口就清零
    capped.record('g', spoke=True, now=100.0, history=[])
    assert capped.allows('g', 100.0, []) and capped._states['g'].silences == 0

    # 发言频率闸：刚说完要等 min_gap；窗口里说满要等滑出去
    speech = SpeechLimiter(window=300.0, maximum=3, min_gap=45.0)
    assert speech.refusal('g', 1000.0) == ''            # 没说过，放行
    speech.note('g', 1000.0)
    assert '刚说过' in speech.refusal('g', 1010.0)
    assert speech.refusal('g', 1045.0) == ''            # 过了 min_gap
    speech.note('g', 1046.0)
    speech.note('g', 1100.0)
    assert '3 条' in speech.refusal('g', 1200.0)        # 窗口里已 3 条
    assert speech.refusal('g', 1310.0) == ''            # 第 1 条滑出窗口（1000 + 300 < 1310）
    # 会话之间互不影响
    assert speech.refusal('另一个群', 1200.0) == ''
    assert speech.counts(1310.0) == {'g': 2}

    print('necessity self-check ok')


if __name__ == '__main__':
    _self_check()
