import asyncio
import base64
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

from loguru import logger
from weixin_bot import Message, WeChatBot, WeChatBotError

from context import SYSTEM_KINDS, ChatWindow, MessageKind, message_handle
from emojis import Emoji
from media import parse_reference_media, render

# 内存里最多留这么多附件字节 / 这么多条：识图只在「刚取到」和「马上要用」之间复用，
# 留多了只是白占内存（附件的长期保管是 daemon 的事）
MEDIA_MEMO_BYTES = 8 * 1024 * 1024
MEDIA_MEMO_ITEMS = 16

# 拉联系人的去重窗口（秒）。daemon 已经登录时，`on_ready` 和 `login_status` 两个事件
# 几乎同时到达，拉两遍只是白花一次 RPC（日志里就是两条一模一样的 Hello）
CONTACTS_DEDUPE_SECONDS = 5.0


class Wechat:
    """ 微信机器人组件（weixin-rs daemon 适配层） """

    AsyncMessageCb = Callable[[Message], Coroutine[Any, Any, None]]

    def __init__(self, host: str, port: int, callback: AsyncMessageCb = None, dry_run: bool = False,
                 unix_path: str = None):
        logger.info('starting robot...')
        self._bot = WeChatBot(host=host, port=port, unix_path=unix_path or None)
        self.dry_run = dry_run
        # 联系人字典，键值对格式: {'wxid': '显示名'}
        self.all_contacts: dict[str, str] = {}
        self._contacts_at = 0.0        # 上次真拉到联系人的时刻，用来去重（见 CONTACTS_DEDUPE_SECONDS）
        # 按需查到的名字（群名、群里的人）；只记成功的结果，查不到就下次再查
        self._names: dict[str, str] = {}
        # 取过的附件（msg_id -> 字节/扩展名）+ 还在路上的合并请求，见 media_bytes_by_id
        self._media_memo: OrderedDict[int, tuple[bytes, str]] = OrderedDict()
        self._media_inflight: dict[int, asyncio.Task] = {}
        self._media_memo_bytes = 0
        self._message_callback = callback or self._on_message
        # 登录成功（首次 / 扫码重登）后的回调，见 on_login
        self._login_handlers: list[Callable[[], Coroutine[Any, Any, None]]] = []

        self._bot.on_message(self._dispatch)
        self._bot.on_ready(self._refresh_contacts)
        self._bot.on_event('login_status')(self._on_login_status)

    @property
    def wxid(self) -> str:
        return self._bot.wxid or ''

    @property
    def nickname(self) -> str:
        return self._bot.nickname or ''

    def on_ready(self, func) -> None:
        """ 注册连接就绪（含重连）后的回调 """
        self._bot.on_ready(func)

    def on_login(self, func: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """ 注册「登录成功」后的回调。

        `on_ready` 在**未登录**时也会触发（daemon 未登录 / 会话过期都能连上，但 RPC 一律
        401），所以拉联系人和加载历史都不能挂在它下面——daemon 激活会话时会广播
        `login_status`，那才是数据查得到的时刻。
        """
        self._login_handlers.append(func)

    async def _on_login_status(self, data: dict) -> None:
        if not data.get('logged_in'):
            logger.warning('daemon 已注销，等重新登录')
            return
        await self._refresh_contacts(self._bot)
        for handler in self._login_handlers:
            try:
                await handler()
            except Exception as e:  # noqa: BLE001 —— 一个回调坏了不能连累后面的
                logger.error(f'登录后处理失败: {e}', exc_info=True)

    async def start(self) -> None:
        """ 连接 daemon 并持续接收消息，断线自动重连 """
        await self._bot.start()

    async def connect(self) -> None:
        """ 只连上、不跑收消息循环（离线工具用；用完 `stop()`）。 """
        await self._bot.connect()

    async def stop(self) -> None:
        await self._bot.close()

    async def _refresh_contacts(self, _bot: WeChatBot) -> None:
        """ 连接就绪 / 登录成功后拉联系人。

        **未登录时 daemon 会拒（401 session expired）**——这在启动时是常态，不是故障，
        扫码之后 `login_status` 会再走一次这里。

        两个事件在「启动时已登录」这条路径上相隔一两毫秒到达（on_ready 先到），所以有个
        很短的窗口去重：只花一次 RPC，日志里也只留一行 Hello。失败不锁窗口，下次事件立刻重试。
        """
        if time.monotonic() - self._contacts_at < CONTACTS_DEDUPE_SECONDS:
            return
        self._contacts_at = time.monotonic()
        try:
            contacts = await self._bot.get_all_contacts(force_refresh=True)
        except WeChatBotError as e:
            self._contacts_at = 0.0
            logger.warning(f'daemon 尚未登录，先不拉联系人（{e}）')
            return
        self.all_contacts = {c.username: c.name for c in contacts}
        logger.info(f'Hello, {self.nickname}! ({len(self.all_contacts)} contacts)')

    async def _dispatch(self, msg: Message) -> None:
        """ 分发消息到回调函数。载荷渲染统一交给上层 PayloadProcessor。 """
        logger.debug(msg)

        kind = MessageKind.of(msg.msg_type)
        # 好友请求 / 设备同步 / 系统消息丢掉；但拍一拍也走 10002（`<sysmsg type="pat">`），
        # 它是社交信号，得让它进历史——否则模型永远不知道自己被谁拍了
        if kind in SYSTEM_KINDS and msg.pat is None:
            return
        if kind is MessageKind.OTHER:
            logger.warning(f'Unknown message type: {msg.msg_type}')
            return
        await self._message_callback(msg)

    @staticmethod
    async def _on_message(msg: Message) -> None:
        """ 默认消息处理函数 """
        logger.info(f'{msg.sender}: {msg.content}')

    async def send_text(self, msg: str, receiver: str, at_list: str = '', ref_svrid: int | None = None) -> dict:
        """ 发送消息
        :param msg: 消息字符串
        :param receiver: 接收人wxid或者群id
        :param at_list: 要@的wxid, 多个用逗号分隔; @所有人的wxid为：notify@all
        :param ref_svrid: 引用回复的目标（被引消息的 64 位 svrId）；不传就是普通消息
        """
        if self.dry_run:
            logger.info(f'[DRY RUN] To {receiver}: {msg}')
            return {'sent': False, 'dry_run': True}

        at_users: list[str] = []
        at_names: list[str] = []
        ats = ''
        if at_list:
            if at_list == 'notify@all':  # @所有人
                at_users = ['notify@all']
                ats = ' @所有人'
            else:
                for wxid in filter(None, at_list.split(',')):
                    name = await self.get_display_name(wxid, receiver)
                    at_users.append(wxid)
                    at_names.append(name)
                    ats += f' @{name}'

        text = f'{ats}\n\n{msg}' if ats else msg
        logger.info(f'To {receiver}: {text}')
        # 返回值里带 daemon 给的 msg_id（微信服务端消息 ID）。管理后台靠它把
        # 「排队中 → 已发送」贴到 daemon 库里的那条出站记录上，不能丢
        return await self._bot.send_text(to=receiver, text=text, at_users=at_users, at_names=at_names,
                                         ref_svrid=ref_svrid)

    async def search_emoji(self, keyword: str, limit: int = 10) -> list[Emoji]:
        """ 按关键词搜表情。返回的已经是可直接发送的完整元数据。 """
        found = await self._bot.search_emoji(keyword)
        emojis = [Emoji.from_dict(item.to_dict() if hasattr(item, 'to_dict') else item) for item in found]
        return [item for item in emojis if item.sendable][:limit]

    async def send_emoji(self, emoji: Emoji, receiver: str) -> dict:
        """ 发一张表情。返回值带 daemon 的 msg_id，理由同 send_text。 """
        if not emoji.sendable:
            logger.warning(f'表情缺下载地址，发不出去：{emoji.md5}')
            return {'sent': False}
        if self.dry_run:
            logger.info(f'[DRY RUN] To {receiver}: 表情「{emoji.desc or emoji.md5}」')
            return {'sent': False, 'dry_run': True}
        logger.info(f'To {receiver}: 表情「{emoji.desc or emoji.md5}」')
        return await self._bot.send_emoji(to=receiver, emoji=emoji.to_daemon())

    async def send_pat(self, receiver: str, target: str = '') -> dict:
        """ 拍一拍。群聊要 `target`（被拍成员 wxid）；私聊留空即拍对方。 """
        if self.dry_run:
            logger.info(f'[DRY RUN] To {receiver}: 拍了拍 {target or receiver}')
            return {'sent': False, 'dry_run': True}
        logger.info(f'To {receiver}: 拍了拍 {target or receiver}')
        return await self._bot.send_pat(receiver, target or None)

    async def revoke(self, to: str, msg_id: int) -> dict:
        """ 撤回自己发出的一条消息。

        msg_id 是 send_text / send_emoji 回执里的那个。daemon 只认**本进程发过、
        2 分钟内**的消息（超时它用 410 快失败），别人的和手机上发的撤不了。
        """
        if self.dry_run:
            logger.info(f'[DRY RUN] Revoke {msg_id} in {to}')
            return {'sent': False, 'dry_run': True}
        logger.info(f'Revoke {msg_id} in {to}')
        return await self._bot.revoke(to=to, msg_id=msg_id)

    def is_session(self, peer: str) -> bool:
        """ 这个 peer 值不值得当会话：私聊（`wxid_`）或群（`@chatroom`）。

        **自己的 wxid 不算**。daemon 的 device-sync（msg_type=51，from/to 都是自己）
        会被 `list_conversations` 按 peer 分组成一个假会话，而 `get_messages` 的
        contact 过滤是 `from_user=? OR to_user=?`——点进去就是**全部**发给 bot 的消息。
        公众号 / filehelper / weixin 也一并排掉，会话的定义只写在这一处。
        """
        return bool(peer) and peer != self.wxid \
            and (peer.startswith('wxid_') or peer.endswith('@chatroom'))

    async def conversations(self, limit: int = 20):
        """ 管理后台用：会话列表（带 display_name / last_message / msg_count）。 """
        return [c for c in await self._bot.get_conversations(limit=limit)
                if self.is_session(c.peer)]

    async def status(self) -> dict:
        """ daemon 的连接 / 登录状态。管理后台的健康卡用。 """
        return await self._bot.get_status()

    async def login_qr(self):
        """ 扫码登录第一步：取二维码（`LoginQR`，png 为空时用 url 自行渲染）。

        不要求已有登录态——daemon 启动时就可以是未登录的。
        """
        return await self._bot.login_qr()

    async def login_qr_poll(self) -> dict:
        """ 轮询一次扫码状态：waiting / logged_in / cancelled。 """
        return await self._bot.login_qr_poll()

    async def login_qr_cancel(self) -> dict:
        """ 放弃扫码：释放 daemon 的登录锁，让新的登录能开始。 """
        return await self._bot.login_qr_cancel()

    async def logout(self) -> dict:
        """ 退出登录：服务端注销 + 停长链 + 清本地会话材料（凭据保留，可再扫）。 """
        return await self._bot.logout()

    async def media_bytes_by_id(self, msg_id: int) -> tuple[bytes, str]:
        """ 取一条消息的附件字节，返回 (data, ext)。**同一个 msg_id 只问 daemon 一次**。

        附件由 daemon 下载并保管（它认得 CDN blob，也有自己的磁盘缓存），bot 只按
        msg_id 要字节——不关心它缓存在哪个目录、叫什么名字，更不该去扒它的文件。

        自己去重的原因：同一张图会被两条路各要一次——当前轮把它内联给模型看，
        后台给它补一条写进历史的描述——两边几乎同时发生。daemon 那边同一张图确实只下
        一次（`fetch_image_original` 先查缓存再拿锁），但省掉的是两次往返、
        以及一次几百 KB 的字节搬运。

        失败不进缓存：网络抖动、daemon 刚重启都会失败，下次再试一次就好。
        """
        hit = self._media_memo.get(msg_id)
        if hit is not None:
            self._media_memo.move_to_end(msg_id)
            return hit
        task = self._media_inflight.get(msg_id)
        if task is None:
            task = asyncio.ensure_future(self._fetch_media(msg_id))
            self._media_inflight[msg_id] = task
            task.add_done_callback(lambda done, key=msg_id: self._finish_media(key, done))
        return await task

    async def _fetch_media(self, msg_id: int) -> tuple[bytes, str]:
        """ 真正问 daemon 要一次字节：分块下载流，扩展名从元数据的 filename 里取。 """
        stream = await self._bot.open_download(msg_id=msg_id)
        meta = await stream.open()
        data = b''.join([chunk async for chunk in stream.chunks()])
        if not data:
            raise ValueError(f'daemon 没返回附件内容（msg_id={msg_id}）')
        return data, Path(str(meta.get('filename') or '')).suffix.lstrip('.').lower()

    def _finish_media(self, msg_id: int, task: asyncio.Task) -> None:
        """ 下载结束：成功就留在内存里（有上限），失败什么都不记。 """
        self._media_inflight.pop(msg_id, None)
        if task.cancelled() or task.exception() is not None:
            return
        self._media_memo[msg_id] = task.result()
        self._media_memo_bytes += len(task.result()[0])
        while len(self._media_memo) > MEDIA_MEMO_ITEMS or self._media_memo_bytes > MEDIA_MEMO_BYTES:
            _, (dropped, _) = self._media_memo.popitem(last=False)
            self._media_memo_bytes -= len(dropped)

    async def media_bytes(self, msg: Message) -> bytes:
        """ 取一条消息附件的原始字节（识图用）。

        走 SDK 的分块下载流（`PKT_DL_OPEN`，见 `_fetch_media`）。曾经这里绕开它去调
        普通 RPC，因为当时 SDK 只往 DL_OPEN 塞 params、daemon 按 `DaemonRequest`
        （action + params）解包，解不出就被 `unwrap_or_default()` 吞掉，最后报
        400「缺少 msg_id」；那个 RPC 后来也随 base64 一起被 daemon 删了（现在只剩
        `download_media` 给元数据）。SDK 现在发的是完整的 `{action, params}` 信封。

        图片现在拿到的是**原图**：入站图片的 XML 里只有缩略图 blob（实测 376/376
        `cdnmidimgurl == cdnthumburl`），daemon 会先走 getmsgimg 取原图，取不到
        （服务端已回收，`-352`）才回退缩略图。
        """
        raw, _ = await self.media_bytes_by_id(msg.msg_id)
        return raw

    def link_media(self, msg: Message, media) -> tuple[int, Callable[[], Awaitable[bytes]] | None]:
        """ 这条消息要内联给模型的媒体从哪来，返回 `(media_id, download)`。

        自己有图就是它自己；自己是个引用、被引的那条是图，就跟到 `ref_svrid` 上去。
        引用的图即使原消息已经滑出窗口也能取——字节按 svrid 问 daemon，不依赖原记录还在。
        缺描述的表情才算值（有标签没必要再附图），和 `Media.describable` 一个口径。
        """
        if media is not None and media.describable:
            return msg.msg_id, (lambda m=msg: self.media_bytes(m))
        if MessageKind.of(msg.msg_type) is MessageKind.APPMSG and msg.ref_svrid:
            quoted = parse_reference_media(msg.content)
            if quoted is not None and quoted.describable:
                async def fetch(rid=msg.ref_svrid) -> bytes:
                    raw, _ = await self.media_bytes_by_id(rid)
                    return raw
                return msg.ref_svrid, fetch
        return 0, None

    async def voice_text(self, msg: Message, timeout: int = 20) -> str:
        """ 语音转文字。

        附件和转写都在 daemon 做（转写要把 SILK 上传到微信服务端再轮询，bot 做不了），
        daemon 用 `{wxid}_voice_{msg_id}` 当 voiceId，所以同一条消息重复请求能命中结果。
        拿不到文本就返回空串——调用方保留占位符就行，别让转写拖垮消息链。
        """
        result = await self._bot.transcribe_voice(msg.msg_id, timeout=timeout)
        return str((result or {}).get('text') or '')

    async def messages(self, peer: str, limit: int = 300, exclude_system: bool = True,
                       before_id: int = 0, after_id: int = 0):
        """ 管理后台用：某个会话的历史。

        直接返回 daemon 的原始消息，不走 fetch_history 那层历史记录封装——
        面板要 msg_id 才能把状态贴上去，也要原始 XML 才能渲染媒体。
        翻页游标是 daemon 的 `messages.id`（不是 64 位的 msg_id）：`before_id` 往早翻，
        `after_id` 往新翻。
        """
        return await self._bot.get_messages(limit=limit, contact=peer, reverse=True,
                                           exclude_system=exclude_system,
                                           before_id=before_id or None, after_id=after_id or None)

    def iter_media(self, peer: str, types: tuple[int, ...] = (3, 47),
                   since: int | None = None) -> AsyncIterator[Message]:
        """ 倒着遍历一个会话里的媒体消息（图片 / 表情）。离线补描述用。

        走 daemon 的分页游标，不限制条数；媒体消息本来就不多（这个群一周 1400 条）。
        """
        return self._bot.iter_history(page_size=200, contact=peer, msg_type=list(types),
                                      since=since, exclude_system=True)

    async def sender_names(self, chat: str, senders) -> dict[str, str]:
        """ 把 wxid 批量解析成显示名（群昵称 > 备注）。

        daemon 的 `sender_display_name` 查不到时会**拿 wxid 当名字**返回，那不是名字。
        bot 自己的消息处理链会退到 `get_contact` 再查一次（见 get_display_name），
        面板走的是 daemon 原始消息，没经过那道，所以要自己补。
        实测某群里 278 条有 130 条的发送者是 wxid，但只有 4 个不同的人——
        解析一遍就都好了，而且查到的名字会缓存。
        """
        names = {}
        for sender in dict.fromkeys(senders):
            if sender:
                names[sender] = await self.get_display_name(sender, chat)
        return names

    async def get_recent_sessions(self, count: int = 10) -> list[str]:
        """ 获取最近的聊天会话 """
        # 公众号推送会占满会话列表，多取一些再过滤掉
        conversations = await self._bot.get_conversations(limit=max(count * 5, 50))
        return [c.peer for c in conversations if self.is_session(c.peer)][:count]

    async def get_display_name(self, wxid: str, chatroom: str = '') -> str:
        """ 获取联系人的显示名称. 对于群友，优先使用本地的备注，其次使用群昵称，最后使用微信昵称

        注意 daemon 查不到群成员时会**拿 wxid 当名字**返回，那不是名字，得接着往下找。
        """
        if chatroom == wxid:  # 非群聊
            chatroom = ''

        if wxid == self.wxid:
            return self.nickname or wxid

        key = f'{chatroom}|{wxid}' if chatroom else wxid
        if key in self._names:
            return self._names[key]

        name = ''
        if chatroom and wxid:
            try:
                member = await self._bot.get_chatroom_member(chatroom, wxid)
                if member.name and member.name != wxid:
                    name = member.name
            except Exception as e:
                logger.debug(f'failed to get chatroom member {wxid} in {chatroom}: {e}')

        if not name:
            name = await self._resolve_contact(wxid)

        if name:
            self._names[key] = name
        return name or wxid

    async def find_member(self, chatroom: str, name: str) -> str:
        """ 按显示名在群里找人，返回 wxid；找不到或不止一个候选就返回空串。

        @ 错人比不 @ 更难看，所以只做精确匹配（群昵称 / 备注 / 昵称）和唯一的子串匹配。
        """
        if not name or not chatroom.endswith('@chatroom'):
            return ''
        try:
            members = (await self._bot.get_chatroom(chatroom)).members
        except Exception as e:
            logger.warning(f'取群成员失败（{chatroom}）: {e}')
            return ''
        # daemon 的成员表里 nickname/group_nickname 实测全是空的（只有 wxid），
        # 真名得走 get_display_name（get_chatroom_member → get_contact），
        # 也就是 transcript 里显示名字的那条路——不然模型照着历史 @ 谁都查不到。
        # ponytail: 首次在某个群 @ 是 O(人数) 次 RPC（174 人的群约两百毫秒），
        # 解析结果落 _names 缓存，之后查人和渲染历史都免查。要再快得让 daemon
        # 的 get_chatroom 直接带上 display_name。
        aliases = [(m.wxid, (m.display_name, await self.get_display_name(m.wxid, chatroom),
                             m.group_nickname, m.nickname))
                   for m in members]
        exact = [wxid for wxid, names in aliases if name in names]
        if exact:
            return exact[0]
        loose = [wxid for wxid, names in aliases if any(name in alias for alias in names)]
        return loose[0] if len(loose) == 1 else ''

    async def _resolve_contact(self, wxid: str) -> str:
        """ 从联系人表查显示名，查不到就现查一次。

        群名也走这条路：`get_all_contacts` 不含群，得单独 `get_contact` 才拿得到。
        """
        if wxid in self.all_contacts:
            return self.all_contacts[wxid]
        try:
            for contact in await self._bot.get_contact(wxid):
                if contact.username == wxid and contact.name:
                    self.all_contacts[wxid] = contact.name
                    return contact.name
        except Exception as e:
            logger.debug(f'failed to resolve contact {wxid}: {e}')
        return ''

    async def message_cursor(self, contact: str, svrid: int) -> int:
        """ 按服务端 msg_id 反查会话内的翻页游标（daemon 的 `messages.id`）。

        面板手里只有引用块带的 64 位 svrId，而翻页要的是另一个空间的 cursor，
        所以得问一次。本地库里没有这条（引用的是早就滚出缓存的消息）返回 0，
        调用方据此退回「只展开被引全文」。
        """
        try:
            data = await self._bot.call('resolve_message', {'contact': contact, 'svrid': int(svrid)})
        except Exception as e:  # noqa: BLE001 —— 404 也走这里，查不到不是故障
            logger.debug(f'resolve message cursor failed: {e}')
            return 0
        return int((data or {}).get('cursor') or 0)

    async def avatar(self, wxid: str) -> tuple[bytes, str]:
        """ 头像原始字节 + 扩展名。

        下载和缓存都在 daemon（`get_headimg`：先读盘、没有再打 batchgetheadimg），
        bot 只按 wxid 要——和附件一个分工，不去扒它的缓存目录。
        服务端没有这张图就抛：调用方（面板）把它变成 404，浏览器退回首字色块。
        """
        data = await self._bot.call('get_headimg', {'users': [wxid]})
        images = (data or {}).get('images') or []
        if not images:
            raise ValueError(f'{wxid} 没有头像')
        img = images[0]
        return base64.b64decode(img['data_base64']), str(img.get('ext') or 'jpg')

    async def fetch_history(self, wxid: str, count: int = 50, query: str = '') -> ChatWindow:
        """ 获取群或联系人的聊天记录

        :param query: 内容关键字，给了就只取命中的（daemon 做 LIKE 匹配），
            同一条件的排序和条数含义不变：最多 count 条、最新在前

        返回一个列表。每个元素为一个元组，格式为 (发送者名称, 消息内容, 创建时间)
        """
        messages = await self._bot.get_messages(limit=count, contact=wxid, reverse=True,
                                                query=query or None, exclude_system=True)
        return await self.window_from(messages)

    async def window_from(self, messages, names: dict[str, str] | None = None) -> ChatWindow:
        """ daemon 原始消息 → 上下文窗口。fetch_history 和「手动回复这条」共用这一套渲染。

        :param names: `{wxid: 显示名}`，补 daemon 没解析出来的发送者。daemon 对不在联系人表
            里的人会直接把 wxid 当名字返回，现取的窗口得自己补一次
        """
        result = ChatWindow()
        for msg in messages:
            if names and msg.sender in names:
                msg.sender_display_name = names[msg.sender]
            handle = message_handle(msg.msg_id)
            quoted = message_handle(msg.ref_svrid) if msg.ref_svrid else ''
            # 被引那条已经在这个窗口里（按时间序在它前面），注解就只留短标签
            content, media = render(msg.msg_type, msg.content, msg.text,
                                     handle=handle, quoted_handle=quoted,
                                     quoted_known=result.shows(msg.ref_svrid))
            # 不知道为什么有的消息还是 xml 格式的，这里直接过滤掉吧
            if not content or content.startswith('<'):
                continue
            media_id, download = self.link_media(msg, media)
            result.append(msg.sender_name, content, msg.create_time, media=media, download=download,
                          is_self=msg.is_self, msg_id=msg.msg_id, media_id=media_id,
                          handle=message_handle(media_id) if media_id else '')

        result.sort()
        return result
