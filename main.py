import asyncio
import time
from abc import abstractmethod

from loguru import logger

from ai import AiProvider, ChatGLM, Doubao
from context import ContextManager
from plugin import ChatPlugin, EndProcessing, Plugin, PluginManager, RepeatPlugin
from wechat import Message, Wechat

logger.add(sink='bot.log', level='DEBUG')


class Preprocessor:
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    async def process(self, msg: Message, **ctx) -> Message:
        pass


class ImageProcessor(Preprocessor):
    def __init__(self, descriptor: AiProvider):
        self.descriptor = descriptor

    @staticmethod
    def get_image_prompt() -> str:
        """ 获取图片描述的提示 """
        return '尽可能少的字数描述图片主体是什么, 里面物品有什么. 给人的感觉如何. 不要描述物品放置的目的.'

    async def process(self, msg: Message, **ctx) -> Message:
        """ Process image message by generating description """
        if msg.msg_type != 3:  # Not an image message
            return msg

        prompt = self.get_image_prompt()
        image = await msg.get_media_bytes()
        description = await self.descriptor.describe_image(prompt, image)
        description = description.replace('\n', '')
        logger.info(f'image description: {description}')
        msg.content = f'图片（文字描述：{description}）'
        return msg


class StickerProcessor(Preprocessor):
    async def process(self, msg: Message, **ctx) -> Message:
        if msg.msg_type != 47:
            return msg

        msg.content = '[表情]'
        return msg


class Bot:
    """ 微信机器人助手 """

    def __init__(self, host: str, port: int, unix_path: str = '', dry_run: bool = False,
                 x_delay: float = 0, preprocessors: list[Preprocessor] = None):
        """ 初始化机器人

        @param host: weixin-rs relay 服务器地址
        @param port: weixin-rs relay 服务器端口
        @param unix_path: relay 的 Unix domain socket 路径，非空时优先于 host/port
        @param dry_run: 是否为测试模式
        @param x_delay: 每条消息回复的延迟时间系数（将根据消息长度乘此系数）
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

    @property
    def wxid(self) -> str:
        return self._wechat.wxid

    async def start(self):
        await self._wechat.start()

    async def stop(self):
        await self._wechat.stop()

    def register_ready(self, func) -> None:
        """ 注册连接就绪后的回调（如加载历史上下文） """
        self._wechat.on_ready(func)

    def add_preprocessor(self, preprocessor: Preprocessor | list[Preprocessor]):
        if isinstance(preprocessor, list):
            self._preprocessors.extend(preprocessor)
        else:
            self._preprocessors.append(preprocessor)

    def set_default_plugin(self, plugin: Plugin | list[Plugin]):
        plugins = [plugin] if isinstance(plugin, Plugin) else plugin

        for p in plugins:
            self._plugins.register(p)

    def attach(self, contact: str, plugin: Plugin | list[Plugin]):
        plugins = [plugin] if isinstance(plugin, Plugin) else plugin
        for p in plugins:
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

    async def on_message(self, msg: Message) -> None:
        """ 消息处理函数 """

        # 自己的消息不用回复，也不用存储。在发送的时候会自动存储。
        if msg.is_self:
            return

        # 自己的名字
        self_name: str = await self._wechat.get_display_name(self.wxid, msg.reply_target)
        # 消息来源。如果是群消息，则为群名称；否则为联系人备注
        def name(wxid: str) -> str:
            return self._wechat.all_contacts.get(wxid, wxid)

        source = name(msg.reply_target)
        logger.debug(msg)

        # 预处理消息
        for preprocessor in self._preprocessors:
            if msg:
                msg = await preprocessor.process(msg)
            else:
                logger.warning(f'Preprocessor {preprocessor.name()} refuse to process, return.')
                return

        self._history.push_message(msg.reply_target, msg.sender_name, msg.content, msg.create_time)

        # 处理消息
        response_back: list[str] | None = []
        chat_context = self._history.get_context(msg.reply_target)
        plugins = self._plugins.get_plugins_for_contact(source)
        for plugin in plugins:
            current_round, _continue = await plugin.handle(msg, wechat=self._wechat, history=chat_context,
                                                           self_name=self_name, contact=source)
            if current_round:
                response_back.extend(current_round)
            if not _continue:
                break

        if not response_back:
            logger.debug('no response to send.')
            return  # 没有回复内容

        # 回复消息
        for text in response_back:
            if self.delay_coefficient:
                await asyncio.sleep(len(text) * self.delay_coefficient)
            now = int(time.time())
            await self._wechat.send_text(text, msg.reply_target)
            self._history.push_message(msg.reply_target, self_name, text, now)
        else:
            logger.debug('all responses sent.')


def main():
    from config import CONFIG

    bot = Bot(CONFIG['relay-host'], CONFIG['relay-port'], unix_path=CONFIG['relay-unix'], x_delay=0.2)

    # 初始化预处理器和插件
    chatglm = ChatGLM(key=CONFIG['chatglm-key'])
    bot.add_preprocessor([ImageProcessor(chatglm), StickerProcessor()])

    ai_provider = Doubao(model=CONFIG['doubao-model'], key=CONFIG['doubao-key'], temperature=0.85)
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
