import os
from dataclasses import dataclass

from persona import Persona


@dataclass(frozen=True)
class Config:
    """ 运行配置。全部来自环境变量，启动时一次性读取。 """

    relay_host: str = '127.0.0.1'
    relay_port: int = 9099
    # 走 Unix domain socket 时填写（如 /tmp/weixin-relay.sock），否则留空走 TCP
    relay_unix: str = ''
    deepseek_key: str = ''
    deepseek_model: str = 'deepseek-flash'
    # 全局最小发送间隔（秒），规避微信风控
    send_interval: float = 1.0
    # 媒体（图片 / 缺 desc 的表情）识图描述
    describe_media: bool = True
    media_cache_path: str = 'media_cache.db'
    describe_concurrency: int = 2
    describe_timeout: float = 30.0
    describe_max_tokens: int = 64
    describe_history_limit: int = 3
    # 群聊参与：必要性打分阈值（越高越沉默）与活跃度统计窗口
    reply_necessity_threshold: int = 60
    reply_activity_window: float = 180.0
    # 群友画像（离线生成，见 build_profiles.py）
    profile_path: str = 'memory/profiles.json'
    # 群里的特色用语（离线生成，见 build_habits.py）
    habit_path: str = 'memory/habits.json'
    # 中期记忆：滑出上下文窗口的消息压成回想，存这个 sqlite
    moment_path: str = 'memory/moments.db'
    moment_batch: int = 20
    # 人格：身份 / 表达风格 / 行动准则（分别喂给 expressor 和 planner）
    personality: str = Persona.personality
    reply_style: str = Persona.reply_style
    behavior_style: str = Persona.behavior_style
    # 备用表达风格，用 | 分隔；按概率抽一条用在本次回复上
    reply_styles: str = ''
    style_probability: float = 0.0
    # 两段式：Planner 判断说与不说，Expressor 负责说成人话
    enable_planner: bool = True
    # 回复轨迹 JSONL（攒数据给以后做效果评估）
    trace_path: str = 'reply_trace.jsonl'
    log_path: str = 'bot.log'
    log_level: str = 'DEBUG'

    @classmethod
    def from_env(cls) -> 'Config':
        return cls(
            relay_host=os.getenv('RELAY_HOST', cls.relay_host),
            relay_port=int(os.getenv('RELAY_PORT', str(cls.relay_port))),
            relay_unix=os.getenv('RELAY_UNIX', cls.relay_unix),
            deepseek_key=os.getenv('DEEPSEEK_KEY', cls.deepseek_key),
            deepseek_model=os.getenv('DEEPSEEK_MODEL', cls.deepseek_model),
            send_interval=float(os.getenv('SEND_INTERVAL', str(cls.send_interval))),
            describe_media=os.getenv('DESCRIBE_MEDIA', '1').lower() not in ('0', 'false', 'no'),
            media_cache_path=os.getenv('MEDIA_CACHE_PATH', cls.media_cache_path),
            describe_concurrency=int(os.getenv('DESCRIBE_CONCURRENCY', str(cls.describe_concurrency))),
            describe_timeout=float(os.getenv('DESCRIBE_TIMEOUT', str(cls.describe_timeout))),
            describe_max_tokens=int(os.getenv('DESCRIBE_MAX_TOKENS', str(cls.describe_max_tokens))),
            describe_history_limit=int(os.getenv('DESCRIBE_HISTORY_LIMIT', str(cls.describe_history_limit))),
            reply_necessity_threshold=int(os.getenv('REPLY_NECESSITY_THRESHOLD', str(cls.reply_necessity_threshold))),
            reply_activity_window=float(os.getenv('REPLY_ACTIVITY_WINDOW', str(cls.reply_activity_window))),
            profile_path=os.getenv('PROFILE_PATH', cls.profile_path),
            habit_path=os.getenv('HABIT_PATH', cls.habit_path),
            moment_path=os.getenv('MOMENT_PATH', cls.moment_path),
            moment_batch=int(os.getenv('MOMENT_BATCH', str(cls.moment_batch))),
            personality=os.getenv('PERSONALITY', cls.personality),
            reply_style=os.getenv('REPLY_STYLE', cls.reply_style),
            behavior_style=os.getenv('BEHAVIOR_STYLE', cls.behavior_style),
            reply_styles=os.getenv('REPLY_STYLES', cls.reply_styles),
            style_probability=float(os.getenv('STYLE_PROBABILITY', str(cls.style_probability))),
            enable_planner=os.getenv('ENABLE_PLANNER', '1').lower() not in ('0', 'false', 'no'),
            trace_path=os.getenv('TRACE_PATH', cls.trace_path),
            log_path=os.getenv('LOG_PATH', cls.log_path),
            log_level=os.getenv('LOG_LEVEL', cls.log_level),
        )


if __name__ == '__main__':
    # 自检：无环境变量时用默认值，有环境变量时覆盖
    base = Config.from_env()
    assert base.relay_port == 9099 and base.send_interval == 1.0 and base.deepseek_key == '', base
    os.environ.update(RELAY_PORT='9098', SEND_INTERVAL='2.5', DEEPSEEK_KEY='k')
    overridden = Config.from_env()
    assert overridden.relay_port == 9098, overridden
    assert overridden.send_interval == 2.5, overridden
    assert overridden.deepseek_key == 'k', overridden
    print('ok')
