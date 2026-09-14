import asyncio
import base64
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import override

from loguru import logger

from ai import AiProvider
from context import ChatWindow, MessageContext, SingleRecord
from habits import HabitStore
from moments import MomentStore, extract_keywords
from necessity import (
    NECESSITY_THRESHOLD,
    PRESENCE_WINDOW_SECONDS,
    NecessityInput,
    NecessityScore,
    score_necessity,
)
from persona import Persona
from planner import Plan
from profiles import ProfileStore
from reply_trace import TraceLogger, TraceRecord


@dataclass(slots=True)
class Reply:
    """ 插件对一条消息的决策。

    :param texts: 要发送的文本，可以为空
    :param stop: 是否终止后续插件的处理
    """

    texts: list[str] = field(default_factory=list)
    stop: bool = False


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

    def register(self, plugin: Plugin) -> None:
        """Register a plugin for all contacts"""
        self._defaults.append(plugin)
        logger.info(f'Registered plugin: {plugin.__class__.__name__}')

    def map_plugin_to_contact(self, contact: str, plugin: Plugin) -> None:
        """Map a specific plugin to a contact"""
        if contact not in self._contact_mappings:
            self._contact_mappings[contact] = []
        self._contact_mappings[contact].append(plugin)
        logger.info(f'Mapped plugin {plugin.__class__.__name__} to contact {contact}')

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
        # 为了避免重复回复，记录上一次重复的消息。
        # key 是聊天窗口编号，value 是最后一次重复的消息（去重）
        self.last_repeat = {}

    @override
    async def handle(self, ctx: MessageContext) -> Reply | None:
        """ 跟队形回复。 如果去掉中文英文标点及表情后的消息连续重复特定数量，则凑个热闹跟着回复一句。 """
        msg = ctx.msg
        if not ctx.is_text:  # 只处理文本消息
            return None

        history: ChatWindow = ctx.history.latest_n(self.context_length)
        if len(history) < self.repeat_count:
            return None

        # 对历史消息去除昵称、标点等操作
        myself = ctx.self_name
        # 队形跟过了就不要再跟了
        blocked_set = {x.pure_text() for x in history if x.sender == myself}
        clear_context: list[str] = [x.pure_text() for x in history if x.pure_text() and x.pure_text() not in blocked_set]
        count_dict: dict[str, list[int]] = {}
        for pos, text in enumerate(clear_context):
            count_dict.setdefault(text, []).append(pos)

        # 找到最多的重复元素及次数, 其实就是找 count_dict 中 value 最长的.
        if not count_dict:
            return None
        max_repeat = max(count_dict.values(), key=len)
        if len(max_repeat) < self.repeat_count:
            return None

        # 找一条别人说过的队形，跟上
        record: SingleRecord = history[max_repeat[-1]]
        text: str = record.text
        # 太长不跟
        if len(text) > self.max_length:
            return None

        self.last_repeat[msg.reply_target] = text
        return Reply(texts=[text], stop=True)


class ReplyPolicy:
    """ 群聊里「这条消息要不要交给模型」。

    便宜的结构性判断，不花 token：私聊 / 被 @ / 被叫到名字直接放行；
    其余群聊消息走必要性打分，过线才交给模型（见 necessity.py）。
    模型仍可用「本轮不发言」做内容层的最终否决。
    """

    def __init__(self, threshold: int = NECESSITY_THRESHOLD, activity_window: float = 180.0):
        """ :param threshold: 必要性分数阈值，越高越沉默
        :param activity_window: 统计群活跃度的时间窗口（秒）
        """
        self.threshold = threshold
        self.activity_window = activity_window

    def evaluate(self, ctx: MessageContext, now: float) -> NecessityScore | None:
        """ 返回 None 表示不交给模型；否则返回打分（可注入 prompt 说明触发原因）。

        now 用 epoch 秒，与 history 里的时间戳同源。
        """
        msg = ctx.msg
        result = score_necessity(NecessityInput(
            text=msg.content,
            is_group=msg.is_chatroom,
            has_at=msg.is_at_me,
            has_mention=self._mentions_me(ctx),
            activity=self._activity(ctx, now),
            self_replies=self._self_replies(ctx, now),
            recent_messages=self._recent_messages(ctx, now),
        ))
        return result if result.score >= self.threshold else None

    def _activity(self, ctx: MessageContext, now: float) -> int:
        """ 活动窗口内别人的消息数。 """
        return sum(1 for r in ctx.history if not r.is_self and now - r.t <= self.activity_window)

    def _recent(self, ctx: MessageContext, now: float) -> list[SingleRecord]:
        return [r for r in ctx.history if now - r.t <= PRESENCE_WINDOW_SECONDS]

    def _self_replies(self, ctx: MessageContext, now: float) -> int:
        return sum(1 for r in self._recent(ctx, now) if r.is_self)

    def _recent_messages(self, ctx: MessageContext, now: float) -> int:
        return len(self._recent(ctx, now))

    @staticmethod
    def _mentions_me(ctx: MessageContext) -> bool:
        return bool(ctx.self_name) and ctx.self_name in ctx.msg.content


