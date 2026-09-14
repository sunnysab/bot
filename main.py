import asyncio
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from ai import Deepseek
from bot import Bot, PayloadProcessor
from config import Config
from describer import MediaCache, MediaDescriber
from habits import HabitStore
from moments import MomentKeeper, MomentStore
from persona import Persona
from plugin import ChatPlugin, EndProcessing, RepeatPlugin, ReplyPolicy
from profiles import ProfileStore
from reply_trace import TraceLogger


def main():
    load_dotenv(Path(__file__).parent / '.env')  # 先读 .env，已存在的环境变量优先
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

    memory_store = MomentStore(config.moment_path)
    bot = Bot(config.relay_host, config.relay_port, unix_path=config.relay_unix, x_delay=0.5,
              send_interval=config.send_interval, describer=describer,
              describe_history_limit=config.describe_history_limit,
              keeper=MomentKeeper(ai_provider, memory_store, batch=config.moment_batch))

    # 初始化预处理器和插件
    bot.add_preprocessor(PayloadProcessor())

    repeater = RepeatPlugin()
    policy = ReplyPolicy(threshold=config.reply_necessity_threshold,
                         activity_window=config.reply_activity_window)
    profiles = ProfileStore.load(config.profile_path)
    if profiles:
        logger.info(f'已加载 {len(profiles)} 个人的画像: {config.profile_path}')
    habits = HabitStore.load(config.habit_path)
    if habits:
        logger.info(f'已加载 {len(habits)} 条群用语: {config.habit_path}')

    persona = Persona(
        personality=config.personality, reply_style=config.reply_style, behavior_style=config.behavior_style,
        alternates=tuple(style.strip() for style in config.reply_styles.split('|') if style.strip()),
        alternate_probability=config.style_probability)
    chat_plugin = ChatPlugin(ai_provider, context_length=30, policy=policy, persona=persona, profiles=profiles,
                             habits=habits, memories=memory_store, tracer=TraceLogger(config.trace_path),
                             enable_planner=config.enable_planner, timeout=config.request_timeout)

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
