""" 决策结果：Planner 输出 JSON，这里负责解析成 Plan 再交给 Expressor。

解析必须宽容——模型偶尔会带 Markdown 代码块、少个字段、或者干脆不按格式来。
宁可把它的原话当成 intent 用掉，也不要因为格式问题白跑一次调用。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

SILENT_MARKERS = ('本轮不发言', '本轮：不发言')
FENCE = re.compile(r'^```[a-zA-Z]*\s*|\s*```$')


@dataclass(frozen=True)
class Plan:
    """ Planner 的判断。 """

    speak: bool
    analysis: str = ''
    intent: str = ''
    emotion: str = ''

    @classmethod
    def parse(cls, raw: str | None) -> Plan:
        """ 从模型输出里抠出决策。 """
        text = (raw or '').strip()
        if not text:
            return cls(speak=False)

        payload = _load_json(text)
        if payload is None:
            # 没按 JSON 来：明确说不发言就当不发言，否则把原话当成想表达的东西
            if any(marker in text for marker in SILENT_MARKERS):
                return cls(speak=False, analysis=text)
            return cls(speak=True, intent=text)

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

    # 带代码块 / 前后有废话
    assert Plan.parse('```json\n{"speak": true, "intent": "x"}\n```').intent == 'x'
    assert Plan.parse('好的：{"speak": true, "intent": "y"} 以上').intent == 'y'

    # 缺 speak 字段时按有没有内容判断
    assert Plan.parse('{"intent": "说点什么"}').speak is True
    assert Plan.parse('{"analysis": ""}').speak is False

    # 完全不合格式：别丢掉模型的工作，但明确说不发言要认
    assert Plan.parse('我觉得该接一句，吐槽下这个班').speak is True
    assert Plan.parse('本轮不发言').speak is False
    assert Plan.parse('').speak is False
    assert Plan.parse(None).speak is False

    print('planner self-check ok')


if __name__ == '__main__':
    _self_check()
