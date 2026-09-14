import base64

from loguru import logger

from tools import AssistantTurn, parse_tool_calls

DESCRIBE_PROMPT = '用不超过 15 个字描述这张图片或表情包的内容和情绪，不要标点符号'


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
        text = (response.choices[0].message.content or '').strip()
        return text or None

    async def summarize(self, prompt: str, text: str, max_tokens: int = 400) -> str | None:
        """ 压缩一段文本，供中期记忆使用。 """
        response = await self.client.chat.completions.create(
            model=self.model,
            temperature=0.3,
            max_tokens=max_tokens,
            messages=[{'role': 'system', 'content': prompt}, {'role': 'user', 'content': text}],
        )
        content = (response.choices[0].message.content or '').strip()
        logger.debug(f'Summarize interface. response: {content!r}')
        return content or None


class Deepseek(OpenAI):
    """ Deepseek；url 可指向任何 OpenAI 兼容的第三方服务 """

    def __init__(self, key: str, model: str = 'deepseek-flash', url: str = 'https://api.deepseek.com', **kwargs):
        super().__init__(url=url, key=key, model=model, **kwargs)

