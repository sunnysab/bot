import base64
from dataclasses import dataclass, field

from loguru import logger

from tools import AssistantTurn, parse_tool_calls

DESCRIBE_PROMPT = '用不超过 15 个字描述这张图片或表情包的内容和情绪，不要标点符号'
# 图片换一条口径：菜单/截图的字不能只压成一句 vibe。表情还是用上面那条短的——
# 表情要的就是情绪标签，长输出只会让 thinking 模型多烧预算
IMAGE_DESCRIBE_PROMPT = (
    '把图里的文字原样抄下来（同一条只抄一次）；再把画面里主要的东西具体列出来，'
    '食物要写到菜名或品类。不要分析，最多 12 行。')
# 提示词换版：老描述在 media_cache 里 version 对不上，查不到就会重新描述
IMAGE_PROMPT_VERSION = 'img-v3'
# 识图被截断时最多翻倍到这里（见 OpenAI.describe）
RETRY_CEILING = 8192


@dataclass
class Usage:
    """ 累计 token 用量。

    以前 response.usage 拿到就丢了——而「KV 缓存到底有没有命中」这个问题
    （notes/未完成.md 里挂着的那条）记下来就有答案。不同供应商字段名不一样，
    认多少算多少，认不到就是 0。
    """

    calls: int = 0
    prompt: int = 0
    completion: int = 0
    cached: int = 0
    cache_write: int = 0
    by_model: dict = field(default_factory=dict)

    def add(self, raw, model: str = '') -> None:
        if raw is None:
            return
        extra = getattr(raw, 'model_extra', None) or {}
        details = getattr(raw, 'prompt_tokens_details', None)
        # 两种口径都认：DeepSeek 原生是 prompt_cache_hit_tokens，
        # Anthropic 式代理是 prompt_tokens_details.cached_tokens
        # （实测本机配的中转返回的是后者 + model_extra.cache_creation_input_tokens）
        cached = (getattr(details, 'cached_tokens', 0) or 0) if details else 0
        cached = cached or extra.get('prompt_cache_hit_tokens', 0) or 0
        prompt = getattr(raw, 'prompt_tokens', 0) or 0
        completion = getattr(raw, 'completion_tokens', 0) or 0
        self.calls += 1
        self.prompt += prompt
        self.completion += completion
        self.cached += cached
        # 写进缓存多少：看了它才分得清「没开缓存」和「开了但从来没命中」
        self.cache_write += extra.get('cache_creation_input_tokens', 0) or 0
        if model:
            bucket = self.by_model.setdefault(model, {'calls': 0, 'prompt': 0, 'completion': 0})
            bucket['calls'] += 1
            bucket['prompt'] += prompt
            bucket['completion'] += completion

    def snapshot(self) -> dict:
        hit = round(self.cached / self.prompt, 3) if self.prompt else None
        return {'calls': self.calls, 'prompt': self.prompt, 'completion': self.completion,
                'cached': self.cached, 'cache_write': self.cache_write,
                'cache_hit_rate': hit, 'by_model': self.by_model}


# 全局累计：面板的统计卡要读，跨所有模型调用
usage = Usage()


def reason_of(error: Exception) -> str:
    """ 一次模型调用失败的原因，压成一句能贴到面板 / 日志上的话。

    只报错误码不够用。2026-09-23 那次面板上显示的是「agent 选择不发言」，翻日志才知道
    是「面板上换过的 key 没生效，旧 key 的周用量早超了」——「限流/用量超了」和
    「key 无效」得一眼分得开，才不用每次去翻异常原文。
    """
    if isinstance(error, TimeoutError):          # wait_for 超时、上游自己超时
        return '调用超时：模型没在限时内返回'
    detail = _error_message(error) or ' '.join(str(error).split())[:160] or error.__class__.__name__
    status = getattr(error, 'status_code', None)
    if status == 429:
        return f'被限流（用量或频率超了）：{detail}'
    if status in (401, 403):
        return f'key 无效或没权限：{detail}'
    if status and status >= 500:
        return f'上游服务出错（HTTP {status}）：{detail}'
    if status:
        return f'接口返回 HTTP {status}：{detail}'
    if error.__class__.__name__ in ('APIConnectionError', 'APITimeoutError'):
        return f'连不上模型服务（网络或地址不对）：{detail}'
    return detail


