import asyncio
import random
import time
from dataclasses import dataclass
from typing import TypeVar

from loguru import logger
from weixin_bot import PatContent, WeChatBotError

from context import (
    ChatWindow,
    ContextManager,
    MessageContext,
    MessageKind,
    SingleRecord,
    is_system_notice,
    message_handle,
)
from describer import MediaDescriber
from emojis import Emoji, EmojiLibrary, parse_emoji_xml
from events import Status, bus
from media import is_placeholder, render
from moments import MomentKeeper
from outbound import Outbound, OutboundQueue
from plugin import EndProcessing, Plugin, PluginManager, Reply
from typos import apply_typos
from wechat import Message, Wechat

# 本地表情够这么多就不再联网搜——本群用过的比商店里的更像人话
EMOJI_LOCAL_ENOUGH = 3
EMOJI_RESULT_LIMIT = 8
EMOJI_SAVE_INTERVAL = 5.0      # 表情库最长隔这么久落一次盘（热度是运行时累加的）
STALE_MESSAGE_SECONDS = 300.0  # 比这更旧的消息只当上下文：daemon 长链重连会补推离线积压
REVOKE_WINDOW_SECONDS = 120.0  # daemon 只认 2 分钟内的撤回，超了不必白跑一次 RPC
SENT_KEEP = 10                 # 每个会话最多记这么多条自己的出站，只用来撤回

T = TypeVar('T')


def _aslist(x: T | list[T]) -> list[T]:
    """ 把「单个对象或列表」统一成列表。 """
    return x if isinstance(x, list) else [x]


def _daemon_id(result) -> int | None:
    """ 从 daemon 的发送回执里取 server_id。拿不到（演习 / 服务端没回 / 假 daemon）返回 None。 """
    return result.get('msg_id') if isinstance(result, dict) else None


@dataclass
class Sent:
    """ 本进程发出去的一条消息。

    daemon 的 msg_id 只有 _deliver 拿得到，而且撤回必须用它，所以得主动记一笔。
    record 指向历史里的那条，撤回成功后从上下文里抹掉。
    """

    msg_id: int
    ts: float
    text: str
    record: SingleRecord | None = None


