"""
编写时参考了以下资料：
1. https://github.com/xaoyaoo/PyWxDump/issues/101
2. https://blog.csdn.net/sepnineth/article/details/129789196
3. https://www.cnblogs.com/RainbowTechnology/p/18535596
4. https://www.javaedit.com/archives/230
5. https://chat.deepseek.com 提供了强有力的帮助.
"""

import base64
from typing import Any, Dict
from xml.etree import ElementTree as ET

from proto.bytes_extra_pb2 import BytesExtra


def decode_bytes_extra(data: str | bytes) -> Dict[str, Any]:
    """解码 MSGi.db 中 msg 表的扩展字节数据"""
    # 解码 Base64
    if isinstance(data, str):  # 如果是字符串
        binary: bytes = base64.b64decode(data)
    elif isinstance(data, bytes):  # 如果是字节串
        binary = data

    # 解析 Protobuf
    message = BytesExtra()
    try:
        message.ParseFromString(binary)  # 直接解析到 message 对象中
    except Exception as e:
        raise ValueError('解析 Protobuf 数据失败') from e

    # 提取字段
    attrs: Dict[str, Any] = {}

    # 遍历 flags 字段（假设 flags 是 repeated 类型）
    for flag_entry in message.flags:
        # 根据你的 .proto 定义调整字段名（例如 flag_entry.enum_code → flag_entry.key）
        attrs[str(flag_entry.enum_code)] = flag_entry.value

    # 遍历 strings 字段（假设 strings 是 repeated 类型）
    for string_entry in message.strings:
        # 根据你的 .proto 定义调整字段名（例如 string_entry.enum_code → string_entry.key）
        attrs[str(string_entry.enum_code)] = string_entry.value

    return attrs


def decode_sender_name(data: str | bytes) -> str:
    """解码 BytesExtra 中发送者 wxid """
    return decode_bytes_extra(data)['1']


def decode_compress_content(data: str | bytes) -> str:
    """解码 CompressContent 字段 """
    import lz4.block as lb

    # 解码 Base64
    if isinstance(data, str):  # 如果是字符串
        binary: bytes = base64.b64decode(data)
    elif isinstance(data, bytes):  # 如果是字节串
        binary = data
    else:
        raise ValueError('data must be str or bytes')

    content = lb.decompress(binary, uncompressed_size=len(binary) << 10)
    return content.decode('utf-8').replace('\x00', '')  # 去掉字符串中的空字符


def fix_xml(xml: str) -> str:
    """修复 XML 格式"""
    replacement = [('&lt;', '<'), ('&gt;', '>'), ('&amp;', '&'),
                   ('<content><?xml version="1.0"?>', '<content><![CDATA[<?xml version="1.0"?>'),
                   ('</msg>\n</content>', '</msg>]]>\n</content>')]
    for old, new in replacement:
        xml = xml.replace(old, new)
    return xml


def parse_reference_message(xml_data: str) -> dict:
    """解析引用消息"""
    xml_data = fix_xml(xml_data)
    root = ET.fromstring(xml_data)

    message_sub_type = root.find('.//type').text
    assert message_sub_type == '57', '消息类型不是引用消息'

    content = root.find('.//title').text
    sender = root.find('.//fromusername').text
    referred_message_sender = root.find('.//refermsg/chatusr').text
    referred_message = root.find('.//refermsg/content').text or root.find('.//refermsg//title').text or ''
    if referred_message.startswith('<'):
        referred_content_root = ET.fromstring(referred_message)
        referred_message = referred_content_root.find('.//title').text

    return {
        'content': content,
        'sender': sender,
        'referred_message': referred_message,
        'referred_message_sender': referred_message_sender
    }


def decode_reference_message(xml_data: str | bytes) -> dict:
    """解码引用消息"""
    content = decode_compress_content(xml_data)
    return parse_reference_message(content)


