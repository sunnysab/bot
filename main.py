""" 入口：读配置、起 Runtime、起管理后台、开收消息。 """
import asyncio

from loguru import logger

from admin import Admin
from config import CONFIG_PATH, Config
from runtime import Runtime


def main():
    config = Config.load()
    logger.add(sink=config.log_path, level=config.log_level)

    if not config.deepseek_key:
        logger.warning(f'{CONFIG_PATH} 里没配 deepseek_key，ChatPlugin 将无法调用模型')

    runtime = Runtime(config)
    if config.planner_url:
        logger.info(f'Planner 使用本地模型: {config.planner_url} / {config.planner_model} '
                    f'(thinking={config.planner_thinking})')
    logger.info(f'机器人别名: {"、".join(runtime.chat_plugin.persona.aliases)}')
    logger.info(f'联网搜索: {"已接入 Tavily" if runtime.chat_plugin.web_search else "未配置，模型没有 web_search 工具"}')
    logger.info(f'专注模式 {"开" if config.enable_focus else "关"}：'
                f'活动 {config.focus_enter_activity} 条进入，'
                f'空闲 {int(config.focus_idle_seconds)}s / 最长 {int(config.focus_max_seconds)}s 退出；'
                f'错别字概率 {config.typo_rate:.0%}')

    async def run():
        # 后台在循环起来之后才起——它要拿 loop 才能把 RPC 切回来跑
        panel = None
        if config.admin_port:
            panel = Admin.start_safely(runtime, host=config.admin_host, port=config.admin_port)
        try:
            await runtime.start()
        finally:
            if panel:
                panel.stop()
            await runtime.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.warning('Ctrl+C pressed. Exit.')


if __name__ == '__main__':
    main()
