import base64
import re
from abc import abstractmethod
from typing import Optional

from loguru import logger


class AiProvider:
    """ 聊天机器人 """

    @staticmethod
    def silent(s: str) -> bool:
        """ 本轮不发言 """
        return '本轮不发言' in s

    @abstractmethod
    async def chat(self, prompt: str, message: str) -> Optional[list[str]]:
        """ 聊天 """
        pass

    @abstractmethod
    async def describe_image(self, prompt: str, image: bytes | str) -> Optional[str]:
        """ 图像描述 """
        pass


class OpenAI(AiProvider):
    """ OpenAI 兼容接口 """

    def __init__(self, url: str, key: str, model: str = 'deepseek-chat', temperature: float = 0.8, top_p: float = 0.95):
        super().__init__()

        from openai import AsyncOpenAI

        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.client = AsyncOpenAI(base_url=url, api_key=key)

    async def chat(self, prompt: str, message: str) -> Optional[list[str]]:
        """ 聊天 """
        response = await self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            top_p=self.top_p,
            messages=[
                {'role': 'system', 'content': prompt},
                {'role': 'user', 'content': message}],
        )

        completion_message = response.choices[0].message
        response_text: str = completion_message.content
        logger.debug(
            f'OpenAI compatible interface. prompt: {repr(prompt)}, message: {repr(message)} response: {repr(response_text)}')
        if 'reasoning_content' in completion_message.model_extra:
            logger.debug(f'reasoning content: {repr(completion_message.model_extra["reasoning_content"])}')

        # 如果回复内容为“本轮不发言”，则返回 None
        if self.silent(response_text):
            return None
        # Deepseek-R1 的思维链过程在 ChatCompletion 的 extra 字段中
        # 无需单独处理，直接返回即可
        return [x.strip() for x in response_text.split('\n') if x.strip()]


class Deepseek(OpenAI):
    """ Deepseek """

    def __init__(self, key: str, model: str = 'deepseek-chat', **kwargs):
        super().__init__(url='https://api.deepseek.com', key=key, model=model, **kwargs)


class ChatGLM(OpenAI):
    """ 智谱清言 """

    def __init__(self, key: str, model: str = 'glm-4-flash', **kwargs):
        super().__init__(url='https://open.bigmodel.cn/api/paas/v4', key=key, model=model, **kwargs)

    @staticmethod
    def get_image_prompt() -> str:
        """ 获取图片描述的提示 """
        return '尽可能少的字数描述图片主体是什么, 里面物品有什么. 给人的感觉如何. 不要描述物品放置的目的.'

    async def describe_image(self, prompt: str, image: bytes | str) -> Optional[str]:
        """ 图像描述 """
        encoded_image = base64.b64encode(image).decode('utf-8')

        response = await self.client.chat.completions.create(
            model='glm-4v-flash', # TODO: 支持修改.
            temperature=0.95,
            top_p=0.70,
            messages=[{'role': 'user', 'content': [
                {'type': 'image_url', 'image_url': {'url': encoded_image}},
                {'type': 'text', 'text': prompt},
            ]}],
        )

        completion_message = response.choices[0].message
        response_text: str = completion_message.content
        logger.debug(f'ChatGLM image description. response: {repr(response_text)}')

        response_text = re.sub(r'\s\S\n', '', response_text)
        return response_text


class Ollama(AiProvider):
    """ Ollama """

    def __init__(self, model: str, url: str, **kwargs):
        super().__init__(**kwargs)

        from ollama import AsyncClient
        self.model = model
        self.client = AsyncClient(url)

    async def chat(self, prompt: str, message: str) -> Optional[list[str]]:
        """ 聊天 """
        response = await self.client.chat(
            model=self.model,
            messages=[
                {'role': 'system', 'content': prompt},
                {'role': 'user', 'content': message}],
        )

        response_text: str = response['message']['content']
        logger.debug(
            f'Ollama interface. prompt: {repr(prompt)}, message: {repr(message)} response: {repr(response_text)}')

        # 额外处理一下 Deepseek-R1 思维链的思维过程.
        RIGHT_THINK_BRACE = '</think>'
        right_brace = response_text.find(RIGHT_THINK_BRACE) + len(RIGHT_THINK_BRACE) + 1
        if right_brace > 0:
            response_text = response_text[right_brace:]

        # 如果回复内容为“本轮不发言”，则返回 None
        if self.silent(response_text):
            return None
        return [x.strip() for x in response_text.split() if x.strip()]


async def main():
    chatglm = ChatGLM(key='your-key')
    prompt = chatglm.get_image_prompt()

    image = open('image/dog.jpg', 'rb').read()
    description = await chatglm.describe_image(prompt, image)
    print('dog: ', description)

    image = open('image/lunch.jpg', 'rb').read()
    description = await chatglm.describe_image(prompt, image)
    print('lunch: ', description)

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
