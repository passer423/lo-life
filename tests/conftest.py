"""测试隔离：**绝不能碰到真实 data/**。

两条都是实测换来的：

1. **数据目录**：在导入 app.* **之前**就把 ``DATA_DIR`` 指到 ``tests/.data``，
   并断言单例确实指向那里。上游曾经有测试往真实查询日志灌了几十条，
   把拒答率和延迟分位当场搞坏，症状还伪装成「p50 延迟 0.0 ms」。

2. **临时目录**：刻意不用 pytest 的 ``tmp_path`` / ``--basetemp``。pytest 建
   临时目录时用 0o700，在 Windows 受限沙箱里连 ``iterdir`` 都会 WinError 5
   （连会话收尾的 ``cleanup_dead_symlinks`` 都会崩）。用 ``Path.mkdir()``（0o777）
   自己建就没问题——所以有了下面这个 ``workdir`` fixture。
"""

from __future__ import annotations

import itertools
import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# 每个 pytest 进程一个自己的数据目录。
#
# 早先这里是固定的 tests/.data：一个进程跑测试没问题，但**两个进程同时跑**就会互相
# 删库——症状很迷惑：一半用例报 "no such table: users"，另一半报
# "PermissionError: [WinError 32]"，单独跑却全绿。原因是彼此的 data_dir 夹具
# 把对方的 app.db 删掉了。加上 pid 之后，两边各跑各的，互不相干。
_RUN_ID = f"pid{os.getpid()}"
TEST_DATA = ROOT / "tests" / ".data" / _RUN_ID
WORK_TMP = ROOT / "tests" / ".tmp" / _RUN_ID

_counter = itertools.count(1)


def _prepare() -> None:
    for path in (TEST_DATA, WORK_TMP):
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True, exist_ok=True)
    # 顺序关键：app.config 在导入期就构造了 settings 单例，之后再设环境变量无效。
    os.environ["DATA_DIR"] = str(TEST_DATA)
    os.environ["LLM_API_KEY"] = ""          # 强制抽取式兜底：测试永不调用真模型
    os.environ["QUERY_LOG_ENABLED"] = "false"


_prepare()


@pytest.fixture()
def workdir() -> Path:
    """每个用例一个干净的自建临时目录。"""
    path = WORK_TMP / f"case{next(_counter):04d}"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def data_dir():
    """每个用例一个**全新目录**，而不是"删旧库再来一次"。

    早先的做法是删掉 app.db + WAL 后复用同一路径。两个坑都真实踩过：

    1. Windows 上只要还有任何一个连接没放开（TestClient 的线程池、上一个用例的
       残留），``unlink`` 就是 ``WinError 32``，整个用例记成 error；
    2. 更糟：主库删了但 ``-wal`` 还在时，sqlite 会把旧快照"恢复"回来，症状是
       ``no such table: users`` —— 一个从源码里根本查不到源头的假象。

    换目录没有这类问题：不删任何东西，也就不会被别人占着。
    """
    from app.config import settings

    original = settings.data_dir
    unique = TEST_DATA / f"data{next(_counter):04d}"
    unique.mkdir(parents=True, exist_ok=True)
    settings.data_dir = unique
    settings.ensure_dirs()
    try:
        yield unique
    finally:
        settings.data_dir = original


@pytest.fixture()
def client(data_dir):
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    # ``/media`` 是 StaticFiles，目录在**导入期**就绑定了。data_dir 每个用例换目录，
    # 所以这里把挂载点重绑到当前 media_dir；否则上传到新目录的文件取不回来
    # （StaticFiles 每次请求都读 self.directory，改这个属性是有效的）。
    for route in app.routes:
        if getattr(route, "name", "") == "media" and hasattr(route, "app"):
            route.app.directory = str(settings.media_dir)

    with TestClient(app) as test_client:
        yield test_client