def _error_message(error: Exception) -> str:
    """ 供应商的真实说明在 body 里；openai 把壳（`Error code: 429 - {...}`）拼进了异常消息。 """
    body = getattr(error, 'body', None)
    inner = body.get('error', body) if isinstance(body, dict) else None
    if isinstance(inner, dict) and inner.get('message'):
        return ' '.join(str(inner['message']).split())
    return ''


class AiProvider:
    """ 聊天机器人 """

    @staticmethod
    def silent(s: str) -> bool:
        """ 本轮不发言 """
        return '本轮不发言' in s

    async def chat(self, prompt: str, message: str | list) -> list[str] | None:
        """ 聊天。message 为字符串，或 OpenAI content block 数组（含图片）。 """
        raise NotImplementedError

    async def chat_tools(self, prompt: str, messages: list[dict],
                         tools: list[dict]) -> AssistantTurn | None:
        """ 带工具的一轮调用。 """
        raise NotImplementedError

    async def describe(self, image: bytes, max_tokens: int = 1024,
                       prompt: str = DESCRIBE_PROMPT, model: str | None = None) -> str | None:
        """ 识图。prompt 可换：读历史用短的，给表情库补使用场景用 emojis.EMOJI_DESCRIBE_PROMPT。

        model 可以单独指定识图模型（`DESCRIBE_MODEL`）：主模型是 thinking 的时候，
        识图换一个不思考的视觉模型能省掉整段推理开销。留空就用主模型。
        """
        return None

    async def summarize(self, prompt: str, text: str, max_tokens: int = 400) -> str | None:
        """ 用给定 prompt 压缩一段文本，供中期记忆使用。默认不支持，返回 None。 """
        return None


