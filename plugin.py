import asyncio
import base64
import random
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import override

from loguru import logger

from ai import AiProvider
from context import ChatWindow, MessageContext, SingleRecord, message_handle
from emojis import FACE_CATALOG, Emoji, EmojiPolicy, filter_faces
from events import Status, bus
from focus import FocusMode
from habits import HabitStore
from images import ImageStore
from moments import MomentStore, extract_keywords
from necessity import (
    NECESSITY_THRESHOLD,
    PRESENCE_WINDOW_SECONDS,
    IdleBackoff,
    NecessityInput,
    NecessityScore,
    SpeechLimiter,
    score_necessity,
)
from persona import Persona
from personas import PersonaStore
from planner import Plan
from profiles import ProfileStore
from reply_trace import TraceLogger, TraceRecord
from tools import (
    AssistantTurn,
    Toolbox,
    ToolResult,
    ToolSpec,
    arg_int,
    arg_str,
    assistant_message,
    tool_message,
)

# 搜表情：给个关键词，还一堆能发出去的候选。由 Bot 提供（它才连 daemon）。
# 插件自己不发表情——发送统一走 OutboundQueue，那里的限速不能绕过去。
EmojiSearch = Callable[[str], Awaitable[list[Emoji]]]

# 撤回：把「自己发错的那条」撤掉。由 Bot 提供——它记着每条出站的 msg_id，
# 而且撤回是传输层的事。入参 (会话, index, text)，返回给模型看的回执。
# 没注入就不挂这个工具。
Revoke = Callable[[str, int, str], Awaitable[str]]

# 找群成员：模型看到的是群昵称，daemon 要的是 wxid。由 Bot 提供。
# 入参 (群, 昵称)，返回 wxid；没这个人返回空串。
MemberLookup = Callable[[str, str], Awaitable[str]]

# 查历史：当前窗口里没有的更早的记录，由模型自己发起去搜。
# 入参 (会话, 关键词)，返回拼好的几行记录；没命中返回空串。
MessageSearch = Callable[[str, str], Awaitable[str]]

# 拍一拍：模型主动拍某人（打招呼 / 催人 / 拍回去）。入参 (会话, 群昵称，私聊可空)，
# 返回 (是否拍成, 给模型看的回执)。由 Bot 提供——发送是传输层的事。
PatAction = Callable[[str, str], Awaitable[tuple[bool, str]]]

# 决策者的工具。规则的细节写在这里，planner.txt 里只列一行——
# 模型看得到每个工具自己的说明书，比在系统提示里堆一大段更不容易漏。
SEARCH_EMOJI = ToolSpec(
    name='search_emoji',
    description='按关键词搜表情包，返回带序号的候选。关键词用一个短的情绪或动作词，'
                '两个字以内（如「无语」「狗头」「麻了」「点赞」），不要写一整句话。'
                '一次只搜一个词；没合适的就换个词再搜一次，别反复搜同一个。',
    parameters={'type': 'object',
                'properties': {'keyword': {'type': 'string', 'description': '短的情绪或动作词'}},
                'required': ['keyword']})
# 搜索关键词的上限：daemon 是按字面匹配的，词越长命中越少，
# 实测两个字的情绪词（无语 / 狗头 / 麻了）最好使，超出的直接截掉。
MAX_EMOJI_KEYWORD = 2
SEND_EMOJI = ToolSpec(
    name='send_emoji',
    description='发出上一次 search_emoji 结果里的第 index 张表情。必须先搜再发。'
                '表情是情绪，不是回答——别人在问问题还是得用说话那个工具答。'
                '调用这个工具本轮就结束了；想配一句话就同一轮里一起调说话工具。',
    parameters={'type': 'object',
                'properties': {'index': {'type': 'integer', 'description': '候选里的序号，从 1 开始'}},
                'required': ['index']})
SPEAK = ToolSpec(
    name='speak',
    description='让机器人用文字说一句。intent 是你想表达什么，不是台词本身——'
                '台词由别人负责写。调用这个工具本轮就结束了。'
                'ref：这句在回哪一条——填聊天记录里那行的 #标签（如 #4k2p），'
                '发出的消息会引用那一条；没在回具体某条就别填。',
    parameters={'type': 'object',
                'properties': {'intent': {'type': 'string', 'description': '想表达什么'},
                               'emotion': {'type': 'string', 'description': '什么情绪'},
                               'at': {'type': 'string', 'description': '要 @ 的群友昵称，不 @ 就别填'},
                               'ref': {'type': 'string',
                                       'description': '要引用（回复某条）的 #标签，没在回具体某条就别填'}},
                'required': ['intent']})
REPLY = ToolSpec(
    name='reply',
    description='用机器人的身份在群里说一句。text 就是要发出去的原话本身（不是意图、不是转述），'
                '写法像微信打字，一轮只说一条。调完本轮就结束，什么都不调就是这一轮不发言。'
                'ref：这句在回哪一条——填聊天记录里那行的 #标签（如 #4k2p），'
                '发出的消息会引用那一条；**答谁就引谁**，没在回具体某条就别填。'
                'at：要 @ 的群友昵称，默认别填。',
    parameters={'type': 'object',
                'properties': {'text': {'type': 'string', 'description': '要发出去的原话'},
                               'at': {'type': 'string', 'description': '要 @ 的群友昵称，不 @ 就别填'},
                               'ref': {'type': 'string',
                                       'description': '要引用（回复某条）的 #标签，没在回具体某条就别填'}},
                'required': ['text']})
REVOKE = ToolSpec(
    name='revoke',
    description='撤回自己刚说错的话。只能撤自己 2 分钟内发出去的那几条，撤了就没了。'
                'index：1 是最近发的一条，2 是再往前一条；也可以给 text（原话里的一小段），'
                '只在唯一命中时直接撤。两个都不给会列出当前能撤的。撤完可以接着说别的。',
    parameters={'type': 'object',
                'properties': {'index': {'type': 'integer', 'description': '1 = 最近一条'},
                               'text': {'type': 'string', 'description': '原话里的一小段'}}})
PAT = ToolSpec(
    name='pat',
    description='拍一拍某个人（微信的「拍了拍」）。私聊不用给名字，就是拍对方；群聊要给对方的群昵称，'
                '名字对不上就拍不成。被拍的人会收到提示，群里也会多一条「X 拍了拍 Y」——'
                '这是打招呼、催人或拍回去，不是说话。同一个人 12 秒内只能拍一次，别连着拍。',
    parameters={'type': 'object',
                'properties': {'target': {'type': 'string', 'description': '群聊里要拍的人的群昵称；私聊不用填'}}})
SEARCH_MESSAGES = ToolSpec(
    name='search_messages',
    description='在**更早的**聊天记录里按关键词找回原话，返回带时间和说话人的几条。'
                '当前聊天记录里只看得到最近一段，别人说「你刚才说的那个」「上次」「之前」'
                '而你想不起来是什么时，用它。关键词给具体一点（人名、话题里的词），'
                '别拿「的」「这个」这种泛词去搜。搜不到就是真没有，别反复搜索。',
    parameters={'type': 'object',
                'properties': {'keyword': {'type': 'string', 'description': '要找的原话里的词'}},
                'required': ['keyword']})

