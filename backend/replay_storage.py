"""Atomic, checksummed cache files; separate from legacy single-stock caches."""

import hashlib
import json
import os
from pathlib import Path
import tempfile

from backend.replay_models import ReplayDataError, SCHEMA_VERSION


def content_hash(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def write_cache(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": SCHEMA_VERSION, "sha256": content_hash(payload), "payload": payload}
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=".replay-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_cache(path: Path):
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document["schema_version"] != SCHEMA_VERSION or document["sha256"] != content_hash(document["payload"]):
            raise ReplayDataError("组合数据缓存版本或校验值无效，需要重新获取")
        return document["payload"]
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise ReplayDataError("组合数据缓存损坏或版本不受支持，需要重新获取") from exc
