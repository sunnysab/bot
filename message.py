"""
编写时参考了以下资料：
1. https://github.com/xaoyaoo/PyWxDump/issues/101
2. https://blog.csdn.net/sepnineth/article/details/129789196
3. https://www.cnblogs.com/RainbowTechnology/p/18535596
4. https://www.javaedit.com/archives/230
5. https://chat.deepseek.com 提供了强有力的帮助.
"""

from xml.etree import ElementTree as et


def fix_xml(xml: str) -> str:
    """修复 XML 格式"""
    replacement = [('&lt;', '<'), ('&gt;', '>'),
                   ('<content><?xml version="1.0"?>', '<content><![CDATA[<?xml version="1.0"?>'),
                   ('</msg>\n</content>', '</msg>]]>\n</content>')]
    for old, new in replacement:
        xml = xml.replace(old, new)
    return xml


def parse_reference_message(xml_data: str) -> dict:
    """解析引用消息。daemon 有时会在 XML 前拼一行 'wxid_xxx:'，直接截到第一个 '<'。

    形状不止一种，都要收：
    - 入站的引用带 `<msg>` 外壳（`<msg><appmsg>…</appmsg><fromusername>…</fromusername></msg>`）；
    - **出站**（daemon 落到缓存里的）没有外壳，`<appmsg>` 后面直接跟 `</appmsg><fromusername>`——
      直接 et.fromstring 会报 `junk after document element`，整个引用于是退化成 `[引用消息]`。
    """
    start = xml_data.find('<')
    xml_data = fix_xml(xml_data[start:] if start > 0 else xml_data)
    # 去掉 XML 声明，再确认有 <msg> 外壳；没有就自己包一个，让 appmsg/fromusername
    # 都变成它的子节点
    body = xml_data
    if body.startswith('<?xml'):
        body = body[body.find('?>') + 2:]
    if not body.lstrip().startswith('<msg'):
        body = f'<msg>{body}</msg>'
    root = et.fromstring(body)

    message_sub_type = root.find('.//type').text
    assert message_sub_type == '57', '消息类型不是引用消息'

    content = root.find('.//title').text
    sender = root.find('.//fromusername').text
    referred_message_sender = root.find('.//refermsg/chatusr').text
    referred_message = root.find('.//refermsg/content').text or root.find('.//refermsg//title').text or ''
    if referred_message.startswith('<'):
        referred_content_root = et.fromstring(referred_message)
        referred_message = referred_content_root.find('.//title').text

    return {
        'content': content,
        'sender': sender,
        'referred_message': referred_message,
        'referred_message_sender': referred_message_sender
    }


if __name__ == '__main__':
    # 自检：真实引用消息里 <content> 是二次转义的，需要先解一层再取内层 <title>
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
    assert parsed['referred_message'] == '和我一起测测', parsed

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
    assert parsed['referred_message'] == '@雨墨蝉烟 咋不说话了', parsed
    assert parsed['referred_message_sender'] == 'wxid_kf7zbyjjxs8r22', parsed
    # 带 XML 声明的也要能解析
    assert parse_reference_message('<?xml version="1.0"?>' + outbound)['content'] == '在呢，看你们聊呢'
    print('ok')