REVOKE_PER_TURN = 3      # 一轮最多撤几条：撤回不可逆，别让模型连点


@dataclass(slots=True)
class Reply:
    """ 插件对一条消息的决策。

    :param texts: 要发送的文本，可以为空
    :param stop: 是否终止后续插件的处理
    """

    texts: list[str] = field(default_factory=list)
    stop: bool = False
    emoji: Emoji | None = None      # 本轮要发的表情；发送由 Bot 统一走队列，插件不发
    at: str = ''                    # 要 @ 的群成员 wxid；由 Bot 解析名字后带回
    ref: int = 0                    # 要引用（微信「回复某条」）的消息 svrid；0 = 不引用
    verbatim: bool = False          # 一字不改发出去（复读机：抖错别字就不叫复读了）


# 模型爱顺手包上的东西：只剥真正在「包裹」的引号，括号不动
# （（目移）这类括号里的话在群里就是正文）
QUOTE_PAIRS = {'「': '」', '“': '”', '‘': '’', '"': '"', "'": "'", '《': '》', '〈': '〉'}
REPLY_PREFIXES = ('回复：', '回复:', '回复你：', '回复你:', '答：', '答:')
NAME_SEPARATORS = '：: ,，、'
BOLD = re.compile(r'^\*\*(.+)\*\*$')
CODE = re.compile(r'^`(.+)`$')


def clean_reply(text: str, names: Iterable[str] = ()) -> str:
    """ 清掉模型顺手带上的包装：昵称前缀、引号、书名号、markdown、多余空行。

    只动首尾和整句包裹，不碰正文里的标点。注意不能用 str.lstrip——
    它是按字符集剥的，'Bother'.lstrip('Bot:') 会变成 'her'。
    """
    cleaned = '\n'.join(line.strip() for line in (text or '').splitlines() if line.strip())
    if not cleaned:
        return ''

    changed = True
    while changed:  # 可能叠了几层：『"回复：xxx"』
        changed = False
        for name in names:
            if not name or not cleaned.startswith(name):
                continue
            rest = cleaned[len(name):]
            # 后面必须跟分隔符，否则「雨姐们」这种正文会被削掉
            if rest and rest[0] not in NAME_SEPARATORS:
                continue
            cleaned = rest.lstrip(NAME_SEPARATORS).strip()
            changed = True
        for prefix in REPLY_PREFIXES:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                changed = True
        if len(cleaned) >= 2 and QUOTE_PAIRS.get(cleaned[0]) == cleaned[-1]:
            cleaned = cleaned[1:-1].strip()
            changed = True

    for pattern in (BOLD, CODE):
        cleaned = pattern.sub(r'\1', cleaned)

    # 内置表情只能发名单里那 147 个，写错了就是纯文本方括号（见 emojis.filter_faces）
    cleaned, dropped = filter_faces(cleaned)
    if dropped:
        logger.warning('回复里的表情名不在内置名单里，已换成省略号：'
                       + '、'.join(f'[{name}]' for name in dropped))
    return cleaned


def _brief(text: str, limit: int = 80) -> str:
    """ 压成一行短摘要，给面板显示「为什么选择不发言」。 """
    one = ' '.join((text or '').split())
    return one if len(one) <= limit else one[:limit] + '…'


class Plugin(ABC):
    @abstractmethod
    async def handle(self, ctx: MessageContext) -> Reply | None:
        """ 处理消息。返回 None 表示不回复且继续后续插件。 """
        pass


class PluginManager:
    """Manages plugin registration and execution"""

    def __init__(self):
        self._defaults: list[Plugin] = []
        self._contact_mappings: dict[str, list[Plugin]] = {}

    def set_defaults(self, plugins: list[Plugin]) -> None:
        """ 整体替换默认插件链（热重载用）。contact 映射不动。 """
        self._defaults = list(plugins)

    def set_contact_plugins(self, contact: str, plugins: list[Plugin]) -> None:
        """ 替换某个会话的插件链（静音 / 解音）。幂等：重复调不会堆一长串。 """
        self._contact_mappings[contact] = list(plugins)
        logger.info(f'Mapped plugins {[p.__class__.__name__ for p in plugins]} to contact {contact}')

    def unmap_contact(self, contact: str) -> None:
        """ 恢复这个会话走默认插件链。 """
        self._contact_mappings.pop(contact, None)
        logger.info(f'Contact {contact} back to default plugins')

    def clear_contacts(self) -> None:
        """ 清掉所有会话映射，回到「大家都走默认链」。

        名单是每一版全量重装的，不先清就会留下上一版的映射。
        """
        self._contact_mappings.clear()

    def get_plugins_for_contact(self, contact: str) -> list[Plugin]:
        """ 注意语义：只要为某个 contact 映射过插件，就完全覆盖默认插件链（例如用它静音某个群）。 """
        return self._contact_mappings.get(contact, self._defaults)


class EndProcessing(Plugin):
    """ 终止处理、不回复。用于给指定会话静音。 """

    @override
    async def handle(self, ctx: MessageContext) -> Reply | None:
        return Reply(stop=True)


class RepeatPlugin(Plugin):
    def __init__(self, repeat_count: int = 3, context_length: int = 10, max_length: int = 20):
        """ 跟队形（复读机）插件

        :param repeat_count: 队形阈值。三条起才叫队形，两条更像碰巧重了
        :param context_length: 拉取的上下文长度
        :param max_length: 单条队形的最大长度。 python 会计算中文字符长度，一个中文字符算一个长度。
        """
        assert repeat_count > 1
        assert context_length > repeat_count
        assert max_length > 0

        self.repeat_count = repeat_count
        self.context_length = context_length
        self.max_length = max_length

    @override
    async def handle(self, ctx: MessageContext) -> Reply | None:
        """ 跟队形回复。 如果去掉中文英文标点及表情后的消息连续重复特定数量，则凑个热闹跟着回复一句。

        去重靠**历史里自己说过的话**（`blocked`），所以 Bot 必须在决策那一刻就把回复写进
        历史：历史晚写一步，紧接着又来了第三条队形，这里就看不到自己刚复读过，会再复读一遍。
        """
        if not ctx.is_text:  # 只处理文本消息
            return None

        history: ChatWindow = ctx.history.latest_n(self.context_length)
        if len(history) < self.repeat_count:
            return None

        # 对历史消息去除昵称、标点等操作；自己已经说过的队形不跟
        myself = ctx.self_name
        blocked = {x.pure_text() for x in history if x.sender == myself}
        groups: dict[str, list[SingleRecord]] = {}
        for record in history:
            if record.sender == myself:
                continue
            clean = record.pure_text()
            # 洗掉标点后是空串的（[表情：欢迎] 这类占位符）不能当队形，
            # 否则会把一串方括号当话发出去
            if clean and clean not in blocked:
                groups.setdefault(clean, []).append(record)

        # 找到最多的重复元素及次数, 其实就是找 groups 中 value 最长的.
        if not groups:
            return None
        repeated = max(groups.values(), key=len)
        if len(repeated) < self.repeat_count:
            return None

        # 找一条别人说过的队形，跟上
        record: SingleRecord = repeated[-1]
        # 复读的是用户看到的那句话。render 给引用消息补的「（引用前文的消息：…）」
        # 是给模型看的注解，原样发出去就是天书；注解永远缀在末尾且以「（引用」开头，
        # 砍一刀就够——正文里真写「（引用」的极罕见
        # ponytail: 只处理这一种注解，别再给渲染文本加会漏给用户的尾巴
        text: str = record.text.split('（引用', 1)[0].strip()
        if not text:
            return None
        # 太长不跟
        if len(text) > self.max_length:
            return None

        return Reply(texts=[text], stop=True, verbatim=True)


