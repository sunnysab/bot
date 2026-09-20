import os
from dataclasses import dataclass, fields
from pathlib import Path

from persona import Persona

# .env 就是配置的落盘位置（启动时读它，管理后台写它）
DOTENV_PATH = Path(__file__).parent / '.env'


def update_dotenv(path: str | Path, updates: dict[str, str]) -> None:
    """ 只改给定的键，其余行原样保留，原子替换。

    不整个重写：`.env` 里可能有注释掉的备用 key（现在那个文件里就有一行），
    整文件重写会把它抹掉。也不直接写原文件：写到一半中断会留下半个 `.env`，
    下次启动就少几个配置。
    """
    path = Path(path)
    lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600

    done = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in stripped:
            key = stripped.split('=', 1)[0].strip()
            if key in updates:
                out.append(f'{key}={_quote(updates[key])}')
                done.add(key)
                continue
        out.append(line)
    # 新键追加到末尾（不动已有行的顺序）
    out.extend(f'{key}={_quote(value)}' for key, value in updates.items() if key not in done)

    tmp = path.parent / f'{path.name}.tmp'
    tmp.write_text('\n'.join(out) + '\n', encoding='utf-8')
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _quote(value: str) -> str:
    """ 值里有换行或首尾空白时加引号，否则 dotenv 会截掉。 """
    value = str(value)
    if '\n' in value or '#' in value or value != value.strip():
        return '"' + value.replace('"', '\\"') + '"'
    return value


