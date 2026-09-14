import random
import time
from abc import abstractmethod
from typing import TypeVar

from loguru import logger

from context import ChatWindow, ContextManager, MessageContext, MessageKind, SingleRecord
from describer import MediaDescriber
from emojis import Emoji, EmojiLibrary, parse_emoji_xml
from media import parse_media, render
from moments import MomentKeeper
from outbound import Outbound, OutboundQueue
from plugin import Plugin, PluginManager, Reply
from typos import apply_typos
from wechat import Message, Wechat

# 本地表情够这么多就不再联网搜——本群用过的比商店里的更像人话
EMOJI_LOCAL_ENOUGH = 3
EMOJI_RESULT_LIMIT = 8
EMOJI_SAVE_INTERVAL = 5.0      # 表情库最长隔这么久落一次盘（热度是运行时累加的）

T = TypeVar('T')


def _aslist(x: T | list[T]) -> list[T]:
    """ 把「单个对象或列表」统一成列表。 """
    return x if isinstance(x, list) else [x]


class Preprocessor:
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    async def process(self, msg: Message, **ctx) -> Message:
        pass


class PayloadProcessor(Preprocessor):
    """ 把非文本消息的正文替换成可读标记：图片/表情/位置/视频/语音/链接/文件/引用。 """

    async def process(self, msg: Message, **ctx) -> Message:
        text, _ = render(msg.msg_type, msg.content, msg.text)
        if text:
            msg.content = msg.clean_text = text
        return msg