class OpenAI(AiProvider):
    """ OpenAI 兼容接口 """

    def __init__(self, url: str, key: str, model: str = 'deepseek-chat', temperature: float = 0.8,
                 top_p: float = 0.95, max_tokens: int | None = None, extra_body: dict | None = None):
        super().__init__()

        from openai import AsyncOpenAI

        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.extra_body = extra_body or {}
        self.client = AsyncOpenAI(base_url=url, api_key=key)

    async def chat(self, prompt: str, message: str | list) -> list[str] | None:
        """ 聊天 """
        extra = {'max_tokens': self.max_tokens} if self.max_tokens else {}
        response = await self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            top_p=self.top_p,
            messages=[
                {'role': 'system', 'content': prompt},
                {'role': 'user', 'content': message}],
            # 供应商特有的参数（如 vLLM 的 chat_template_kwargs）走 extra_body，不能当普通 kwarg 展开
            extra_body=self.extra_body or None,
            **extra,
        )

        completion_message = response.choices[0].message
        usage.add(response.usage, self.model)
        reasoning = (completion_message.model_extra or {}).get('reasoning_content') or ''
        response_text: str = completion_message.content or ''
        if not response_text.strip() and reasoning.strip():
            # thinking 模型偶尔把整段预算烧在推理上、content 为空；
            # 答案（JSON / 要点）通常已经在推理里，回退去拿它，比丢掉这次调用强
            logger.debug('content 为空，回退到 reasoning_content')
            response_text = reasoning
        logger.debug(
            f'OpenAI compatible interface. prompt: {repr(prompt)}, response: {repr(response_text)}')
        if reasoning:
            logger.debug(f'reasoning content: {repr(reasoning)}')

        # 如果回复内容为“本轮不发言”，则返回 None
        if self.silent(response_text):
            return None
        # Deepseek-R1 的思维链过程在 ChatCompletion 的 extra 字段中
        # 无需单独处理，直接返回即可
        return [x.strip() for x in response_text.split('\n') if x.strip()]

    async def chat_tools(self, prompt: str, messages: list[dict],
                         tools: list[dict]) -> AssistantTurn | None:
        """ 带工具的一轮调用。

        messages 是 system 之后的完整列表：开场只有一条 user，之后每轮把 assistant
        的 tool_calls 和 tool 结果接上去（function calling 的协议，少了接不上）。
        和 chat() 分开是因为 chat() 会把回复按行切好、把「本轮不发言」当空，
        那是表达者要的形状；这里要原始 message。
        """
        response = await self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            top_p=self.top_p,
            messages=[{'role': 'system', 'content': prompt}, *messages],
            tools=tools or None,
            extra_body=self.extra_body or None,
        )
        completion_message = response.choices[0].message
        usage.add(response.usage, self.model)
        reasoning = (completion_message.model_extra or {}).get('reasoning_content') or ''
        turn = AssistantTurn(
            content=(completion_message.content or '').strip(),
            reasoning=reasoning.strip(),
            tool_calls=parse_tool_calls(completion_message.tool_calls),
        )
        logger.debug(f'工具轮 -> content={turn.content!r} '
                     f'calls={[(call.name, call.arguments) for call in turn.tool_calls]}')
        return turn

    async def describe(self, image: bytes, max_tokens: int = 1024,
                       prompt: str = DESCRIBE_PROMPT, model: str | None = None) -> str | None:
        """ 识图描述：把图片/表情压成一句短语，写入历史记录。

        **只在什么都没吐出来时才重试**：这个模型是 thinking 的，推理也算在 max_tokens
        里，碰上截断就翻倍再来一次。但第一遍已经有正文（哪怕被截断）时不能再试——
        翻倍只换一次更慢的调用，而整条是被 describer 的 timeout 罩着的，多花的那些秒
        正好会把已经拿到的那半段一起超时丢掉（线上就是这样：截断 → 重试 → 超时 → 空）。
        """
        text, truncated = await self._describe_once(image, prompt, max_tokens, model)
        if not text and truncated and max_tokens < RETRY_CEILING:
            logger.debug(f'识图被截断且没拿到内容（max_tokens={max_tokens}），翻倍重试一次')
            text, truncated = await self._describe_once(image, prompt, max_tokens * 2, model)
        if not text:
            logger.warning(f'识图没拿到内容（{"推理吃完了预算" if truncated else "空回复"}，'
                           f'max_tokens={max_tokens}）：thinking 模型的推理也算在生成预算里，'
                           f'调大 DESCRIBE_MAX_TOKENS；prompt 越啰嗦它想得越久')
        return text or None

    async def _describe_once(self, image: bytes, prompt: str,
                             max_tokens: int, model: str | None = None) -> tuple[str, bool]:
        """ 返回 (文本, 是不是被预算截断的)。 """
        model = model or self.model
        encoded = base64.b64encode(image).decode()
        response = await self.client.chat.completions.create(
            model=model,
            temperature=0.3,
            max_tokens=max_tokens,
            messages=[{'role': 'user', 'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{encoded}'}},
            ]}],
        )
        usage.add(response.usage, model)
        choice = response.choices[0]
        text = (choice.message.content or '').strip()
        if not text:
            # 与 chat() 一样回退到 reasoning（有些供应商把答案放那里）
            text = ((choice.message.model_extra or {}).get('reasoning_content') or '').strip()
        return text, choice.finish_reason == 'length'

    async def summarize(self, prompt: str, text: str, max_tokens: int = 400) -> str | None:
        """ 压缩一段文本，供中期记忆使用。 """
        response = await self.client.chat.completions.create(
            model=self.model,
            temperature=0.3,
            max_tokens=max_tokens,
            messages=[{'role': 'system', 'content': prompt}, {'role': 'user', 'content': text}],
        )
        usage.add(response.usage, self.model)
        content = (response.choices[0].message.content or '').strip()
        logger.debug(f'Summarize interface. response: {content!r}')
        return content or None


class Deepseek(OpenAI):
    """ Deepseek；url 可指向任何 OpenAI 兼容的第三方服务 """

    def __init__(self, key: str, model: str = 'deepseek-flash', url: str = 'https://api.deepseek.com', **kwargs):
        super().__init__(url=url, key=key, model=model, **kwargs)



