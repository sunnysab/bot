""" 微信媒体消息（图片 / 表情）XML 载荷解析。

表情消息（msg_type=47）的 desc 属性是 base64 编码的 protobuf，结构为 { locale: 描述 }，
locale 形如 zh_cn / zh_tw / default。实测能解出「摊倒」「啧啧」「哇」这类中文含义，无需调模型。
"""

import base64
import re


def _strip_prefix(content: str) -> str:
    """ relay 有时在 XML 前拼一行 'wxid_xxx:'，截到第一个 '<'。 """
    start = content.find('<')
    return content[start:] if start > 0 else content


def _attr(xml: str, name: str) -> str:
    m = re.search(rf'{name}\s*=\s*"(.*?)"', xml)
    return m.group(1) if m else ''


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7


def _pb_fields(buf: bytes) -> list[bytes]:
    """ 极简 protobuf：返回顶层所有 length-delimited 字段的原始字节。 """
    fields: list[bytes] = []
    i = 0
    while i < len(buf):
        tag, i = _varint(buf, i)
        wire = tag & 7
        if wire == 2:
            length, i = _varint(buf, i)
            fields.append(buf[i:i + length])
            i += length
        elif wire == 0:
            _, i = _varint(buf, i)
        else:  # desc 里用不到其它 wire type
            break
    return fields


def parse_emoji_description(content: str) -> str | None:
    """ 取表情消息的中文描述，如 '摊倒'。取不到返回 None。 """
    if '<emoji' not in content:
        return None
    desc = _attr(_strip_prefix(content), 'desc')
    if not desc:
        return None
    try:
        raw = base64.b64decode(desc + '=' * (-len(desc) % 4))
        entries = _pb_fields(raw)
        key_values = _pb_fields(entries[0]) if entries else []
        text = key_values[1].decode('utf-8') if len(key_values) > 1 else ''
    except Exception:
        return None
    return text or None


def parse_image_md5(content: str) -> str | None:
    """ 图片消息 XML 的 md5，可作去重 / 缓存键。 """
    if '<img' not in content:
        return None
    return _attr(_strip_prefix(content), 'md5') or None


if __name__ == '__main__':
    # 自检：真实 relay 数据的 desc（含多语言条目），以及各种解析失败的兜底
    assert parse_emoji_description('<msg><emoji desc="ChEKB2RlZmF1bHQSBuWVp+WVpw==" /></msg>') == '啧啧'
    assert parse_emoji_description('<msg><emoji desc="ChEKB2RlZmF1bHQSBuaRiuWAkg==" /></msg>') == '摊倒'
    assert parse_emoji_description(
        '<msg><emoji desc="CgwKBXpoX2NuEgPlk4cKCQoFemhfdHcSAAoLCgdkZWZhdWx0EgA=" /></msg>') == '哇'
    assert parse_emoji_description('<msg><emoji desc="" /></msg>') is None
    assert parse_emoji_description('<msg><emoji /></msg>') is None
    assert parse_emoji_description('not xml') is None
    assert parse_emoji_description('<msg><img md5="abc" /></msg>') is None
    assert parse_image_md5('wxid_x:\n<msg><img md5="f9bfba97" /></msg>') == 'f9bfba97'
    assert parse_image_md5('<msg><emoji md5="x" /></msg>') is None
    print('ok')