if __name__ == '__main__':
    from pprint import pprint

    message = 'CgQIEBAAGhcIARITd3hpZF9rZjd6YnlqanhzOHIyMhqBAwgHEvwCPG1zZ3NvdXJjZT4KICAgIDxhdHVzZXJsaXN0PgogICAgICAgIDwhW0NEQVRBWyx3eGlkX21zODBpbHk1Nnk0bjIxXV0+CiAgICA8L2F0dXNlcmxpc3Q+CiAgICA8cHVhPjE8L3B1YT4KICAgIDxzaWxlbmNlPjE8L3NpbGVuY2U+CiAgICA8bWVtYmVyY291bnQ+ODwvbWVtYmVyY291bnQ+CiAgICA8c2lnbmF0dXJlPlYxX3gyUHNvSnVDfHYxX3gyUHNvSnVDPC9zaWduYXR1cmU+CiAgICA8dG1wX25vZGU+CiAgICAgICAgPHB1Ymxpc2hlci1pZCAvPgogICAgPC90bXBfbm9kZT4KICAgIDxzZWNfbXNnX25vZGU+CiAgICAgICAgPGFsbm9kZT4KICAgICAgICAgICAgPGZyPjE8L2ZyPgogICAgICAgIDwvYWxub2RlPgogICAgPC9zZWNfbXNnX25vZGU+CjwvbXNnc291cmNlPgoaJAgCEiBhODlkYTMwMzE5ZGVmYWY0OWUyMjhhYWI5ZjU5NmU5NA=='
    data = decode_bytes_extra(message)
    pprint(data)

    compressed = '8js8P3htbCB2ZXJzaW9uPSIxLjAiPz4KPG1zZz4KCTxhcHBtc2cgYXBwaWQ9IiIgc2RrdmVyPSIwIj4KCQk8dGl0bGU+cHJvdG88Lw0AABcAUWRlcyAvIQDTYWN0aW9uPnZpZXc8Lw0AACIAkXR5cGU+NTc8LwkAABIAQXNob3cNADUwPC8MAAAZAHNjb250ZW50UQAzdXJsCgBGZGF0YQ4AOWxvdw0ACh4Ao3JlY29yZGl0ZW09AFZ0aHVtYjEAcm1lc3NhZ2WhAAMjAHNsYW5pbmZvDgBScmVmZXIVAQD4AAC/ABYxvgDwCwk8c3ZyaWQ+Mjk1OTkzMzgxMTYyODQwMTE2zgABGwABOAD0D2Zyb211c3I+MjU1Njk4ODY5ODVAY2hhdHJvb208Lx4AASsAABcAACsA9AZ3eGlkX21zODBpbHk1Nnk0bjIxPC8dAAIqAPcHcmVhdGV0aW1lPjE3MzgyMzcyOTI8LxcAAScA+BFkaXNwbGF5bmFtZT7lvLrlk6XlpKflj7fnsonkuJ08LyAAATEA5W1zZ3NvdXJjZT4mbHQ7DgCQJmd0OwogICAgFgBWYWxub2QTAAACAAAXACBmchMAEDELABIvDAAFMgAbLzMAACQAUnNpbGVuWgACNAAHEQAFOQCwbWVtYmVyY291bnRdABA4OQAbLxUABS0AgXNpZ25hdHVymwDWVjFfNVdkYXdybnB8dgwAAUEACSkABT8AT3RtcF/ZAALgcHVibGlzaGVyLWlkIC+IAAU0AB0vNQAAYQB/c2VjX21zZ04AAwsLAQQCAAA4AAI2AQIOAQIMAAFpAAACAAAfAA9GAQEdL2wAASoACrkBFjwQAAChAwVfA5M+5LiA5qC3PC9vAwAdACg8L/YCRzxleHQRAwIbAkB1c2VyUgIDJgMCFQAHSgIDGAB2Y29tbWVudHEDkGFwcGF0dGFjaGgA5Qk8dG90YWxsZW4+MDwvDAABhQICJgAAbQECKgDoZW1vdGljb25tZDU+PC8OAAEvAGNmaWxlZXgtBLMJPGFlc2tleT48LwkAAdUACXwAQDx3ZWKeBGJzaGFyZWRkAAXlARVJdwAFEwBQUmVxSWSnAAsSAAFaAA1MADA8d2U7BTNuZm9aAHRhZ2VwYXRoVwAKPwEBFwEG6wCkYXBwc2VydmljZRgFCxIAA3QAB2IAAM4AUnNlYXJjYgABMgACtQQSPF0EAq4BAlUE5GtmN3pieWpqeHM4cjIyfwQDIgByCgk8c2Nlbo8FAQkAEQqSAAACAgDMABM8MgZEPjE8LwsAAJAANWFwcBACAX8AAzEACwMCgDwvbXNnPgoA'
    data = decode_reference_message(compressed)
    pprint(data)
