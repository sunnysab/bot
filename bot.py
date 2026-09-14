import random
import time
from typing import TypeVar

from loguru import logger

from context import ChatWindow, ContextManager, MessageContext, MessageKind, SingleRecord
from describer import MediaDescriber
from emojis import Emoji, EmojiLibrary, parse_emoji_xml
from events import Status, bus
from media import is_placeholder, render
from moments import MomentKeeper
from outbound import Outbound, OutboundQueue
from plugin import Plugin, PluginManager, Reply
from typos import apply_typos
from wechat import Message, Wechat

# 本地表情够这么多就不再联网搜——本群用过的比商店里的更像人话
EMOJI_LOCAL_ENOUGH = 3
EMOJI_RESULT_LIMIT = 8
EMOJI_SAVE_INTERVAL = 5.0      # 表情库最长隔这么久落一次盘（热度是运行时累加的）
STALE_MESSAGE_SECONDS = 300.0  # 比这更旧的消息只当上下文：relay 长链重连会补推离线积压

T = TypeVar('T')


def _aslist(x: T | list[T]) -> list[T]:
    """ 把「单个对象或列表」统一成列表。 """
    return x if isinstance(x, list) else [x]


def _relay_id(result) -> int | None:
    """ 从 relay 的发送回执里取 server_id。拿不到（演习 / 服务端没回 / 假 relay）返回 None。 """
    return result.get('msg_id') if isinstance(result, dict) else None


