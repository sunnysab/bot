import logging
from abc import abstractmethod


class ChatAI:
    """ 聊天机器人 """

    @staticmethod
    def silent(s: str) -> bool:
        """ 本轮不发言 """
        return '本轮不发言' in s

    @abstractmethod
    def chat(self, prompt: str, message: str) -> str | None:
        """ 聊天 """
        pass


class ChatGLM(ChatAI):
    """ 智谱清言 """

    def __init__(self, key: str, model: str = 'glm-4-plus'):
        super().__init__()

        from zhipuai import ZhipuAI

        self.model = model
        self.client = ZhipuAI(api_key=key)

    def chat(self, prompt: str, message: str) -> str | None:
        """ 聊天 """
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {'role': 'system', 'content': prompt},
                {'role': 'user', 'content': message}],
        )

        completion_message = response.choices[0].message
        response_text: str = completion_message.content
        logging.debug(f'ChatGLM response: {response_text}')

        # 如果回复内容为“本轮不发言”，则返回 None
        if self.silent(response_text):
            return None
        return response_text.strip()


class Ollama(ChatAI):
    """ Ollama """

    def __init__(self, model: str, url: str):
        super().__init__()

        from ollama import Client
        self.model = model
        self.client = Client(url)

    def chat(self, prompt: str, message: str) -> str | None:
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
        return response_text.strip()
