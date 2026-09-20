""" 媒体名录：给群里所有图片 / 表情补一条文字描述，并导出成一份可以直接查的文件。

两件事：

1. **补描述**：走 daemon RPC 扫历史，缺的才做，做完写进 `media_cache.db`（md5 → 描述，
   下次不会重复花钱）。表情的标签是微信 XML 自带的（免费），所以只有**没标签的表情**
   和**图片**才需要调 vision。
2. **导出** `memory/media.md`：每张媒体的 md5 / 描述 / 群里出现过几次 / 最近一次。
   以后想「这张图是什么」直接翻文件或者 grep md5。

用法：

    python describe_media.py                 # 扫历史 → 补描述 → 导出
    python describe_media.py --dry-run       # 只统计缺口（顺带导出「还没描述」清单）
    python describe_media.py --export        # 只导出，不连 daemon（列出已知的）
    python describe_media.py --limit 50      # 这次最多排 50 张识图（控制花钱）
    python describe_media.py --self-check    # 自检（不连 daemon、不花钱）

前置：daemon 得是**登录状态**。会话失效时连 `get_messages` 都会 401
（那时先 `login auto` / `login qr`）。
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from weixin_bot import WeChatBotError

from ai import IMAGE_PROMPT_VERSION
from config import DOTENV_PATH, Config
from context import SingleRecord
from describer import MediaCache, MediaDescriber
from emojis import Emoji, EmojiLibrary
from media import Media, render
from runtime import _providers
from wechat import Wechat

MEDIA_TYPES = (3, 47)          # 图片 / 表情
DEFAULT_DAYS = 30              # 只看最近这些天：更早的媒体 CDN 早过期了，补不动


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='补描述 + 导出媒体名录')
    parser.add_argument('--export', action='store_true', help='只导出，不连 daemon')
    parser.add_argument('--dry-run', action='store_true', help='只统计缺口，不下载不调模型')
    parser.add_argument('--self-check', action='store_true', help='只跑自检（不连 daemon、不花钱）')
    parser.add_argument('--out', default='memory/media.md', help='导出目标（默认 memory/media.md）')
    parser.add_argument('--days', type=int, default=DEFAULT_DAYS, help='往前扫多少天')
    parser.add_argument('--limit', type=int, default=0, help='这次最多排多少张识图（0 = 不限）')
    parser.add_argument('--concurrency', type=int, default=3, help='并发识图数')
    return parser.parse_args()


class Inventory:
    """ 名录：md5 → 种类 / 出现过几次 / 最近一次。 """

    def __init__(self) -> None:
        self.kind: dict[str, str] = {}
        self.count: Counter[str] = Counter()
        self.last: dict[str, int] = {}
        self.updated: dict[str, int] = {}       # 描述写入时间（没有扫描时就是它）

    def __len__(self) -> int:
        return len(self.kind)

    def add(self, media: Media, when: int) -> None:
        if not media.md5:
            return
        self.kind[media.md5] = media.kind
        self.count[media.md5] += 1
        self.last[media.md5] = max(self.last.get(media.md5, 0), when or 0)

    def seed(self, cache: MediaCache, library: EmojiLibrary) -> None:
        """ 不扫历史时，名录就从两个库拼出来（只能列已经见过的）。 """
        for md5, kind, _desc, updated in cache.items():
            self.kind.setdefault(md5, kind)
            self.updated[md5] = updated
        for emoji in library.items():
            self.kind.setdefault(emoji.md5, 'emoji')
            self.count.setdefault(emoji.md5, emoji.count)

    def by_kind(self, kind: str) -> list[str]:
        return [md5 for md5, item in self.kind.items() if item == kind]


async def fill(config: Config, cache: MediaCache, inventory: Inventory,
               describer: MediaDescriber | None, days: int, limit: int) -> int:
    """ 扫历史里的图片/表情，顺手把缺的描述排进 describer（它自己按 md5 去重）。 """
    wechat = Wechat(config.daemon_host, config.daemon_port, unix_path=config.daemon_unix or None)
    await wechat.connect()
    since = int(datetime.now().timestamp()) - days * 86400
    scheduled = 0
    try:
        for peer in await wechat.conversations(limit=100):
            async for msg in wechat.iter_media(peer.peer, MEDIA_TYPES, since=since):
                _text, media = render(msg.msg_type, msg.content, msg.text)
                if media is None or not media.md5:
                    continue
                inventory.add(media, msg.create_time)
                if describer is None or not media.needs_description:
                    continue
                # 图片只认新提示词那版描述，老的 15 字版要重新描述（describer 也这么判）
                if cache.get(media.md5, IMAGE_PROMPT_VERSION if media.kind == 'image' else ''):
                    continue
                if limit and scheduled >= limit:
                    continue
                describer.schedule(SingleRecord(
                    msg.sender_name, media.placeholder, msg.create_time, media=media,
                    download=lambda m=msg: wechat.media_bytes(m)))
                scheduled += 1
                await asyncio.sleep(0)          # 让识图任务跑起来（并发由 describer 管）
            if describer:
                await describer.join()
            logger.info(f'{peer.peer}：名录 {len(inventory)} 项，已排 {scheduled} 张识图')
    finally:
        await wechat.stop()
    return scheduled


def export(cache: MediaCache, library: EmojiLibrary, inventory: Inventory, path: Path,
           filled: int) -> None:
    """ 写成 markdown：一张表一顿饭钱，比 sqlite 好查。 """

    def describe(md5: str) -> str:
        """ 描述：vision 的优先，否则用表情 XML 自带的标签；场景拼在后面。 """
        emoji = library.get(md5)
        text = cache.get(md5) or (emoji.desc if emoji else '')
        if emoji and emoji.scene:
            text = f'{text}（{emoji.scene}）' if text else emoji.scene
        return text

    lines = [
        '# 群里的图片与表情',
        '',
        f'> 生成时间：{datetime.now():%Y-%m-%d %H:%M} · 这次新排 {filled} 张识图',
        '> 描述来源：`media_cache.db`（vision：图片 + 没标签的表情）+ `memory/emojis.json`'
        '（表情自带的标签 / 使用场景）。由 `describe_media.py` 生成，别手改。',
        '',
    ]
    for label, kind in (('图片', 'image'), ('表情', 'emoji')):
        md5s = sorted(inventory.by_kind(kind), key=lambda md5: -inventory.count[md5])
        described = sum(1 for md5 in md5s if describe(md5))
        lines += [f'## {label}（{len(md5s)} 张，有描述 {described}）', '']
        if not md5s:
            lines += ['（还没有：先跑一次 `python describe_media.py`）', '']
            continue
        lines += ['| md5 | 描述 | 群里出现过 | 最近一次 |', '|---|---|---|---|']
        for md5 in md5s:
            when = inventory.last.get(md5) or inventory.updated.get(md5, 0)
            last = datetime.fromtimestamp(when).strftime('%m-%d %H:%M') if when else '—'
            lines.append(f'| `{md5}` | {describe(md5) or "**还没描述**"} | '
                         f'{inventory.count[md5] or "—"} | {last} |')
        lines += ['']
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines), encoding='utf-8')


async def amain(args: argparse.Namespace) -> None:
    load_dotenv(DOTENV_PATH)
    config = Config.from_env()
    cache = MediaCache(config.media_cache_path)
    library = EmojiLibrary.load(config.emoji_path) if config.emoji_path else EmojiLibrary()
    inventory = Inventory()
    filled = 0
    try:
        if not args.export:
            if not config.deepseek_key:
                raise SystemExit('没配 DEEPSEEK_KEY，识图跑不了（只导出请加 --export）')
            ai, _planner = _providers(config)
            describer = None if args.dry_run else MediaDescriber(
                ai, cache, model=config.describe_model or config.deepseek_model,
                concurrency=args.concurrency,
                timeout=config.describe_timeout, max_tokens=config.describe_max_tokens,
                image_max_tokens=config.describe_image_max_tokens)
            try:
                filled = await fill(config, cache, inventory, describer, args.days, args.limit)
            except WeChatBotError as e:
                # 会话失效时**所有**消息 RPC 都 401（daemon 自己就这么答的），说清楚怎么办
                raise SystemExit(
                    f'问 daemon 要数据失败：{e}\n（登录态失效时先 `login auto` / `login qr`）') from e
        else:
            inventory.seed(cache, library)
        export(cache, library, inventory, Path(args.out), filled)
        logger.info(f'名录 {len(inventory)} 项 → {args.out}')
    finally:
        cache.close()


def _self_check() -> None:
    """ 名录汇总 + 导出格式：真数据跑一次太贵，这里用小的。 """
    import tempfile

    cache = MediaCache(':memory:')
    cache.put('i' * 32, 'image', '一只橘猫趴在键盘上', 'test')
    library = EmojiLibrary()
    library.record(Emoji(md5='e' * 32, desc='摊倒', scene='累到瘫在椅子上时', cdnurl='http://x'))

    inventory = Inventory()
    inventory.add(Media('image', 'i' * 32), 1000)
    inventory.add(Media('image', 'i' * 32), 2000)                 # 同一张出现两次
    inventory.add(Media('image', 'u' * 32), 3000)                 # 没描述的那张
    inventory.add(Media('emoji', 'e' * 32), 4000)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / 'media.md'
        export(cache, library, inventory, out, filled=3)
        text = out.read_text(encoding='utf-8')
    assert '## 图片（2 张，有描述 1）' in text, text
    assert '| `' + 'i' * 32 + '` | 一只橘猫趴在键盘上 | 2 |' in text, text
    assert '**还没描述**' in text, text
    assert '摊倒（累到瘫在椅子上时）' in text, text
    cached = cache.items()
    assert any(md5 == 'i' * 32 and kind == 'image' for md5, kind, _desc, _ts in cached), cached
    cache.close()
    print('describe_media self-check ok')


if __name__ == '__main__':
    args = parse_args()
    if args.self_check:
        _self_check()
    else:
        asyncio.run(amain(args))
