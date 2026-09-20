""" 引用消息解析：入站带 `<msg>` 外壳，出站的落库形状没有。 """

import unittest

from message import parse_reference_message

# 入站：完整外壳，<content> 里的内层 XML 被二次转义
INBOUND = ('<msg><appmsg><title>直接用这个点进去对比</title><type>57</type>'
           '<fromusername>wxid_sender</fromusername><refermsg><type>49</type>'
           '<svrid>3159424424184903022</svrid><fromusr>46086622803@chatroom</fromusr>'
           '<chatusr>wxid_kf7zbyjjxs8r22</chatusr><displayname>zzq</displayname>'
           '<content>&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;和我一起测测&lt;/title&gt;'
           '&lt;/appmsg&gt;&lt;/msg&gt;</content></refermsg></appmsg></msg>')

# 出站：daemon 按「发送形状」落库——没有 <msg> 外壳，<fromusername> 是第二个根节点
OUTBOUND = ('<appmsg appid="" sdkver="0"><title>在呢，看你们聊呢</title><type>57</type>'
            '<refermsg><type>1</type><svrid>4443715993374741156</svrid>'
            '<fromusr>47870419781@chatroom</fromusr><chatusr>wxid_kf7zbyjjxs8r22</chatusr>'
            '<displayname>zzq</displayname><content>@雨墨蝉烟 咋不说话了</content>'
            '<createtime>1789868031</createtime></refermsg></appmsg>'
            '<fromusername>wxid_6aob7w8khxa322</fromusername>')


class TestReferenceMessage(unittest.TestCase):
    def test_inbound_shape(self):
        parsed = parse_reference_message(INBOUND)
        self.assertEqual(parsed['content'], '直接用这个点进去对比')
        self.assertEqual(parsed['referred_message'], '和我一起测测')

    def test_outbound_shape_without_msg_wrapper(self):
        """ 回归：以前直接 et.fromstring 会报 junk after document element，
        整个引用退化成 `[引用消息]`（后台看到的就是那四个字）。 """
        parsed = parse_reference_message(OUTBOUND)
        self.assertEqual(parsed['content'], '在呢，看你们聊呢')
        self.assertEqual(parsed['referred_message'], '@雨墨蝉烟 咋不说话了')
        self.assertEqual(parsed['referred_message_sender'], 'wxid_kf7zbyjjxs8r22')

    def test_leading_xml_declaration_and_sender_prefix(self):
        self.assertEqual(parse_reference_message('<?xml version="1.0"?>' + OUTBOUND)['content'],
                         '在呢，看你们聊呢')
        self.assertEqual(parse_reference_message('wxid_abc:\n' + INBOUND)['content'],
                         '直接用这个点进去对比')

    def test_not_a_quote_raises(self):
        with self.assertRaises(ValueError):
            parse_reference_message('<msg><appmsg><title>文章</title><type>5</type></appmsg></msg>')


if __name__ == '__main__':
    unittest.main()
