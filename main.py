import asyncio

from loguru import logger

from ai import Deepseek
from bot import Bot, PayloadProcessor
from config import Config
from describer import MediaCache, MediaDescriber
from plugin import ChatPlugin, EndProcessing, RepeatPlugin


def main():
    config = Config.from_env()
    logger.add(sink=config.log_path, level=config.log_level)

    if not config.deepseek_key:
        logger.warning('DEEPSEEK_KEY 未设置，ChatPlugin 将无法调用模型')

    ai_provider = Deepseek(key=config.deepseek_key, model=config.deepseek_model, temperature=0.85)

    describer = None
    if config.describe_media:
        describer = MediaDescriber(ai_provider, MediaCache(config.media_cache_path),
                                   model=config.deepseek_model, concurrency=config.describe_concurrency,
                                   timeout=config.describe_timeout, max_tokens=config.describe_max_tokens)

    bot = Bot(config.relay_host, config.relay_port, unix_path=config.relay_unix, x_delay=0.5,
              send_interval=config.send_interval, describer=describer,
              describe_history_limit=config.describe_history_limit)

    # 初始化预处理器和插件
    bot.add_preprocessor(PayloadProcessor())

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
