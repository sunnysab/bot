"""
编写时参考了以下资料：
1. https://github.com/xaoyaoo/PyWxDump/issues/101
2. https://blog.csdn.net/sepnineth/article/details/129789196
3. https://www.cnblogs.com/RainbowTechnology/p/18535596
4. https://www.javaedit.com/archives/230
5. https://chat.deepseek.com 提供了强有力的帮助.
"""

from xml.etree import ElementTree as et


def _body(xml_data: str) -> str:
    """ 截到第一个 `<`，去掉文档声明，没有 `<msg>` 外壳就自己包一个。

    daemon 有时在 XML 前拼一行 'wxid_xxx:'（`strip_prefix` 干的事），出站的引用
    还没了 `<msg>` 外壳——直接 et.fromstring 会报 `junk after document element`。

    **不做全文档反转义**。以前这里把 `&lt;` 全部还原成 `<` 再给内嵌的 `<?xml ?>`
    补 CDATA，理由是「daemon 有时不转义」——实测 69 条引用里 69 条都是转义的，
    而那次反转义会把**合法**的文档弄坏：被引内容里的声明只要不在 `<content>` 紧后面
    （比如前面还带个 'wxid_x:' 前缀），就变成「声明不在文档开头」，整条引用退化成
    `[引用消息]`。
    """
    start = xml_data.find('<')
    body = xml_data[start:] if start > 0 else xml_data
    if body.startswith('<?xml'):
        body = body[body.find('?>') + 2:]
    if not body.lstrip().startswith('<msg'):
        body = f'<msg>{body}</msg>'
    return body


def _text(root: et.Element, path: str) -> str:
    """ 取一个节点的文本，没有就返回空串（引用里的字段本来就常有缺的）。 """
    node = root.find(path)
    return (node.text or '').strip() if node is not None else ''


def _int_or(text: str, default: int = 0) -> int:
    """ 取一个数字字段，缺了或不是数字就返回默认值。 """
    try:
        return int(text)
    except ValueError:
        return default


def parse_reference_message(xml_data: str) -> dict:
    """ 解析引用消息，返回 `{content, sender, referred_message, referred_message_sender,
    referred_message_sender_name, referred_message_time, referred_message_type,
    referred_message_id}`。

    `referred_message_sender_name` / `referred_message_time` 是**被引那条**的说话人昵称和
    时间（引用滑出窗口后，渲染时要拿它补上「谁、什么时候说的」）。缺字段一律空串 / 0。

    `referred_message` 是**原文**：被引的那条可能是图片 / 表情 / 链接 / 又一串引用，
    这时它是那段消息的 XML。渲染成人话是 `media.render_referred` 的事（解析器不碰
    `media`，两边不互相依赖）。
    """
    root = et.fromstring(_body(xml_data))

    sub_type = _text(root, './/type')
    # 拿到的 XML 不是引用：这是 daemon 给的输入不对，不是内部不变式被破坏，
    # 所以抛 ValueError（`media.parse_appmsg` 捕 Exception 退回 `[引用消息]`）。
    # 用 assert 的话 `python -O` 下这层校验直接消失，接着读 `refermsg/chatusr`
    # 会以「NoneType 没有 text」的名义报一个和真实原因无关的错。
    if sub_type != '57':
        raise ValueError(f'不是引用消息（appmsg type={sub_type or "?"}）')

    return {
        'content': _text(root, './/title'),
        'sender': _text(root, './/fromusername'),
        # 被引的那条：优先级 = refermsg 的正文 > 它的 title（链接/文件才只有 title）
        'referred_message': _text(root, './/refermsg/content') or _text(root, './/refermsg//title'),
        'referred_message_sender': _text(root, './/refermsg/chatusr'),
        # 被引那条的说话人昵称 / 时间。引用滑出窗口后就只剩正文能看了，这两样是给
        # 模型补上「谁、什么时候说的」用的——refermsg 里现成带着，不用回 daemon 取
        'referred_message_sender_name': _text(root, './/refermsg/displayname'),
        'referred_message_time': _int_or(_text(root, './/refermsg/createtime')),
        # 被引那条的 msg_type：图片 3 / 表情 47 / 链接 49 / 文本 1 …… 渲染时要它
        'referred_message_type': int(_text(root, './/refermsg/type') or 0),
        # 被引那条的服务端 id（svrId）。面板点引用块跳过去就靠它。
        # daemon 的 `ref_svrid` 是权威来源，这里是 XML 里还留着 svrid 的历史数据兜底
        'referred_message_id': _text(root, './/refermsg/svrid'),
    }


