import os
from dataclasses import dataclass, fields

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
    # 主模型的服务地址，可指向第三方 OpenAI 兼容服务（更便宜的代理/中转）
    deepseek_url: str = 'https://api.deepseek.com'
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
    # 专注模式（HFC）：群里热起来就盯一会儿，期间不看门控
    enable_focus: bool = True
    focus_enter_activity: int = 15      # 活动窗口内别人的消息数够多就进
    focus_idle_seconds: float = 300.0   # 这么久没新消息自动退出
    focus_max_seconds: float = 1800.0   # 单次专注最长多久
    # 错别字：出站文本抖一个同音/形近字的概率（0 关掉）
    typo_rate: float = 0.08
    # 群友画像（离线生成，见 build_profiles.py）
    profile_path: str = 'memory/profiles.json'
    # 群里的特色用语（离线生成，见 build_habits.py）
    habit_path: str = 'memory/habits.json'
    # 中期记忆：滑出上下文窗口的消息压成回想，存这个 sqlite
    moment_path: str = 'memory/moments.db'
    emoji_path: str = 'memory/emojis.json'
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
    # Planner 可以走本地免费模型（OpenAI 兼容）；planner_url 留空则两段都用主模型
    planner_url: str = ''
    planner_model: str = 'minicpm5'
    planner_key: str = 'local'
    planner_temperature: float = 0.3
    planner_max_tokens: int = 1024   # 生成上限，不是上下文；thinking 模型会先吐 reasoning，给够即可
    enable_tools: bool = True        # 决策者是否用工具调用（搜表情等）；关掉退回单轮 JSON
    planner_rounds: int = 3          # 决策循环最多几轮：搜一轮、发一轮就够
    # 开 thinking：判断质量更好，但慢很多（中位 3.2s、最坏 8s）
    planner_thinking: bool = True
    # 用 json_schema 强制输出合法 JSON。注意：开 schema 会压掉 thinking
    # （实测约束解码下无论开关都是 0.6s 且几乎总是输出 speak=true），二选一
    planner_json_schema: bool = False
    # 单次模型调用超时（秒），避免一次 API 抽风拖死消息处理链
    request_timeout: float = 60.0
    # 回复轨迹 JSONL（攒数据给以后做效果评估）
    trace_path: str = 'reply_trace.jsonl'
    log_path: str = 'bot.log'
    log_level: str = 'DEBUG'

    @classmethod
    def from_env(cls) -> 'Config':
        """ 环境变量名 = 字段名大写；布尔按 '0/false/no' 为假解析。 """
        values = {}
        for f in fields(cls):
            raw = os.getenv(f.name.upper())
            if raw is None:
                continue
            values[f.name] = (raw.lower() not in ('0', 'false', 'no')) if f.type is bool else f.type(raw)
        return cls(**values)


if __name__ == '__main__':
    # 自检：无环境变量时用默认值，有环境变量时覆盖
    base = Config.from_env()
    assert base.relay_port == 9099 and base.send_interval == 1.0 and base.deepseek_key == '', base
    os.environ.update(RELAY_PORT='9098', SEND_INTERVAL='2.5', DEEPSEEK_KEY='k')
    overridden = Config.from_env()
    assert overridden.relay_port == 9098, overridden
    assert overridden.send_interval == 2.5, overridden
    assert overridden.deepseek_key == 'k', overridden
    # 专注模式与错别字可以从环境变量调
    assert base.enable_focus and base.typo_rate == 0.08, base
    os.environ.update(ENABLE_FOCUS='0', TYPO_RATE='0')
    off = Config.from_env()
    assert not off.enable_focus and off.typo_rate == 0.0, off
    print('ok')
