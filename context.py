

class ChatWindow:
    """ 维护单个聊天会话（群或联系人）的状态 """

    def __init__(self, history=None, max_history: int = 100):
        self.max_history = max_history

        history = history or []
        self._history = history[:max_history]

    @staticmethod
    def message_template(user: str, message: str):
        message = message.replace('\n', ' ')
        return f'{user}: {message}'

    def append(self, user: str, message: str):
        """ 添加消息 """
        if len(self._history) >= self.max_history:
            self._history.pop(0)
        self._history.append(self.message_template(user, message))

    def clear(self):
        """ 清空聊天记录 """
        self._history.clear()

    def __str__(self):
        return '\n'.join(self._history)


class ContextManager:
    """ 上下文管理器 """

    def __init__(self):
        self._contexts = {}

    def get_context(self, key: str) -> ChatWindow:
        """ 获取上下文 """
        if key not in self._contexts:
            self._contexts[key] = ChatWindow()
        return self._contexts[key]

    def clear_context(self, key: str):
        """ 清空上下文 """
        if key in self._contexts:
            self._contexts[key].clear()

    def clear_all(self):
        """ 清空所有上下文 """
        self._contexts.clear()

    def push_message(self, contact: str, message: str, user: str=''):
        """ 存储消息 """
        if not user:
            user = contact

        if contact in self._contexts:
            self._contexts[contact].append(user, message)
        else:
            self._contexts[contact] = ChatWindow()
            self._contexts[contact].append(user, message)

    def push_window(self, chat_window: ChatWindow):
        """ 批量存储消息 """
        self._contexts[chat_window] = chat_window