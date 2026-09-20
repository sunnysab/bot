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
    """解析引用消息。daemon 有时会在 XML 前拼一行 'wxid_xxx:'，直接截到第一个 '<'。"""
    start = xml_data.find('<')
    xml_data = fix_xml(xml_data[start:] if start > 0 else xml_data)
    root = et.fromstring(xml_data)

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
    print('ok')
