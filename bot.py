import random
import time
from abc import abstractmethod
from typing import TypeVar

from loguru import logger

from context import ChatWindow, ContextManager, MessageContext, MessageKind
from describer import MediaDescriber
from media import parse_media, render
from outbound import Outbound, OutboundQueue
from plugin import Plugin, PluginManager, Reply
from wechat import Message, Wechat

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
                 describer: MediaDescriber | None = None, describe_history_limit: int = 3):
        """ 初始化机器人

        @param host: weixin-rs relay 服务器地址
        @param port: weixin-rs relay 服务器端口
        @param unix_path: relay 的 Unix domain socket 路径，非空时优先于 host/port
        @param dry_run: 是否为测试模式
        @param x_delay: 每条消息回复的延迟时间系数（将根据消息长度乘此系数，再叠加随机抖动）
        @param send_interval: 全局最小发送间隔（秒），用于规避风控
        @param describer: 媒体描述器，后台把图片/表情含义写回历史；为 None 则不做识图
        @param describe_history_limit: 启动加载历史时，每个会话最多补描述多少条媒体
        """
        async def callback(msg: Message) -> None:
            await self.on_message(msg)

        self._wechat = Wechat(host, port, callback=callback, dry_run=dry_run, unix_path=unix_path)

        # 聊天上下文记录
        self._history = ContextManager()
        # 消息处理插件
        self._plugins = PluginManager()
        # 消息预处理插件
        self._preprocessors: list[Preprocessor] = preprocessors or []
        # 媒体识图（可选）
        self._describer = describer
        self.describe_history_limit = describe_history_limit

        assert x_delay >= 0
        self.delay_coefficient = x_delay
        # 全局发送队列：限流 + 拟人延迟，与消息处理解耦
        self.outbox = OutboundQueue(send=self._deliver, min_interval=send_interval,
                                    typing_delay=self._typing_delay)

    @property
    def wxid(self) -> str:
        return self._wechat.wxid

    async def start(self):
        await self.outbox.start()
        await self._wechat.start()

    async def stop(self):
        await self._wechat.stop()
        await self.outbox.stop()
        if self._describer:
            await self._describer.stop()

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
        await self._wechat.send_text(item.text, item.receiver)
        self._history.push_message(item.receiver, item.sender, item.text, int(time.time()), is_self=True)

    async def _preprocess(self, msg: Message, **ctx) -> Message | None:
        """ 依次跑预处理器。任一预处理器返回空则放弃这条消息。 """
        for preprocessor in self._preprocessors:
            msg = await preprocessor.process(msg, **ctx)
            if not msg:
                logger.warning(f'Preprocessor {preprocessor.name()} refuse to process, return.')
                return None
        return msg

    async def _run_plugins(self, msg: Message, self_name: str, source: str) -> list[str]:
        """ 把当前消息交给该会话的插件链，收集要回复的文本。 """
        ctx = MessageContext(msg=msg, history=self._history.get_context(msg.reply_target),
                             self_name=self_name, contact=source)
        response_back: list[str] = []
        for plugin in self._plugins.get_plugins_for_contact(source):
            reply = await plugin.handle(ctx) or Reply()
            response_back.extend(reply.texts)
            if reply.stop:
                break
        return response_back

    async def on_message(self, msg: Message) -> None:
        """ 消息处理函数 """

        # 自己的消息不用回复，也不用存储。在发送的时候会自动存储。
        if msg.is_self:
            return

        # 图片/表情的原始 XML 在预处理后会被替换成占位标记，先解析出来
        kind = MessageKind.of(msg.msg_type)
        media = parse_media(msg.content) if kind in (MessageKind.IMAGE, MessageKind.EMOJI) else None
        download = msg.get_media_bytes if (media and media.needs_description) else None

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

        response_back = await self._run_plugins(msg, self_name, source)
        if not response_back:
            logger.debug('no response to send.')
            return  # 没有回复内容

        # 交给全局发送队列统一限流和拟人延迟，避免阻塞消息接收循环
        for text in response_back:
            self.outbox.put(msg.reply_target, text, sender=self_name)
        logger.debug(f'{len(response_back)} responses queued.')
