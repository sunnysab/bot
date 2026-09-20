""" 把「构造 → 启动 → 热重载」收在一处。

为什么要这一层：管理后台要在运行时改配置并生效，而原来的 `main()` 是一串
线性构造语句，没有能重新构造的对象可拿。

两条设计要点（见 notes/管理后台设计.md 第六节）：

1. **三个对象热重载时不能重建**：`FocusMode` / `IdleBackoff` / `EmojiPolicy`。
   专注会话、连续沉默计数、表情冷却都是「机器人的记性」，重建等于一改配置
   它就把刚才在聊什么、发过什么表情全忘了。所以它们归 Runtime 长期持有，
   热重载只就地改常数。
2. **分两档**：能热生效的（模型、提示词、门控、专注常数、错别字）就地换；
   路径 / daemon / 识图 / 日志只能整进程重启——那些是构造期就定死的东西。
   `apply()` 返回「改了但热重载管不着」的字段列表，让面板如实提示。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Iterable
from dataclasses import fields
from pathlib import Path

from loguru import logger

from ai import Deepseek, OpenAI
from bot import Bot
from config import Config
from describer import EmojiDescriber, MediaCache, MediaDescriber
from emojis import EmojiLibrary, EmojiPolicy
from focus import FocusMode
from habits import HabitStore
from images import ImageStore
from moments import MomentKeeper, MomentStore
from necessity import IdleBackoff, SpeechLimiter
from persona import Persona
from personas import PersonaStore
from planner import PLAN_RESPONSE_FORMAT
from plugin import ChatPlugin, EndProcessing, RepeatPlugin, ReplyPolicy
from profiles import ProfileStore
from reply_trace import TraceLogger

# 默认静音的会话。键是**显示名**——插件链是按显示名分发的（见 plugin.PluginManager）。
# 只在名单文件还不存在时当种子用，以后以面板上改的为准
DEFAULT_MUTED = ('研究生摆烂群',)


class ReplyScope:
    """ 「允许回复谁」：一份落盘名单 + 一个模式。

    - `mode='all'`（默认）：没表过态的都回——新群不会突然被静音
    - `mode='allow'`：没表过态的不回——新群默认不说话，想回得手动勾

    `allowed` / `blocked` 是两个互斥的显式集合（表过态的会话），模式只管**没表过态**的。
    两边都留着，换模式不丢之前的勾选。键是**显示名**，和插件链分发用的是同一个。
    """

    MODES = ('all', 'allow')

    def __init__(self, path: str | Path, seed: Iterable[str] = ()):
        self.path = Path(path)
        data: dict = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as e:  # noqa: BLE001 —— 名单坏了不能拖垮启动
                logger.warning(f'回复名单读不出来（{self.path}），按默认来: {e}')
        mode = data.get('mode')
        self.mode = mode if mode in self.MODES else 'all'
        self.allowed = set(data.get('allowed') or ())
        # 文件还不存在时用代码里的默认名单兜底；存在之后以文件为准，
        # 否则面板上解了静音、重启又给塞回来
        self.blocked = set(data.get('blocked') or (() if self.path.exists() else seed))

    def allows(self, contact: str) -> bool:
        if contact in self.allowed:
            return True
        if contact in self.blocked:
            return False
        return self.mode == 'all'    # 两边都没表态的新会话：跟模式走

    def set_allowed(self, contact: str, on: bool) -> bool:
        """ on=True = 允许回复。两个集合互斥，换模式不改写已表过态的那个。 """
        (self.allowed if on else self.blocked).add(contact)
        (self.blocked if on else self.allowed).discard(contact)
        self.save()
        return on

    def set_mode(self, mode: str) -> str:
        if mode not in self.MODES:
            raise ValueError(f'模式只能是 {self.MODES}：{mode!r}')
        self.mode = mode
        self.save()
        return mode

    def to_dict(self) -> dict:
        return {'mode': self.mode, 'allowed': sorted(self.allowed), 'blocked': sorted(self.blocked)}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + '.tmp')
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, self.path)

# 热重载能生效的配置字段。其余字段改了得整进程重启
HOT_FIELDS = frozenset({
    'deepseek_key', 'deepseek_model', 'deepseek_url', 'send_interval',
    'reply_necessity_threshold', 'reply_activity_window',
    'reply_context_length', 'agent_max_replies', 'focus_score_bonus',
    'quote_min_activity', 'quote_window_seconds',
    'reply_min_gap', 'reply_rate_window', 'reply_rate_max',
    'enable_focus', 'focus_enter_activity', 'focus_idle_seconds', 'focus_max_seconds',
    'backoff_start_after', 'backoff_base_seconds', 'backoff_max_seconds', 'backoff_bypass_messages',
    'typo_rate', 'dry_run', 'quote_replies',
    'personality', 'reply_style', 'behavior_style', 'reply_styles', 'style_probability',
    'enable_planner', 'reply_mode', 'planner_url', 'planner_model', 'planner_key', 'planner_temperature',
    'planner_max_tokens', 'planner_thinking', 'planner_json_schema', 'enable_tools',
    'planner_rounds', 'request_timeout', 'trace_path',
})
# 构造期就定死的东西：路径 / 传输 / 识图 / 日志
RESTART_FIELDS = frozenset({
    'daemon_host', 'daemon_port', 'daemon_unix', 'admin_port', 'log_path', 'log_level',
    'describe_media', 'media_cache_path', 'describe_concurrency', 'describe_timeout',
    'describe_max_tokens', 'describe_image_max_tokens', 'describe_history_limit',
    'image_inline', 'image_shrink_threshold', 'image_shrink_target', 'image_fetch_timeout',
    'image_cache_dir',
    'profile_path', 'habit_path', 'moment_path', 'emoji_path', 'scope_path', 'moment_batch',
    'persona_path',
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
        self.speech = SpeechLimiter()
        self.emoji_policy = EmojiPolicy()
        # 允许回复名单：面板上改、落盘、热重载不重建
        self.reply_scope = ReplyScope(config.scope_path, seed=DEFAULT_MUTED)
        # 分会话人设覆盖：同样是面板上改、落盘、热重载不重建
        self.personas = PersonaStore(config.persona_path)

        # 构造期定死的东西
        self.media_cache = MediaCache(config.media_cache_path)
        self.images = ImageStore(config.image_cache_dir, config.image_shrink_threshold,
                                 config.image_shrink_target) if config.image_inline else None
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
        # 未登录时 on_ready 撞 401（连得上但查不了），真正拉历史要等 daemon 广播
        # login_status。两个都注册：启动已登录时走前者，扫码重登走后者
        self.bot.register_login(self.bot.load_context)
        if config.dry_run:
            logger.warning('演习模式：全链路照跑，但不真的发消息')

    # ---------- 构造 ----------

    def _make_bot(self) -> Bot:
        config = self.config
        return Bot(config.daemon_host, config.daemon_port, unix_path=config.daemon_unix, x_delay=0.5,
                   send_interval=config.send_interval, describer=self.describer,
                   describe_history_limit=config.describe_history_limit,
                   keeper=MomentKeeper(self.ai, self.memories, batch=config.moment_batch),
                   emojis=self.emojis, emoji_path=config.emoji_path,
                   emoji_describer=self.emoji_describer, typo_rate=config.typo_rate,
                   quote_replies=config.quote_replies, quote_min_activity=config.quote_min_activity,
                   quote_window=config.quote_window_seconds, dry_run=config.dry_run)

    def _make_describer(self) -> MediaDescriber | None:
        if not self.config.describe_media:
            return None
        return MediaDescriber(self.ai, self.media_cache, model=self.config.deepseek_model,
                              concurrency=self.config.describe_concurrency,
                              timeout=self.config.describe_timeout,
                              max_tokens=self.config.describe_max_tokens,
                              image_max_tokens=self.config.describe_image_max_tokens)

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
                           focus_score_bonus=config.focus_score_bonus,
                           backoff=self.backoff, focus=self.focus, speech=self.speech,
                           aliases_for=lambda contact: self._make_persona(contact).aliases)

    def _make_persona(self, contact: str = '') -> Persona:
        config = self.config
        base = Persona(
            personality=config.personality, reply_style=config.reply_style,
            behavior_style=config.behavior_style,
            alternates=tuple(style.strip() for style in config.reply_styles.split('|') if style.strip()),
            alternate_probability=config.style_probability)
        return self.personas.persona_for(contact, base) if contact else base

    @property
    def persona(self) -> Persona:
        """ 当前**全局**人设（不含分会话覆盖）。面板要拿它当「继承」的参照。 """
        return self._make_persona()

    def _make_chat_plugin(self) -> ChatPlugin:
        config = self.config
        return ChatPlugin(self.ai, context_length=config.reply_context_length, policy=self._make_policy(),
                          persona=self._make_persona(), profiles=self.profiles,
                          habits=self.habits, memories=self.memories,
                          tracer=TraceLogger(config.trace_path),
                          enable_planner=config.enable_planner, planner_ai=self.planner_ai,
                          emoji_search=self.bot.search_emoji, enable_tools=config.enable_tools,
                          planner_rounds=config.planner_rounds, emoji_policy=self.emoji_policy,
                          reply_mode=config.reply_mode, revoke=self.bot.revoke,
                          member_lookup=self.bot.lookup_member,
                          message_search=self.bot.search_messages,
                          agent_max_replies=config.agent_max_replies,
                          personas=self.personas,
                          image_store=self.images, image_timeout=config.image_fetch_timeout,
                          timeout=config.request_timeout)

    def _install_plugins(self) -> None:
        """ 按「允许回复」名单装插件链。

        mode='all'：默认链 + 静音名单走 EndProcessing；
        mode='allow'：默认就是 EndProcessing，只给允许名单装真链。
        两种模式复用同一套「按显示名覆盖插件链」的机制，`is_muted()` 在两种模式下
        都如实反映「这个会话会不会回」。
        """
        scope = self.reply_scope
        chain = [RepeatPlugin(), self.chat_plugin]
        self.bot.clear_contacts()      # 名单每一版全量重装，不清会留下上一版的映射
        if scope.mode == 'allow':
            self.bot.set_default_plugin([EndProcessing()])
            for contact in scope.allowed:
                self.bot.attach(contact, chain)
        else:
            self.bot.set_default_plugin(chain)
            for contact in scope.blocked:
                self.bot.attach(contact, EndProcessing())

    def install_scope(self) -> None:
        """ 面板改完允许名单后立刻生效：只重装插件链，不重建模型客户端、不清运行状态。 """
        self._install_plugins()

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
        self.speech.min_gap = config.reply_min_gap
        self.speech.window = config.reply_rate_window
        self.speech.maximum = config.reply_rate_max

        self.ai, self.planner_ai = _providers(config)
        self.chat_plugin = self._make_chat_plugin()
        self._install_plugins()
        self.bot.typo_rate = config.typo_rate
        self.bot.quote_replies = config.quote_replies
        self.bot.quote_min_activity = config.quote_min_activity
        self.bot.quote_window = config.quote_window_seconds
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