def _self_check() -> None:
    """ token 记账：两种供应商口径都要认（字段名错了会一直静默显示 0）。 """
    import types

    fresh = Usage()
    assert fresh.snapshot() == {'calls': 0, 'prompt': 0, 'completion': 0, 'cached': 0,
                                'cache_write': 0, 'cache_hit_rate': None, 'by_model': {}}
    fresh.add(None)                                   # 拿不到 usage 也不能炸
    assert fresh.calls == 0

    # DeepSeek 原生：命中数在 model_extra
    deepseek = types.SimpleNamespace(prompt_tokens=100, completion_tokens=40,
                                     model_extra={'prompt_cache_hit_tokens': 80})
    fresh.add(deepseek, 'm1')
    # Anthropic 式代理：命中数在 prompt_tokens_details，创建数在 model_extra
    anthropic = types.SimpleNamespace(
        prompt_tokens=100, completion_tokens=10,
        prompt_tokens_details=types.SimpleNamespace(cached_tokens=60),
        model_extra={'cache_creation_input_tokens': 25})
    fresh.add(anthropic, 'm2')

    snap = fresh.snapshot()
    assert snap['calls'] == 2 and snap['prompt'] == 200 and snap['completion'] == 50
    assert snap['cached'] == 140, snap          # 80 + 60
    assert snap['cache_write'] == 25
    assert snap['cache_hit_rate'] == 0.7        # 140/200
    assert snap['by_model']['m2'] == {'calls': 1, 'prompt': 100, 'completion': 10}

    # 只有 prompt 没有 completion 的供应商也不能算错
    odd = Usage()
    odd.add(types.SimpleNamespace(prompt_tokens=7, model_extra={}))
    assert odd.snapshot()['completion'] == 0 and odd.snapshot()['cache_hit_rate'] == 0.0
    # 识图被截断要翻倍重试一次（推理长度看 prompt 心情，8 ~ >2048）
    async def retry_check():
        provider = object.__new__(OpenAI)          # 不跑 __init__，不建 client
        calls: list[int] = []

        async def fake(image, prompt, budget, model=None):
            calls.append(budget)
            return ('', True) if budget < 2048 else ('好', False)

        provider._describe_once = fake
        assert await provider.describe(b'x', max_tokens=1024) == '好'
        assert calls == [1024, 2048], calls
        # 已经到顶就不再翻倍，返回 None（调用方会记警告）
        calls.clear()

        async def always_truncated(image, prompt, budget, model=None):
            calls.append(budget)
            return ('', True)

        provider._describe_once = always_truncated
        assert await provider.describe(b'x', max_tokens=RETRY_CEILING) is None
        assert calls == [RETRY_CEILING], calls       # 到顶了就不再翻倍
        # 第一遍已经有正文（哪怕被截断）就不再翻倍：翻倍只会更慢，
        # 还可能被 describer 的 timeout 把已经拿到的这段一起丢掉
        calls.clear()

        async def truncated_with_text(image, prompt, budget, model=None):
            calls.append(budget)
            return ('文字：菜单', True)

        provider._describe_once = truncated_with_text
        assert await provider.describe(b'x', max_tokens=4096) == '文字：菜单'
        assert calls == [4096], calls
        # 识图模型可以单独指定（DESCRIBE_MODEL），要真的透传下去
        seen: list[str | None] = []

        async def capture(image, prompt, budget, model=None):
            seen.append(model)
            return ('好', False)

        provider._describe_once = capture
        assert await provider.describe(b'x', max_tokens=512, model='vision-x') == '好'
        assert seen == ['vision-x'], seen
        # 没截断就不重试
        calls.clear()
        provider._describe_once = lambda *a: _ok()
        assert await provider.describe(b'x', max_tokens=512) == '行'

    async def _ok():
        return ('行', False)

    # 失败分类：限流 / key / 超时 / 上游 / 认不出（认不出的也必须能看懂）
    class Boom(Exception):
        def __init__(self, status=None, body=None):
            self.status_code, self.body = status, body
            super().__init__(f'Error code: {status} - {body}')

    limited = Boom(429, {'error': {'message': "You've reached   your weekly\nlimit"}})
    assert reason_of(limited) == '被限流（用量或频率超了）：You\'ve reached your weekly limit', reason_of(limited)
    assert reason_of(Boom(401, {'error': {'message': 'invalid api key'}})).startswith('key 无效'), 'key'
    assert '超时' in reason_of(TimeoutError())
    assert '上游' in reason_of(Boom(503, None))          # 没有 body 也要能说清楚
    assert reason_of(ValueError('bad json')) == 'bad json'
    assert reason_of(Exception()) == 'Exception'        # 什么都不说的异常也得有名字

    import asyncio
    asyncio.run(retry_check())
    print('ai self-check ok')


if __name__ == '__main__':
    _self_check()
