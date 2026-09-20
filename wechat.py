import asyncio
import base64
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from loguru import logger
from weixin_bot import Message, WeChatBot

from context import SYSTEM_KINDS, ChatWindow, MessageKind
from emojis import Emoji
from media import render

# 内存里最多留这么多附件字节 / 这么多条：识图只在「刚取到」和「马上要用」之间复用，
# 留多了只是白占内存（附件的长期保管是 daemon 的事）
MEDIA_MEMO_BYTES = 8 * 1024 * 1024
MEDIA_MEMO_ITEMS = 16


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
        # 按需查到的名字（群名、群里的人）；只记成功的结果，查不到就下次再查
        self._names: dict[str, str] = {}
        # 取过的附件（msg_id -> 字节/扩展名）+ 还在路上的合并请求，见 media_bytes_by_id
        self._media_memo: OrderedDict[int, tuple[bytes, str]] = OrderedDict()
        self._media_inflight: dict[int, asyncio.Task] = {}
        self._media_memo_bytes = 0
        self._message_callback = callback or self._on_message

        self._bot.on_message(self._dispatch)
        self._bot.on_ready(self._refresh_contacts)

    @property
    def wxid(self) -> str:
        return self._bot.wxid or ''

    @property
    def nickname(self) -> str:
        return self._bot.nickname or ''

    def on_ready(self, func) -> None:
        """ 注册连接就绪（含重连）后的回调 """
        self._bot.on_ready(func)

    async def start(self) -> None:
        """ 连接 daemon 并持续接收消息，断线自动重连 """
        await self._bot.start()

    async def connect(self) -> None:
        """ 只连上、不跑收消息循环（离线工具用；用完 `stop()`）。 """
        await self._bot.connect()

    async def stop(self) -> None:
        await self._bot.close()

    async def _refresh_contacts(self, _bot: WeChatBot) -> None:
        contacts = await self._bot.get_all_contacts(force_refresh=True)
        self.all_contacts = {c.username: c.name for c in contacts}
        logger.info(f'Hello, {self.nickname}! ({len(self.all_contacts)} contacts)')

    async def _dispatch(self, msg: Message) -> None:
        """ 分发消息到回调函数。载荷渲染统一交给上层 PayloadProcessor。 """
        logger.debug(msg)

        kind = MessageKind.of(msg.msg_type)
        if kind in SYSTEM_KINDS:  # 好友请求 / 设备同步 / 系统消息
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

    async def media_bytes_by_id(self, msg_id: int) -> tuple[bytes, str]:
        """ 取一条消息的附件字节，返回 (data, ext)。**同一个 msg_id 只问 daemon 一次**。

        附件由 daemon 下载并保管（它认得 CDN blob，也有自己的磁盘缓存），bot 只按
        msg_id 要字节——不关心它缓存在哪个目录、叫什么名字，更不该去扒它的文件。

        自己去重的原因：同一张图会被两条路各要一次——当前轮把它内联给模型看，
        后台给它补一条写进历史的描述——两边几乎同时发生，而 daemon 的 CDN 回退
        **不持锁**（它自己标了 ponytail），并发过去就是两次 CDN 下载。

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
        """ 真正问 daemon 要一次字节。 """
        data = await self._bot.call('get_media_bytes', {'msg_id': msg_id})
        payload = data if isinstance(data, dict) else {}
        encoded = payload.get('data_base64')
        if not encoded:
            raise ValueError(f'daemon 没返回附件内容（msg_id={msg_id}）')
        return base64.b64decode(encoded), str(payload.get('ext') or '')

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

        **不走 SDK 的 `msg.get_media_bytes()`**：那条路走 PKT_DL_OPEN 分块流，
        而 daemon 的 DL_OPEN 分支按 `DaemonRequest`（action + params）解包，SDK 只塞了
        params，解不出就被 `unwrap_or_default()` 吞掉，最后报 400「缺少 msg_id」——
        报错和真实原因（图片的 XML 里没有 cdnurl）完全无关，查半天。

        这里走普通 RPC 的 `get_media_bytes`（形状是对的），拿 base64 再解回来，
        失败时的报错就是 daemon 的原始报错。
        """
        raw, _ = await self.media_bytes_by_id(msg.msg_id)
        return raw

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
        exact = [m.wxid for m in members
                 if name in (m.display_name, m.group_nickname, m.nickname)]
        if exact:
            return exact[0]
        loose = [m.wxid for m in members if name in m.name]
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

    async def fetch_history(self, wxid: str, count: int = 50, query: str = '') -> ChatWindow:
        """ 获取群或联系人的聊天记录

        :param query: 内容关键字，给了就只取命中的（daemon 做 LIKE 匹配），
            同一条件的排序和条数含义不变：最多 count 条、最新在前

        返回一个列表。每个元素为一个元组，格式为 (发送者名称, 消息内容, 创建时间)
        """
        messages = await self._bot.get_messages(limit=count, contact=wxid, reverse=True,
                                                query=query or None, exclude_system=True)

        result = ChatWindow()
        for msg in messages:
            content, media = render(msg.msg_type, msg.content, msg.text)
            # 不知道为什么有的消息还是 xml 格式的，这里直接过滤掉吧
            if not content or content.startswith('<'):
                continue
            download = (lambda m=msg: self.media_bytes(m)) if (media and media.describable) else None
            result.append(msg.sender_name, content, msg.create_time, media=media, download=download,
                          is_self=msg.is_self)

        result.sort()
        return result