class Bot:
    """ 微信机器人助手 """

    def __init__(self, host: str, port: int, unix_path: str = '', dry_run: bool = False,
                 x_delay: float = 0, send_interval: float = 1.0,
                 describer: MediaDescriber | None = None, describe_history_limit: int = 3,
                 keeper: MomentKeeper | None = None, emojis: EmojiLibrary | None = None,
                 emoji_path: str = '', emoji_describer=None, typo_rate: float = 0.0,
                 quote_replies: bool = False, rng: random.Random | None = None):
        """ 初始化机器人

        @param host: weixin-rs daemon 服务器地址
        @param port: weixin-rs daemon 服务器端口
        @param unix_path: daemon 的 Unix domain socket 路径，非空时优先于 host/port
        @param dry_run: 是否为测试模式
        @param x_delay: 每条消息回复的延迟时间系数（将根据消息长度乘此系数，再叠加随机抖动）
        @param send_interval: 全局最小发送间隔（秒），用于规避风控
        @param describer: 媒体描述器，后台把图片/表情含义写回历史；为 None 则不做识图
        @param describe_history_limit: 启动加载历史时，每个会话最多补描述多少条媒体
        @param keeper: 中期记忆：消息滑出上下文窗口时压缩成回想；为 None 则不记
        @param emojis: 本地表情库：收到的表情顺手存下，以后能原样发回去；为 None 则不偷
        @param emoji_path: 表情库落盘位置；留空则不落盘（重启就没了）
        @param emoji_describer: 给偷来的表情补「使用场景」的后台任务；为 None 则不补
        @param typo_rate: 出站文本抖错别字的概率（0 为关闭）
        @param quote_replies: 允许模型引用（微信的「回复某条」）。关掉就一律不引；
            模型自己选引哪条（reply / speak 的 ref），没选就不引——它不一定是在回谁
        @param rng: 错别字用的随机源，测试时可注入
        """
        async def callback(msg: Message) -> None:
            await self.on_message(msg)

        self.dry_run = dry_run
        self._wechat = Wechat(host, port, callback=callback, dry_run=dry_run, unix_path=unix_path)

        # 聊天上下文记录。消息滑出窗口时交给 keeper 压成中期记忆
        self._history = ContextManager(on_evict=self._on_evicted)
        # 启动历史只加载一次：on_ready 和 login_status 各会触发一次，重复加载会让
        # 同一批消息在窗口里出现两遍，复读机会把每条都当成队形
        self._history_loaded = False
        self._keeper = keeper
        # 消息处理插件
        self._plugins = PluginManager()
        # 媒体识图（可选）
        self._describer = describer
        self.describe_history_limit = describe_history_limit
        # 表情库：群里收到的表情带完整下载信息，存下来就能原样发回去
        self.emojis = emojis
        self.emoji_path = emoji_path
        self.emoji_describer = emoji_describer
        self.typo_rate = typo_rate
        self.quote_replies = quote_replies
        # 运行计数：面板的统计卡要看（拍的那些常数到底撞了多少次）
        self.typo_count = 0
        self.emoji_count = 0
        self.reply_count = 0
        self._rng = rng or random.Random()
        self._emoji_saved_at = time.time()
        # 每个会话一轮跑一个：跑 LLM 的那几秒里新来的消息攒成下一块，等这轮结束合成一轮处理
        self._turns: dict[str, asyncio.Task] = {}
        self._blocks: dict[str, list[tuple[Message, str, str]]] = {}
        # 自己发出去的消息（按会话分），撤回的定位依据。只留最近几条，越攒越多没意义
        self._sent: dict[str, list[Sent]] = {}

        assert x_delay >= 0
        self.delay_coefficient = x_delay
        # 全局发送队列：限流 + 拟人延迟，与消息处理解耦
        self.outbox = OutboundQueue(send=self._deliver, min_interval=send_interval,
                                    typing_delay=self._typing_delay)

    @property
    def wxid(self) -> str:
        return self._wechat.wxid

    @property
    def wechat(self) -> Wechat:
        """ daemon 适配层。管理后台直接拿它读会话列表和历史。 """
        return self._wechat

    @property
    def history(self) -> ContextManager:
        """ 会话窗口。管理后台要看「模型此刻能看到多少条」。 """
        return self._history

    async def search_emoji(self, keyword: str) -> list[Emoji]:
        """ 搜表情：先翻本群的梗，不够再联网，线上的顺手存进库里当缓存。

        本群用过的表情比商店里的更像人话，所以本地够挑就不联网。
        """
        local = self.emojis.search(keyword) if self.emojis else []
        if len(local) >= EMOJI_LOCAL_ENOUGH:
            return local
        try:
            online = await self._wechat.search_emoji(keyword)
        except Exception as e:
            logger.warning(f'搜表情失败（{keyword}）: {e}')
            return local
        seen = {item.md5 for item in local}
        merged = local + [item for item in online if item.md5 not in seen]
        # 只把「真能发出去」的存进库：线上搜回来的经常缺 cdnurl/aeskey（实测 40 条里
        # cdnurl 全空、aeskey 只有 10 条有），存下来也发不出去，只是占位置
        if self.emojis:
            cached = [item for item in online if item.sendable]
            for item in cached:
                self.emojis.record(item)
            if cached:
                self.save_emojis()
        return merged[:EMOJI_RESULT_LIMIT]

    async def lookup_member(self, chat: str, name: str) -> str:
        """ 群昵称 → wxid。@ 人用：模型看到的是名字，daemon 要的是 wxid。 """
        return await self._wechat.find_member(chat, name)

    async def pat(self, chat: str, name: str) -> tuple[bool, str]:
        """ 拍一拍某人（模型发起的动作）。返回 (是否拍成, 给模型看的回执)。

        群聊按群昵称找 wxid，名字对不上不硬拍；私聊直接拍对方。拍完写一条 is_self
        记录进窗口，模型下一轮看得到自己拍过谁（daemon 的频控只是第二道闸）。
        """
        target = chat
        if chat.endswith('@chatroom'):
            if not name:
                return False, '群聊里要拍谁？给一个群昵称。'
            target = await self.lookup_member(chat, name)
            if not target:
                return False, f'群里没找到叫「{name}」的人，没拍成。'
        else:
            name = await self._wechat.get_display_name(chat)
        try:
            await self._wechat.send_pat(chat, target)
        except Exception as e:  # noqa: BLE001 —— daemon 的频控（429）要原样转给模型
            logger.warning(f'拍一拍失败（{chat} -> {target}）: {e}')
            return False, f'没拍成：{e}'
        if not self.dry_run:
            self._history.push_message(chat, await self._wechat.get_display_name(self.wxid),
                                       f'拍了拍 {name}', int(time.time()), is_self=True)
        return True, f'拍好了：你拍了拍{name}。'

    async def search_messages(self, chat: str, keyword: str, count: int = 40) -> str:
        """ 给模型查更早的聊天记录。命中就返回 transcript 形状的几行，没命中返回空串。

        搜的是 daemon 本地缓存的记录（它同步微信本机的库），只能搜到它见过的那部分。
        """
        window = await self._wechat.fetch_history(chat, count, query=keyword)
        names = await self._wechat.sender_names(chat, [r.sender for r in window if not r.is_self])
        for record in window:
            if not record.is_self and record.sender in names:
                record.sender = names[record.sender]
        return str(window)

    def steal_emoji(self, msg) -> None:
        """ 群里发的表情顺手存下来。

        微信的表情消息 XML 自带 cdnurl/aeskey，所以存下来就能原样发回去——
        这就是「偷表情」的全部关键，不用找图，也不用视觉模型。

        顺便排一个后台任务补「使用场景」：微信给的 desc 只有一个标签（「摊倒」），
        够搜标签、不够搜场合。只要本群用过的那些（有热度），不然后台识图会爆。
        """
        if not self.emojis:
            return
        emoji = parse_emoji_xml(msg.content)
        if emoji is None:
            return
        if self.emojis.record(emoji, used=True):      # 群里用了一次：热度加一
            # 落盘不用每条都做，攒一会儿再写；stop() 里还会再存一次
            if time.time() - self._emoji_saved_at >= EMOJI_SAVE_INTERVAL:
                self.save_emojis()
        if self.emoji_describer:
            # 不再先看 XML 里有没有明文 cdnurl：daemon 的 `cdn::find_attr` 现在容忍
            # `cdnurl = "…` 这种写法，没有明文链接时还能按 msg_id 回退 CDN 帧下载。
            # 以前这里卡着这个条件，群里 19 条带 cdnurl 的表情有 15 条永远补不上场景。
            self.emoji_describer.schedule(self.emojis.get(emoji.md5),
                                          lambda m=msg: self._wechat.media_bytes(m))

    def save_emojis(self) -> None:
        if not (self.emojis and self.emoji_path):
            return
        try:
            self.emojis.save(self.emoji_path)
            self._emoji_saved_at = time.time()
        except OSError as e:
            logger.warning(f'表情库落盘失败 {self.emoji_path}: {e}')

    async def start(self):
        await self.outbox.start()
        await self._wechat.start()

    async def stop(self):
        await self._wechat.stop()
        # 收到一半的块不再跑：先停会话任务，再停发送队列
        await self._stop_turns()
        if self.emoji_describer:
            await self.emoji_describer.join()
        self.save_emojis()
        await self.outbox.stop()
        if self._describer:
            await self._describer.stop()
        if self._keeper:
            await self._keeper.join()  # 收尾：等在跑的压缩任务写完

    async def _stop_turns(self) -> None:
        tasks = list(self._turns.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._turns.clear()
        self._blocks.clear()

    def _on_evicted(self, chat: str, record: SingleRecord) -> None:
        """ 消息滑出上下文窗口，交给中期记忆。 """
        if self._keeper:
            self._keeper.schedule(chat, record)

    def register_ready(self, func) -> None:
        """ 注册连接就绪后的回调（如加载历史上下文）

        注意 daemon「未登录 / 会话过期」时也会就绪（连得上但 RPC 一律 401），所以
        想拿数据得用 `register_login`。
        """
        self._wechat.on_ready(func)

    def register_login(self, func) -> None:
        """ 注册登录成功（首次 / 扫码重登）后的回调。 """
        self._wechat.on_login(func)

    def set_default_plugin(self, plugin: Plugin | list[Plugin]):
        """ 整体替换默认插件链（**不是追加**）——热重载靠它换掉一整套。 """
        self._plugins.set_defaults(_aslist(plugin))

    def attach(self, contact: str, plugin: Plugin | list[Plugin]):
        """ 给某个会话单独指定插件链。注意语义：一旦指定就**完全覆盖**默认链。 """
        self._plugins.set_contact_plugins(contact, _aslist(plugin))

    def mute(self, contact: str) -> None:
        """ 静音一个会话：只留 EndProcessing，什么也不回。 """
        self._plugins.set_contact_plugins(contact, [EndProcessing()])

    def unmute(self, contact: str) -> None:
        self._plugins.unmap_contact(contact)

    def is_muted(self, contact: str) -> bool:
        return any(isinstance(p, EndProcessing) for p in self._plugins.get_plugins_for_contact(contact))

    def clear_contacts(self) -> None:
        """ 清掉所有 per-contact 插件映射（重装「允许回复」名单前调）。 """
        self._plugins.clear_contacts()

    async def load_context(self, session_count: int = 10, window_size: int = 30) -> None:
        """ 可以在启动时加载最近的若干聊天记录, 如果想提高启动速度, 可以不调用它.

        :param session_count: 加载的最近会话数量
        :param window_size: 每个会话加载的消息数量
        """
        if self._history_loaded:  # on_ready 与 login_status 都会拉一次，第二次直接跳过
            return
        count_loaded = 0
        try:
            sessions = await self._wechat.get_recent_sessions(session_count)
        except WeChatBotError as e:
            # 会话过期时 daemon 让连不让查，on_ready 那一轮只能跳过；等扫码登录后
            # register_login 会再调一次
            logger.warning(f'daemon 尚未登录，先不加载历史上下文（{e}）')
            return
        for contact in sessions:
            window = await self._wechat.fetch_history(contact, window_size)
            count_loaded += len(window)
            self._history.get_context(contact).extend(window)
            self._schedule_history_media(window)

        self._history_loaded = True
        logger.info(f'{len(sessions)} sessions, {count_loaded} messages loaded.')

    def _schedule_history_media(self, window: ChatWindow) -> None:
        """ 启动加载的历史媒体按会话限量补描述，避免一次烧太多识图调用。 """
        if not self._describer:
            return
        scheduled = 0
        for record in reversed(list(window)):
            if scheduled >= self.describe_history_limit:
                break
            if record.media and record.media.needs_description and record.download:
                self._describer.schedule(record)
                scheduled += 1

    def _typing_delay(self, text: str) -> float:
        """ 拟人打字延迟 = 随机思考时间 + 与字数成比例的时间（再乘随机抖动），单条最多 50 秒。 """
        if not self.delay_coefficient:
            return 0.0
        think = random.uniform(1.5, 4.0)
        typing = len(text) * self.delay_coefficient * random.uniform(0.7, 1.3)
        return min(think + typing, 50)

    def enqueue(self, chat: str, text: str = '', sender: str = '', reply_to=None,
                chat_name: str = '', emoji: Emoji | None = None, verbatim: bool = False,
                ref_svrid: int = 0, at_list: str = '') -> None:
        """ 决策已定：**先把这条写进历史**，再交给出站队列。

        历史在决策时就落，不等真发出去。回复会卡在打字延迟和最小发送间隔里好几秒，
        这期间对方可能又发来消息、又跑一轮决策；只有历史已经记下这句，下一轮才
        不会把自己还没发出去的话再答一遍（日志里「在？/有事」被回两次就是这个原因）。

        verbatim：一字不改。复读机要的就是原句，抖错别字就不叫复读了。
        """
        # 错别字只在出站这一刻抖，历史里记的也是抖过的版本——跟真人一致
        final = (text if verbatim else apply_typos(text, self.typo_rate, self._rng)) if text else ''
        if text and not verbatim and final != text:
            self.typo_count += 1
        if final or emoji:
            self.reply_count += 1
        if emoji:
            self.emoji_count += 1
        stored = final or (f'[表情：{emoji.desc or "表情"}]' if emoji else '')
        # 演习：没发出去、不进历史、也不在 daemon 里。正文只能靠事件带出去
        record = None
        if stored and not self.dry_run:
            record = self._history.push_message(chat, sender, stored, int(time.time()), is_self=True)
        if final:
            self.outbox.put(chat, final, sender=sender, reply_to=reply_to, chat_name=chat_name,
                            record=record, ref_svrid=ref_svrid, at_list=at_list)
        if emoji:
            # 表情排在文字后面：先说一句，再补个表情收尾。
            # 同轮既有文字又有表情时，历史里记的是文字那条，表情不重复挂一个 record
            self.outbox.put_emoji(chat, emoji, sender=sender, reply_to=reply_to,
                                  chat_name=chat_name, record=None if final else record)

    async def _deliver(self, item: Outbound) -> None:
        """ 真正发送，由 OutboundQueue 串行调用。历史和计数在 enqueue 时就已经落好。 """
        # daemon 给的 server_id：这条出站消息在 daemon 里的身份。拿不到就留空
        daemon_id = None
        error = ''
        try:
            if item.text:
                result = await self._wechat.send_text(item.text, item.receiver,
                                                      at_list=item.at_list or None,
                                                      ref_svrid=item.ref_svrid or None)
                daemon_id = _daemon_id(result)
            if item.emoji:
                result = await self._wechat.send_emoji(item.emoji, item.receiver)
                daemon_id = daemon_id or _daemon_id(result)
        except Exception as e:
            # daemon 长链发送回执解析有 bug：消息通常已经投递出去，重发会重复发。
            # 所以只告警不重试；历史在 enqueue 时已经写过，下一轮模型仍看得到。
            error = str(e)
            logger.warning(f'发送报错，按已投递处理（不重试）{item.receiver}: {e}')

        # 记下自己的出站 msg_id：撤回只有这里拿得到。就算上面报过错，
        # 只要拿到 id 就说明 daemon 已经收下了（长链回执解析有 bug，消息通常已投递）
        if daemon_id and not self.dry_run:
            self._remember(item, daemon_id)

        if self.dry_run:
            # 演习：没发出去、不进历史、也不在 daemon 里。正文只能靠事件带出去，
            # 否则面板上这条回复就彻底消失了
            stored = item.text or (f'[表情：{item.emoji.desc or "表情"}]' if item.emoji else '')
            bus.publish(Status.DRY_RUN, chat_id=item.receiver, chat_name=item.chat_name,
                        reply_to=item.reply_to, origin='bot', text=stored,
                        detail='演习模式，未发送')
            return

        bus.publish(Status.FAILED if error else Status.SENT, chat_id=item.receiver,
                    chat_name=item.chat_name, msg_id=daemon_id, reply_to=item.reply_to,
                    origin='bot', detail=error[:200])

    def _remember(self, item: Outbound, daemon_id) -> None:
        """ 记下一条自己的出站，撤回时靠它定位。 """
        try:
            msg_id = int(daemon_id)
        except (TypeError, ValueError):
            return
        text = item.text or (f'[表情：{item.emoji.desc or "表情"}]' if item.emoji else '')
        entries = self._sent.setdefault(item.receiver, [])
        entries.append(Sent(msg_id=msg_id, ts=time.time(), text=text, record=item.record))
        del entries[:-SENT_KEEP]

    def _revocable(self, chat: str) -> list[Sent]:
        """ 当前还能撤的：2 分钟内、本进程发出去的。顺便把过期的清掉。 """
        now = time.time()
        entries = [sent for sent in self._sent.get(chat, ())
                   if now - sent.ts <= REVOKE_WINDOW_SECONDS]
        if entries:
            self._sent[chat] = entries
        else:
            self._sent.pop(chat, None)
        return entries

    @staticmethod
    def _listing(entries: list[Sent]) -> str:
        """ 候选表。**按时间从新到旧编号**，1 = 最近一条，跟人的直觉一致。 """
        lines = []
        for number, sent in enumerate(reversed(entries), 1):
            preview = sent.text if len(sent.text) <= 30 else sent.text[:30] + '…'
            lines.append(f'{number}. {preview}')
        return '\n'.join(lines)

    async def revoke(self, chat: str, index: int = 0, text: str = '') -> str:
        """ 撤回自己发过的话，返回给模型看的回执。

        index：1 = 最近一条，2 = 再往前一条。text：原话里的一小段，只在
        **唯一命中**时直接撤，命中 0 条或多条都把候选表回给模型让它自己挑。
        两个都不给也是列候选表。这样模型不需要（也拿不到）daemon 的 msg_id。
        """
        if index and text:
            return '一次只能按 index 或 text 指定一种。'
        entries = self._revocable(chat)
        if not entries:
            return '没有可撤回的消息（只能撤自己 2 分钟内发出去的）。'
        if text:
            hits = [i for i, sent in enumerate(entries) if text in sent.text]
            if len(hits) != 1:
                head = '没找到这条' if not hits else '有好几条都对得上，请用 index 指定'
                return f'{head}。可撤回的（1 = 最近一条）：\n{self._listing(entries)}'
            target = hits[0]
        elif index:
            if not 1 <= index <= len(entries):
                return f'没有第 {index} 条。可撤回的（1 = 最近一条）：\n{self._listing(entries)}'
            target = len(entries) - index          # 1 = 最近 = 列表尾巴
        else:
            return f'可撤回的（1 = 最近一条）：\n{self._listing(entries)}'

        sent = entries[target]
        try:
            await self._wechat.revoke(chat, sent.msg_id)
        except Exception as e:
            # daemon 会给可读的原因（超 2 分钟是 410），原样转给模型
            logger.warning(f'撤回失败（{chat} msg_id={sent.msg_id}）: {e}')
            return f'撤回失败：{e}'
        entries.pop(target)
        if sent.record is not None:
            self._history.get_context(chat).forget(sent.record)
        preview = sent.text if len(sent.text) <= 20 else sent.text[:20] + '…'
        return f'已撤回：「{preview}」'

    async def _run_plugins(self, msg: Message, self_name: str, source: str) -> Reply:
        """ 把当前消息交给该会话的插件链，收集要回复的文本和表情。 """
        ctx = MessageContext(msg=msg, history=self._history.get_context(msg.reply_target),
                             self_name=self_name, contact=source)
        texts: list[str] = []
        emoji = None
        verbatim = False
        at = ''
        ref = 0
        for plugin in self._plugins.get_plugins_for_contact(source):
            reply = await plugin.handle(ctx) or Reply()
            texts.extend(reply.texts)
            emoji = emoji or reply.emoji
            verbatim = verbatim or reply.verbatim
            at = at or reply.at
            ref = ref or reply.ref
            if reply.stop:
                break
        return Reply(texts=texts, emoji=emoji, verbatim=verbatim, at=at, ref=ref)

    def _schedule_turn(self, msg: Message, self_name: str, source: str) -> None:
        """ 把这条消息排进它所在会话的「当前这一块」，交由后台任务回复。

        一轮 LLM 要跑好几秒，期间同一会话可能又来了几条（on_message 不再堵住 SDK 事件队列）。
        它们都进同一块，等这一轮跑完合成**一轮**（历史里全在，模型一次看全），
        而不是一条一轮地追——群里一热闹就永远落后。不同会话各跑各的，互不阻塞。
        """
        contact = msg.reply_target
        self._blocks.setdefault(contact, []).append((msg, self_name, source))
        task = self._turns.get(contact)
        if task is None or task.done():
            self._turns[contact] = asyncio.create_task(self._turn_loop(contact),
                                                       name=f'turn:{contact}')

    @staticmethod
    def _pick_trigger(block: list[tuple[Message, str, str]]) -> tuple[Message, str, str]:
        """ 从一块里挑「本轮」那条：优先被 @ 的（最该回），否则取最新。

        不能一律取最新：忙的时候别人 @ 了机器人、紧跟着又一句「哈哈」，拿「哈哈」当本轮
        会被门控判成不值得回，那个 @ 就没人理了。
        """
        for item in block:
            if item[0].is_at_me:
                return item
        return block[-1]

    async def _turn_loop(self, contact: str) -> None:
        """ 一个会话的回复循环：把攒到的块一轮一轮跑完。 """
        try:
            while (block := self._blocks.pop(contact, None)) is not None:
                msg, self_name, source = self._pick_trigger(block)
                try:
                    await self._reply_once(msg, self_name, source)
                except Exception as e:  # 一轮报错不能把整个会话的回复循环带死
                    logger.error(f'回复处理失败（{contact}）: {e}', exc_info=True)
        finally:
            # 这里跟 _schedule_turn 无 await 交错：pop 拿到 None 到 pop 掉 task 之间
            # 不让出事件循环，所以不会丢掉刚要进来的消息
            if self._turns.get(contact) is asyncio.current_task():
                self._turns.pop(contact, None)

    async def _reply_once(self, msg: Message, self_name: str, source: str) -> None:
        """ 跑一轮插件链并把回复写历史 + 入队。 """
        response = await self._run_plugins(msg, self_name, source)
        if not response.texts and not response.emoji:
            logger.debug('no response to send.')
            # 插件链什么都没说，而且这一轮也没留下别的埋点（语音消息、或该会话静音），
            # 补一条，否则面板上这条消息会永远停在「已接收」
            latest = bus.status_of(msg.msg_id)
            if latest is not None and latest.status == Status.RECEIVED:
                # 走到这里只剩一种正常情况：该会话被 EndProcessing 静音。
                # 其余路径（门控/决策/表达）都会自己发一条状态。
                bus.publish(Status.MUTED, chat_id=msg.reply_target, chat_name=source,
                            msg_id=msg.msg_id, detail='该会话已静音')
            return  # 没有回复内容

        # 交给全局发送队列统一限流和拟人延迟，避免阻塞消息接收循环
        texts = [text for text in response.texts if not is_placeholder(text)]
        if len(texts) != len(response.texts):
            logger.warning(f'拦下 {len(response.texts) - len(texts)} 条媒体占位符，不往群里发：{response.texts}')
            bus.publish(Status.PLACEHOLDER_BLOCKED, chat_id=msg.reply_target, chat_name=source,
                        msg_id=msg.msg_id,
                        detail=f'拦下 {len(response.texts) - len(texts)} 条媒体占位符')
        # 引用哪条听模型的：reply / speak 的 ref 已经在插件里解成了 svrId；
        # 没选就不引——它不一定是在回谁。复读机是跟队形，系统通知（拍一拍）
        # 引用框会贴出「拍了拍 你」，两者都按「不该引」处理
        notice = is_system_notice(msg)
        if notice:
            logger.info(f'系统通知不引用（msg_id={msg.msg_id}）: {msg.sender_name}: {msg.content[:30]}')
        ref_svrid = 0 if (response.verbatim or notice or not self.quote_replies) else response.ref
        for text in texts:
            self.enqueue(msg.reply_target, text, sender=self_name,
                         reply_to=msg.msg_id, chat_name=source, verbatim=response.verbatim,
                         ref_svrid=ref_svrid, at_list=response.at)
        if response.emoji:
            self.enqueue(msg.reply_target, sender=self_name, reply_to=msg.msg_id,
                         chat_name=source, emoji=response.emoji)
        logger.debug(f'{len(texts)} responses queued.')

    async def _render_pat(self, msg: Message, pat: PatContent) -> str:
        """ 拍一拍 → 历史里的一行（前缀的发送者由记录本身带上）。

        被拍的是自己时等同于「被点名」（置 `is_at_me`）：门控直接放行、模型必须回应。
        拍别人的只是群里的一条动态，看得见就行。
        """
        if pat.patted_user == self.wxid:
            msg.is_at_me = True
            return f'拍了拍 你{pat.suffix or ""}'
        patted = await self._wechat.get_display_name(pat.patted_user, msg.reply_target)
        return f'拍了拍 {patted}{pat.suffix or ""}'

    async def on_message(self, msg: Message) -> None:
        """ 消息处理函数 """

        # 图片/表情的原始 XML 要在被替换成占位标记前解析出来。
        # 放在 is_self 判断之前：手机上自己发的表情也是群里用过的，值得偷
        kind = MessageKind.of(msg.msg_type)
        if kind is MessageKind.EMOJI:
            self.steal_emoji(msg)

        # 自己的名字；消息来源：群消息取群名称，否则取联系人备注
        self_name: str = await self._wechat.get_display_name(self.wxid, msg.reply_target)
        source = await self._wechat.get_display_name(msg.reply_target)
        # daemon 有时把群成员的显示名解析成了 wxid，那样历史里全是 wxid，得自己补一次
        if msg.is_chatroom and msg.sender_name == msg.sender:
            msg.sender_display_name = await self._wechat.get_display_name(msg.sender, msg.reply_target)
        logger.debug(msg)

        # 非文本消息的正文换成可读标记：图片/表情/位置/视频/语音/链接/文件/引用。
        # handle 是内联图片的短标签（`#4k2p`）；引用图用被引那条的 svrid 生成，
        # 这样原文那行和引用注解里会落在同一个标签上
        handle = message_handle(msg.msg_id)
        quoted_handle = message_handle(msg.ref_svrid) if msg.ref_svrid else ''
        # 拍一拍走 10002（`<sysmsg type="pat">`），不是一种 msg_type，得单独看 parsed 内容。
        # 假消息（测试 / 离线工具）没有 `pat`，所以用 getattr
        pat = getattr(msg, 'pat', None)
        if pat is not None:
            text, media = await self._render_pat(msg, pat), None
        else:
            text, media = render(msg.msg_type, msg.content, msg.text,
                                 handle=handle, quoted_handle=quoted_handle)
        # 要内联给模型的图从哪来：自己有图就是它自己，引用图就跟到 ref_svrid 上。
        # **必须在下面改写 msg.content 之前调**——link_media 要读引用消息里的原始 XML
        media_id, download = self._wechat.link_media(msg, media)
        # 记录的标签由 media_id 定：引用图时它是被引那条的 svrid，不是自己这条的
        record_handle = message_handle(media_id) if media_id else ''
        if text:
            msg.content = msg.clean_text = text
        # 语音：先让 daemon 转成文字再进历史，否则模型只看到 `[语音 3″]`，什么也接不上。
        # 转写要上传 + 轮询（秒级），失败就保留占位符——不能让它拖垮消息链
        if kind is MessageKind.VOICE and not msg.is_self and media is not None:
            try:
                transcript = await self._wechat.voice_text(msg)
            except Exception as e:  # noqa: BLE001 —— 转写失败不是这一轮的错
                logger.warning(f'语音转写失败（msg_id={msg.msg_id}）: {e}')
                transcript = ''
            if transcript:
                msg.content = msg.clean_text = f'{media.placeholder[:-1]}：{transcript}]'

        # 手机上自己发的消息：写进上下文让模型看见，但**不触发任何插件**。
        # 从群里看人设确实在说话（transcript 里渲染成「我」），所以存在感统计也算数
        if msg.is_self:
            record = self._history.push_message(msg.reply_target, self_name, msg.content,
                                                msg.create_time, media=media, download=download,
                                                is_self=True, msg_id=msg.msg_id, media_id=media_id,
                                                handle=record_handle)
            if self._describer:
                self._describer.schedule(record)
            bus.publish(Status.PHONE, chat_id=msg.reply_target, chat_name=source,
                        msg_id=msg.msg_id, origin='phone', detail='手机上发的，只记不回')
            return

        record = self._history.push_message(msg.reply_target, msg.sender_name, msg.content, msg.create_time,
                                            media=media, download=download, msg_id=msg.msg_id,
                                            media_id=media_id, handle=record_handle)
        # 媒体描述缺失时后台识图，稍后原地回写这条记录，不阻塞本轮回复
        if self._describer:
            self._describer.schedule(record)
        bus.publish(Status.RECEIVED, chat_id=msg.reply_target, chat_name=source, msg_id=msg.msg_id)

        # 离线的积压不触发决策：daemon 长链重连会把几个钟头前的消息当 live 推过来，
        # 照发会对着死了的话题连回几十条。写进历史当上下文就够了。
        if msg.create_time and time.time() - msg.create_time > STALE_MESSAGE_SECONDS:
            ago = int(time.time() - msg.create_time)
            logger.info(f'积压消息只记不回（{ago}s 前）: {msg.sender_name}: {msg.content[:30]}')
            bus.publish(Status.STALE, chat_id=msg.reply_target, chat_name=source,
                        msg_id=msg.msg_id, detail=f'积压 {ago // 60} 分钟，只记不回')
            return

        # 这个人上一条回复可能还卡在打字延迟/限速里。不用管它：它已经在 enqueue 时
        # 写进历史了，本轮决策看得到，不会重复回答。也不作废——群里一直有人说话时，
        # 作废会让机器人的回复永远发不出去。
        self._schedule_turn(msg, self_name, source)
