""" 运行配置：**`config.toml` 是唯一的事实源**（启动时读它，管理后台改它）。

以前是「环境变量 + `.env`」两份，谁说了算要看 dotenv 的 `override` 语义。2026-09-23 栽在
这上面：面板上刚存的新 key 被 `execv` 继承下来的旧值盖回去，429 一遍又一遍，而面板把
「接口压根没调通」显示成「agent 选择不发言」。现在只有一份文件——读它、改它，没有优先级。

字段没写进文件就用这里的默认值（`Config.load`）。不认识的行/键直接报错：手写配置最怕
的是「改了没生效」，不是「启动失败」。

**不要在这里加 `from __future__ import annotations`**：dataclass 记字段类型，加了之后
`field.type` 变成字符串 `'int'`，面板和自检里那些 `field.type is bool` 全会悄悄走错分支。
"""

import json
import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from persona import Persona

CONFIG_PATH = Path(__file__).parent / 'config.toml'


def read(path: str | Path = CONFIG_PATH) -> dict:
    """ 读配置。文件不存在 = 空（全用默认值），新克隆的仓库照样能起来。 """
    path = Path(path)
    if not path.exists():
        return {}
    with path.open('rb') as file:
        return tomllib.load(file)


def save(path: str | Path, updates: dict) -> None:
    """ 把给定的键写回文件，**其余行（含注释）原样保留**，原子替换。

    不整个重写：手写的注释就是配置文件的文档，重写一次全没了。也不直接写原文件：
    写到一半中断会留下半个配置，下次启动就少几个字段。
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
                # 被改的这一行连同它的行尾注释一起换掉；没动的行一个字都不碰
                out.append(f'{key} = {_literal(updates[key])}')
                done.add(key)
                continue
        out.append(line)
    out.extend(f'{key} = {_literal(value)}' for key, value in updates.items() if key not in done)

    tmp = path.parent / f'{path.name}.tmp'
    tmp.write_text('\n'.join(out) + '\n', encoding='utf-8')
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _literal(value) -> str:
    """ 值 → toml 字面量。json 的字符串转义正好是合法的 toml 基本字符串（含 `\\n`）。 """
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return repr(value)


@dataclass(frozen=True)
class Config:
    """ 运行配置。**全部来自 `config.toml`**，其余用下面的默认值。 """

    daemon_host: str = '127.0.0.1'
    daemon_port: int = 9099
    # 走 Unix domain socket 时填写（如 /tmp/weixin-daemon.sock），否则留空走 TCP
    daemon_unix: str = ''
    deepseek_key: str = ''
    # 联网搜索（Tavily）的 key。留空就不挂 web_search 工具
    tavily_key: str = ''
    deepseek_model: str = 'deepseek-flash'
    # 主模型的服务地址，可指向第三方 OpenAI 兼容服务（更便宜的代理/中转）
    deepseek_url: str = 'https://api.deepseek.com'
    # 全局最小发送间隔（秒），规避微信风控
    send_interval: float = 1.0
    # 媒体（图片 / 缺 desc 的表情）识图描述
    describe_media: bool = True
    # 识图单独用哪个模型。留空 = 主模型。主模型是 thinking 的时候（如 deepseek-v4.1-flash，
    # 推理和回答共用 max_tokens、开关还关不掉），换一个不思考的视觉模型能省掉整段推理开销
    describe_model: str = ''
    media_cache_path: str = 'media_cache.db'
    describe_concurrency: int = 2
    # 单张图的下载+识图超时。中转实测一张普通照片要 ~45s（250 个输出 token 就花了
    # 40 多秒，慢在中转/上游排队而不是 token 速率），30s 会把结果整条丢掉；
    # 120s 给排队留余量（真超时会进负缓存，不会每轮都从头等）
    describe_timeout: float = 120.0
    # 识图的生成上限（不是上下文）。**thinking 模型会先推理再回答，推理也算在这个上限里**——
    # 实测（deepseek-v4.1-flash，识图 prompt + 9094 字节真表情图）：
    #   64 → finish=length，reasoning=64，content 空
    #   256/512 → 同理，预算多大就烧多大，一个字不吐
    #   1024 → finish=stop，reasoning=754，拿到「兴奋|对方说有瓜…」
    #   2048 → finish=stop，reasoning=1137（它想得更多，但答案一样）
    # 给不思考的模型给大了也没代价（上限不是目标值），所以直接定 1024
    describe_max_tokens: int = 1024
    # 图片描述的生成上限。新 prompt 把输出限在 12 行（约 150~450 token），1024 够用；
    # 也不能给大：Kimi-K2.5 在 4096 下会退化成「六个核桃×N」死循环（实测）
    describe_image_max_tokens: int = 1024
    # 决策前最多等识图多久（秒）。图片消息一到，识图是后台跑的，模型常常先看到
    # [图片] 占位符，就照着自己的想象评论这张图。>0 时这一轮先等**本轮要用的图**
    # 识别完（实测单张 3~5s + 下载），等到点还没好才照旧用占位符；0 = 不等
    describe_wait: float = 15.0
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
    agent_max_replies: int = 2           # agent 模式一轮最多发几条消息
    focus_score_bonus: int = 0           # 专注期加分。默认 0：热群本来就有压力分，
                                         # 再加就成了「热群必回」（见 necessity.PRESSURE_MAX）
    # 发言频率闸：阈值过了也可能说得太密，这是最后一道闸（只拦主动参与）
    reply_min_gap: float = 45.0          # 同一会话两条之间至少隔多久（秒）
    reply_rate_window: float = 300.0     # 滑动窗口（秒）
    reply_rate_max: int = 5              # 窗口内最多主动说几条
    # 专注模式（HFC）：群里热起来就盯一会儿，期间不看门控
    enable_focus: bool = True
    focus_enter_activity: int = 15      # 活动窗口内别人的消息数够多就进
    focus_idle_seconds: float = 300.0   # 这么久没新消息自动退出
    focus_max_seconds: float = 1800.0   # 单次专注最长多久
    # 连续沉默退避：连着几轮都不开口就歇一会儿（见 necessity.IdleBackoff）
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
    def load(cls, path: str | Path = CONFIG_PATH) -> Config:
        """ 文件里写了哪个键就用它，没写的用默认值。不认识的名字当场报错。 """
        values = {}
        for name, raw in read(path).items():
            field = _FIELDS.get(name)
            if field is None:
                raise ValueError(f'配置项 {name!r} 不存在；可用的是：{"、".join(sorted(_FIELDS))}')
            values[name] = coerce(field, raw)
        return cls(**values)


# 字段名 → 定义。面板和配置文件都按这个名字索引
_FIELDS = {field.name: field for field in fields(Config)}


def coerce(field, raw):
    """ 按字段类型转。手写的 toml 会把端口写成 `"9099"`、开关写成 `"0"`，都认。

    面板表单送来的也是字符串，所以两个入口共用这一个转换。
    """
    if field.type is bool:
        return raw if isinstance(raw, bool) else str(raw).strip().lower() not in ('0', 'false', 'no', '')
    if field.type is str:
        return raw if isinstance(raw, str) else str(raw)
    try:
        return field.type(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f'{field.name} 要 {field.type.__name__}，收到 {raw!r}') from e


if __name__ == '__main__':
    # 自检：默认值 / 读文件 / 转换 / 回写保留注释 / 原子替换
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'config.toml'
        assert Config.load(path) == Config()        # 文件不在 = 全默认值

        path.write_text('# 注释\nnonsense = 1\n', encoding='utf-8')
        try:
            Config.load(path)
            raise AssertionError('不认识的配置项要报错')
        except ValueError as e:
            assert 'nonsense' in str(e), e

        path.write_text('# 注释\nadmin_port = "9098"   # 手写成字符串也认\ndry_run = "0"\n'
                        'daemon_port = 9099\ntypo_rate = 0.25\n', encoding='utf-8')
        os.chmod(path, 0o644)
        loaded = Config.load(path)
        assert loaded.admin_port == 9098 and loaded.dry_run is False, loaded
        assert loaded.daemon_port == 9099 and loaded.typo_rate == 0.25, loaded
        assert loaded.send_interval == 1.0, loaded           # 没写的字段回默认

        save(path, {'daemon_port': 9098, 'send_interval': 2.5, 'dry_run': True,
                    'deepseek_key': 'a "b" #c'})
        written = path.read_text(encoding='utf-8')
        assert '# 注释' in written, written                    # 注释原样留着
        assert 'daemon_port = 9098' in written and 'daemon_port = 9099' not in written, written
        assert 'dry_run = true' in written, written           # 布尔存成 toml 字面量
        assert 'send_interval = 2.5' in written, written       # 新键追加到末尾
        assert 'deepseek_key = "a \\"b\\" #c"' in written, written
        assert path.stat().st_mode & 0o777 == 0o644           # 权限保留
        assert not (Path(tmp) / 'config.toml.tmp').exists()    # 临时文件已经替换掉

        # 读-改-写是面板的常态：第二次改不能把上一次的值弄丢
        save(path, {'typo_rate': 0.0})
        again = Config.load(path)
        assert again.send_interval == 2.5 and again.dry_run is True, again
        assert again.typo_rate == 0.0 and again.deepseek_key == 'a "b" #c', again
        print('ok')
