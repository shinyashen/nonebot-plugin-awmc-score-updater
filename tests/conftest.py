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


_session_db: dict[str, Path] = {}
"""本 worker 会话级两仓 db 路径（after_nonebot_init 填充；stores fixture
teardown 回落用——置 None 会落回 CWD 生产路径，见 after_nonebot_init）。"""


@pytest.fixture(scope="session", autouse=True)
async def after_nonebot_init(after_nonebot_init: None, tmp_path_factory, worker_id):
    # 加载适配器
    driver = nonebot.get_driver()
    driver.register_adapter(OnebotV11Adapter)

    # 加载插件（[tool.nonebot]：本插件 require 主插件自动加载）
    nonebot.load_from_toml("pyproject.toml")

    # 两仓 db 钉到 per-worker 唯一临时文件。必须在**这里**做（依赖链保证
    # 先于 nonebug_init 的 lifespan startup，即第一次建表之前）：本插件
    # on_startup(init_store) 与主插件 on_startup(init_db) 随 lifespan 触发，
    # _db_file 为 None 时打 CWD 生产路径（LOCALSTORE_USE_CWD=true）——
    # xdist 各 worker 对同一文件并发 create_all，check 与 CREATE 交错即
    # "table already exists"（2026-09-28 CI 实测；本地不复现因 data/ 踩着
    # 开发残留库，表早已建好）。SQLModel 全局 metadata（两仓 store.py 共知
    # 边界）使任一仓 create_all 连另一仓模型一起建，扩大了撞名面。
    # 注意不能做成独立 autouse fixture：与 nonebug_init 并列无依赖，顺序
    # 未定义（实测 lifespan 可先跑）。init_db 有 already-exists 容错、
    # init_store（9938cc8 起）同款，双保险。
    from nonebot_plugin_awmc_helper.core import store as awmc_store

    from nonebot_plugin_awmc_score_updater import store as su_store

    base = tmp_path_factory.mktemp(f"awmc-db-{worker_id}")
    _session_db["su"] = base / "su.db"
    _session_db["awmc"] = base / "awmc.db"
    await su_store.set_db_file(_session_db["su"])
    awmc_store.set_db_file(_session_db["awmc"])


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
