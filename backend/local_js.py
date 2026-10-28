"""Serialize the native V8 runtime: concurrent first initialization can crash Windows."""
from threading import Lock

_lock = Lock()


def call_local_script(script, function, *args, timeout=10000):
    import py_mini_racer
    from backend.replay_models import ReplayDataError
    try:
        with _lock, py_mini_racer.MiniRacer() as decoder:
            decoder.eval(script, timeout=timeout)
            return decoder.call(function, *args, timeout=timeout)
    except Exception as exc:
        raise ReplayDataError("行情资料解码失败或超时，未使用未经校验的数据") from exc
