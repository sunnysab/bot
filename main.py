import asyncio
import random
import time
from abc import abstractmethod
from typing import TypeVar

from loguru import logger

from ai import Deepseek
from config import Config
from context import ContextManager, MessageContext, MessageKind
from outbound import Outbound, OutboundQueue
from plugin import ChatPlugin, EndProcessing, Plugin, PluginManager, RepeatPlugin, Reply
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


class MediaProcessor(Preprocessor):
    """ 把图片/表情消息的正文替换为文字标记，真实图片在回复时随请求直接发送给模型。 """

    async def process(self, msg: Message, **ctx) -> Message:
        kind = MessageKind.of(msg.msg_type)
        if kind is MessageKind.IMAGE:
            msg.content = msg.clean_text = '[图片]'
        elif kind is MessageKind.EMOJI:
            msg.content = msg.clean_text = '[表情]'
        return msg


class Bot:
    """ 微信机器人助手 """

    def __init__(self, host: str, port: int, unix_path: str = '', dry_run: bool = False,
                 x_delay: float = 0, send_interval: float = 1.0, preprocessors: list[Preprocessor] = None):
        """ 初始化机器人

        @param host: weixin-rs relay 服务器地址
        @param port: weixin-rs relay 服务器端口
        @param unix_path: relay 的 Unix domain socket 路径，非空时优先于 host/port
        @param dry_run: 是否为测试模式
        @param x_delay: 每条消息回复的延迟时间系数（将根据消息长度乘此系数，再叠加随机抖动）
        @param send_interval: 全局最小发送间隔（秒），用于规避风控
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

        logger.info(f'{len(sessions)} sessions, {count_loaded} messages loaded.')

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
        self._history.push_message(item.receiver, item.sender, item.text, int(time.time()))

    async def _preprocess(self, msg: Message) -> Message | None:
        """ 依次跑预处理器。任一预处理器返回空则放弃这条消息。 """
        for preprocessor in self._preprocessors:
            msg = await preprocessor.process(msg)
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

        # 自己的名字；消息来源：群消息取群名称，否则取联系人备注
        self_name: str = await self._wechat.get_display_name(self.wxid, msg.reply_target)
        source = self._wechat.all_contacts.get(msg.reply_target, msg.reply_target)
        logger.debug(msg)

        msg = await self._preprocess(msg)
        if msg is None:
            return

        self._history.push_message(msg.reply_target, msg.sender_name, msg.content, msg.create_time)
        response_back = await self._run_plugins(msg, self_name, source)
        if not response_back:
            logger.debug('no response to send.')
            return  # 没有回复内容

        # 交给全局发送队列统一限流和拟人延迟，避免阻塞消息接收循环
        for text in response_back:
            self.outbox.put(msg.reply_target, text, sender=self_name)
        logger.debug(f'{len(response_back)} responses queued.')


def main():
    config = Config.from_env()
    logger.add(sink=config.log_path, level=config.log_level)

    bot = Bot(config.relay_host, config.relay_port, unix_path=config.relay_unix, x_delay=0.5,
              send_interval=config.send_interval)

    if not config.deepseek_key:
        logger.warning('DEEPSEEK_KEY 未设置，ChatPlugin 将无法调用模型')

    # 初始化预处理器和插件
    bot.add_preprocessor(MediaProcessor())

    ai_provider = Deepseek(key=config.deepseek_key, model=config.deepseek_model, temperature=0.85)
    repeater = RepeatPlugin()
    chat_plugin = ChatPlugin(ai_provider, context_length=30)

    bot.set_default_plugin([repeater, chat_plugin])
    bot.attach('研究生摆烂群', EndProcessing())

    # 连接就绪后加载最近的聊天记录
    bot.register_ready(lambda _bot: bot.load_context())

    async def run():
        try:
            await bot.start()
        finally:
            await bot.stop()

    # 开始接收消息
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.warning('Ctrl+C pressed. Exit.')


if __name__ == '__main__':
    main()
