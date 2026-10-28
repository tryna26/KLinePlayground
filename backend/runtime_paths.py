"""Resolve writable storage separately from bundled, read-only application files."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    home: Path
    data: Path
    users: Path


def get_runtime_paths() -> RuntimePaths:
    if getattr(sys, "frozen", False):
        # The installed executable lives in <installation>/bin.
        default_home = Path(sys.executable).resolve().parent.parent
    else:
        default_home = Path(__file__).resolve().parent.parent / ".runtime"
    home = Path(os.environ.get("KLINE_TRAINER_HOME") or default_home).expanduser().resolve()
    data = Path(os.environ.get("KLINE_TRAINER_DATA_DIR") or home / "data").expanduser().resolve()
    users = Path(os.environ.get("KLINE_TRAINER_USERS_DIR") or home / "users").expanduser().resolve()
    if data == users or data in users.parents or users in data.parents:
        raise ValueError("行情目录与用户目录必须彼此独立，不能相同或互相包含")
    return RuntimePaths(home, data, users)
