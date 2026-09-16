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
from context import ChatWindow, MessageContext, SingleRecord
from emojis import Emoji, EmojiPolicy
from events import Status, bus
from focus import FocusMode
from habits import HabitStore
from moments import MomentStore, extract_keywords
from necessity import (
    NECESSITY_THRESHOLD,
    PRESENCE_WINDOW_SECONDS,
    IdleBackoff,
    NecessityInput,
    NecessityScore,
    score_necessity,
)
from persona import Persona
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

# 搜表情：给个关键词，还一堆能发出去的候选。由 Bot 提供（它才连 relay）。
# 插件自己不发表情——发送统一走 OutboundQueue，那里的限速不能绕过去。
EmojiSearch = Callable[[str], Awaitable[list[Emoji]]]

# 决策者的工具。规则的细节写在这里，planner.txt 里只列一行——
# 模型看得到每个工具自己的说明书，比在系统提示里堆一大段更不容易漏。
SEARCH_EMOJI = ToolSpec(
    name='search_emoji',
    description='按关键词搜表情包，返回带序号的候选。关键词用一个短的情绪或动作词'
                '（如「无语」「狗头」「点赞」「撑不住了」），不要写一整句话。'
                '一次只搜一个词；没合适的就换个词再搜一次，别反复搜同一个。',
    parameters={'type': 'object',
                'properties': {'keyword': {'type': 'string', 'description': '短的情绪或动作词'}},
                'required': ['keyword']})
SEND_EMOJI = ToolSpec(
    name='send_emoji',
    description='发出上一次 search_emoji 结果里的第 index 张表情。必须先搜再发。'
                '表情是情绪，不是回答——别人在问问题还是得用 speak 答。'
                '调用这个工具本轮就结束了；想配一句话就同一轮里一起调 speak。',
    parameters={'type': 'object',
                'properties': {'index': {'type': 'integer', 'description': '候选里的序号，从 1 开始'}},
                'required': ['index']})
SPEAK = ToolSpec(
    name='speak',
    description='让机器人用文字说一句。intent 是你想表达什么，不是台词本身——'
                '台词由别人负责写。调用这个工具本轮就结束了。',
    parameters={'type': 'object',
                'properties': {'intent': {'type': 'string', 'description': '想表达什么'},
                               'emotion': {'type': 'string', 'description': '什么情绪'}},
                'required': ['intent']})


@dataclass(slots=True)
class Reply:
    """ 插件对一条消息的决策。

    :param texts: 要发送的文本，可以为空
    :param stop: 是否终止后续插件的处理
    """

    texts: list[str] = field(default_factory=list)
    stop: bool = False
    emoji: Emoji | None = None      # 本轮要发的表情；发送由 Bot 统一走队列，插件不发
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
    return cleaned.strip()


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

    def get_plugins_for_contact(self, contact: str) -> list[Plugin]:
        """ 注意语义：只要为某个 contact 映射过插件，就完全覆盖默认插件链（例如用它静音某个群）。 """
        return self._contact_mappings.get(contact, self._defaults)


class EndProcessing(Plugin):
    """ 终止处理、不回复。用于给指定会话静音。 """

    @override
    async def handle(self, ctx: MessageContext) -> Reply | None:
        return Reply(stop=True)


