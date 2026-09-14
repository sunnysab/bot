""" 把「构造 → 启动 → 热重载」收在一处。

为什么要这一层：管理后台要在运行时改配置并生效，而原来的 `main()` 是一串
线性构造语句，没有能重新构造的对象可拿。

两条设计要点（见 notes/管理后台设计.md 第六节）：

1. **三个对象热重载时不能重建**：`FocusMode` / `IdleBackoff` / `EmojiPolicy`。
   专注会话、连续沉默计数、表情冷却都是「机器人的记性」，重建等于一改配置
   它就把刚才在聊什么、发过什么表情全忘了。所以它们归 Runtime 长期持有，
   热重载只就地改常数。
2. **分两档**：能热生效的（模型、提示词、门控、专注常数、错别字）就地换；
   路径 / relay / 识图 / 日志只能整进程重启——那些是构造期就定死的东西。
   `apply()` 返回「改了但热重载管不着」的字段列表，让面板如实提示。
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import fields

from loguru import logger

from ai import Deepseek, OpenAI
from bot import Bot
from config import Config
from describer import EmojiDescriber, MediaCache, MediaDescriber
from emojis import EmojiLibrary, EmojiPolicy
from focus import FocusMode
from habits import HabitStore
from moments import MomentKeeper, MomentStore
from necessity import IdleBackoff
from persona import Persona
from planner import PLAN_RESPONSE_FORMAT
from plugin import ChatPlugin, EndProcessing, RepeatPlugin, ReplyPolicy
from profiles import ProfileStore
from reply_trace import TraceLogger

# 默认静音的会话。键是**显示名**——插件链是按显示名分发的（见 plugin.PluginManager）
DEFAULT_MUTED = ('研究生摆烂群',)

# 热重载能生效的配置字段。其余字段改了得整进程重启
HOT_FIELDS = frozenset({
    'deepseek_key', 'deepseek_model', 'deepseek_url', 'send_interval',
    'reply_necessity_threshold', 'reply_activity_window',
    'enable_focus', 'focus_enter_activity', 'focus_idle_seconds', 'focus_max_seconds',
    'backoff_start_after', 'backoff_base_seconds', 'backoff_max_seconds', 'backoff_bypass_messages',
    'typo_rate', 'dry_run',
    'personality', 'reply_style', 'behavior_style', 'reply_styles', 'style_probability',
    'enable_planner', 'planner_url', 'planner_model', 'planner_key', 'planner_temperature',
    'planner_max_tokens', 'planner_thinking', 'planner_json_schema', 'enable_tools',
    'planner_rounds', 'request_timeout', 'trace_path',
})
# 构造期就定死的东西：路径 / 传输 / 识图 / 日志
RESTART_FIELDS = frozenset({
    'relay_host', 'relay_port', 'relay_unix', 'admin_port', 'log_path', 'log_level',
    'describe_media', 'media_cache_path', 'describe_concurrency', 'describe_timeout',
    'describe_max_tokens', 'describe_history_limit',
    'profile_path', 'habit_path', 'moment_path', 'emoji_path', 'moment_batch',
})


class Runtime:
    def __init__(self, config: Config):
        self.config = config
        self.loop: asyncio.AbstractEventLoop | None = None

        # 「记性」：热重载不重建
        self.focus = FocusMode(enabled=config.enable_focus,
                               enter_activity=config.focus_enter_activity,
                               idle=config.focus_idle_seconds,
                               maximum=config.focus_max_seconds)
        self.backoff = IdleBackoff()
        self.emoji_policy = EmojiPolicy()

        # 构造期定死的东西
        self.media_cache = MediaCache(config.media_cache_path)
        self.memories = MomentStore(config.moment_path)
        self.emojis = EmojiLibrary.load(config.emoji_path)
        self.profiles = ProfileStore.load(config.profile_path)
        self.habits = HabitStore.load(config.habit_path)
        self.ai, self.planner_ai = _providers(config)
        self.describer = self._make_describer()
        self.emoji_describer = self._make_emoji_describer()
        logger.info(f'已加载 {len(self.emojis)} 张表情（{self.emojis.sendable_count} 张能发，'
                    f'{self.emojis.scene_count} 张写了使用场景）: {config.emoji_path}')
        if self.profiles:
            logger.info(f'已加载 {len(self.profiles)} 个人的画像: {config.profile_path}')
        if self.habits:
            logger.info(f'已加载 {len(self.habits)} 条群用语: {config.habit_path}')

        self.bot = self._make_bot()
        if self.emoji_describer:
            self.emoji_describer.on_update = self.bot.save_emojis
        self.chat_plugin = self._make_chat_plugin()
        self._install_plugins()
        self.bot.register_ready(lambda _bot: self.bot.load_context())
        if config.dry_run:
            logger.warning('演习模式：全链路照跑，但不真的发消息')

    # ---------- 构造 ----------

    def _make_bot(self) -> Bot:
        config = self.config
        return Bot(config.relay_host, config.relay_port, unix_path=config.relay_unix, x_delay=0.5,
                   send_interval=config.send_interval, describer=self.describer,
                   describe_history_limit=config.describe_history_limit,
                   keeper=MomentKeeper(self.ai, self.memories, batch=config.moment_batch),
                   emojis=self.emojis, emoji_path=config.emoji_path,
                   emoji_describer=self.emoji_describer, typo_rate=config.typo_rate,
                   dry_run=config.dry_run)

    def _make_describer(self) -> MediaDescriber | None:
        if not self.config.describe_media:
            return None
        return MediaDescriber(self.ai, self.media_cache, model=self.config.deepseek_model,
                              concurrency=self.config.describe_concurrency,
                              timeout=self.config.describe_timeout,
                              max_tokens=self.config.describe_max_tokens)

    def _make_emoji_describer(self) -> EmojiDescriber | None:
        # 给偷来的表情补「使用场景」：微信的 desc 只有一个标签，够搜标签不够搜场合
        if not self.config.describe_media:
            return None
        return EmojiDescriber(self.ai, self.emojis, model=self.config.deepseek_model,
                              timeout=self.config.describe_timeout,
                              max_tokens=self.config.describe_max_tokens)

    def _make_policy(self) -> ReplyPolicy:
        config = self.config
        return ReplyPolicy(threshold=config.reply_necessity_threshold,
                           activity_window=config.reply_activity_window,
                           aliases=self._make_persona().aliases,
                           backoff=self.backoff, focus=self.focus)

    def _make_persona(self) -> Persona:
        config = self.config
        return Persona(
            personality=config.personality, reply_style=config.reply_style,
            behavior_style=config.behavior_style,
            alternates=tuple(style.strip() for style in config.reply_styles.split('|') if style.strip()),
            alternate_probability=config.style_probability)

    def _make_chat_plugin(self) -> ChatPlugin:
        config = self.config
        return ChatPlugin(self.ai, context_length=30, policy=self._make_policy(),
                          persona=self._make_persona(), profiles=self.profiles,
                          habits=self.habits, memories=self.memories,
                          tracer=TraceLogger(config.trace_path),
                          enable_planner=config.enable_planner, planner_ai=self.planner_ai,
                          emoji_search=self.bot.search_emoji, enable_tools=config.enable_tools,
                          planner_rounds=config.planner_rounds, emoji_policy=self.emoji_policy,
                          timeout=config.request_timeout)

    def _install_plugins(self) -> None:
        self.bot.set_default_plugin([RepeatPlugin(), self.chat_plugin])
        for contact in DEFAULT_MUTED:
            self.bot.attach(contact, EndProcessing())

    # ---------- 热重载 ----------

    def apply(self, config: Config) -> list[str]:
        """ 就地换掉能换的，返回「改了但热重载管不着」的字段名。 """
        stale = [f.name for f in fields(Config)
                 if f.name in RESTART_FIELDS and getattr(config, f.name) != getattr(self.config, f.name)]
        self.config = config

        # 就地改常数，不重建这两个对象——专注会话和退避计数都留着。
        # 代价：正在进行的专注会话会立刻按新常数判退出（可能当场退出），这是
        # 「保留状态」的必然结果，不是 bug
        self.focus.enabled = config.enable_focus
        self.focus.enter_activity = config.focus_enter_activity
        self.focus.idle = config.focus_idle_seconds
        self.focus.maximum = config.focus_max_seconds
        self.backoff.start_after = config.backoff_start_after
        self.backoff.base = config.backoff_base_seconds
        self.backoff.maximum = config.backoff_max_seconds
        self.backoff.bypass = config.backoff_bypass_messages

        self.ai, self.planner_ai = _providers(config)
        self.chat_plugin = self._make_chat_plugin()
        self._install_plugins()
        self.bot.typo_rate = config.typo_rate
        self.bot.dry_run = config.dry_run
        self.bot.wechat.dry_run = config.dry_run
        self.bot.outbox.set_interval(config.send_interval)
        logger.info(f'热重载完成（{", ".join(stale) or "没有需要重启的改动"}）')
        return stale

    # ---------- 运行 ----------

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self.bot.start()

    async def stop(self) -> None:
        await self.bot.stop()
        self.memories.close()

    def restart_process(self) -> None:
        """ 整进程重启。必须先停干净再 exec，否则长链会被硬断。 """
        assert self.loop, '事件循环还没起来'
        self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self._restart()))

    async def _restart(self) -> None:
        logger.warning('按管理后台要求重启进程')
        await self.stop()
        os.execv(sys.executable, [sys.executable, *sys.argv])  # noqa: S606 —— 就是要重启自己


def _providers(config: Config):
    """ 主模型 + 决策者模型。决策者留空 PLANNER_URL 就两段都用主模型。 """
    # key 为空时给个占位：openai 客户端会因为「Missing credentials」直接抛，
    # 而没配 key 是能启动的（main 里只打了警告）——调用失败由各层的 try/except 兜
    ai = Deepseek(key=config.deepseek_key or 'not-set', model=config.deepseek_model,
                  url=config.deepseek_url, temperature=0.85)
    planner = None
    if config.planner_url:
        planner = OpenAI(url=config.planner_url, key=config.planner_key, model=config.planner_model,
                         temperature=config.planner_temperature, max_tokens=config.planner_max_tokens,
                         # thinking 与 json_schema 二选一：开 schema 会压掉思考（见 config 注释）
                         extra_body={'chat_template_kwargs': {'enable_thinking': config.planner_thinking}},
                         response_format=PLAN_RESPONSE_FORMAT if config.planner_json_schema else None)
    return ai, planner