@dataclass(frozen=True)
class GateDecision:
    """ 门控的判断结果。

    比 NecessityScore 多一层「为什么没放行」——原来 evaluate() 只返回
    NecessityScore | None，把「是分数不够还是退避中」丢了，而那正是管理后台
    最需要的原因。所以单开一个详细版，evaluate() 保留原签名不动。
    """

    allowed: bool
    score: int = 0
    kind: str = ''
    reasons: tuple[str, ...] = ()
    blocked_by: str = ''            # 'score' | 'backoff' | 'rate' | ''
    score_obj: NecessityScore | None = None
    threshold: int = 0
    remaining: float = 0.0          # 退避里还剩多少秒
    rate_detail: str = ''           # 频率闸拦下时的原因

    @property
    def detail(self) -> str:
        if self.blocked_by == 'backoff':
            return f'沉默退避中，剩 {int(self.remaining)}s'
        if self.blocked_by == 'rate':
            return f'说得太频繁：{self.rate_detail}'
        if self.blocked_by == 'score':
            return f'门控 {self.score}/{self.threshold} · {"+".join(self.reasons) or "无"}'
        return ''


class ReplyPolicy:
    """ 群聊里「这条消息要不要交给模型」。

    便宜的结构性判断，不花 token：私聊 / 被 @ / 被叫到名字直接放行；
    其余群聊消息走必要性打分，过线才交给模型（见 necessity.py）。
    模型仍可用「本轮不发言」做内容层的最终否决。
    """

    def __init__(self, threshold: int = NECESSITY_THRESHOLD, activity_window: float = 180.0,
                 aliases: tuple[str, ...] = (), backoff: IdleBackoff | None = None,
                 focus: FocusMode | None = None, focus_score_bonus: int = 0,
                 aliases_for: Callable[[str], tuple[str, ...]] | None = None,
                 speech: SpeechLimiter | None = None):
        """ :param threshold: 必要性分数阈值，越高越沉默
        :param activity_window: 统计群活跃度的时间窗口（秒）
        :param aliases: 机器人自己的其他叫法，命中就当被叫到名字
        :param backoff: 连续沉默退避；不传则用默认（连续两轮不开口开始歇 15s）
        :param focus: 专注模式；群聊得热时盯一会儿，期间给必要性打分加分
        :param focus_score_bonus: 专注期的加分。默认 0——专注本身只意味着「不省那次模型调用」，
            而热群的压力分已经封顶送它过线；再加分就是热群必回（实测 82% 的轮次在专注期）
        :param aliases_for: 按会话取别名（分会话人设改了别名也要能叫应）；不传用上面的 aliases
        :param speech: 发言频率闸；不传就不限频（测试用）
        """
        self.threshold = threshold
        self.activity_window = activity_window
        self.aliases = aliases
        self.aliases_for = aliases_for or (lambda contact: self.aliases)
        self.backoff = backoff if backoff is not None else IdleBackoff()
        self.focus = focus
        self.focus_score_bonus = focus_score_bonus
        self.speech = speech

    def evaluate_detailed(self, ctx: MessageContext, now: float) -> GateDecision:
        """ 门控 + 理由。面板要显示「跳过的原因」，所以不能再只给 None。 """
        msg = ctx.msg
        activity = self._activity(ctx, now)
        result = score_necessity(NecessityInput(
            text=msg.content,
            is_group=msg.is_chatroom,
            has_at=msg.is_at_me,
            has_mention=self._mentions_me(ctx),
            activity=activity,
            self_replies=self._self_replies(ctx, now),
            recent_messages=self._recent_messages(ctx, now),
        ))
        # 专注模式：聊得正热就盯一会儿。**不再动分数**（focused 只用于跳过沉默退避
        # 和往 prompt 里加一句「可更主动」）——热群本来就有压力分，再加就成了热群必回
        if result.kind == 'proactive' and self.focus:
            if self.focus.tick(ctx.contact, now, activity):
                result = replace(result, focused=True, reasons=(*result.reasons, '专注'),
                                 score=min(100, result.score + self.focus_score_bonus))
        if result.score < self.threshold:
            return GateDecision(allowed=False, score=result.score, kind=result.kind,
                                reasons=result.reasons, blocked_by='score',
                                threshold=self.threshold)
        # 只有主动参与才退避：被 @ / 叫名字 / 私聊是必须回应的
        if (result.kind == 'proactive' and not result.focused
                and not self.backoff.allows(ctx.contact, now, ctx.history)):
            return GateDecision(allowed=False, score=result.score, kind=result.kind,
                                reasons=result.reasons, blocked_by='backoff',
                                threshold=self.threshold,
                                remaining=self.backoff.remaining(ctx.contact, now))
        # 频率闸：分数和退避都过了，也要看「我最近是不是说太多了」
        if result.kind == 'proactive' and self.speech is not None:
            refusal = self.speech.refusal(ctx.contact, now)
            if refusal:
                return GateDecision(allowed=False, score=result.score, kind=result.kind,
                                    reasons=result.reasons, blocked_by='rate',
                                    threshold=self.threshold, rate_detail=refusal)
        return GateDecision(allowed=True, score=result.score, kind=result.kind,
                            reasons=result.reasons, score_obj=result,
                            threshold=self.threshold)

    def evaluate(self, ctx: MessageContext, now: float) -> NecessityScore | None:
        """ 返回 None 表示不交给模型；否则返回打分（可注入 prompt 说明触发原因）。

        now 用 epoch 秒，与 history 里的时间戳同源。要「为什么没过」用 evaluate_detailed。
        """
        decision = self.evaluate_detailed(ctx, now)
        return decision.score_obj if decision.allowed else None

    def record(self, ctx: MessageContext, spoke: bool, now: float) -> None:
        """ 记下这一轮最后说没说：退避靠它清零，频率闸靠它计时。 """
        self.backoff.record(ctx.contact, spoke, now, ctx.history)
        if spoke and self.speech is not None:
            self.speech.note(ctx.contact, now)

    def _activity(self, ctx: MessageContext, now: float) -> int:
        """ 活动窗口内别人的消息数。 """
        return sum(1 for r in ctx.history if not r.is_self and now - r.t <= self.activity_window)

    def _recent(self, ctx: MessageContext, now: float) -> list[SingleRecord]:
        return [r for r in ctx.history if now - r.t <= PRESENCE_WINDOW_SECONDS]

    def _self_replies(self, ctx: MessageContext, now: float) -> int:
        return sum(1 for r in self._recent(ctx, now) if r.is_self)

    def _recent_messages(self, ctx: MessageContext, now: float) -> int:
        return len(self._recent(ctx, now))

    def _mentions_me(self, ctx: MessageContext) -> bool:
        """ 群昵称或任一别名出现在消息里，就算在叫它。别名按会话取（可能有覆盖）。 """
        names = (ctx.self_name, *self.aliases_for(ctx.contact))
        return any(name and name in ctx.msg.content for name in names)