class Bot:
    """ 微信机器人助手 """

    def __init__(self, host: str, port: int, unix_path: str = '', dry_run: bool = False,
                 x_delay: float = 0, send_interval: float = 1.0, preprocessors: list[Preprocessor] = None,
                 describer: MediaDescriber | None = None, describe_history_limit: int = 3,
                 keeper: MomentKeeper | None = None, emojis: EmojiLibrary | None = None,
                 emoji_path: str = '', emoji_describer=None, typo_rate: float = 0.0,
                 rng: random.Random | None = None):
        """ 初始化机器人

        @param host: weixin-rs relay 服务器地址
        @param port: weixin-rs relay 服务器端口
        @param unix_path: relay 的 Unix domain socket 路径，非空时优先于 host/port
        @param dry_run: 是否为测试模式
        @param x_delay: 每条消息回复的延迟时间系数（将根据消息长度乘此系数，再叠加随机抖动）
        @param send_interval: 全局最小发送间隔（秒），用于规避风控
        @param describer: 媒体描述器，后台把图片/表情含义写回历史；为 None 则不做识图
        @param describe_history_limit: 启动加载历史时，每个会话最多补描述多少条媒体
        @param keeper: 中期记忆：消息滑出上下文窗口时压缩成回想；为 None 则不记
        @param emojis: 本地表情库：收到的表情顺手存下，以后能原样发回去；为 None 则不偷
        @param emoji_path: 表情库落盘位置；留空则不落盘（重启就没了）
        @param emoji_describer: 给偷来的表情补「使用场景」的后台任务；为 None 则不补
        @param typo_rate: 出站文本抖错别字的概率（0 为关闭）
        @param rng: 错别字用的随机源，测试时可注入
        """
        async def callback(msg: Message) -> None:
            await self.on_message(msg)

        self._wechat = Wechat(host, port, callback=callback, dry_run=dry_run, unix_path=unix_path)

        # 聊天上下文记录。消息滑出窗口时交给 keeper 压成中期记忆
        self._history = ContextManager(on_evict=self._on_evicted)
        self._keeper = keeper
        # 消息处理插件
        self._plugins = PluginManager()
        # 消息预处理插件
        self._preprocessors: list[Preprocessor] = preprocessors or []
        # 媒体识图（可选）
        self._describer = describer
        self.describe_history_limit = describe_history_limit
        # 表情库：群里收到的表情带完整下载信息，存下来就能原样发回去
        self.emojis = emojis
        self.emoji_path = emoji_path
        self.emoji_describer = emoji_describer
        self.typo_rate = typo_rate
        self._rng = rng or random.Random()
        self._emoji_saved_at = time.time()

        assert x_delay >= 0
        self.delay_coefficient = x_delay
        # 全局发送队列：限流 + 拟人延迟，与消息处理解耦
        self.outbox = OutboundQueue(send=self._deliver, min_interval=send_interval,
                                    typing_delay=self._typing_delay)

    @property
    def wxid(self) -> str:
        return self._wechat.wxid

    async def search_emoji(self, keyword: str) -> list[Emoji]:
        """ 搜表情：先翻本群的梗，不够再联网，线上的顺手存进库里当缓存。

        本群用过的表情比商店里的更像人话，所以本地够挑就不联网。
        """
        local = self.emojis.search(keyword) if self.emojis else []
        if len(local) >= EMOJI_LOCAL_ENOUGH:
            return local
        try:
            online = await self._wechat.search_emoji(keyword)
        except Exception as e:
            logger.warning(f'搜表情失败（{keyword}）: {e}')
            return local
        seen = {item.md5 for item in local}
        merged = local + [item for item in online if item.md5 not in seen]
        if self.emojis and online:
            for item in online:
                self.emojis.record(item)   # 当缓存，下次同类关键词不用再联网
            self.save_emojis()
        return merged[:EMOJI_RESULT_LIMIT]

    def steal_emoji(self, msg) -> None:
        """ 群里发的表情顺手存下来。

        微信的表情消息 XML 自带 cdnurl/aeskey，所以存下来就能原样发回去——
        这就是「偷表情」的全部关键，不用找图，也不用视觉模型。

        顺便排一个后台任务补「使用场景」：微信给的 desc 只有一个标签（「摊倒」），
        够搜标签、不够搜场合。只要本群用过的那些（有热度），不然后台识图会爆。
        """
        if not self.emojis:
            return
        emoji = parse_emoji_xml(msg.content)
        if emoji is None:
            return
        if self.emojis.record(emoji, used=True):      # 群里用了一次：热度加一
            # 落盘不用每条都做，攒一会儿再写；stop() 里还会再存一次
            if time.time() - self._emoji_saved_at >= EMOJI_SAVE_INTERVAL:
                self.save_emojis()
        if self.emoji_describer:
            self.emoji_describer.schedule(self.emojis.get(emoji.md5),
                                          getattr(msg, 'get_media_bytes', None))

    def save_emojis(self) -> None:
        if not (self.emojis and self.emoji_path):
            return
        try:
            self.emojis.save(self.emoji_path)
            self._emoji_saved_at = time.time()
        except OSError as e:
            logger.warning(f'表情库落盘失败 {self.emoji_path}: {e}')

    async def start(self):
        await self.outbox.start()
        await self._wechat.start()

    async def stop(self):
        await self._wechat.stop()
        if self.emoji_describer:
            await self.emoji_describer.join()
        self.save_emojis()
        await self.outbox.stop()
        if self._describer:
            await self._describer.stop()
        if self._keeper:
            await self._keeper.join()  # 收尾：等在跑的压缩任务写完

    def _on_evicted(self, chat: str, record: SingleRecord) -> None:
        """ 消息滑出上下文窗口，交给中期记忆。 """
        if self._keeper:
            self._keeper.schedule(chat, record)

    def register_ready(self, func) -> None:
        """ 注册连接就绪后的回调（如加载历史上下文） """
        self._wechat.on_ready(func)

    def add_preprocessor(self, preprocessor: Preprocessor | list[Preprocessor]):
        self._preprocessors.extend(_aslist(preprocessor))

    def set_default_plugin(self, plugin: Plugin | list[Plugin]):
        for p in _aslist(plugin):
            self._plugins.register(p)

    def attach(self, contact: str, plugin: Plugin | list[Plugin]):
        for p in _aslist(plugin):
            self._plugins.map_plugin_to_contact(contact, p)

    async def load_context(self, session_count: int = 10, window_size: int = 30) -> None:
        """ 可以在启动时加载最近的若干聊天记录, 如果想提高启动速度, 可以不调用它.

        :param session_count: 加载的最近会话数量
        :param window_size: 每个会话加载的消息数量
        """
        count_loaded = 0
        sessions = await self._wechat.get_recent_sessions(session_count)
        for contact in sessions:
            window = await self._wechat.fetch_history(contact, window_size)
            count_loaded += len(window)
            self._history.get_context(contact).extend(window)
            self._schedule_history_media(window)

        logger.info(f'{len(sessions)} sessions, {count_loaded} messages loaded.')

    def _schedule_history_media(self, window: ChatWindow) -> None:
        """ 启动加载的历史媒体按会话限量补描述，避免一次烧太多识图调用。 """
        if not self._describer:
            return
        scheduled = 0
        for record in reversed(list(window)):
            if scheduled >= self.describe_history_limit:
                break
            if record.media and record.media.needs_description and record.download:
                self._describer.schedule(record)
                scheduled += 1

    def _typing_delay(self, text: str) -> float:
        """ 拟人打字延迟 = 随机思考时间 + 与字数成比例的时间（再乘随机抖动），单条最多 50 秒。 """
        if not self.delay_coefficient:
            return 0.0
        think = random.uniform(1.5, 4.0)
        typing = len(text) * self.delay_coefficient * random.uniform(0.7, 1.3)
        return min(think + typing, 50)

    async def _deliver(self, item: Outbound) -> None:
        """ 真正发送并写入上下文，由 OutboundQueue 串行调用。 """
        # 错别字只在出站这一刻抖，历史里记的也是抖过的版本——跟真人一致
        text = apply_typos(item.text, self.typo_rate, self._rng) if item.text else ''
        try:
            if text:
                await self._wechat.send_text(text, item.receiver)
            if item.emoji:
                await self._wechat.send_emoji(item.emoji, item.receiver)
        except Exception as e:
            # relay 长链发送回执解析有 bug：消息通常已经投递出去，重发会重复发。
            # 所以只告警不重试，但仍然写历史，否则下一轮模型看不到自己说过的话。
            # ponytail: 真·投递失败时历史会多一条，靠日志排查
            logger.warning(f'发送报错，按已投递处理（不重试）{item.receiver}: {e}')
        if not text:
            self._history.push_message(item.receiver, item.sender,
                                       f'[表情：{item.emoji.desc or "表情"}]', int(time.time()), is_self=True)
            return
        self._history.push_message(item.receiver, item.sender, text, int(time.time()), is_self=True)

    async def _preprocess(self, msg: Message, **ctx) -> Message | None:
        """ 依次跑预处理器。任一预处理器返回空则放弃这条消息。 """
        for preprocessor in self._preprocessors:
            msg = await preprocessor.process(msg, **ctx)
            if not msg:
                logger.warning(f'Preprocessor {preprocessor.name()} refuse to process, return.')
                return None
        return msg

    async def _run_plugins(self, msg: Message, self_name: str, source: str) -> Reply:
        """ 把当前消息交给该会话的插件链，收集要回复的文本和表情。 """
        ctx = MessageContext(msg=msg, history=self._history.get_context(msg.reply_target),
                             self_name=self_name, contact=source)
        texts: list[str] = []
        emoji = None
        for plugin in self._plugins.get_plugins_for_contact(source):
            reply = await plugin.handle(ctx) or Reply()
            texts.extend(reply.texts)
            emoji = emoji or reply.emoji
            if reply.stop:
                break
        return Reply(texts=texts, emoji=emoji)

    async def on_message(self, msg: Message) -> None:
        """ 消息处理函数 """

        # 自己的消息不用回复，也不用存储。在发送的时候会自动存储。
        if msg.is_self:
            return

        # 图片/表情的原始 XML 在预处理后会被替换成占位标记，先解析出来
        kind = MessageKind.of(msg.msg_type)
        media = parse_media(msg.content) if kind in (MessageKind.IMAGE, MessageKind.EMOJI) else None
        download = msg.get_media_bytes if (media and media.needs_description) else None
        if kind is MessageKind.EMOJI:
            self.steal_emoji(msg)

        # 自己的名字；消息来源：群消息取群名称，否则取联系人备注
        self_name: str = await self._wechat.get_display_name(self.wxid, msg.reply_target)
        source = self._wechat.all_contacts.get(msg.reply_target, msg.reply_target)
        logger.debug(msg)

        msg = await self._preprocess(msg, media=media)
        if msg is None:
            return

        record = self._history.push_message(msg.reply_target, msg.sender_name, msg.content, msg.create_time,
                                            media=media, download=download)
        # 媒体描述缺失时后台识图，稍后原地回写这条记录，不阻塞本轮回复
        if self._describer:
            self._describer.schedule(record)

        response = await self._run_plugins(msg, self_name, source)
        if not response.texts and not response.emoji:
            logger.debug('no response to send.')
            return  # 没有回复内容

        # 交给全局发送队列统一限流和拟人延迟，避免阻塞消息接收循环
        for text in response.texts:
            self.outbox.put(msg.reply_target, text, sender=self_name)
        if response.emoji:
            # 表情排在文字后面：先说一句，再补个表情收尾
            self.outbox.put_emoji(msg.reply_target, response.emoji, sender=self_name)
        logger.debug(f'{len(response.texts)} responses queued.')