class ChatPlugin(Plugin):
    """ 聊天插件。两段式：Planner 判断说与不说、说什么，Expressor 负责说成人话。

    Planner 被明确告知「你不是本人，你是决策者」，输出的是判断而不是台词；
    Expressor 拿人设 + 表达风格 + 决策当作参考，只负责组织语言。
    enable_planner=False 时退回单段（Expressor 直接看着聊天记录说），便于对比。

    决策者只负责「要不要主动插话」这一件事：被 @ / 被叫名字 / 私聊都是必须回应的
    场景，直接交给表达者，不多花一次调用；只有没人点名的情形才请决策者判断。
    enable_planner=False 时连这种情况也直接回，便于对比。
    """

    def __init__(self, ai_provider: AiProvider, context_length: int = 10, policy: ReplyPolicy | None = None,
                 persona: Persona | None = None, profiles: ProfileStore | None = None,
                 habits: HabitStore | None = None, memories: MomentStore | None = None,
                 tracer: TraceLogger | None = None, enable_planner: bool = True,
                 planner_ai: AiProvider | None = None,
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
        self.timeout = timeout
        self.rng = rng

        from jinja2 import Template
        base = Path(__file__).parent
        self.planner_template = Template((base / 'planner.txt').read_text(encoding='utf-8'), enable_async=True)
        self.replyer_template = Template((base / 'replyer.txt').read_text(encoding='utf-8'), enable_async=True)

    @property
    def planner_ai(self) -> AiProvider:
        """ 决策者用的模型；没单独配就和表达者共用一个。动态回退，换 ai 也能跟上。 """
        return self._planner_ai or self.ai

    @override
    async def handle(self, ctx: MessageContext) -> Reply | None:
        if not (ctx.is_text or ctx.is_image):  # 只处理文本和图片消息
            return None

        necessity = self.policy.evaluate(ctx, time.time())
        if necessity is None:
            return None

        history: ChatWindow = ctx.history.latest_n(self.context_length)
        blocks = self._context_blocks(ctx, history)
        transcript = '以下聊天记录供你参考：\n\n' + str(history)

        started = time.perf_counter()
        plan = ''
        # 只有「没人点名」才需要决策者判断要不要插话；其余情形直接回，省一次调用
        if self.enable_planner and necessity.kind == 'proactive':
            decided = await self._plan(ctx, necessity, blocks, transcript)
            if decided is not None and not decided.speak:
                self._trace(ctx, necessity, transcript, None, self._elapsed(started), '')
                return Reply(stop=True)
            plan = decided.render() if decided else ''

        response = await self._express(ctx, blocks, transcript, plan)
        latency_ms = self._elapsed(started)
        self._trace(ctx, necessity, transcript, response, latency_ms, plan)
        if not response:
            return Reply(stop=True)

        # 有时候模型返回数据中会带有 "昵称: " 的前缀，这里需要去掉
        prefix = ctx.self_name + ':'
        return Reply(texts=[x.lstrip(prefix) for x in response])

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
        if plan:
            lines.append(f'【决策】\n{plan}')
        if alternate:
            lines.append(f'【临时风格】{alternate}')
        for label, key in (('群友画像', 'profiles'), ('群里的说法', 'habits'), ('记忆（可能记错，别当准）', 'memories')):
            if blocks.get(key):
                lines.append(f'【{label}】\n{blocks[key]}')
        lines.append(transcript)
        return '\n\n'.join(lines)

    async def _plan(self, ctx: MessageContext, necessity: NecessityScore, blocks: dict[str, str],
                    transcript: str) -> Plan | None:
        """ 请决策者判断要不要主动插话；返回 None 表示这次没拿到决策（调用失败），不代表不开口。 """
        prompt = await self.planner_template.render_async(behavior_style=self.persona.behavior_style,
                                                          self_name=ctx.self_name, contact=ctx.contact,
                                                          is_group=ctx.msg.is_chatroom)
        raw = await self._ask(prompt, self._compose(ctx, blocks, transcript), ctx, image=ctx.is_image,
                              ai=self.planner_ai)
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
            latency_ms=latency_ms, plan=plan))

    @staticmethod
    async def _build_message(ctx: MessageContext, transcript: str, image: bool = False) -> str | list:
        """ 文本消息直接发送；图片消息把真实图片作为 content block 附在文本后（DeepSeek 原生识图）。 """
        if not image:
            return transcript

        raw = await ctx.msg.get_media_bytes()
        if not raw:
            return transcript

        encoded = base64.b64encode(raw).decode()
        return [
            {'type': 'text', 'text': transcript},
            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{encoded}'}},
        ]
