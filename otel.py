"""OpenTelemetry 引导与埋点小工具。

为什么不用自动插桩解决一切：bot 的语义层（门控 / 上下文组装 / 工具循环 / 出站排队）
只有它自己知道，自动插桩只能给 HTTP、DB 这类基础设施噪音。所以业务层手动埋点，
**LLM 那一层交给 `opentelemetry-instrumentation-openai-v2` 自动产出 `gen_ai.*`**
（属性名比手写全，而且会跟着语义约定更新）。

两条硬规矩：

1. **观测坏了绝不能影响聊天。** 没配 endpoint、缺依赖、导出失败——一律降级成 no-op，
   所有埋点函数在没有初始化时都是空操作，不抛异常。
2. **span 名必须低基数。** SigNoz 的 operation 列表按 `span.name` 聚合，
   把 chat_id / 模型名 / msg_id 拼进名字会把它炸掉——那些动态值一律走 attribute。

手里没有 provider 时不要调 `trace.set_tracer_provider`：那是全进程一次性的，
重复调会被静默忽略，配置就再也改不过来了。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from loguru import logger

SERVICE_NAME = 'wechat-bot'
_TRACER_NAME = 'wechat-bot'

_provider = None
_tracer = None
_enabled = False


def boot(endpoint: str, service_version: str = '') -> bool:
    """初始化 TracerProvider。endpoint 为空 = 关闭（埋点全部变 no-op）。

    必须在 `asyncio.run()` 之前、`openai` 客户端构造之前调：OpenAI 插桩要 patch
    已经加载的类，晚一步就只对新建的客户端生效。

    返回是否真的开了。
    """
    global _provider, _tracer, _enabled
    if _enabled:
        return True
    if not endpoint:
        logger.info('未配 otel_endpoint，链路追踪关闭')
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        logger.warning(f'配了 otel_endpoint 但没装 opentelemetry 依赖，链路追踪关闭：{e}')
        return False

    resource = Resource.create({
        'service.name': SERVICE_NAME,
        'service.version': service_version or os.getenv('GIT_SHA', 'dev'),
        'deployment.environment': os.getenv('OTEL_ENV', 'prod'),
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=endpoint, insecure=True),
        # 默认 5s 太慢：这是交互式 bot，想看的是「刚刚那一轮」
        schedule_delay_millis=2000))
    trace.set_tracer_provider(provider)

    _provider = provider
    _tracer = trace.get_tracer(_TRACER_NAME)
    _enabled = True
    logger.info(f'链路追踪已开启：{endpoint}')
    # 不挂 OpenAI 自动插桩：当前环境的 openai（3.13）走 httpx2，插桩 patch 的是旧
    # httpx，打不到。模型调用的 gen_ai.* span 在 ai.py 里手动埋。
    return True


def enabled() -> bool:
    return _enabled


@contextmanager
def span(name: str, attrs: dict[str, Any] | None = None, *,
         parent: Any = None, new_trace: bool = False) -> Iterator[Any]:
    """开一段 span；没初始化就 yield None。

    :param parent: 父 span 的 SpanContext。出站这种「决策在此、发送在几秒后」的场景
        靠它把两条异步路径接起来——不能指望 contextvars，那几个秒早跨过去了
    :param new_trace: 显式开一条新 trace（清空继承来的 context）。常驻后台任务里
        每轮都该用它，否则第一轮之后的 span 会全挂在任务创建时那个陈旧 parent 上
    """
    if not _enabled or _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name, context=_parent_context(parent, new_trace)) as current:
        if attrs:
            mark(attrs)
        yield current


def mark(attrs: dict[str, Any]) -> None:
    """往当前 span 塞属性。不在 span 里 / 没初始化就什么都不做。 """
    if not _enabled:
        return
    try:
        from opentelemetry import trace

        current = trace.get_current_span()
        if not current.is_recording():
            return
        for key, value in attrs.items():
            if value is not None:
                current.set_attribute(key, value)
    except Exception as e:  # noqa: BLE001 —— 观测出问题不能连累聊天
        logger.debug(f'写 span 属性失败：{e}')


def fail(reason: str) -> None:
    """把这次失败标在当前 span 上。失败率要看得到「为什么」，不只是「失败了」。 """
    if not _enabled:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode

        current = trace.get_current_span()
        if current.is_recording():
            current.set_status(Status(StatusCode.ERROR, reason))
    except Exception as e:  # noqa: BLE001
        logger.debug(f'标记 span 失败失败：{e}')


def capture() -> Any:
    """当前 span 的 SpanContext，交给 `span(parent=...)` 用。没有就 None。 """
    if not _enabled:
        return None
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        return context if context.is_valid else None
    except Exception:  # noqa: BLE001
        return None


def trace_id() -> str:
    """当前 trace 的 32 位 hex；没有就空串。回复轨迹靠它跟 span 树对上。 """
    if not _enabled:
        return ''
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        return format(context.trace_id, '032x') if context.is_valid else ''
    except Exception:  # noqa: BLE001
        return ''


def context_from_traceparent(traceparent: str):
    """ 把 W3C traceparent 还原成 SpanContext，交给 `span(parent=...)`；没有 / 解不了就 None。

    用来接上 daemon 推这条消息时所在的那条 trace（见 weixin-cli 的 current_traceparent）。
    """
    if not _enabled or not traceparent:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        context = TraceContextTextMapPropagator().extract({'traceparent': traceparent})
        span_context = trace.get_current_span(context).get_span_context()
        return span_context if span_context.is_valid else None
    except Exception:  # noqa: BLE001
        return None


def traceparent() -> str:
    """ 当前 span 的 W3C traceparent（`00-<trace32>-<span16>-<flags>`）；没有就空串。

    给 RPC 用：塞进请求信封，daemon 那边 extract 出来当 parent，一条 trace 才能
    从 bot 穿进 daemon。没有活跃 span 时返回空串，调用方就不带这个字段。
    """
    if not _enabled:
        return ''
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if not context.is_valid:
            return ''
        return f'00-{context.trace_id:032x}-{context.span_id:016x}-{int(context.trace_flags):02x}'
    except Exception:  # noqa: BLE001
        return ''


def shutdown() -> None:
    """关停前把队列里的 span 刷出去。

    BatchSpanProcessor 跑在后台线程，进程被 systemd 停掉时队列里最后几秒的 span
    会直接丢——这是自建 trace 最常见的「数据缺失」来源，别省这个调用。
    """
    if _provider is None:
        return
    try:
        _provider.force_flush()
        _provider.shutdown()
    except Exception as e:  # noqa: BLE001
        logger.warning(f'刷新链路追踪失败：{e}')


def _parent_context(parent: Any, new_trace: bool):
    if new_trace:
        from opentelemetry.context import Context

        return Context()
    if parent is None:
        return None
    from opentelemetry.trace import NonRecordingSpan, set_span_in_context

    return set_span_in_context(NonRecordingSpan(parent))


def _self_check() -> None:
    # 没初始化：所有函数都是 no-op，不抛异常、不返回脏值
    assert enabled() is False
    assert trace_id() == '' and capture() is None and traceparent() == ''
    with span('x', {'a': 1}) as current:
        assert current is None
    mark({'a': 1})
    fail('boom')
    shutdown()

    # 空 endpoint = 明确关闭
    assert boot('') is False
    assert enabled() is False

    # 显式父 span context 和新 trace 的构造不能炸（这里只测不会抛）
    assert _parent_context(None, False) is None
    assert _parent_context(None, True) is not None
    print('otel self-check ok')


if __name__ == '__main__':
    _self_check()
