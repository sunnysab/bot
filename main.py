import asyncio
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from ai import Deepseek, OpenAI
from bot import Bot, PayloadProcessor
from config import Config
from describer import MediaCache, MediaDescriber
from habits import HabitStore
from moments import MomentKeeper, MomentStore
from persona import Persona
from planner import PLAN_RESPONSE_FORMAT
from plugin import ChatPlugin, EndProcessing, RepeatPlugin, ReplyPolicy
from profiles import ProfileStore
from reply_trace import TraceLogger


def main():
    load_dotenv(Path(__file__).parent / '.env')  # 先读 .env，已存在的环境变量优先
    config = Config.from_env()
    logger.add(sink=config.log_path, level=config.log_level)

    if not config.deepseek_key:
        logger.warning('DEEPSEEK_KEY 未设置，ChatPlugin 将无法调用模型')

    ai_provider = Deepseek(key=config.deepseek_key, model=config.deepseek_model, url=config.deepseek_url,
                           temperature=0.85)

    # 决策者可以走本地免费模型；留空 PLANNER_URL 就两段都用主模型
    planner_ai = None
    if config.planner_url:
        planner_ai = OpenAI(url=config.planner_url, key=config.planner_key, model=config.planner_model,
                            temperature=config.planner_temperature, max_tokens=config.planner_max_tokens,
                            # thinking 与 json_schema 二选一：开 schema 会压掉思考（见 config 注释）
                            extra_body={'chat_template_kwargs': {'enable_thinking': config.planner_thinking}},
                            response_format=PLAN_RESPONSE_FORMAT if config.planner_json_schema else None)
        logger.info(f'Planner 使用本地模型: {config.planner_url} / {config.planner_model} '
                    f'(thinking={config.planner_thinking}, json_schema={config.planner_json_schema})')

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

    # 人格：谁、怎么说话、怎么行动。别名也在这里
    persona = Persona(
        personality=config.personality, reply_style=config.reply_style, behavior_style=config.behavior_style,
        alternates=tuple(style.strip() for style in config.reply_styles.split('|') if style.strip()),
        alternate_probability=config.style_probability)
    logger.info(f'机器人别名: {"、".join(persona.aliases)}')

    policy = ReplyPolicy(threshold=config.reply_necessity_threshold,
                         activity_window=config.reply_activity_window, aliases=persona.aliases)
    profiles = ProfileStore.load(config.profile_path)
    if profiles:
        logger.info(f'已加载 {len(profiles)} 个人的画像: {config.profile_path}')
    habits = HabitStore.load(config.habit_path)
    if habits:
        logger.info(f'已加载 {len(habits)} 条群用语: {config.habit_path}')

    chat_plugin = ChatPlugin(ai_provider, context_length=30, policy=policy, persona=persona, profiles=profiles,
                             habits=habits, memories=memory_store, tracer=TraceLogger(config.trace_path),
                             enable_planner=config.enable_planner, planner_ai=planner_ai,
                             timeout=config.request_timeout)

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
