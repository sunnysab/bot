""" agent 模式 vs 两段式的离线 A/B：拿真实模型跑同一批历史轮次，并排看。

数据来自 `reply_trace.jsonl`——那里存了每一轮真正送到模型的 transcript。
两条路都喂**同一份 transcript**、同一个模型、同一个温度，只比「说什么/怎么说」。

三个如实说明的限制（别拿它当线上效果）：
1. trace 里没存画像 / 群用语 / 中期记忆当时注入了什么，所以两边都少几块上下文；
2. 两段式的 planner 在线上可能单独配模型（留空才用主模型），这里统一用主模型；
3. 温度不为 0，同一条重跑会变；样本量小，只在「差别大到肉眼可见」时才有意义。

用法：
    .venv/bin/python ab_agent.py            # 默认最近 6 条开口的轮次
    .venv/bin/python ab_agent.py --n 10 --out ab.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import types
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from ai import usage
from config import DOTENV_PATH, Config
from context import ChatWindow, MessageContext
from persona import Persona
from plugin import ChatPlugin, clean_reply
from runtime import _providers

TRACE = Path(__file__).parent / 'reply_trace.jsonl'
# 机器人自己的显示名。trace 里只存了「我」，没存名字；A/B 只影响 prompt 里那句自我介绍
SELF_NAME = '雨墨蝉烟'


async def no_emoji(_keyword: str) -> list:
    """ 离线 A/B 不接表情库：让表情工具安静地搜不到，两条路都一样。 """
    return []


def load_samples(path: Path, limit: int, only_spoken: bool) -> list[dict]:
    """ 从 JSONL 里取最近 limit 条有 transcript 的记录。 """
    records = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not record.get('transcript'):
            continue
        if only_spoken and not record.get('reply'):
            continue
        records.append(record)
    return records[-limit:]


def build_ctx(record: dict, self_name: str) -> MessageContext:
    """ 用 trace 里的 transcript 末行当「本轮消息」，还原出插件要的 ctx。

    transcript 是 `'以下聊天记录供你参考：\\n\\n' + str(history)`，触发这轮的那条就是最后一行。
    """
    lines = [line for line in record['transcript'].splitlines() if line.strip()]
    last = re.sub(r'^\[\d\d-\d\d \d\d:\d\d\]\s*', '', lines[-1]) if lines else ''
    who, _, content = last.partition(': ')
    msg = types.SimpleNamespace(msg_type=1, content=content, clean_text=content, text=content,
                                sender_name=who, sender='', reply_target='ab@chatroom',
                                msg_id=0, is_chatroom=True, is_at_me=False, is_self=False,
                                create_time=0)
    return MessageContext(msg=msg, history=ChatWindow(), self_name=self_name,
                          contact=record.get('contact_name') or '某群')


def clean(texts, plugin, ctx) -> list[str]:
    names = (ctx.self_name, *plugin.persona.aliases)
    return [t for t in (clean_reply(x, names) for x in texts or ()) if t]


async def run_two_stage(plugin: ChatPlugin, ctx: MessageContext, transcript: str,
                        must_speak: bool) -> dict:
    started = time.perf_counter()
    before = usage.snapshot()
    decided, _emoji, _at, _ref, _log = await plugin._plan_with_tools(ctx, {}, transcript)
    texts: list[str] = []
    plan = ''
    if decided is not None:
        plan = decided.render()
    if decided is None:
        reply = None
    elif decided.speak or must_speak:
        reply = await plugin._express(ctx, {}, transcript, plan)
        texts = clean(reply, plugin, ctx)
    after = usage.snapshot()
    return {'texts': texts, 'plan': plan, 'latency_ms': int((time.perf_counter() - started) * 1000),
            'tokens': _delta(before, after), 'error': decided is None}


async def run_agent(plugin: ChatPlugin, ctx: MessageContext, transcript: str,
                    must_speak: bool) -> dict:
    started = time.perf_counter()
    before = usage.snapshot()
    replies, _emoji, _at, _ref, _log = await plugin._reply_agent(ctx, {}, transcript, must_speak)
    texts = clean(replies, plugin, ctx)
    after = usage.snapshot()
    return {'texts': texts, 'latency_ms': int((time.perf_counter() - started) * 1000),
            'tokens': _delta(before, after)}


def _delta(before: dict, after: dict) -> dict:
    return {'calls': after['calls'] - before['calls'],
            'prompt': after['prompt'] - before['prompt'],
            'completion': after['completion'] - before['completion']}


async def amain(args) -> None:
    load_dotenv(DOTENV_PATH)
    config = Config.from_env()
    if not config.deepseek_key:
        raise SystemExit('没配 DEEPSEEK_KEY，跑不了')
    ai, planner = _providers(config)
    persona = Persona(
        personality=config.personality, reply_style=config.reply_style,
        behavior_style=config.behavior_style,
        alternates=tuple(style.strip() for style in config.reply_styles.split('|') if style.strip()),
        alternate_probability=config.style_probability)

    two_stage = ChatPlugin(ai, context_length=30, persona=persona, enable_planner=True,
                           enable_tools=True, planner_ai=planner, emoji_search=no_emoji,
                           planner_rounds=3, timeout=config.request_timeout)
    agent = ChatPlugin(ai, context_length=30, persona=persona, reply_mode='agent',
                       planner_ai=planner, emoji_search=no_emoji,
                       planner_rounds=3, timeout=config.request_timeout)
    # 这轮只比「说什么 / 怎么说」：把表情工具摘掉，否则模型会花一轮去搜一个空库，
    # 把 agent 的调用数抬高（表情选择是另一个轴，已由 test_chat_plugin 覆盖）
    for plugin in (two_stage, agent):
        plugin._emoji_tools = lambda state: []          # noqa: ARG005

    samples = load_samples(TRACE, args.n, only_spoken=not args.all)
    logger.info(f'{config.deepseek_model} · {len(samples)} 条样本（{"全部" if args.all else "开口的"}轮次）')
    rows = []
    for index, record in enumerate(samples, 1):
        ctx = build_ctx(record, SELF_NAME)
        transcript = record['transcript']
        must_speak = record.get('trigger') != 'proactive'
        was = list(record.get('reply') or ())
        got_two = await run_two_stage(two_stage, ctx, transcript, must_speak)
        got_agent = await run_agent(agent, ctx, transcript, must_speak)
        rows.append({'i': index, 'trigger': record.get('trigger'), 'was': was,
                     'two_stage': got_two, 'agent': got_agent})
        print(f"\n#{index} 触发={record.get('trigger')} 当时回了: {' / '.join(was) or '（没开口）'}")
        print(f"  两段式 {got_two['latency_ms']:>6}ms  {_tokens(got_two)}  "
              f"{' / '.join(got_two['texts']) or '（没开口）'}")
        print(f"  agent  {got_agent['latency_ms']:>6}ms  {_tokens(got_agent)}  "
              f"{' / '.join(got_agent['texts']) or '（没开口）'}")

    _summary(rows)
    if args.out:
        Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
        print(f'\n明细已写 {args.out}')


def _tokens(result: dict) -> str:
    tokens = result['tokens']
    return f"tok {tokens['prompt']:>5}+{tokens['completion']:<4} calls {tokens['calls']}"


def _summary(rows: list[dict]) -> None:
    def agg(key: str) -> tuple[int, int, int, int]:
        spoke = sum(1 for r in rows if r[key]['texts'])
        latency = sum(r[key]['latency_ms'] for r in rows) // max(1, len(rows))
        prompt = sum(r[key]['tokens']['prompt'] for r in rows)
        completion = sum(r[key]['tokens']['completion'] for r in rows)
        return spoke, latency, prompt, completion

    print('\n' + '=' * 62)
    print(f"{'':10} {'开口':>5} {'平均延迟':>9} {'总 prompt':>11} {'总 completion':>15}")
    for key in ('was', 'two_stage', 'agent'):
        if key == 'was':
            spoke = sum(1 for r in rows if r['was'])
            print(f"{'线上原样':10} {spoke:>3}/{len(rows):<2} {'-':>9} {'-':>11} {'-':>15}")
        else:
            spoke, latency, prompt, completion = agg(key)
            print(f'{key:10} {spoke:>3}/{len(rows):<2} {latency:>7}ms {prompt:>11} {completion:>15}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--n', type=int, default=6)
    parser.add_argument('--all', action='store_true', help='连没开口的轮次一起跑')
    parser.add_argument('--out', default='', help='把明细写成 JSON')
    logger.remove()
    logger.add(lambda message: print(message, end=''), level='WARNING')
    asyncio.run(amain(parser.parse_args()))