class RepeatPlugin(Plugin):
    def __init__(self, repeat_count: int = 2, context_length: int = 10, max_length: int = 20):
        """ 跟队形（复读机）插件

        :param repeat_count: 队形阈值
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
        text: str = record.text
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
    blocked_by: str = ''            # 'score' | 'backoff' | ''
    score_obj: NecessityScore | None = None
    threshold: int = 0
    remaining: float = 0.0          # 退避里还剩多少秒

    @property
    def detail(self) -> str:
        if self.blocked_by == 'backoff':
            return f'沉默退避中，剩 {int(self.remaining)}s'
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
                 focus: FocusMode | None = None):
        """ :param threshold: 必要性分数阈值，越高越沉默
        :param activity_window: 统计群活跃度的时间窗口（秒）
        :param aliases: 机器人自己的其他叫法，命中就当被叫到名字
        :param backoff: 连续沉默退避；不传则用默认（连续两轮不开口开始歇 15s）
        :param focus: 专注模式；群聊得热时盯一会儿，期间门控和退避都让路
        """
        self.threshold = threshold
        self.activity_window = activity_window
        self.aliases = aliases
        self.backoff = backoff if backoff is not None else IdleBackoff()
        self.focus = focus

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
        # 专注模式：聊得正热就盯一会儿；期间不看门控，也不连续沉默退避
        if result.kind == 'proactive' and self.focus:
            if self.focus.tick(ctx.contact, now, activity):
                result = replace(result, focused=True, reasons=(*result.reasons, '专注'))
        if result.score < self.threshold and not result.focused:
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
        """ 记下这一轮最后说没说，给连续沉默退避用。 """
        self.backoff.record(ctx.contact, spoke, now, ctx.history)

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
        """ 群昵称或任一别名出现在消息里，就算在叫它。 """
        return any(name and name in ctx.msg.content for name in (ctx.self_name, *self.aliases))


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
                 timeout: float = 60.0, rng: random.Random | None = None):
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
        :param timeout: 单次模型调用超时（秒）
        :param rng: 抽临时风格用的随机源，测试时可注入
        """
        self.ai = ai_provider
        self._planner_ai = planner_ai
        self.context_length = context_length
        self.policy = policy or ReplyPolicy()
        self.persona = persona or Persona()
        self.profiles = profiles
        self.habits = habits
        self.memories = memories
        self.tracer = tracer
        self.enable_planner = enable_planner
        self.emoji_search = emoji_search
        self.enable_tools = enable_tools
        self.planner_rounds = planner_rounds
        self.emoji_policy = emoji_policy or EmojiPolicy()
        self.timeout = timeout
        self.rng = rng

        from jinja2 import Template
        base = Path(__file__).parent
        # trim_blocks/lstrip_blocks：{% if %} 独占一行时不留空行。
        # 不设的话每个分支里都会多出一堆空行，白烧 token。
        options = {'enable_async': True, 'trim_blocks': True, 'lstrip_blocks': True}
        self.planner_template = Template(
            (base / 'planner.txt').read_text(encoding='utf-8'), **options)
        self.replyer_template = Template(
            (base / 'replyer.txt').read_text(encoding='utf-8'), **options)

    @property
    def planner_ai(self) -> AiProvider:
        """ 决策者用的模型；没单独配就和表达者共用一个。动态回退，换 ai 也能跟上。 """
        return self._planner_ai or self.ai

    @override
    async def handle(self, ctx: MessageContext) -> Reply | None:
        if not (ctx.is_text or ctx.is_image):  # 只处理文本和图片消息
            return None

        now = time.time()
        decision = self.policy.evaluate_detailed(ctx, now)
        if not decision.allowed:
            self._emit(ctx, Status.GATED if decision.blocked_by == 'score' else Status.BACKOFF,
                       decision.detail, score=decision.score)
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

        started = time.perf_counter()
        plan = ''
        emoji: Emoji | None = None
        if self.enable_planner:
            self._emit(ctx, Status.DECIDING, f'{necessity.kind} · 分数 {necessity.score}')
            if self._tools_ready:
                decided, emoji, tool_log = await self._plan_with_tools(ctx, blocks, transcript)
            else:
                decided, tool_log = await self._plan(ctx, necessity, blocks, transcript), ''
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
        response = await self._express(ctx, blocks, transcript, plan)
        latency_ms = self._elapsed(started)
        self._trace(ctx, necessity, transcript, response, latency_ms, plan)
        if response is None:
            self._emit(ctx, Status.EXPRESS_FAILED, '表达者调用失败')
            return self._silent(ctx, now, emoji=emoji)

        # 模型常顺手带上「昵称：」前缀或引号包裹，不能直接漏到群里
        names = (ctx.self_name, *self.persona.aliases)
        texts = [text for text in (clean_reply(x, names) for x in response) if text]
        if not texts:
            self._emit(ctx, Status.EMPTY_REPLY)
            return self._silent(ctx, now, emoji=emoji)
        self.policy.record(ctx, spoke=True, now=now)
        return Reply(texts=texts, emoji=emoji)

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

    async def _plan_with_tools(self, ctx: MessageContext, blocks: dict[str, str],
                               transcript: str) -> tuple[Plan | None, Emoji | None, str]:
        """ 带工具的决策循环。返回（决策, 选中的表情, 工具调用记录）。

        模型只挑动作（搜表情 / 选一张 / 说要说话），不写台词——台词还是表达者的事。
        一个工具都不调 = 本轮不发言；调了动作类工具本轮就结束。
        """
        candidates: list[Emoji] = []   # 上一次搜索的结果，send_emoji 的序号就是它
        picked: list[Emoji] = []
        said = {'intent': '', 'emotion': ''}

        async def search(arguments: dict) -> ToolResult:
            keyword = arg_str(arguments, 'keyword')
            if not keyword:
                return ToolResult('没给关键词，什么也没搜到。')
            if self.emoji_search is None:
                return ToolResult('这会儿搜不了表情。')
            found = await self.emoji_search(keyword)
            if not found:
                return ToolResult(f'没搜到跟「{keyword}」有关的表情。换个词，或者不用表情。')
            candidates[:] = found
            listing = '\n'.join(item.render(i) for i, item in enumerate(found, 1))
            return ToolResult(f'搜到这些，序号可以直接给 send_emoji：\n{listing}')

        async def send(arguments: dict) -> ToolResult:
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
            picked.append(chosen)
            self.emoji_policy.note(chosen.md5, time.time())
            return ToolResult(f'好，这一轮发这张：{chosen.desc or "选中的表情"}。', stop=True)

        async def speak(arguments: dict) -> ToolResult:
            said['intent'] = arg_str(arguments, 'intent')
            said['emotion'] = arg_str(arguments, 'emotion')
            return ToolResult('好，交给表达者。', stop=True)

        tools = [(SEARCH_EMOJI, search)]
        if self.emoji_search is not None:
            tools.append((SEND_EMOJI, send))
        tools.append((SPEAK, speak))
        toolbox = Toolbox(tools)

        prompt = await self._planner_prompt(ctx, toolbox.names)
        messages = [{'role': 'user',
                     'content': await self._build_message(ctx, self._compose(ctx, blocks, transcript),
                                                          ctx.is_image)}]
        analysis = ''
        for _ in range(max(1, self.planner_rounds)):
            turn = await self._ask_tools(prompt, messages, toolbox.schema(), ctx)
            if turn is None:
                return None, None, toolbox.render_calls()      # 这次没拿到决策
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

        emoji = picked[0] if picked else None
        log = toolbox.render_calls()
        if emoji is None and not said['intent']:
            return Plan(speak=False, analysis=analysis), None, log
        plan = Plan(speak=bool(said['intent']), analysis=analysis, intent=said['intent'],
                    emotion=said['emotion'], emoji=emoji.desc if emoji else '')
        return plan, emoji, log

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
        return await self.planner_template.render_async(behavior_style=self.persona.behavior_style,
                                                        self_name=ctx.self_name, contact=ctx.contact,
                                                        is_group=ctx.msg.is_chatroom,
                                                        aliases=self.persona.aliases, tools=tools)

    async def _plan(self, ctx: MessageContext, necessity: NecessityScore, blocks: dict[str, str],
                    transcript: str) -> Plan | None:
        """ 单轮 JSON 决策。后端不支持工具时走这条。 """
        raw = await self._ask(await self._planner_prompt(ctx), self._compose(ctx, blocks, transcript),
                              ctx, image=ctx.is_image, ai=self.planner_ai)
        if not raw:
            return None
        # ai.chat 返回的是按行切好的列表，决策是一整块 JSON，拼回去再解析
        return Plan.parse('\n'.join(raw))

    async def _express(self, ctx: MessageContext, blocks: dict[str, str], transcript: str,
                       plan: str) -> list[str] | None:
        """ 跑表达者。先把「本轮要回哪条」摆明，再给决策；没决策也不会跑偏到旧话题。 """
        prompt = await self.replyer_template.render_async(
            identity=self.persona.identity(ctx.self_name), reply_style=self.persona.reply_style,
            self_name=ctx.self_name, contact=ctx.contact, is_group=ctx.msg.is_chatroom)
        message = self._compose(ctx, blocks, transcript, plan=plan,
                                alternate=self.persona.maybe_alternate(self.rng))
        return await self._ask(prompt, message, ctx, image=ctx.is_image)

    async def _ask(self, prompt: str, message: str, ctx: MessageContext, image: bool = False,
                   ai: AiProvider | None = None) -> list[str] | None:
        """ 调模型。超时或报错只当这次没结果——不能让一次 API 抽风拖死消息处理链。 """
        provider = ai or self.ai
        try:
            return await asyncio.wait_for(
                provider.chat(prompt.strip(), await self._build_message(ctx, message, image)), self.timeout)
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
        new_plan = plan
        if stage == 'all' and self.enable_planner:
            prompt = await self.planner_template.render_async(
                behavior_style=self.persona.behavior_style, self_name=self_name, contact=contact,
                is_group=True, aliases=self.persona.aliases, tools=())
            raw = await self._complete(prompt, transcript, ai=self.planner_ai)
            decided = Plan.parse('\n'.join(raw)) if raw else None
            new_plan = decided.render() if decided and decided.speak else ''

        replyer = await self.replyer_template.render_async(
            identity=self.persona.identity(self_name), reply_style=self.persona.reply_style,
            self_name=self_name, contact=contact, is_group=True)
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
            latency_ms=latency_ms, plan=plan, msg_id=str(getattr(msg, 'msg_id', ''))))

    @staticmethod
    async def _build_message(ctx: MessageContext, transcript: str, image: bool = False) -> str | list:
        """ 文本消息直接发送；图片消息把真实图片作为 content block 附在文本后（DeepSeek 原生识图）。

        图片拉不下来就退回纯文本：附件取不到是常事（这套 relay 下的图片 XML 里
        根本没有可下载的 cdnurl，只有加密 fileid），它不该把整轮回复带崩——
        更不该让用户看到一条和真实原因无关的报错。历史里本来就有 `[图片]` 占位符。
        """
        if not image:
            return transcript

        if ctx.download is None:
            # 拿不到可下载的地址（图片的 XML 里只有加密 fileid）。不去试：
            # 每次尝试都是一次注定失败的 RPC，历史里本来就有 `[图片]` 占位符
            return transcript
        try:
            raw = await ctx.download()
        except Exception as e:
            logger.warning(f'取图片失败，这一轮只发文字（{ctx.contact}）: {e}')
            return transcript
        if not raw:
            return transcript

        encoded = base64.b64encode(raw).decode()
        return [
            {'type': 'text', 'text': transcript},
            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{encoded}'}},
        ]
