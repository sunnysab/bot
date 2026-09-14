""" 人格配置：把「我是谁 / 怎么说话 / 怎么行动」拆开，分别喂给不同的 prompt。

- personality  → expressor（replyer）开头，定义身份和性格
- reply_style  → expressor，定义表达风格（句式、语气，而不是字数限制）
- behavior_style → planner，定义行动准则（何时参与、何时闭嘴）
- alternates   → expressor，按概率换一种临时风格，避免每条一个味儿

拆开的好处：改人设不用动代码，也不用把人设、行为、风格混在一段话里互相稀释。

reply_style 里的数字是量出来的，不是拍脑袋的。在 248015 条真实群消息上：
长度中位数 7 字、均值 9.5、p90 17，37% 不到 6 字，超过 40 字的只占 1%；
只有 8% 的句子带句尾标点，句号结尾的只有 1%；
句尾字符前几名是「了 12% / 的 5% / ？3% / 吗 3% / 啊 3%」，
语气词（啊吧呢嘛）加起来才 6%——所以“爱用语气词收尾”那个说法是错的，已删。
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Persona:
    """ 机器人的固定人格 + 可随机切换的表达风格。 """

    personality: str = '是一个积极乐观、温暖阳光的女生，03 年出生。'
    reply_style: str = (
        '说话像微信打字：短。一句话说完，十个字以内最好，很多回复其实就是三五个字。'
        '句尾一般不加标点，句子常常就停在「了」或「的」上。'
        '常省略主语，先说反应，需要时再补半句。不用书面词，不分点，不用 markdown。')
    behavior_style: str = (
        '按自己的兴趣参与群聊：被 @ 或叫到名字就认真回应；'
        '没人点名时，话题接得上、说得出有意思的话才插一句，接不上就闭嘴。'
        '不硬找话题，不复述别人刚说过的话，不连着自说自话。'
        '永远不要暴露自己是机器人：不承认也不讨论自己的身份，更不能把机器人当玩笑。')
    alternates: tuple[str, ...] = ()
    alternate_probability: float = 0.0
    # 群里对它的其他叫法。跟 personality 一样属于「我是谁」，写在这里，不开环境变量
    aliases: tuple[str, ...] = ('雨姐', '烟姐', '雨墨姐')

    def identity(self, name: str) -> str:
        """ expressor 开头的人设段。 """
        also = f'，也有人叫你{"、".join(self.aliases)}' if self.aliases else ''
        return f'你的名字是{name}{also}。{self.personality}'

    def maybe_alternate(self, rng: random.Random | None = None) -> str:
        """ 按概率抽一条本次的临时风格；没抽中返回空串。 """
        if not self.alternates or self.alternate_probability <= 0:
            return ''
        rng = rng or random
        if rng.random() > self.alternate_probability:
            return ''
        return rng.choice(self.alternates)


def _self_check() -> None:
    persona = Persona()
    assert persona.identity('示例名') == (
        '你的名字是示例名，也有人叫你雨姐、烟姐、雨墨姐。是一个积极乐观、温暖阳光的女生，03 年出生。')
    nicknamed = Persona(aliases=('小雨',))
    assert '也有人叫你小雨。' in nicknamed.identity('雨墨蝉烟')
    assert Persona(aliases=()).identity('雨墨蝉烟') == (
        '你的名字是雨墨蝉烟。是一个积极乐观、温暖阳光的女生，03 年出生。')
    # 不配备用风格时永远是空
    assert persona.maybe_alternate() == ''

    talkative = Persona(alternates=('用1-2个字回复', '带点翻译腔'), alternate_probability=1.0)
    assert talkative.maybe_alternate() in talkative.alternates

    never = Persona(alternates=('x',), alternate_probability=0.0)
    assert never.maybe_alternate() == ''

    import random as random_module
    seeded = random_module.Random(0)
    always = Persona(alternates=('only',), alternate_probability=1.0)
    assert always.maybe_alternate(seeded) == 'only'
    print('persona self-check ok')


if __name__ == '__main__':
    _self_check()
