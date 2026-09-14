import base64
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import override

from loguru import logger

from ai import AiProvider
from context import ChatWindow, MessageContext, SingleRecord
from habits import HabitStore
from necessity import (
    NECESSITY_THRESHOLD,
    PRESENCE_WINDOW_SECONDS,
    NecessityInput,
    NecessityScore,
    score_necessity,
)
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
    def __init__(self, ai_provider: AiProvider, context_length: int = 10, policy: ReplyPolicy | None = None,
                 profiles: ProfileStore | None = None, habits: HabitStore | None = None,
                 tracer: TraceLogger | None = None):
        """ 聊天插件

        :param ai_provider: AI 服务提供者
        :param context_length: 送入模型的上下文消息条数
        :param policy: 是否回复的策略；不传则用默认（必要性打分）
        :param profiles: 群友画像；传了就注入 prompt 让机器人认识人
        :param habits: 群里的特色用语；当前消息命中才注入
        :param tracer: 回复轨迹记录；传了就把每轮触发/上下文/回复写成 JSONL
        """
        self.ai = ai_provider
        self.context_length = context_length
        self.policy = policy or ReplyPolicy()
        self.profiles = profiles
        self.habits = habits
        self.tracer = tracer

        from jinja2 import Template
        template_file = (Path(__file__).parent / 'prompt.txt').read_text(encoding='utf-8')
        self.prompt_template = Template(template_file, enable_async=True)

    @override
    async def handle(self, ctx: MessageContext) -> Reply | None:
        msg = ctx.msg
        if not (ctx.is_text or ctx.is_image):  # 只处理文本和图片消息
            return None

        necessity = self.policy.evaluate(ctx, time.time())
        if necessity is None:
            return None

        history: ChatWindow = ctx.history.latest_n(self.context_length)
        profiles = ''
        if self.profiles:
            profiles = self.profiles.render_for(record.sender for record in reversed(history))
        habit_block = self.habits.render(msg.content) if self.habits else ''
        prompt = await self.prompt_template.render_async(self_name=ctx.self_name, contact=ctx.contact,
                                                         is_group=msg.is_chatroom, trigger=necessity.kind,
                                                         trigger_detail=necessity.detail, profiles=profiles,
                                                         habits=habit_block)

        text = '回复你扮演的角色所要说的话。以下聊天记录供你参考：\n\n' + str(history)
        started = time.perf_counter()
        response = await self.ai.chat(prompt.strip(), await self._build_message(ctx, text))
        latency_ms = int((time.perf_counter() - started) * 1000)
        self._trace(ctx, necessity, text, response, latency_ms)
        if not response:
            return Reply(stop=True)

        # 有时候模型返回数据中会带有 "昵称: " 的前缀，这里需要去掉
        prefix = ctx.self_name + ':'
        return Reply(texts=[x.lstrip(prefix) for x in response])

    def _trace(self, ctx: MessageContext, necessity: NecessityScore, transcript: str,
               response: list[str] | None, latency_ms: int) -> None:
        """ 记录这一轮：为什么开口、看到了什么、说了什么。 """
        if not self.tracer:
            return
        msg = ctx.msg
        self.tracer.log(TraceRecord(
            ts=time.time(), contact=msg.reply_target, contact_name=ctx.contact,
            sender=msg.sender_name, trigger=necessity.kind, score=necessity.score,
            reasons=necessity.reasons, transcript=transcript, reply=tuple(response or ()),
            latency_ms=latency_ms))

    @staticmethod
    async def _build_message(ctx: MessageContext, transcript: str) -> str | list:
        """ 文本消息直接发送；图片消息把真实图片作为 content block 附在文本后（DeepSeek 原生识图）。 """
        if not ctx.is_image:
            return transcript

        image = await ctx.msg.get_media_bytes()
        if not image:
            return transcript

        encoded = base64.b64encode(image).decode()
        return [
            {'type': 'text', 'text': transcript},
            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{encoded}'}},
        ]
