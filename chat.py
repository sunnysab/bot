from abc import abstractmethod

from config import CONFIG


def get_system_prompt(self_name: str):
    """ 获取系统提示 """

    return '''你现在位于一个群聊中聊天。请你发表回复，尽量简洁，符合语境和聊天人类习惯，不要添加标点符号。''' \
           '''如果相关主题回复过，或你决定不说话，请说：本轮不发言。''' \
           f'''聊天记录包含了你和群友最近的发言。你叫 {{{self_name}}}：\n'''


class ChatAI:
    """ 聊天机器人 """

    def __init__(self):
        self.name = CONFIG['self-name']
        self.prompt = get_system_prompt(self.name)

    @staticmethod
    def silent(s: str) -> bool:
        """ 本轮不发言 """
        return '本轮不发言' in s

    @abstractmethod
    def chat(self, message: str) -> str | None:
        """ 聊天 """
        pass


class ChatGLM(ChatAI):
    """ 智谱清言 """

    def __init__(self, key: str, model: str = 'glm-4-plus'):
        super().__init__()

        from zhipuai import ZhipuAI

        self.model = model
        self.client = ZhipuAI(api_key=key)

    def chat(self, message: str) -> str | None:
        """ 聊天 """
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {'role': 'system', 'content': get_system_prompt(self.name)},
                {'role': 'user', 'content': message}],
        )

        response_text: str = response.choices[0].message

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

    def chat(self, message: str) -> str | None:
        """ 聊天 """
        response = self.client.chat(
            model=self.model,
            messages=[
                {'role': 'system', 'content': get_system_prompt(self.name)},
                {'role': 'user', 'content': message}],
        )

        response_text: str = response.choices[0].message
        # 额外处理一下 Deepseek-R1 思维链的思维过程.
        RIGHT_THINK_BRACE = '</think>'
        right_brace = response_text.find(RIGHT_THINK_BRACE) + len(RIGHT_THINK_BRACE) + 1
        if right_brace > 0:
            response_text = response_text[right_brace:]

        # 如果回复内容为“本轮不发言”，则返回 None
        if self.silent(response_text):
            return None
        return response_text.strip()