class Bot:
    """ 微信机器人助手 """

    def __init__(self, host: str, port: int, unix_path: str = '', dry_run: bool = False,
                 x_delay: float = 0, send_interval: float = 1.0,
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

        self.dry_run = dry_run
        self._wechat = Wechat(host, port, callback=callback, dry_run=dry_run, unix_path=unix_path)

        # 聊天上下文记录。消息滑出窗口时交给 keeper 压成中期记忆
        self._history = ContextManager(on_evict=self._on_evicted)
        self._keeper = keeper
        # 消息处理插件
        self._plugins = PluginManager()
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

    @property
    def wechat(self) -> Wechat:
        """ relay 适配层。管理后台直接拿它读会话列表和历史。 """
        return self._wechat

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
        # 只把「真能发出去」的存进库：线上搜回来的经常缺 cdnurl/aeskey（实测 40 条里
        # cdnurl 全空、aeskey 只有 10 条有），存下来也发不出去，只是占位置
        if self.emojis:
            cached = [item for item in online if item.sendable]
            for item in cached:
                self.emojis.record(item)
            if cached:
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
        stored = text or (f'[表情：{item.emoji.desc or "表情"}]' if item.emoji else '')
        # relay 给的 server_id：这条出站消息在 relay 里的身份。拿不到就留空
        relay_id = None
        error = ''
        try:
            if text:
                result = await self._wechat.send_text(text, item.receiver)
                relay_id = _relay_id(result)
            if item.emoji:
                result = await self._wechat.send_emoji(item.emoji, item.receiver)
                relay_id = relay_id or _relay_id(result)
        except Exception as e:
            # relay 长链发送回执解析有 bug：消息通常已经投递出去，重发会重复发。
            # 所以只告警不重试，但仍然写历史，否则下一轮模型看不到自己说过的话。
            # ponytail: 真·投递失败时历史会多一条，靠日志排查
            error = str(e)
            logger.warning(f'发送报错，按已投递处理（不重试）{item.receiver}: {e}')

        if self.dry_run:
            # 演习：没发出去、不进历史、也不在 relay 里。正文只能靠事件带出去，
            # 否则面板上这条回复就彻底消失了
            bus.publish(Status.DRY_RUN, chat_id=item.receiver, chat_name=item.chat_name,
                        reply_to=item.reply_to, origin='bot', text=stored,
                        detail='演习模式，未发送')
            return

        if stored:
            self._history.push_message(item.receiver, item.sender, stored, int(time.time()), is_self=True)
        bus.publish(Status.FAILED if error else Status.SENT, chat_id=item.receiver,
                    chat_name=item.chat_name, msg_id=relay_id, reply_to=item.reply_to,
                    origin='bot', detail=error[:200])

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

        # 图片/表情的原始 XML 要在被替换成占位标记前解析出来。
        # 放在 is_self 判断之前：手机上自己发的表情也是群里用过的，值得偷
        kind = MessageKind.of(msg.msg_type)
        if kind is MessageKind.EMOJI:
            self.steal_emoji(msg)

        # 自己的名字；消息来源：群消息取群名称，否则取联系人备注
        self_name: str = await self._wechat.get_display_name(self.wxid, msg.reply_target)
        source = await self._wechat.get_display_name(msg.reply_target)
        # relay 有时把群成员的显示名解析成了 wxid，那样历史里全是 wxid，得自己补一次
        if msg.is_chatroom and msg.sender_name == msg.sender:
            msg.sender_display_name = await self._wechat.get_display_name(msg.sender, msg.reply_target)
        logger.debug(msg)

        # 非文本消息的正文换成可读标记：图片/表情/位置/视频/语音/链接/文件/引用
        text, media = render(msg.msg_type, msg.content, msg.text)
        if text:
            msg.content = msg.clean_text = text
        download = msg.get_media_bytes if (media and media.needs_description) else None

        # 手机上自己发的消息：写进上下文让模型看见，但**不触发任何插件**。
        # 从群里看人设确实在说话（transcript 里渲染成「我」），所以存在感统计也算数
        if msg.is_self:
            record = self._history.push_message(msg.reply_target, self_name, msg.content,
                                                msg.create_time, media=media, download=download,
                                                is_self=True)
            if self._describer:
                self._describer.schedule(record)
            bus.publish(Status.PHONE, chat_id=msg.reply_target, chat_name=source,
                        msg_id=msg.msg_id, origin='phone', detail='手机上发的，只记不回')
            return

        record = self._history.push_message(msg.reply_target, msg.sender_name, msg.content, msg.create_time,
                                            media=media, download=download)
        # 媒体描述缺失时后台识图，稍后原地回写这条记录，不阻塞本轮回复
        if self._describer:
            self._describer.schedule(record)
        bus.publish(Status.RECEIVED, chat_id=msg.reply_target, chat_name=source, msg_id=msg.msg_id)

        # 离线的积压不触发决策：relay 长链重连会把几个钟头前的消息当 live 推过来，
        # 照发会对着死了的话题连回几十条。写进历史当上下文就够了。
        if msg.create_time and time.time() - msg.create_time > STALE_MESSAGE_SECONDS:
            ago = int(time.time() - msg.create_time)
            logger.info(f'积压消息只记不回（{ago}s 前）: {msg.sender_name}: {msg.content[:30]}')
            bus.publish(Status.STALE, chat_id=msg.reply_target, chat_name=source,
                        msg_id=msg.msg_id, detail=f'积压 {ago // 60} 分钟，只记不回')
            return

        response = await self._run_plugins(msg, self_name, source)
        if not response.texts and not response.emoji:
            logger.debug('no response to send.')
            # 插件链什么都没说，而且这一轮也没留下别的埋点（语音消息、或该会话静音），
            # 补一条，否则面板上这条消息会永远停在「已接收」
            latest = bus.status_of(msg.msg_id)
            if latest is not None and latest.status == Status.RECEIVED:
                handled = kind in (MessageKind.TEXT, MessageKind.IMAGE)
                bus.publish(Status.MUTED if handled else Status.UNSUPPORTED,
                            chat_id=msg.reply_target, chat_name=source, msg_id=msg.msg_id,
                            detail='该会话已静音' if handled else f'{kind.name} 不处理')
            return  # 没有回复内容

        # 交给全局发送队列统一限流和拟人延迟，避免阻塞消息接收循环
        texts = [text for text in response.texts if not is_placeholder(text)]
        if len(texts) != len(response.texts):
            logger.warning(f'拦下 {len(response.texts) - len(texts)} 条媒体占位符，不往群里发：{response.texts}')
            bus.publish(Status.PLACEHOLDER_BLOCKED, chat_id=msg.reply_target, chat_name=source,
                        msg_id=msg.msg_id,
                        detail=f'拦下 {len(response.texts) - len(texts)} 条媒体占位符')
        for text in texts:
            self.outbox.put(msg.reply_target, text, sender=self_name,
                            reply_to=msg.msg_id, chat_name=source)
        if response.emoji:
            # 表情排在文字后面：先说一句，再补个表情收尾
            self.outbox.put_emoji(msg.reply_target, response.emoji, sender=self_name,
                                  reply_to=msg.msg_id, chat_name=source)
        logger.debug(f'{len(texts)} responses queued.')
