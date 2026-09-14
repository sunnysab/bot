""" 给决策者的工具：声明、调用、结果。

形状照着 MaiBot 的 core/tooling.py 收，但只留现在用得上的。它有 provider 抽象、
deferred 可见性、tool_search、hook 那几层，是因为要收敛插件 Tool / 旧 Action /
MCP Tool 四种来源；我们只有几个内置动作，工具多起来再往上加。

约定：**规则的细节写进 ToolSpec.description，系统提示里只列一行**。模型看得到
每个工具自己的说明书，比在 planner.txt 里堆一大段更不容易漏。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

EMPTY_PARAMETERS: dict[str, Any] = {'type': 'object', 'properties': {}}


@dataclass(frozen=True)
class ToolSpec:
    """ 一个工具的声明。 """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: dict(EMPTY_PARAMETERS))

    def to_openai(self) -> dict[str, Any]:
        """ 转成 OpenAI function calling 形状。 """
        return {'type': 'function', 'function': {
            'name': self.name,
            'description': self.description,
            'parameters': self.parameters,
        }}


@dataclass(frozen=True)
class ToolCall:
    """ 模型发起的一次调用。 """

    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """ 工具执行结果。content 会写回对话历史给模型看。 """

    content: str
    stop: bool = False


@dataclass(frozen=True)
class AssistantTurn:
    """ 模型的一轮输出。 """

    content: str = ''
    reasoning: str = ''
    tool_calls: tuple[ToolCall, ...] = ()


ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


def arg_str(arguments: dict[str, Any], key: str, default: str = '') -> str:
    """ 取一个字符串参数。模型有时把数字也塞进字符串字段，这里统一成 str。 """
    value = arguments.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def arg_int(arguments: dict[str, Any], key: str, default: int = 0) -> int:
    """ 取一个整数参数。模型经常写成 "3" 这种字符串，得接住。 """
    value = arguments.get(key, default)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class Toolbox:
    """ 一组工具 + 执行器，并记下这一轮调用过什么（给回复轨迹用）。 """

    def __init__(self, tools: list[tuple[ToolSpec, ToolHandler]] | None = None):
        tools = tools or []
        self._handlers = {spec.name: handler for spec, handler in tools}
        self._specs = [spec for spec, _ in tools]
        self.calls: list[dict[str, Any]] = []

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._specs)

    def schema(self) -> list[dict[str, Any]]:
        """ 给模型的工具清单；没有工具时返回空列表（不能传 None）。 """
        return [spec.to_openai() for spec in self._specs]

    def add(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self._handlers[spec.name] = handler
        self._specs.append(spec)

    async def call(self, call: ToolCall) -> ToolResult:
        """ 执行一次调用。工具自己出错不能把整条消息处理链拖死。 """
        handler = self._handlers.get(call.name)
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        if handler is None:
            result = ToolResult(content=f'没有这个工具：{call.name}')
        else:
            try:
                result = await handler(arguments)
            except Exception as e:
                logger.warning(f'工具 {call.name} 执行失败: {e}')
                result = ToolResult(content=f'工具执行失败：{e}')
        self.calls.append({
            'name': call.name,
            'arguments': dict(arguments),
            'content': result.content,
            'stop': result.stop,
        })
        return result

    def render_calls(self) -> str:
        """ 把这一轮的调用过程压成一段文本，写进轨迹和决策块。 """
        if not self.calls:
            return ''
        return '\n'.join(
            f'{item["name"]}({json.dumps(item["arguments"], ensure_ascii=False)})'
            f' -> {item["content"]}' for item in self.calls)


def assistant_message(turn: AssistantTurn) -> dict[str, Any]:
    """ 把模型这一轮转回 OpenAI 的消息形状，下一轮要原样带上。 """
    message: dict[str, Any] = {'role': 'assistant', 'content': turn.content or ''}
    if turn.tool_calls:
        message['tool_calls'] = [
            {'id': call.call_id, 'type': 'function',
             'function': {'name': call.name,
                          'arguments': json.dumps(call.arguments, ensure_ascii=False)}}
            for call in turn.tool_calls]
    return message


def tool_message(call: ToolCall, content: str) -> dict[str, Any]:
    """ 工具结果。call_id 必须对上，否则供应商会报错。 """
    return {'role': 'tool', 'tool_call_id': call.call_id, 'content': content}


def parse_tool_calls(raw_calls) -> tuple[ToolCall, ...]:
    """ 把 SDK 返回的 tool_calls 转成我们自己的类型。

    参数是 JSON 字符串，解析不了就当作空参数——宁可按无参调用，也别整轮丢掉。
    """
    calls: list[ToolCall] = []
    for index, raw in enumerate(raw_calls or ()):
        function = getattr(raw, 'function', None)
        name = str(getattr(function, 'name', '') or '').strip()
        if not name:
            continue
        raw_arguments = getattr(function, 'arguments', '') or '{}'
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            try:
                parsed = json.loads(raw_arguments)
            except (TypeError, ValueError):
                logger.warning(f'工具参数不是合法 JSON，按空参数处理：{raw_arguments!r}')
                parsed = {}
            arguments = parsed if isinstance(parsed, dict) else {}
        calls.append(ToolCall(call_id=str(getattr(raw, 'id', '') or index), name=name, arguments=arguments))
    return tuple(calls)


def _self_check() -> None:
    import asyncio

    spec = ToolSpec('send_emoji',
                    '发一张表情。',
                    {'type': 'object', 'properties': {'index': {'type': 'integer'}}, 'required': ['index']})
    schema = spec.to_openai()
    assert schema['type'] == 'function'
    assert schema['function']['name'] == 'send_emoji'
    assert schema['function']['parameters']['required'] == ['index']
    # 无参数工具也得给一个合法 schema，不能是 None
    assert ToolSpec('wait', '等一会').to_openai()['function']['parameters'] == {'type': 'object', 'properties': {}}

    # 参数强转：模型经常把整数写成字符串
    assert arg_int({'index': '3'}, 'index') == 3
    assert arg_int({'index': 3}, 'index') == 3
    assert arg_int({'index': '第三张'}, 'index', default=-1) == -1
    assert arg_int({}, 'index', default=-1) == -1
    assert arg_int({'index': True}, 'index', default=-1) == -1
    assert arg_str({'keyword': ' 无语 '}, 'keyword') == '无语'
    assert arg_str({}, 'keyword', '狗头') == '狗头'

    async def search(arguments):
        return ToolResult(f'搜到 {arg_str(arguments, "keyword")}')

    async def boom(arguments):
        raise RuntimeError('网络挂了')

    box = Toolbox([(spec, search), (ToolSpec('bad', '会炸'), boom)])
    assert [item['function']['name'] for item in box.schema()] == ['send_emoji', 'bad']
    assert box.names == ('send_emoji', 'bad')

    result = asyncio.run(box.call(ToolCall('1', 'send_emoji', {'keyword': '无语'})))
    assert result.content == '搜到 无语' and result.stop is False
    # 工具名不存在 / 工具抛异常，都只变成一条给模型看的结果，不往外抛
    assert '没有这个工具' in asyncio.run(box.call(ToolCall('2', 'nope'))).content
    assert '工具执行失败' in asyncio.run(box.call(ToolCall('3', 'bad'))).content
    assert len(box.calls) == 3
    assert box.render_calls().count('\n') == 2

    # 参数解析
    class RawFunction:
        def __init__(self, name, arguments):
            self.name, self.arguments = name, arguments

    class RawCall:
        def __init__(self, id_, name, arguments):
            self.id, self.function = id_, RawFunction(name, arguments)

    parsed = parse_tool_calls([
        RawCall('a', 'speak', '{"intent": "附和一句"}'),
        RawCall('b', 'send_emoji', '{"index": 2}'),
        RawCall('c', 'broken', 'not json'),
    ])
    assert [call.name for call in parsed] == ['speak', 'send_emoji', 'broken']
    assert parsed[0].arguments == {'intent': '附和一句'}
    assert parsed[2].arguments == {}
    assert parse_tool_calls(None) == ()

    # 多轮往返要的消息形状
    turn = AssistantTurn(content='我想找个表情', tool_calls=parsed[:2])
    assistant = assistant_message(turn)
    assert assistant['role'] == 'assistant'
    assert [item['function']['name'] for item in assistant['tool_calls']] == ['speak', 'send_emoji']
    # 参数要转回 JSON 字符串，供应商只认字符串
    assert json.loads(assistant['tool_calls'][1]['function']['arguments']) == {'index': 2}
    assert 'tool_calls' not in assistant_message(AssistantTurn(content='算了'))
    assert tool_message(parsed[0], '好') == {'role': 'tool', 'tool_call_id': 'a', 'content': '好'}

    print('tools self-check ok')


if __name__ == '__main__':
    _self_check()
