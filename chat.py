import logging
from abc import abstractmethod


class ChatAI:
    """ 聊天机器人 """

    @staticmethod
    def silent(s: str) -> bool:
        """ 本轮不发言 """
        return '本轮不发言' in s

    @abstractmethod
    def chat(self, prompt: str, message: str) -> list[str] | None:
        """ 聊天 """
        pass


class OpenAI(ChatAI):
    """ OpenAI 兼容接口 """

    def __init__(self, url: str, key: str, model: str = 'deepseek-chat', temperature: float = 0.8, top_p: float = 0.95):
        super().__init__()

        from openai import OpenAI

        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.client = OpenAI(base_url=url, api_key=key)

    def chat(self, prompt: str, message: str) -> list[str] | None:
        """ 聊天 """
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            top_p=self.top_p,
            messages=[
                {'role': 'system', 'content': prompt},
                {'role': 'user', 'content': message}],
        )

        completion_message = response.choices[0].message
        response_text: str = completion_message.content
        logging.debug(f'OpenAI compatible response: {response_text}')

        # 如果回复内容为“本轮不发言”，则返回 None
        if self.silent(response_text):
            return None
        return [x.strip() for x in response_text.split() if x.strip()]


class Deepseek(OpenAI):
    """ Deepseek """

    def __init__(self, key: str, model: str = 'deepseek-chat'):
        super().__init__(url='https://api.deepseek.com', key=key, model=model)

    def chat(self, prompt: str, message: str) -> list[str] | None:
        """ 聊天 """
        texts = super().chat(prompt, message)

        # 额外处理一下 Deepseek-R1 思维链的思维过程.
        RIGHT_THINK_BRACE = '</think>'
        try:
            right_brace = texts.index(RIGHT_THINK_BRACE)
            if right_brace > 0:
                 return texts[right_brace + 1:]
        except ValueError:
            pass
        return texts


class ChatGLM(OpenAI):
    """ 智谱清言 """

    def __init__(self, key: str, model: str = 'glm-4-flash'):
        super().__init__(url='https://open.bigmodel.cn/api/paas/v4', key=key, model=model)


class Ollama(ChatAI):
    """ Ollama """

    def __init__(self, model: str, url: str):
        super().__init__()

        from ollama import Client
        self.model = model
        self.client = Client(url)

    def chat(self, prompt: str, message: str) -> list[str] | None:
        """ 聊天 """
        response = self.client.chat(
            model=self.model,
            messages=[
                {'role': 'system', 'content': prompt},
                {'role': 'user', 'content': message}],
        )

        response_text: str = response['message']['content']
        logging.debug(f'Ollama response: {response_text}')

        # 额外处理一下 Deepseek-R1 思维链的思维过程.
        RIGHT_THINK_BRACE = '</think>'
        right_brace = response_text.find(RIGHT_THINK_BRACE) + len(RIGHT_THINK_BRACE) + 1
        if right_brace > 0:
            response_text = response_text[right_brace:]

        # 如果回复内容为“本轮不发言”，则返回 None
        if self.silent(response_text):
            return None
        return [x.strip() for x in response_text.split() if x.strip()]
