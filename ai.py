import base64
from dataclasses import dataclass, field

from loguru import logger

from tools import AssistantTurn, parse_tool_calls

DESCRIBE_PROMPT = '用不超过 15 个字描述这张图片或表情包的内容和情绪，不要标点符号'


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


class AiProvider:
    """ 聊天机器人 """

    # 后端支不支持工具调用。不支持就退回单段的 JSON 决策，不影响其余功能。
    supports_tools: bool = False

    @staticmethod
    def silent(s: str) -> bool:
        """ 本轮不发言 """
        return '本轮不发言' in s

    async def chat(self, prompt: str, message: str | list) -> list[str] | None:
        """ 聊天。message 为字符串，或 OpenAI content block 数组（含图片）。 """
        raise NotImplementedError

    async def chat_tools(self, prompt: str, messages: list[dict],
                         tools: list[dict]) -> AssistantTurn | None:
        """ 带工具的一轮调用。返回 None 表示这个后端不支持，调用方自己决定退路。 """
        return None

    async def describe(self, image: bytes, max_tokens: int = 64,
                       prompt: str = DESCRIBE_PROMPT) -> str | None:
        """ 识图。prompt 可换：读历史用短的，给表情库补使用场景用 emojis.EMOJI_DESCRIBE_PROMPT。 """
        return None

    async def summarize(self, prompt: str, text: str, max_tokens: int = 400) -> str | None:
        """ 用给定 prompt 压缩一段文本，供中期记忆使用。默认不支持，返回 None。 """
        return None


class OpenAI(AiProvider):
    """ OpenAI 兼容接口 """

    supports_tools = True

    def __init__(self, url: str, key: str, model: str = 'deepseek-chat', temperature: float = 0.8,
                 top_p: float = 0.95, max_tokens: int | None = None, extra_body: dict | None = None,
                 response_format: dict | None = None):
        super().__init__()

        from openai import AsyncOpenAI

        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.extra_body = extra_body or {}
        self.response_format = response_format
        self.client = AsyncOpenAI(base_url=url, api_key=key)

    async def chat(self, prompt: str, message: str | list) -> list[str] | None:
        """ 聊天 """
        extra = {'max_tokens': self.max_tokens} if self.max_tokens else {}
        if self.response_format:
            extra['response_format'] = self.response_format
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

    async def describe(self, image: bytes, max_tokens: int = 64,
                       prompt: str = DESCRIBE_PROMPT) -> str | None:
        """ 识图描述：把图片/表情压成一句短语，写入历史记录。 """
        encoded = base64.b64encode(image).decode()
        response = await self.client.chat.completions.create(
            model=self.model,
            temperature=0.3,
            max_tokens=max_tokens,
            messages=[{'role': 'user', 'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{encoded}'}},
            ]}],
        )
        usage.add(response.usage, self.model)
        choice = response.choices[0]
        text = (choice.message.content or '').strip()
        if not text:
            # 与 chat() 一样回退到 reasoning（有些供应商把答案放那里）
            text = ((choice.message.model_extra or {}).get('reasoning_content') or '').strip()
        if not text:
            # 不说清楚的话，调用方只看到「描述失败」。实测最常见的原因是**推理把生成预算吃完了**：
            # thinking 模型要先想再答，finish_reason=length 时 content 就是空的，
            # 而 completion_tokens 会全部算成 reasoning_tokens（识图 prompt 实测要 ~754）
            logger.warning(f'识图没拿到内容（finish_reason={choice.finish_reason}，'
                           f'max_tokens={max_tokens}）：thinking 模型的推理也算在生成预算里，'
                           f'调大 DESCRIBE_MAX_TOKENS（实测 1024 够）；若一直如此再看模型是否支持图片')
        return text or None

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
    print('ai self-check ok')


if __name__ == '__main__':
    _self_check()