if __name__ == '__main__':
    sample = ('<msg><appmsg><title>直接用这个点进去对比</title><type>57</type>'
              '<fromusername>wxid_sender</fromusername><refermsg><type>49</type>'
              '<svrid>3159424424184903022</svrid><fromusr>46086622803@chatroom</fromusr>'
              '<chatusr>wxid_kf7zbyjjxs8r22</chatusr><displayname>zzq</displayname>'
              '<content>&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;和我一起测测&lt;/title&gt;'
              '&lt;/appmsg&gt;&lt;/msg&gt;</content></refermsg></appmsg></msg>')
    parsed = parse_reference_message(sample)
    assert parsed['content'] == '直接用这个点进去对比', parsed
    assert parsed['sender'] == 'wxid_sender', parsed
    assert parsed['referred_message_sender'] == 'wxid_kf7zbyjjxs8r22', parsed
    assert parsed['referred_message_sender_name'] == 'zzq', parsed
    assert parsed['referred_message_time'] == 0, parsed          # 这条没带 createtime
    assert parsed['referred_message'].startswith('<msg><appmsg><title>和我一起测测'), parsed
    assert parsed['referred_message_type'] == 49, parsed
    assert parsed['referred_message_id'] == '3159424424184903022', parsed

    # daemon 在正文前拼了 sender 前缀的场景
    assert parse_reference_message('wxid_abc:\n' + sample)['content'] == '直接用这个点进去对比'

    # 出站引用没有 <msg> 外壳，<appmsg> 后面还跟着 <fromusername>——以前直接 ParseError，
    # 后台就只显示 `[引用消息]`（改成包一层 <msg> 再解析）
    outbound = ('<appmsg appid="" sdkver="0"><title>在呢，看你们聊呢</title><type>57</type>'
                '<refermsg><type>1</type><svrid>4443715993374741156</svrid>'
                '<fromusr>47870419781@chatroom</fromusr><chatusr>wxid_kf7zbyjjxs8r22</chatusr>'
                '<displayname>zzq</displayname><content>@雨墨蝉烟 咋不说话了</content>'
                '<createtime>1789868031</createtime></refermsg></appmsg>'
                '<fromusername>wxid_6aob7w8khxa322</fromusername>')
    parsed = parse_reference_message(outbound)
    assert parsed['content'] == '在呢，看你们聊呢', parsed
    assert parsed['referred_message_id'] == '4443715993374741156', parsed
    assert parsed['referred_message'] == '@雨墨蝉烟 咋不说话了', parsed
    assert parsed['referred_message_sender'] == 'wxid_kf7zbyjjxs8r22', parsed
    assert parsed['referred_message_sender_name'] == 'zzq', parsed
    assert parsed['referred_message_time'] == 1789868031, parsed
    # 带 XML 声明的也要能解析
    assert parse_reference_message('<?xml version="1.0"?>' + outbound)['content'] == '在呢，看你们聊呢'

    # 被引的是图片 / 表情：refermsg 的正文是它的 XML，没有 <title>。
    # 以前这里 `root.find('.//title').text` 直接 AttributeError，整条引用变 `[引用消息]`
    image_quote = ('<msg><appmsg><title>这也太好看了</title><type>57</type>'
                   '<refermsg><type>3</type><chatusr>wxid_bob</chatusr>'
                   '<content>&lt;?xml version="1.0"?&gt;&lt;msg&gt;&lt;img aeskey="0bda33" '
                   'md5="2f8f1e" /&gt;&lt;/msg&gt;</content></refermsg></appmsg>'
                   '<fromusername>wxid_alice</fromusername></msg>')
    parsed = parse_reference_message(image_quote)
    assert parsed['content'] == '这也太好看了', parsed
    assert parsed['referred_message_type'] == 3, parsed
    assert parsed['referred_message'].startswith('<?xml'), parsed

    # 被引内容里带 'wxid_x:' 前缀 + XML 声明（真实样本）：外层文档是合法的，
    # 只要别去整体反转义就不会炸
    article_quote = ('<msg><appmsg><title>但是光到机场就要两小时了</title><type>57</type>'
                     '<refermsg><type>49</type><chatusr>wxid_bob</chatusr><content>wxid_bob:\n'
                     '&lt;?xml version="1.0"?&gt;\n&lt;msg&gt;&lt;appmsg&gt;'
                     '&lt;title&gt;北京飞上海仅需半小时&lt;/title&gt;&lt;type&gt;5&lt;/type&gt;'
                     '&lt;/appmsg&gt;&lt;/msg&gt;</content></refermsg></appmsg>'
                     '<fromusername>wxid_alice</fromusername></msg>')
    parsed = parse_reference_message(article_quote)
    assert parsed['referred_message_type'] == 49, parsed
    assert parsed['referred_message'].startswith('wxid_bob:'), parsed
    assert '北京飞上海仅需半小时' in parsed['referred_message'], parsed

    # 缺字段的引用不能把整条消息带崩：字段取空串就行
    bare = parse_reference_message('<msg><appmsg><title>嗯</title><type>57</type></appmsg></msg>')
    assert bare['referred_message'] == '' and bare['referred_message_type'] == 0, bare
    # 昵称 / 时间缺了就是空串 / 0，不能把整条引用带崩
    assert bare['referred_message_sender_name'] == '' and bare['referred_message_time'] == 0, bare
    # 没有 svrid 时给空串：面板拿到空串就不做跳转，不能给它 None
    assert bare['referred_message_id'] == '', bare
    # createtime 不是数字也不能炸
    bad_time = parse_reference_message('<msg><appmsg><title>嗯</title><type>57</type>'
                                       '<refermsg><createtime>不是数字</createtime>'
                                       '</refermsg></appmsg></msg>')
    assert bad_time['referred_message_time'] == 0, bad_time

    # 不是引用：抛 ValueError（不是 AssertionError——`python -O` 下 assert 会消失）
    try:
        parse_reference_message('<msg><appmsg><title>文章</title><type>5</type></appmsg></msg>')
    except ValueError as e:
        assert 'type=5' in str(e), e
    else:
        raise AssertionError('非引用消息应该抛 ValueError')
    print('ok')
