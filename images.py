""" 内联给模型的图片：按体积压一下，再按消息 id 落盘复用。

微信原图动辄几 MB，原样 base64 塞进请求会把一轮撑爆。这里只做一件事：超过阈值的图压到
目标体积以下，**优先保分辨率**——菜单、截图里的字缩掉就再也认不回来了，所以先降 JPEG
质量，实在不行才缩边。

压缩结果按媒体 svrid 落盘：同一张图只在第一次下载 + 压缩，之后每轮直接读文件。
"""

import io
from pathlib import Path

from loguru import logger

SHRINK_THRESHOLD = 2 * 1024 * 1024   # 超过这么大才压
SHRINK_TARGET = 1024 * 1024          # 压到这个体积以下
MIN_EDGE = 1024                      # 缩边缩到这里还超标，就发最低质量的结果


class ImageStore:
    """ 媒体的压缩 + 落盘缓存。一条消息的媒体只会被同一个 key 写一次，不需要加锁。 """

    def __init__(self, path: str | Path, threshold: int = SHRINK_THRESHOLD,
                 target: int = SHRINK_TARGET):
        self.path = Path(path)
        self.threshold = threshold
        self.target = target
        try:
            self.path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f'图片缓存目录建不了，图片每轮重压: {e}')

    def prepare(self, key: int, raw: bytes) -> bytes:
        """ key = 媒体的 daemon svrid。命中缓存直接返回，否则压一下落盘。 """
        cached = self.get(key)
        if cached is not None:
            return cached
        out = shrink(raw, self.threshold, self.target)
        self.put(key, out)
        return out

    def get(self, key: int) -> bytes | None:
        try:
            return self._file(key).read_bytes()
        except OSError:
            return None

    def put(self, key: int, raw: bytes) -> None:
        try:
            self._file(key).write_bytes(raw)
        except OSError as e:
            logger.debug(f'图片缓存写失败 key={key}: {e}')

    def _file(self, key: int) -> Path:
        return self.path / str(key)


def shrink(raw: bytes, threshold: int = SHRINK_THRESHOLD, target: int = SHRINK_TARGET) -> bytes:
    """ 压到 target 以下；压不动 / 没装 Pillow 就原样返回（宁可费点 token，别丢图）。 """
    if len(raw) <= threshold:
        return raw
    try:
        from PIL import Image
    except ImportError:
        logger.warning('没装 Pillow，图片不压缩直接发')
        return raw
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as e:  # noqa: BLE001 —— 解不开就原样发
        logger.warning(f'图片解不开，原样发: {e}')
        return raw
    if image.mode not in ('RGB', 'L'):
        image = image.convert('RGB')

    best = raw
    for quality in (85, 75, 65, 55):     # 先保分辨率降质量：可读性靠分辨率
        out = _jpeg(image, quality)
        if out is None:
            return best
        best = out
        if len(out) <= target:
            return out
    while max(image.size) > MIN_EDGE:    # 还大才缩边
        image = image.resize((max(1, image.width * 4 // 5), max(1, image.height * 4 // 5)),
                             Image.Resampling.LANCZOS)
        for quality in (80, 70, 60):
            out = _jpeg(image, quality)
            if out is None:
                return best
            best = out
            if len(out) <= target:
                return out
    return best


def _jpeg(image, quality: int) -> bytes | None:
    buf = io.BytesIO()
    try:
        image.save(buf, format='JPEG', quality=quality, optimize=True)
    except OSError as e:
        logger.warning(f'JPEG 编码失败: {e}')
        return None
    return buf.getvalue()


if __name__ == '__main__':
    import os
    import tempfile

    from PIL import Image

    # 一张 >2MB 的噪声图（照片型的极端情况）：压完必须是 JPEG 且落到目标以内
    width, height = 2000, 1500
    noise = Image.frombytes('RGB', (width, height), os.urandom(width * height * 3))
    buf = io.BytesIO()
    noise.save(buf, format='PNG')
    raw = buf.getvalue()
    assert len(raw) > SHRINK_THRESHOLD, len(raw)
    out = shrink(raw)
    assert out[:2] == b'\xff\xd8', out[:8]          # JPEG
    assert len(out) <= SHRINK_TARGET, len(out)

    # 小图不动它（阈值以内原样返回）
    small = b'\x89PNG\r\n\x1a\n' + b'x' * 100
    assert shrink(small) == small
    assert shrink(b'not an image' * 200_000, threshold=1, target=1)  # 解不开也不抛

    # 落盘缓存：写过的读回来，没写过的返回 None
    with tempfile.TemporaryDirectory() as tmp:
        store = ImageStore(tmp)
        store.put(123, b'abc')
        assert store.get(123) == b'abc' and store.get(999) is None
        assert store.prepare(7, small) == small
        assert store.get(7) == small
    print('ok')