@dataclass(frozen=True)
class Config:
    """ 运行配置。全部来自环境变量，启动时一次性读取。 """

    daemon_host: str = '127.0.0.1'
    daemon_port: int = 9099
    # 走 Unix domain socket 时填写（如 /tmp/weixin-daemon.sock），否则留空走 TCP
    daemon_unix: str = ''
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
    # 识图的生成上限（不是上下文）。**thinking 模型会先推理再回答，推理也算在这个上限里**——
    # 实测（deepseek-v4.1-flash，识图 prompt + 9094 字节真表情图）：
    #   64 → finish=length，reasoning=64，content 空
    #   256/512 → 同理，预算多大就烧多大，一个字不吐
    #   1024 → finish=stop，reasoning=754，拿到「兴奋|对方说有瓜…」
    #   2048 → finish=stop，reasoning=1137（它想得更多，但答案一样）
    # 给不思考的模型给大了也没代价（上限不是目标值），所以直接定 1024
    describe_max_tokens: int = 1024
    describe_history_limit: int = 3
    # 内联给模型的图片：窗口里的图按需取字节、附在请求里。这是本轮最费 token 的一块，
    # 留个开关能随时关掉
    image_inline: bool = True
    # 超过这么大才压；压到目标体积以下。优先保分辨率（先降 JPEG 质量，实在不行才缩边）
    image_shrink_threshold: int = 2 * 1024 * 1024
    image_shrink_target: int = 1024 * 1024
    image_fetch_timeout: float = 8.0    # 单张图取字节的超时（秒），超时就少附这一张
    image_cache_dir: str = 'media_cache/blobs'   # 压过的图按消息 svrid 落盘，下一轮直接读
    # 群聊参与：必要性打分阈值（越高越沉默）与活跃度统计窗口
    reply_necessity_threshold: int = 60
    reply_activity_window: float = 180.0
    # 回复行为（面板上直接调，都不用重启）
    reply_context_length: int = 60       # 每轮送进模型的历史条数（上限 ChatWindow.max_history=100）
    agent_max_replies: int = 1           # agent 模式一轮最多发几条消息
    focus_score_bonus: int = 0           # 专注期加分。默认 0：热群本来就有压力分，
                                         # 再加就成了「热群必回」（见 necessity.PRESSURE_MAX）
    quote_min_activity: int = 8          # 窗口内别人至少说这么多条，引用才不突兀
    quote_window_seconds: float = 180.0  # 判断「群里乱不乱」的时间窗口
    # 发言频率闸：阈值过了也可能说得太密，这是最后一道闸（只拦主动参与）
    reply_min_gap: float = 45.0          # 同一会话两条之间至少隔多久（秒）
    reply_rate_window: float = 300.0     # 滑动窗口（秒）
    reply_rate_max: int = 5              # 窗口内最多主动说几条
    # 专注模式（HFC）：群里热起来就盯一会儿，期间不看门控
    enable_focus: bool = True
    focus_enter_activity: int = 15      # 活动窗口内别人的消息数够多就进
    focus_idle_seconds: float = 300.0   # 这么久没新消息自动退出
    focus_max_seconds: float = 1800.0   # 单次专注最长多久
    # 连续沉默退避：连着几轮都不开口就歶一会儿（见 necessity.IdleBackoff）
    backoff_start_after: int = 2        # 连着几轮没开口才开始退避
    backoff_base_seconds: float = 15.0  # 第一档冷却，逐次翻倍
    backoff_max_seconds: float = 300.0  # 冷却上限
    backoff_bypass_messages: int = 6    # 中间又来了这么多条就当话题翻篇
    # 错别字：出站文本抖一个同音/形近字的概率（0 关掉）
    typo_rate: float = 0.08
    # 群友画像（离线生成，见 build_profiles.py）
    profile_path: str = 'memory/profiles.json'
    # 群里的特色用语（离线生成，见 build_habits.py）
    habit_path: str = 'memory/habits.json'
    # 中期记忆：滑出上下文窗口的消息压成回想，存这个 sqlite
    moment_path: str = 'memory/moments.db'
    emoji_path: str = 'memory/emojis.json'
    # 「允许回复」名单：per-contact 开关 + 模式，面板上改，落盘
    scope_path: str = 'memory/reply_scope.json'
    # 分会话人设覆盖：某个群用另一套人设，落盘
    persona_path: str = 'memory/personas.json'
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
    # 回复链路：'two_stage' = 决策者 + 表达者；'agent' = 单模型，回话本身就是工具调用
    # （reply）。**默认 agent**：离线 A/B 里开口率持平，延迟 −40%、token −30%
    reply_mode: str = 'agent'
    # Planner 可以单独配一个 OpenAI 兼容服务；**留空就走主模型（推荐）**。
    # 不要指到本地小模型：实测 2B 那档「开口 0/9、推理原地打转」，
    # 连「说一个字」都是 finish=length + content 空（见 notes/未完成.md）
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
    # 管理后台（本地网页）：只绑 127.0.0.1，不做鉴权；设 0 就不起
    admin_port: int = 8099
    # 演习模式：全链路照跑（门控/决策/表达/状态都有），但不真发、自己的回复不进历史
    dry_run: bool = False
    # 回复时引用被打断的那条消息（微信的「回复某条」）。真机（filehelper）验过：
    # 自己发的和别人发的都能引用，客户端渲染正常。只在群聊且最近够热闹时才引
    quote_replies: bool = True
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
    assert base.daemon_port == 9099 and base.send_interval == 1.0 and base.deepseek_key == '', base
    os.environ.update(DAEMON_PORT='9098', SEND_INTERVAL='2.5', DEEPSEEK_KEY='k')
    overridden = Config.from_env()
    assert overridden.daemon_port == 9098, overridden
    assert overridden.send_interval == 2.5, overridden
    assert overridden.deepseek_key == 'k', overridden
    # 专注模式与错别字可以从环境变量调
    assert base.enable_focus and base.typo_rate == 0.08, base
    os.environ.update(ENABLE_FOCUS='0', TYPO_RATE='0')
    off = Config.from_env()
    assert not off.enable_focus and off.typo_rate == 0.0, off    # 退避四档也能从环境变量调（面板上要能改）
    assert base.backoff_start_after == 2 and base.backoff_max_seconds == 300.0, base
    os.environ.update(BACKOFF_START_AFTER='3', BACKOFF_MAX_SECONDS='60')
    tuned = Config.from_env()
    assert tuned.backoff_start_after == 3 and tuned.backoff_max_seconds == 60.0, tuned
    assert base.describe_max_tokens == 1024, base     # thinking 模型的推理也算进生成预算
    assert base.image_inline is True and base.image_shrink_target == 1024 * 1024, base
    assert base.reply_mode == 'agent', base           # 默认单段式；two_stage 留作对照
    assert base.quote_replies is True, base            # 真机验过，引用回复默认开
    # 回复行为也能从环境变量调（面板上要能改）
    assert base.reply_context_length == 60 and base.agent_max_replies == 1, base
    assert base.focus_score_bonus == 0 and base.quote_min_activity == 8, base
    assert base.reply_min_gap == 45.0 and base.reply_rate_max == 5, base
    os.environ.update(AGENT_MAX_REPLIES='2', QUOTE_MIN_ACTIVITY='12')
    behavior = Config.from_env()
    assert behavior.agent_max_replies == 2 and behavior.quote_min_activity == 12, behavior
    # 管理后台端口和演习开关也能从环境变量调
    assert base.admin_port == 8099 and base.dry_run is False, base
    os.environ.update(ADMIN_PORT='0', DRY_RUN='1')
    panel = Config.from_env()
    assert panel.admin_port == 0 and panel.dry_run is True, panel

    # .env 回写：只改动的键、保留注释和其余行、原子写
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / '.env'
        path.write_text('# 备用\nDEEPSEEK_KEY=old\n#DEEPSEEK_KEY=spare\nSEND_INTERVAL=1.0\n', encoding='utf-8')
        os.chmod(path, 0o644)
        update_dotenv(path, {'SEND_INTERVAL': '2.5', 'TYPO_RATE': '0'})
        written = path.read_text(encoding='utf-8')
        assert 'DEEPSEEK_KEY=old' in written            # 没动的键保持原样
        assert '#DEEPSEEK_KEY=spare' in written         # 注释没被抹掉
        assert 'SEND_INTERVAL=2.5' in written and 'SEND_INTERVAL=1.0' not in written
        assert 'TYPO_RATE=0' in written                 # 新键追加
        assert path.stat().st_mode & 0o777 == 0o644     # 权限保留
        assert not (Path(tmp) / '.env.tmp').exists()    # 临时文件已替换掉
        # 带空格 / 井号的值要加引号，否则 dotenv 会截
        update_dotenv(path, {'PERSONALITY': '温柔 阳光 #1'})
        assert 'PERSONALITY="温柔 阳光 #1"' in path.read_text(encoding='utf-8')
        print('ok')