def _mime(raw: bytes) -> str:
    """ 按魔数判图片格式。以前写死 image/jpeg，小 PNG / GIF 会被标错。 """
    if raw.startswith(b'\x89PNG'):
        return 'image/png'
    if raw.startswith(b'GIF8'):
        return 'image/gif'
    if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return 'image/webp'
    if raw[:2] == b'BM':
        return 'image/bmp'
    return 'image/jpeg'


class ChatPlugin(Plugin):
    """ 聊天插件。两段式：Planner 判断说与不说、说什么，Expressor 负责说成人话。

    Planner 被明确告知「你不是本人，你是决策者」，输出的是判断而不是台词；
    Expressor 拿人设 + 表达风格 + 决策当作参考，只负责组织语言。
    enable_planner=False 时退回单段（Expressor 直接看着聊天记录说），便于对比。

    被 @ / 被叫名字 / 私聊也走决策者——它们只需要表情工具，而工具在决策者手里；
    区别是这几个场景下决策者的「不发言」被忽略（必须给个回应），只是额外花一次调用。
    """

    def __init__(self, ai_provider: AiProvider, context_length: int = 10, policy: ReplyPolicy | None = None,
                 persona: Persona | None = None, profiles: ProfileStore | None = None,
                 habits: HabitStore | None = None, memories: MomentStore | None = None,
                 tracer: TraceLogger | None = None, enable_planner: bool = True,
                 planner_ai: AiProvider | None = None,
                 emoji_search: EmojiSearch | None = None, enable_tools: bool = True,
                 planner_rounds: int = 3, emoji_policy: EmojiPolicy | None = None,
                 reply_mode: str = 'two_stage', revoke: Revoke | None = None,
                 timeout: float = 60.0, rng: random.Random | None = None,
                 agent_max_replies: int = 1, personas: PersonaStore | None = None,
                 member_lookup: MemberLookup | None = None,
                 message_search: MessageSearch | None = None,
                 image_store: ImageStore | None = None, image_timeout: float = 8.0,
                 pat: PatAction | None = None):
        """ 聊天插件

        :param ai_provider: AI 服务提供者
        :param context_length: 送入模型的上下文消息条数
        :param policy: 是否回复的策略；不传则用默认（必要性打分）
        :param persona: 人格配置（身份 / 表达风格 / 行动准则 / 临时风格）
        :param profiles: 群友画像；传了就注入 prompt 让机器人认识人
        :param habits: 群里的特色用语；当前消息命中才注入
        :param memories: 中期记忆；按当前消息关键词召回，命中才注入
        :param tracer: 回复轨迹记录；传了就把每轮触发/上下文/回复写成 JSONL
        :param enable_planner: 是否跑两段式；False 则只调 Expressor
        :param planner_ai: 决策者专用模型；留空则和 Expressor 用同一个
        :param emoji_search: 搜表情的函数；不传就只剩说话，决策者看不到表情工具
        :param enable_tools: 决策者是否用工具调用；关掉就退回单轮 JSON 决策
        :param planner_rounds: 决策循环最多几轮（搜 → 发 两轮就够）
        :param emoji_policy: 发表情的节制（间隔 + 不重复）；模型看不见自己上次什么时候发的
        :param reply_mode: 'two_stage' = 决策者 + 表达者；'agent' = 单模型，回话就是 reply 工具
        :param revoke: 撤回入口（Bot 提供）；不传就没有 revoke 工具
        :param timeout: 单次模型调用超时（秒）
        :param rng: 抽临时风格用的随机源，测试时可注入
        :param agent_max_replies: agent 模式一轮最多发几条消息
        :param personas: 分会话人设覆盖；传了就按会话取人设，没覆盖的回落到 persona
        :param member_lookup: 群昵称 → wxid；不传就 @ 不了人
        :param message_search: 搜更早聊天记录；不传就没 search_messages 工具
        :param image_store: 窗口里图片的压缩 + 缓存；不传就不内联图片（测试用）
        :param image_timeout: 单张内联图片的取字节超时（秒）
        :param pat: 拍一拍入口（Bot 提供）；不传就没 pat 工具
        """
        self.ai = ai_provider
        self._planner_ai = planner_ai
        self.context_length = context_length
        self.policy = policy or ReplyPolicy()
        self.persona = persona or Persona()
        self.personas = personas
        self.profiles = profiles
        self.habits = habits
        self.memories = memories
        self.tracer = tracer
        self.enable_planner = enable_planner
        self.emoji_search = emoji_search
        self.enable_tools = enable_tools
        self.planner_rounds = planner_rounds
        self.emoji_policy = emoji_policy or EmojiPolicy()
        self.reply_mode = reply_mode
        self.revoke = revoke
        self.member_lookup = member_lookup
        self.message_search = message_search
        self.pat = pat
        self.image_store = image_store
        self.image_timeout = image_timeout
        self.timeout = timeout
        self.rng = rng
        self.agent_max_replies = agent_max_replies

        from jinja2 import Template
        base = Path(__file__).parent
        # trim_blocks/lstrip_blocks：{% if %} 独占一行时不留空行。
        # 不设的话每个分支里都会多出一堆空行，白烧 token。
        options = {'enable_async': True, 'trim_blocks': True, 'lstrip_blocks': True}
        self.planner_template = Template(
            (base / 'planner.txt').read_text(encoding='utf-8'), **options)
        self.replyer_template = Template(
            (base / 'replyer.txt').read_text(encoding='utf-8'), **options)
        self.agent_template = Template(
            (base / 'agent.txt').read_text(encoding='utf-8'), **options)

    @property
    def planner_ai(self) -> AiProvider:
        """ 决策者用的模型；没单独配就和表达者共用一个。动态回退，换 ai 也能跟上。 """
        return self._planner_ai or self.ai

    def persona_for(self, contact: str) -> Persona:
        """ 这个会话用的人设：有分会话覆盖就盖上，没有就用全局。 """
        if self.personas is None:
            return self.persona
        return self.personas.persona_for(contact, self.persona)

    @override
    async def handle(self, ctx: MessageContext) -> Reply | None:
        # 这里**不按 msg_type 筛消息**。系统/未知类型在 `Wechat._dispatch` 就丢了，
        # 剩下的每一种 `media.render` 都能渲染成可读文本。以前这里又列一遍
        # 「文本/图片/表情/语音」，结果是引用（type=49）被当成「不支持的类型」没人回——
        # 而它的正文就是一句话；@ 上一篇文章也一样被吞。
        now = time.time()
        decision = self.policy.evaluate_detailed(ctx, now)
        if not decision.allowed:
            blocked = {'score': Status.GATED, 'rate': Status.RATE_LIMITED}.get(
                decision.blocked_by, Status.BACKOFF)
            self._emit(ctx, blocked, decision.detail, score=decision.score)
            return None
        necessity = decision.score_obj

        history: ChatWindow = ctx.history.latest_n(self.context_length)
        blocks = self._context_blocks(ctx, history)
        # 这两块是「本轮」的事：专注状态和必须回应，都放请求正文，不进 system prompt
        if necessity.focused and self.policy.focus:
            blocks['focus'] = self.policy.focus.describe(ctx.contact)
        must_speak = necessity.kind != 'proactive'      # 被点名 / 私聊：必须回
        if must_speak:
            blocks['must'] = f'这条点名了 {ctx.self_name}，必须给个回应，不能装没看见。'
        transcript = '以下聊天记录供你参考：\n\n' + str(history)
        # 窗口里的图一并取回来附给模型（新→旧、按媒体去重）。取不到就少一张，不影响回复
        images = await self._collect_images(history)

        # agent 模式：判断和台词在同一次调用里完成，不走表达者
        if self.reply_mode == 'agent' and self._tools_ready:
            return await self._agent_round(ctx, blocks, transcript, images, must_speak, necessity, now)

        started = time.perf_counter()
        plan = ''
        emoji: Emoji | None = None
        at = ''
        ref = 0
        if self.enable_planner:
            self._emit(ctx, Status.DECIDING, f'{necessity.kind} · 分数 {necessity.score}')
            if self._tools_ready:
                decided, emoji, at, ref, tool_log = await self._plan_with_tools(ctx, blocks, transcript, images)
            else:
                decided, tool_log = await self._plan(ctx, necessity, blocks, transcript, images), ''
            # 决策本身没跑通（超时/报错）：不算「不发言」，照走表达者
            if decided is None:
                self._emit(ctx, Status.PLANNER_FAILED, '决策调用失败，直接表达')
            # 必须回应的场景忽略「不发言」，但采纳它挑的表情——
            # 被 @ 时配个表情本来就是最自然的
            if decided is not None and not decided.speak and not must_speak:
                self._trace(ctx, necessity, transcript, None, self._elapsed(started), tool_log)
                self._emit(ctx, Status.PLANNER_SILENT, _brief(decided.analysis))
                return self._silent(ctx, now, emoji=emoji)
            if decided is not None and decided.speak:
                plan = decided.render()
            if tool_log:
                plan = f'{plan}\n【工具】\n{tool_log}' if plan else f'【工具】\n{tool_log}'

        self._emit(ctx, Status.EXPRESSING)
        response = await self._express(ctx, blocks, transcript, images, plan)
        latency_ms = self._elapsed(started)
        self._trace(ctx, necessity, transcript, response, latency_ms, plan)
        if response is None:
            self._emit(ctx, Status.EXPRESS_FAILED, '表达者调用失败')
            return self._silent(ctx, now, emoji=emoji)

        # 模型常顺手带上「昵称：」前缀或引号包裹，不能直接漏到群里（见 _clean）
        texts = self._clean(response, ctx)
        if not texts:
            self._emit(ctx, Status.EMPTY_REPLY)
            return self._silent(ctx, now, emoji=emoji)
        self.policy.record(ctx, spoke=True, now=now)
        return Reply(texts=texts, emoji=emoji, at=at, ref=ref)

    def _emit(self, ctx: MessageContext, status: str, detail: str = '', **fields) -> None:
        """ 把这一轮的状态发给管理后台。失败也不影响聊天（bus 自己吞异常）。

        msg_id 用 getattr：这是可观测性，不该因为拿不到身份键就把回复链路打断。
        """
        bus.publish(status, chat_id=ctx.msg.reply_target, chat_name=ctx.contact,
                    msg_id=getattr(ctx.msg, 'msg_id', ''), detail=detail, **fields)

    @property
    def _tools_ready(self) -> bool:
        """ 决策者能不能用工具。后端不支持就退回单轮 JSON，功能不受影响。 """
        return self.enable_tools and bool(getattr(self.planner_ai, 'supports_tools', False))

    def _silent(self, ctx: MessageContext, now: float, emoji: Emoji | None = None) -> Reply:
        """ 这一轮没说话（最多只发了个表情），记一笔给连续沉默退避用。 """
        self.policy.record(ctx, spoke=emoji is not None, now=now)
        return Reply(stop=True, emoji=emoji)

    def _emoji_tools(self, state: dict) -> list:
        """ 搜表情 / 发一张。两处决策路径（两段式的 speak、agent 的 reply）共用。

        state 就是个可变字典 {'candidates': [...], 'picked': [...]}：
        send_emoji 的序号是上一次搜索的结果，所以状态得跨轮留着。
        """
        async def search(arguments: dict) -> ToolResult:
            keyword = arg_str(arguments, 'keyword')[:MAX_EMOJI_KEYWORD]
            if not keyword:
                return ToolResult('没给关键词，什么也没搜到。')
            if self.emoji_search is None:
                return ToolResult('这会儿搜不了表情。')
            found = await self.emoji_search(keyword)
            if not found:
                return ToolResult(f'没搜到跟「{keyword}」有关的表情。换个词，或者不用表情。')
            state['candidates'][:] = found
            listing = '\n'.join(item.render(i) for i, item in enumerate(found, 1))
            return ToolResult(f'搜到这些，序号可以直接给 send_emoji：\n{listing}')

        async def send(arguments: dict) -> ToolResult:
            candidates = state['candidates']
            if not candidates:
                return ToolResult('还没搜过。先用 search_emoji 搜一个词。')
            index = arg_int(arguments, 'index', 0)
            if not 1 <= index <= len(candidates):
                return ToolResult(f'没有第 {index} 张，只有 1 到 {len(candidates)}。')
            chosen = candidates[index - 1]
            # 刚发过、或者这张刚发过，就别发了——模型不知道这些
            refusal = self.emoji_policy.refuses(chosen.md5, time.time())
            if refusal:
                return ToolResult(refusal)
            state['picked'].append(chosen)
            self.emoji_policy.note(chosen.md5, time.time())
            return ToolResult(f'好，这一轮发这张：{chosen.desc or "选中的表情"}。', stop=True)

        tools = [(SEARCH_EMOJI, search)]
        if self.emoji_search is not None:
            tools.append((SEND_EMOJI, send))
        return tools

    def _revoke_tool(self, ctx: MessageContext):
        """ 撤回自己刚发错的一条。一轮最多撤 REVOKE_PER_TURN 次（不可逆，别让模型连点）。 """
        calls = {'n': 0}

        async def revoke(arguments: dict) -> ToolResult:
            if self.revoke is None:
                return ToolResult('这会儿撤不了。')
            if calls['n'] >= REVOKE_PER_TURN:
                return ToolResult('这一轮撤得够多了，先说想说的。')
            calls['n'] += 1
            return ToolResult(await self.revoke(ctx.msg.reply_target,
                                                arg_int(arguments, 'index', 0),
                                                arg_str(arguments, 'text')))

        return revoke

    def _pat_tool(self, ctx: MessageContext, state: dict) -> list:
        """ 拍一拍。私聊不填名字就是拍对方；群聊按群昵称找 wxid。 """
        if self.pat is None:
            return []

        async def pat(arguments: dict) -> ToolResult:
            if state.get('pat'):
                return ToolResult('这一轮已经拍过了，先做别的。')
            target = arg_str(arguments, 'target')
            ok, message = await self.pat(ctx.msg.reply_target, target)
            if ok:
                state['pat'] = message
            return ToolResult(message)

        return [(PAT, pat)]

    def _message_tools(self, ctx: MessageContext) -> list:
        """ 查更早的聊天记录。当前窗口里搜不到的东西，模型自己发起去搜。 """
        if self.message_search is None:
            return []

        async def search(arguments: dict) -> ToolResult:
            keyword = arg_str(arguments, 'keyword')
            if not keyword:
                return ToolResult('没给关键词，什么也没搜到。')
            found = await self.message_search(ctx.msg.reply_target, keyword)
            if not found:
                return ToolResult(f'更早的记录里没搜到跟「{keyword}」有关的，换个词或者当你不知道。')
            return ToolResult(f'更早的记录里翻到这些（只看跟当前话题有关的）：\n{found}')

        return [(SEARCH_MESSAGES, search)]

    async def _resolve_at(self, arguments: dict, state: dict, ctx: MessageContext) -> str:
        """ 解析 @ 目标，把 wxid 存进 state（本轮结束时由 Reply 带出去给 Bot 发）。

        返回一句给模型看的回执；没提 @ 就返回空串。名字对不上人时不拦回复——
        @ 不到人不是不发这条消息的理由。
        """
        name = arg_str(arguments, 'at')
        if not name:
            return ''
        if not ctx.msg.is_chatroom:
            return '（这是私聊，@ 不了人。）'
        if self.member_lookup is None:
            return '（这会儿 @ 不了人。）'
        if state.get('at'):
            return f'（这一轮已经 @ 过别人了，「{name}」没算。）'
        wxid = await self.member_lookup(ctx.msg.reply_target, name)
        if not wxid:
            return f'（群里没找到叫「{name}」的人，这条没 @ 成。）'
        state['at'] = wxid
        return f'（这条会 @{name}。）'

    @staticmethod
    def _resolve_ref(arguments: dict, state: dict, ctx: MessageContext) -> str:
        """ 解析引用目标：聊天记录里的 #标签 → 那条消息的 svrid，存进 state。

        返回一句给模型看的回执；没给 ref 就返回空串。标签对不上（写错 / 已被刷出
        窗口）就不引用——引用错了比不引用更糟，回执说清楚，让模型自己决定要不要重发。
        """
        ref = arg_str(arguments, 'ref')
        if not ref:
            return ''
        record = ctx.history.find_by_handle(ref)
        if record is None:
            return f'（聊天记录里没有「{ref}」这条，这条就不引用了。）'
        state['ref'] = record.msg_id
        return f'（这条会引用 {message_handle(record.msg_id)}。）'

    def _tools(self, state: dict, terminal: ToolSpec, handler, ctx: MessageContext) -> list:
        """ 一轮的完整工具集：表情对 + 终点动作（speak / reply）+ 查历史 +（可选）撤回。 """
        tools = self._emoji_tools(state)
        tools.append((terminal, handler))
        tools.extend(self._message_tools(ctx))
        tools.extend(self._pat_tool(ctx, state))
        if self.revoke is not None:
            tools.append((REVOKE, self._revoke_tool(ctx)))
        return tools

    async def _plan_with_tools(self, ctx: MessageContext, blocks: dict[str, str], transcript: str,
                               images: list[tuple[str, bytes]]) \
            -> tuple[Plan | None, Emoji | None, str, int, str]:
        """ 带工具的决策循环（两段式）。返回（决策, 选中的表情, 要 @ 的人, 引用的消息 svrid, 工具记录）。

        模型只挑动作（搜表情 / 选一张 / 说要说话），不写台词——台词还是表达者的事。
        一个工具都不调 = 本轮不发言；调了动作类工具本轮就结束。
        """
        state: dict = {'candidates': [], 'picked': [], 'at': '', 'ref': 0, 'pat': ''}
        said = {'intent': '', 'emotion': ''}

        async def speak(arguments: dict) -> ToolResult:
            said['intent'] = arg_str(arguments, 'intent')
            said['emotion'] = arg_str(arguments, 'emotion')
            note = (await self._resolve_at(arguments, state, ctx)
                    + self._resolve_ref(arguments, state, ctx))
            return ToolResult(f'好，交给表达者。{note}', stop=True)

        toolbox = Toolbox(self._tools(state, SPEAK, speak, ctx))
        prompt = await self._planner_prompt(ctx, toolbox.names)
        messages = [{'role': 'user',
                     'content': self._build_message(self._compose(ctx, blocks, transcript), images)}]
        analysis = ''
        for _ in range(max(1, self.planner_rounds)):
            turn = await self._ask_tools(prompt, messages, toolbox.schema(), ctx)
            if turn is None:
                return None, None, '', 0, toolbox.render_calls()      # 这次没拿到决策
            analysis = turn.content or turn.reasoning or analysis
            if not turn.tool_calls:                    # 什么都不调 = 不发言
                break
            messages.append(assistant_message(turn))
            stopped = False
            for call in turn.tool_calls:
                result = await toolbox.call(call)
                stopped = stopped or result.stop
                messages.append(tool_message(call, result.content))
            if stopped:
                break

        emoji = state['picked'][0] if state['picked'] else None
        log = toolbox.render_calls()
        if emoji is None and not said['intent']:
            return Plan(speak=False, analysis=analysis), None, '', 0, log
        plan = Plan(speak=bool(said['intent']), analysis=analysis, intent=said['intent'],
                    emotion=said['emotion'], emoji=emoji.desc if emoji else '')
        return plan, emoji, state['at'], state['ref'], log

    async def _reply_agent(self, ctx: MessageContext, blocks: dict[str, str], transcript: str,
                           images: list[tuple[str, bytes]], must_speak: bool) \
            -> tuple[list[str], Emoji | None, str, int, str]:
        """ 单段式：模型自己判断、自己写台词，回话就是一次 `reply` 调用。

        与 _plan_with_tools 只差终点动作：那边调 speak（只说意图），这边调 reply（原话）。
        表情工具、查历史工具、撤回工具、循环骨架完全一致。
        """
        state: dict = {'candidates': [], 'picked': [], 'at': '', 'ref': 0, 'pat': ''}
        replies: list[str] = []

        async def reply(arguments: dict) -> ToolResult:
            text = arg_str(arguments, 'text')
            if not text:
                return ToolResult('text 是空的，没发出去。')
            if len(replies) >= self.agent_max_replies:
                return ToolResult('这一轮说得够多了，剩下的下轮再说。')
            replies.append(text)
            note = (await self._resolve_at(arguments, state, ctx)
                    + self._resolve_ref(arguments, state, ctx))
            # stop=True：调完本轮就收，别再多跑一轮去确认。同一轮里的多个 reply
            # 照样全部执行（Toolbox.call 是先把这轮的调用跑完再 break），所以多行不受影响
            return ToolResult(f'好，这条算说完了（第 {len(replies)} 条）。{note}', stop=True)

        toolbox = Toolbox(self._tools(state, REPLY, reply, ctx))
        prompt = await self._agent_prompt(ctx, toolbox.names)
        messages = [{'role': 'user',
                     'content': self._build_message(self._compose(ctx, blocks, transcript), images)}]
        forced = False
        for _ in range(max(1, self.planner_rounds)):
            turn = await self._ask_tools(prompt, messages, toolbox.schema(), ctx)
            if turn is None:
                break
            if not turn.tool_calls:
                # 点了名却什么都没做：只补一轮提醒，之后不再纠缠
                # 拍了别人也算回应过——别把「拍回去」当成什么都没做
                if must_speak and not replies and not state.get('pat') and not forced:
                    forced = True
                    messages.append({'role': 'user',
                                     'content': '【必须回应】你还没给回应。至少调一次 reply，'
                                                '或者发个表情。'})
                    continue
                break
            messages.append(assistant_message(turn))
            stopped = False
            for call in turn.tool_calls:
                result = await toolbox.call(call)
                stopped = stopped or result.stop
                messages.append(tool_message(call, result.content))
            if stopped:
                break
        return (replies, (state['picked'][0] if state['picked'] else None),
                state['at'], state['ref'], toolbox.render_calls())

    async def _agent_round(self, ctx: MessageContext, blocks: dict[str, str], transcript: str,
                           images: list[tuple[str, bytes]], must_speak: bool,
                           necessity: NecessityScore, now: float) -> Reply:
        """ agent 模式的一轮：决策和表达在同一次调用里完成，没有表达者这一步。 """
        self._emit(ctx, Status.DECIDING, f'{necessity.kind} · agent')
        started = time.perf_counter()
        texts, emoji, at, ref, tool_log = await self._reply_agent(ctx, blocks, transcript, images, must_speak)
        self._trace(ctx, necessity, transcript, texts or None, self._elapsed(started), tool_log)
        cleaned = self._clean(texts, ctx)
        if not cleaned:
            self._emit(ctx, Status.PLANNER_SILENT, 'agent 选择不发言')
            return self._silent(ctx, now, emoji=emoji)
        self.policy.record(ctx, spoke=True, now=now)
        return Reply(texts=cleaned, emoji=emoji, at=at, ref=ref)

    async def _ask_tools(self, prompt: str, messages: list[dict], tools: list[dict],
                         ctx: MessageContext) -> AssistantTurn | None:
        """ 调一轮工具。超时或报错只当这次没结果，不能让一次 API 抽风拖死消息处理链。 """
        try:
            return await asyncio.wait_for(
                self.planner_ai.chat_tools(prompt.strip(), messages, tools), self.timeout)
        except Exception as e:
            logger.warning(f'决策工具调用失败（{ctx.contact}）: {e}')
            return None

    def _context_blocks(self, ctx: MessageContext, history: ChatWindow) -> dict[str, str]:
        """ 画像 / 群用语 / 中期记忆，三者都是「能捞到才注入」。 """
        msg = ctx.msg
        block = {'profiles': '', 'habits': '', 'memories': ''}
        if self.profiles:
            block['profiles'] = self.profiles.render_for(record.sender for record in reversed(history))
        if self.habits:
            block['habits'] = self.habits.render(msg.content)
        if self.memories:
            recalled = self.memories.search(msg.reply_target, extract_keywords(msg.content))
            block['memories'] = '\n'.join(moment.render() for moment in recalled)
        return block

    @staticmethod
    def _compose(ctx: MessageContext, blocks: dict[str, str], transcript: str,
                 plan: str = '', alternate: str = '') -> str:
        """ 拼请求正文。

        所有会变的东西（本轮消息 / 决策 / 临时风格 / 画像 / 群用语 / 记忆）都放这里，
        不放 system prompt——system 恒定才能吃到 KV 前缀缓存。
        """
        lines = [f'【本轮】{ctx.msg.sender_name}: {ctx.msg.content}']
        for label, key in (('必须回应', 'must'), ('专注模式', 'focus')):
            if blocks.get(key):
                lines.append(f'【{label}】\n{blocks[key]}')
        if plan:
            lines.append(f'【决策】\n{plan}')
        if alternate:
            lines.append(f'【临时风格】{alternate}')
        for label, key in (('群友画像', 'profiles'), ('群里的说法', 'habits'), ('记忆（可能记错，别当准）', 'memories')):
            if blocks.get(key):
                lines.append(f'【{label}】\n{blocks[key]}')
        lines.append(transcript)
        return '\n\n'.join(lines)

    async def _planner_prompt(self, ctx: MessageContext, tools: tuple[str, ...] = ()) -> str:
        """ tools 传空就用单轮 JSON 那套输出格式，传工具名就换成工具行动那套。 """
        persona = self.persona_for(ctx.contact)
        return await self.planner_template.render_async(behavior_style=persona.behavior_style,
                                                        self_name=ctx.self_name, contact=ctx.contact,
                                                        is_group=ctx.msg.is_chatroom,
                                                        aliases=persona.aliases, tools=tools)

    async def _agent_prompt(self, ctx: MessageContext, tools: tuple[str, ...] = ()) -> str:
        """ agent 模式的 system prompt：人设 + 行动准则 + 工具说明，一次调用把判断和台词都做了。 """
        persona = self.persona_for(ctx.contact)
        return await self.agent_template.render_async(
            identity=persona.identity(ctx.self_name), reply_style=persona.reply_style,
            behavior_style=persona.behavior_style, self_name=ctx.self_name,
            contact=ctx.contact, is_group=ctx.msg.is_chatroom, aliases=persona.aliases,
            tools=tools, faces=FACE_CATALOG)

    def _clean(self, texts: Iterable[str] | None, ctx: MessageContext) -> list[str]:
        """ 模型常顺手带上「昵称：」前缀或引号包裹，不能直接漏到群里。 """
        names = (ctx.self_name, *self.persona_for(ctx.contact).aliases)
        return [text for text in (clean_reply(x, names) for x in texts or ()) if text]

    async def _plan(self, ctx: MessageContext, necessity: NecessityScore, blocks: dict[str, str],
                    transcript: str, images: list[tuple[str, bytes]]) -> Plan | None:
        """ 单轮 JSON 决策。后端不支持工具时走这条。 """
        raw = await self._ask(await self._planner_prompt(ctx), self._compose(ctx, blocks, transcript),
                              ctx, images, ai=self.planner_ai)
        if not raw:
            return None
        # ai.chat 返回的是按行切好的列表，决策是一整块 JSON，拼回去再解析
        return Plan.parse('\n'.join(raw))

    async def _express(self, ctx: MessageContext, blocks: dict[str, str], transcript: str,
                       images: list[tuple[str, bytes]], plan: str) -> list[str] | None:
        """ 跑表达者。先把「本轮要回哪条」摆明，再给决策；没决策也不会跑偏到旧话题。 """
        persona = self.persona_for(ctx.contact)
        prompt = await self.replyer_template.render_async(
            identity=persona.identity(ctx.self_name), reply_style=persona.reply_style,
            self_name=ctx.self_name, contact=ctx.contact, is_group=ctx.msg.is_chatroom,
            faces=FACE_CATALOG)
        message = self._compose(ctx, blocks, transcript, plan=plan,
                                alternate=persona.maybe_alternate(self.rng))
        return await self._ask(prompt, message, ctx, images)

    async def _ask(self, prompt: str, message: str, ctx: MessageContext,
                   images: list[tuple[str, bytes]], ai: AiProvider | None = None) -> list[str] | None:
        """ 调模型。超时或报错只当这次没结果——不能让一次 API 抽风拖死消息处理链。 """
        provider = ai or self.ai
        try:
            return await asyncio.wait_for(
                provider.chat(prompt.strip(), self._build_message(message, images)), self.timeout)
        except Exception as e:
            logger.warning(f'调用模型失败（{ctx.contact}）: {e}')
            return None

    @staticmethod
    def _elapsed(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    async def _complete(self, prompt: str, message: str,
                        ai: AiProvider | None = None) -> list[str] | None:
        """ 单轮纯文本调用。重放用——没有 ctx，不走 _ask 的图片分支。 """
        try:
            return await asyncio.wait_for((ai or self.ai).chat(prompt.strip(), message), self.timeout)
        except Exception as e:
            logger.warning(f'调用模型失败（重放）: {e}')
            return None

    async def replay(self, transcript: str, plan: str = '', self_name: str = '',
                     contact: str = '', stage: str = 'all') -> dict:
        """ 用**当前**提示词重跑这一轮，只看输出：不碰历史、不进队列、不发送。

        三个如实说明的限制：
        1. 上下文只能用 trace 里存的那份聊天记录——画像 / 群用语 / 中期记忆当时注入了
           什么没有留痕，所以重放的输入比真实那一轮少几块；
        2. 决策那一步走**单轮 JSON 模式**（线上是工具模式），两者提示词不同，
           拿它比决策质量不可靠；
        3. 比表达风格（replyer）是可靠的，那也是调得最多的那个。
        """
        started = time.perf_counter()
        persona = self.persona_for(contact)
        new_plan = plan
        if stage == 'all' and self.enable_planner:
            prompt = await self.planner_template.render_async(
                behavior_style=persona.behavior_style, self_name=self_name, contact=contact,
                is_group=True, aliases=persona.aliases, tools=())
            raw = await self._complete(prompt, transcript, ai=self.planner_ai)
            decided = Plan.parse('\n'.join(raw)) if raw else None
            new_plan = decided.render() if decided and decided.speak else ''

        replyer = await self.replyer_template.render_async(
            identity=persona.identity(self_name), reply_style=persona.reply_style,
            self_name=self_name, contact=contact, is_group=True, faces=FACE_CATALOG)
        body = f'{new_plan}\n\n{transcript}' if new_plan else transcript
        reply = await self._complete(replyer, body)
        return {'stage': stage, 'plan': new_plan, 'reply': reply or [],
                'latency_ms': self._elapsed(started)}

    def _trace(self, ctx: MessageContext, necessity: NecessityScore, transcript: str,
               response: list[str] | None, latency_ms: int, plan: str) -> None:
        """ 记录这一轮：为什么开口、看到了什么、说了什么。 """
        if not self.tracer:
            return
        msg = ctx.msg
        self.tracer.log(TraceRecord(
            ts=time.time(), contact=msg.reply_target, contact_name=ctx.contact,
            sender=msg.sender_name, trigger=necessity.kind, score=necessity.score,
            reasons=necessity.reasons, transcript=transcript, reply=tuple(response or ()),
            latency_ms=latency_ms, plan=plan, mode=self.reply_mode,
            msg_id=str(getattr(msg, 'msg_id', ''))))

    async def _collect_images(self, history: ChatWindow) -> list[tuple[str, bytes]]:
        """ 把窗口里能拿到的图片都取回来（新→旧、按媒体 svrid 去重）。

        每条记录自己知道字节从哪来（自己发的图 / 引用的图），这里只负责并发取、压缩、
        缓存。取不到 / 超时的那张直接跳过——少一张图顶多少点信息，不能让一轮回复卡住。
        """
        if self.image_store is None:
            return []
        pending: list = []
        seen: set[int] = set()
        for record in reversed(list(history)):
            if not record.media_id or record.media_id in seen or record.download is None:
                continue
            seen.add(record.media_id)
            pending.append(record)

        async def fetch(record) -> tuple[str, bytes] | None:
            try:
                raw = await asyncio.wait_for(record.download(), self.image_timeout)
            except Exception as e:  # noqa: BLE001 —— 一张图取不到不该影响这一轮
                logger.debug(f'内联图片失败 media_id={record.media_id}: {e}')
                return None
            if not raw:
                return None
            # 压缩是 CPU 活，丢到线程里，别把消息循环堵住
            return record.handle, await asyncio.to_thread(
                self.image_store.prepare, record.media_id, raw)

        # 并发取：串行的话 N 张图 × 单张超时会把这一轮拖到没法看
        results = await asyncio.gather(*(fetch(record) for record in pending))
        return [item for item in results if item is not None]

    @staticmethod
    def _build_message(message: str, images: list[tuple[str, bytes]]) -> str | list:
        """ 文本消息直接发；窗口里的图片作为 content block 附在文本后（原生识图）。

        images 是 (handle, 字节)；handle 就是聊天记录里那行 `[图片 #abcd]` 的标签，
        每张图前面配一行同样的标签，多图时模型才知道哪张对应哪句话。
        没有图片（或全取不到）就退回纯文本：拿不到图是常事，不该把整轮回复带崩。
        """
        if not images:
            return message
        blocks: list[dict] = [{'type': 'text', 'text': message}]
        for handle, raw in images:
            blocks.append({'type': 'text', 'text': f'[图片 {handle}]'})
            encoded = base64.b64encode(raw).decode()
            blocks.append({'type': 'image_url',
                           'image_url': {'url': f'data:{_mime(raw)};base64,{encoded}'}})
        return blocks
