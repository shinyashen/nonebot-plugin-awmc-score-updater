import os
from pathlib import Path

import pytest
import nonebot
from pytest_asyncio import is_async_test
from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

if Path(".env.dev").exists():
    os.environ["ENVIRONMENT"] = "dev"
else:
    os.environ["ENVIRONMENT"] = "test"


def pytest_collection_modifyitems(items: list[pytest.Item]):
    pytest_asyncio_tests = (item for item in items if is_async_test(item))
    session_scope_marker = pytest.mark.asyncio(loop_scope="session")
    for async_test in pytest_asyncio_tests:
        async_test.add_marker(session_scope_marker, append=False)


@pytest.fixture(scope="session", autouse=True)
async def after_nonebot_init(after_nonebot_init: None):
    # 加载适配器
    driver = nonebot.get_driver()
    driver.register_adapter(OnebotV11Adapter)

    # 加载插件（[tool.nonebot]：本插件 require 主插件自动加载）
    nonebot.load_from_toml("pyproject.toml")


@pytest.fixture(autouse=True)
async def _bypass_song_ensure_loaded(monkeypatch):
    """曲库就绪等待的测试替身：无预热环境会死等 _ready。

    已注入曲库（seed_service）的用例：等待立即放行并返回真曲库（过滤
    逻辑仍走真实数据）；未注入的用例 1 秒超时后返回 None 放行。
    """
    import asyncio

    from nonebot_plugin_awmc_helper.core.songs import song_service
    from nonebot_plugin_awmc_helper.core.client import client

    async def fake_ensure():
        try:
            await asyncio.wait_for(song_service._ready.wait(), timeout=1)
        except asyncio.TimeoutError:
            return None
        return await client.songs()

    monkeypatch.setattr(song_service, "ensure_loaded", fake_ensure)


@pytest.fixture(scope="session", autouse=True)
async def _isolate_store_db(after_nonebot_init, tmp_path_factory, worker_id):
    """两仓 db 钉到 per-worker 唯一临时文件并预建表（CI xdist 建表 race 修复）。

    背景：两插件都有 ``on_startup`` 建表钩子（主插件 init_db、本插件
    init_store），随 nonebug lifespan 在**每个测试**触发；``_db_file`` 为
    None 时打生产 localstore 路径，且 SQLModel 全局 metadata（两仓 store.py
    共知的边界）使任一仓 create_all 把另一仓已加载模型一并建出——CI xdist
    多 worker 并发对同一 SQLite 文件建表，check 与 CREATE 交错即
    ``table already exists``（2026-09-28 CI 实测，本地从未复现：本地生产
    路径早有开发残留库，测试直接踩在上面）。

    会话开始（插件模型注册完毕后）即对本 worker 独占的文件建表：跨 worker
    路径互异消除并发，同 worker 内顺序执行本就无竞争；后续各 fixture 的
    ``set_db_file`` 切换/回落都以 :class:`SimpleNamespace` 路径为准，不再
    置 None（置 None = 落回生产路径 = race 回归）。
    """
    from types import SimpleNamespace

    from nonebot_plugin_awmc_helper.core import store as awmc_store

    from nonebot_plugin_awmc_score_updater import store as su_store

    base = tmp_path_factory.mktemp(f"awmc-db-{worker_id}")
    iso = SimpleNamespace(su=base / "su.db", awmc=base / "awmc.db")
    awmc_store.set_db_file(iso.awmc)
    await awmc_store.init_db()
    su_store.set_db_file(iso.su)
    await su_store.init_store()
    yield iso
    su_store.set_db_file(None)
    awmc_store.set_db_file(None)
