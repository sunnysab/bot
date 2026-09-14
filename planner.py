""" 决策结果：Planner 输出 JSON，这里负责解析成 Plan 再交给 Expressor。

解析很严：只认 JSON，认不出来就当没决策（沉默）。
理由：thinking 模型偶尔把整段预算烧在推理上、content 为空，我们回退拿到的是
一大段反复纠结的推理（有时还回显了 prompt 里的格式样例）。把那种文本当 intent
塞给表达者会直接污染输出；沉默是安全的失败方向。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

FENCE = re.compile(r'^```[a-zA-Z]*\s*|\s*```$')

# Planner 的结构化输出约束。支持 json_schema 的供应商会强制返回合法 JSON，
# 实测 completion 从 ~400 tokens 降到 ~44，而且不再有解析失败。
PLAN_SCHEMA = {
    'type': 'object',
    'properties': {
        'speak': {'type': 'boolean'},
        'analysis': {'type': 'string'},
        'intent': {'type': 'string'},
        'emotion': {'type': 'string'},
    },
    'required': ['speak', 'analysis'],
    'additionalProperties': False,
}
PLAN_RESPONSE_FORMAT = {
    'type': 'json_schema',
    'json_schema': {'name': 'plan', 'schema': PLAN_SCHEMA, 'strict': True},
}


@dataclass(frozen=True)
class Plan:
    """ Planner 的判断。 """

    speak: bool
    analysis: str = ''
    intent: str = ''
    emotion: str = ''
    emoji: str = ''            # 本轮同时要发的表情（描述）；写进决策块让表达者避开重复

    @classmethod
    def parse(cls, raw: str | None) -> Plan:
        """ 从模型输出里抠出决策；认不出来就是「不发言」。 """
        text = (raw or '').strip()
        payload = _load_json(text) if text else None
        if payload is None:
            return cls(speak=False)

        speak = payload.get('speak')
        if speak is None:
            speak = bool(payload.get('intent') or payload.get('analysis'))
        return cls(
            speak=bool(speak),
            analysis=str(payload.get('analysis') or '').strip(),
            intent=str(payload.get('intent') or '').strip(),
            emotion=str(payload.get('emotion') or '').strip())

    def render(self) -> str:
        """ 渲染成给 Expressor 看的参考文本；不发言时返回空串。 """
        if not self.speak:
            return ''
        lines = []
        if self.analysis:
            lines.append(f'局面：{self.analysis}')
        if self.intent:
            lines.append(f'想表达：{self.intent}')
        if self.emotion:
            lines.append(f'情绪：{self.emotion}')
        if self.emoji:
            lines.append(f'同时会发一张表情：{self.emoji}（文字不要重复这个情绪）')
        return '\n'.join(lines)


def _load_json(text: str) -> dict | None:
    try:
        data = json.loads(FENCE.sub('', text).strip())
    except ValueError:
        match = re.search(r'\{.*\}', text, re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def _self_check() -> None:
    plan = Plan.parse('{"speak": true, "analysis": "在吐槽加班", "intent": "附和一句", "emotion": "无奈"}')
    assert plan.speak and plan.intent == '附和一句'
    assert '局面：在吐槽加班' in plan.render() and '情绪：无奈' in plan.render()

    assert Plan.parse('{"speak": false, "analysis": "接不上"}').speak is False
    assert Plan.parse('{"speak": false}').render() == ''

    # 表情写进决策块，让表达者知道文字别重复这个情绪
    assert '同时会发一张表情：无语' in Plan(speak=True, intent='附和', emoji='无语').render()

    # 带代码块 / 前后有废话
    assert Plan.parse('```json\n{"speak": true, "intent": "x"}\n```').intent == 'x'
    assert Plan.parse('好的：{"speak": true, "intent": "y"} 以上').intent == 'y'

    # 缺 speak 字段时按有没有内容判断
    assert Plan.parse('{"intent": "说点什么"}').speak is True
    assert Plan.parse('{"analysis": ""}').speak is False

    # 完全不按格式来：宁可沉默，也不要把一段推理当 intent 塞给表达者
    assert Plan.parse('我觉得该接一句，吐槽下这个班').speak is False
    assert Plan.parse('本轮不发言').speak is False
    assert Plan.parse('').speak is False
    assert Plan.parse(None).speak is False

    # 结构化输出约束（能用的供应商会强制产出合法 JSON）
    assert PLAN_RESPONSE_FORMAT['type'] == 'json_schema'
    assert 'speak' in PLAN_RESPONSE_FORMAT['json_schema']['schema']['properties']

    print('planner self-check ok')


if __name__ == '__main__':
    _self_check()
