import signal
import asyncio
import requests
from loguru import logger
from ai import Deepseek, ChatGLM
from config import CONFIG
from context import ContextManager
from message import parse_reference_message
from plugin import *
from wechat import WxBot, RawMessage

logger.add(sink='bot.log', rotation='1 week', retention='7 days', level='DEBUG')

class WxHelper(WxBot):
    """ 微信机器人助手 """

    _default_plugins: list[Plugin]
    _plugin_mappings: dict[str, list[Plugin]]
    """ 插件映射表. 键为联系人名称，值为插件列表 """

    def __init__(
            self,
            host: str,
            port: int,
            remote_storage_path: str,
            remote_server_prefix: str,
            dry_run: bool = False,
            _default_plugin: Plugin | list[Plugin] = EndProcessingPlugin(),
            reply_delay_coefficient: float = 0
    ):
        # TODO: 用 kwargs 传参，简化代码
        super().__init__(host, port, remote_storage_path=remote_storage_path, remote_server_prefix=remote_server_prefix,
                         dry_run=dry_run)

        assert isinstance(_default_plugin, Plugin) or isinstance(_default_plugin, list)
        assert reply_delay_coefficient >= 0

        self.set_default_plugin(_default_plugin)
        self._plugin_mappings = {}
        self._context = ContextManager()
        self.delay_coefficient = reply_delay_coefficient
        logger.info(f'Hello, {self.self_info["name"]}!')

    def set_default_plugin(self, plugin: Plugin | list[Plugin]):
        """ 设置默认插件 """
        if isinstance(plugin, Plugin):
            self._default_plugins = [plugin]
        else:
            self._default_plugins = plugin

    def attach_plugin(self, contact: str, plugins: Plugin | list[Plugin]):
        """ 设置插件 """
        if isinstance(plugins, Plugin):
            plugins = [plugins]
        self._plugin_mappings[contact] = plugins

    async def load_context(self, session_count: int = 10, window_size: int = 30) -> None:
        """ 可以在启动时加载最近的若干聊天记录, 如果想提高启动速度, 可以不调用它.

        :param session_count: 加载的最近会话数量
        :param window_size: 每个会话加载的消息数量
        """
        count_loaded = 0
        sessions = await self.get_recent_sessions(session_count)
        for contact in sessions:
            window = await self.fetch_history(contact, window_size)
            count_loaded += len(window)
            self._context.get_context(contact).extend(window)

        logger.info(f'{len(sessions)} sessions, {count_loaded} messages loaded.')

    @staticmethod
    async def _parse_reference_message(msg: RawMessage) -> None:
        """ 解析引用消息. 注意。这个函数会修改原始消息对象 """

        assert msg.type == 49
        try:
            parsed_message = parse_reference_message(msg.content)
        except Exception as e:
            logger.error(f'Error on parsing reference message: {e}')
            raise e
        msg.content = parsed_message['content'] + f'引用了消息（{parsed_message['referred_message']}）'

    async def _parse_image_message(self, msg: RawMessage) -> None:
        """ 解析图片消息 """
        try:
            accessible_url = await self._fetch_image(msg)
        except Exception as e:
            logger.error(e)
            raise e

        image_content = requests.get(accessible_url).content
        assert image_content, '图片内容为空'

        chatglm_provider: ChatGLM = ChatGLM(key=CONFIG['chatglm-key'])
        description = await chatglm_provider.describe_image(chatglm_provider.get_image_prompt(), image_content)
        description = description.replace('\n', '')
        logger.info(f'image description: {description}')
        msg.content = f'图片（文字描述：{description}）'

    async def _special_message_hook(self, msg: RawMessage) -> None:
        """ 特殊消息处理 """
        match msg.type:
            case 49:  # 引用消息
                await self._parse_reference_message(msg)
            case 3:  # 图片消息
                await self._parse_image_message(msg)
            case _:
                pass

    @override
    async def on_message(self, msg: RawMessage) -> None:
        """ 消息处理函数 """

        await self._special_message_hook(msg)

        # 自己的消息不用回复，也不用存储。在发送的时候会自动存储。
        if msg.from_self():
            return

        if self._context.get_context(msg.roomid).empty():
            # 如果收到某人的消息，但是没有上下文，则尝试加载历史消息
            window = await self.fetch_history(msg.roomid)
            self._context.get_context(msg.roomid).extend(window)

        # 自己的名字
        self_name: str = await self.get_display_name(self.wxid, msg.roomid)
        # 消息来源。如果是群消息，则为群名称；否则为联系人备注
        name = lambda x: self.all_contacts.get(x, x)
        source = name(msg.roomid)
        sender = name(msg.sender)
        logger.info(f'new message from {name(msg.roomid)}: {repr(msg.content)}')

        # TODO: 回复或记录表情消息
        if msg.type == 47:
            return

        # 加入聊天记录缓存
        self._context.push_message(msg.roomid, sender, msg.content, msg.ts)

        # 消息处理
        response_back: list[str] | None = []
        for plugin in self._plugin_mappings.get(source, self._default_plugins):
            chat_context = self._context.get_context(msg.roomid)
            current_round, _continue = await plugin.handle(msg, context=chat_context, self_name=self_name, contact=source)
            if not _continue:
                break
            if current_round:
                response_back.extend(current_round)

        if not response_back:
            logger.debug('no response to send.')
            return  # 没有回复内容

        # 回复消息
        for text in response_back:
            if self.delay_coefficient:
                time.sleep(len(text) * self.delay_coefficient)
            now = int(time.time())
            await self.send_text_msg(text, msg.roomid)
            self._context.push_message(msg.roomid, self_name, text, now)


def main():
    from config import CONFIG

    host, port = CONFIG['wcf-host'], CONFIG['wcf-port']
    FERRY: WxHelper = WxHelper(host, port, remote_storage_path=CONFIG['remote-storage-path'],
                               remote_server_prefix=CONFIG['remote-server-prefix'], reply_delay_coefficient=0.2)

    # 捕获 Ctrl+C 信号
    def signal_handler(sig, frame):
        logger.warning('Ctrl+C pressed. Exit.')
        FERRY.stop_receiving_message()
        FERRY.cleanup()
        exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # 设置插件
    # ai_provider: ChatAI = ChatGLM(key=CONFIG['chatglm-key'])
    ai_provider: AiProvider = Deepseek(key=CONFIG['deepseek-key'], model='deepseek-chat', temperature=1.3)
    chat_plugin = ChatPlugin(ai_provider, max_ignore=2, frequency=10, context_length=10)
    repeater = RepeatPlugin()

    FERRY.set_default_plugin([repeater, chat_plugin])
    FERRY.attach_plugin('研究生摆烂群', [EndProcessingPlugin()])
    # FERRY.attach_plugin('sunnysab', [chat_plugin])

    asyncio.run(FERRY.load_context())

    # 开始接收消息
    FERRY.start_receiving_message()
    while True:
        time.sleep(1)


if __name__ == '__main__':
    main()
