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
    """导分前曲库就绪等待在无预热测试环境下会死等，统一旁路。"""
    from nonebot_plugin_awmc_helper.core.songs import song_service

    async def fake_ensure():
        return None

    monkeypatch.setattr(song_service, "ensure_loaded", fake_ensure)
