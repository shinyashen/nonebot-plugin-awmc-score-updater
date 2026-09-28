"""指令层测试：白名单门禁、绑定链路、传分流程编排（run_update 打桩）。

插件相关导入一律函数内进行（收集期不触发插件加载链）。
"""

from base64 import b64encode

import respx
import pytest
from httpx import Response
from nonebug import App

MAIN = "https://salt_api_main.realtvop.top"

SGWCMAID = "SGWCMAID" + "0" * (84 - len("SGWCMAID"))


@pytest.fixture
async def stores(tmp_path, _isolate_store_db):
    """主插件库与本插件库各自重定向到临时文件（每用例独立，结束后回落
    worker 会话库——置 None 会落回生产路径，见 _isolate_store_db docstring）。"""
    from nonebot_plugin_awmc_helper.core import store as awmc_store

    from nonebot_plugin_awmc_score_updater import store as su_store

    awmc_store.set_db_file(tmp_path / "awmc.db")
    await awmc_store.init_db()
    su_store.set_db_file(tmp_path / "su.db")
    await su_store.init_store()
    yield
    awmc_store.set_db_file(_isolate_store_db.awmc)
    su_store.set_db_file(_isolate_store_db.su)


async def _bind_token(df: str | None = None, lx: str | None = None) -> None:
    from sqlmodel.ext.asyncio.session import AsyncSession
    from nonebot_plugin_awmc_helper.core.store import UserBinding, get_engine

    async with AsyncSession(get_engine()) as session:
        session.add(
            UserBinding(
                platform="OneBot V11",
                user_id="12345678",
                divingfish_import_token=df,
                lxns_token=lx,
            )
        )
        await session.commit()


async def _bind_wechat(arcade_user_id: str) -> None:
    from nonebot_plugin_awmc_score_updater.store import wechat_store

    await wechat_store.bind("OneBot V11", "12345678", arcade_user_id)


async def _send(app: App, matcher, event, reply: str, *, private: bool = False) -> None:
    """断言单事件回复：群聊 [at, " text"]，私聊去前导空格。"""
    import nonebot
    from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
    from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

    async with app.test_matcher(matcher) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter))
        if private:
            ctx.receive_event(bot, event)
            ctx.should_call_send(
                event,
                Message([MessageSegment.text(reply)]),
                result=None,
                bot=bot,
            )
        else:
            ctx.should_call_api(
                "get_group_info",
                {"group_id": 87654321},
                result={
                    "group_id": 87654321,
                    "group_name": "测试群",
                    "member_count": 10,
                    "max_member_count": 100,
                },
            )
            ctx.should_call_api(
                "get_group_member_info",
                {"group_id": 87654321, "user_id": event.user_id, "no_cache": True},
                result={
                    "user_id": event.user_id,
                    "role": "member",
                    "card": "",
                    "nickname": "test",
                },
            )
            ctx.receive_event(bot, event)
            ctx.should_call_send(
                event,
                Message(
                    [
                        MessageSegment.at(event.user_id),
                        MessageSegment.text(f" {reply}"),
                    ]
                ),
                result=None,
                bot=bot,
            )


async def test_group_qr_denied_by_default(app: App, stores):
    """白名单默认空：群内带二维码直接拒绝（不触外网、不查绑定）。"""
    from fake import fake_group_message_event_v11

    from nonebot_plugin_awmc_score_updater import matchers

    event = fake_group_message_event_v11(message=f"导 {SGWCMAID}")
    await _send(
        app,
        matchers.update_cmd,
        event,
        "二维码包含账号凭据，本群不在全量上传白名单内，请私聊使用",
    )


@respx.mock
async def test_group_qr_allowed_in_whitelist_then_unbound(
    app: App, stores, monkeypatch
):
    """白名单群放行二维码解析，随后因未绑定 token 引导去主插件。"""
    from fake import fake_group_message_event_v11

    from nonebot_plugin_awmc_score_updater import matchers
    from nonebot_plugin_awmc_score_updater.config import plugin_config

    monkeypatch.setattr(plugin_config, "awmc_su_whitelist_groups", ["87654321"])
    respx.post(f"{MAIN}/getQRInfo").mock(
        return_value=Response(200, json={"errorID": 0, "userID": "888"})
    )
    event = fake_group_message_event_v11(message=f"导 {SGWCMAID}")
    await _send(
        app,
        matchers.update_cmd,
        event,
        "没绑数据站你怎么导。。。先对我说“导帮助”看看怎么绑定喵",
    )


async def test_private_simple_update_unbound(app: App, stores):
    from fake import fake_private_message_event_v11

    from nonebot_plugin_awmc_score_updater import matchers

    event = fake_private_message_event_v11(message="导", user_id=12345678, to_me=True)
    await _send(
        app,
        matchers.update_cmd,
        event,
        "没绑数据站你怎么导。。。先对我说“导帮助”看看怎么绑定喵",
        private=True,
    )


@respx.mock
async def test_bindwx_success(app: App, stores):
    from fake import fake_private_message_event_v11

    from nonebot_plugin_awmc_score_updater import matchers
    from nonebot_plugin_awmc_score_updater.store import wechat_store

    route = respx.post(f"{MAIN}/getQRInfo").mock(
        return_value=Response(200, json={"errorID": 0, "userID": "888"})
    )
    event = fake_private_message_event_v11(
        message=f"绑定微信 {SGWCMAID}", user_id=12345678, to_me=True
    )
    await _send(app, matchers.bindwx_cmd, event, "绑定微信二维码信息成功", private=True)

    row = await wechat_store.get("OneBot V11", "12345678")
    assert route.called
    assert row is not None
    assert row.arcade_user_id == "888"


@respx.mock
async def test_bindwx_rejected_in_group(app: App, stores):
    from fake import fake_group_message_event_v11

    from nonebot_plugin_awmc_score_updater import matchers

    event = fake_group_message_event_v11(message=f"绑定微信 {SGWCMAID}")
    await _send(app, matchers.bindwx_cmd, event, "二维码包含账号凭据，仅支持私聊绑定")


@respx.mock
async def test_full_update_account_mismatch(app: App, stores):
    """全量上传二维码与已绑机台账号不符 → 拒绝。"""
    from fake import fake_private_message_event_v11

    from nonebot_plugin_awmc_score_updater import matchers

    await _bind_token(df="a" * 128)
    await _bind_wechat("111")
    respx.post(f"{MAIN}/getQRInfo").mock(
        return_value=Response(200, json={"errorID": 0, "userID": "222"})
    )
    event = fake_private_message_event_v11(
        message=f"导 {SGWCMAID}", user_id=12345678, to_me=True
    )
    await _send(
        app, matchers.update_cmd, event, "怎么，还想帮别人导一导？", private=True
    )


@respx.mock
async def test_simple_update_flow(app: App, stores, monkeypatch):
    """简略上传全流程：绑定齐备 → SaltNet 拉取（mock）→ 传分（打桩）→ 记录时间。"""
    from fake import fake_private_message_event_v11

    from nonebot_plugin_awmc_score_updater import matchers
    from nonebot_plugin_awmc_score_updater.store import wechat_store

    async def fake_run_update(
        client, source, target, *, full, max_retries, pc_hook=None
    ):
        assert full is False
        assert len(target) == 2  # 水鱼 + 落雪
        return 1.23, 0, []

    monkeypatch.setattr(matchers, "run_update", fake_run_update)
    # ensure_loaded 在无预热环境会死等 _ready：流程编排测试直接旁路
    from nonebot_plugin_awmc_helper.core.songs import song_service

    async def fake_ensure():
        return None

    monkeypatch.setattr(song_service, "ensure_loaded", fake_ensure)
    await _bind_token(df="a" * 128, lx="b" * 32)
    await _bind_wechat("888")

    payload = {"userMusicList": [{"userMusicDetailList": [{"musicId": 200}]}]}
    respx.post(f"{MAIN}/updateUser").mock(return_value=Response(200, json=payload))

    event = fake_private_message_event_v11(message="导", user_id=12345678, to_me=True)
    async with app.test_matcher(matchers.update_cmd) as ctx:
        import nonebot
        from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
        from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter))
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message([MessageSegment.text("推分了？你先别急")]),
            result=None,
            bot=bot,
        )
        ctx.should_call_send(
            event,
            Message(
                [
                    MessageSegment.text(
                        "导到水鱼和落雪了喵！\n你这次导了1.23秒，很厉害了喵~\n怎么导的：简单的导"
                    )
                ]
            ),
            result=None,
            bot=bot,
        )

    row = await wechat_store.get("OneBot V11", "12345678")
    assert row is not None
    assert row.last_update is not None


@respx.mock
async def test_simple_update_flow_missing_wechat(app: App, stores):
    """绑定了 token 但没绑微信 → 引导绑定微信。"""
    from fake import fake_private_message_event_v11

    from nonebot_plugin_awmc_score_updater import matchers

    await _bind_token(df="a" * 128)
    event = fake_private_message_event_v11(message="导", user_id=12345678, to_me=True)
    await _send(
        app,
        matchers.update_cmd,
        event,
        "没绑微信二维码你怎么导。。。私聊对我说：绑定微信 <二维码内容>",
        private=True,
    )


def _make_jwt(scope: str) -> str:
    import json
    import base64

    def b64(s: str) -> str:
        return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")

    return f"{b64('{}')}.{b64(json.dumps({'scope': scope}))}.{b64('sig')}"


async def _bind_lx_token(token: str) -> None:
    from sqlmodel import select
    from sqlmodel.ext.asyncio.session import AsyncSession
    from nonebot_plugin_awmc_helper.core.store import UserBinding, get_engine

    async with AsyncSession(get_engine()) as session:
        stmt = select(UserBinding).where(
            UserBinding.platform == "OneBot V11", UserBinding.user_id == "12345678"
        )
        if row := (await session.exec(stmt)).first():
            row.lxns_token = token
        else:
            session.add(
                UserBinding(platform="OneBot V11", user_id="12345678", lxns_token=token)
            )
        await session.commit()


async def test_lxns_readonly_jwt_only_rejected(app: App, stores):
    """只有旧版授权（JWT 无 write_player）→ 引导重绑，不导任何目标。"""
    from fake import fake_private_message_event_v11

    from nonebot_plugin_awmc_score_updater import matchers

    await _bind_lx_token(_make_jwt("read_player read_user_profile"))
    event = fake_private_message_event_v11(message="导", user_id=12345678, to_me=True)
    await _send(
        app,
        matchers.update_cmd,
        event,
        "检测到你的落雪授权不含成绩写入权限，本次未导出落雪；"
        "请重新「绑定落雪」完成授权后即可导分",
        private=True,
    )


@respx.mock
async def test_lxns_readonly_jwt_with_df_still_exports(app: App, stores, monkeypatch):
    """旧版授权 + 水鱼 → 水鱼照常导出，成功文案附落雪重绑提示。"""
    from fake import fake_private_message_event_v11

    from nonebot_plugin_awmc_score_updater import matchers

    async def fake_run_update(
        client, source, target, *, full, max_retries, pc_hook=None
    ):
        assert len(target) == 1  # 仅水鱼
        assert target[0][2]["name"] == "水鱼"
        return 0.5, 0, []

    monkeypatch.setattr(matchers, "run_update", fake_run_update)
    await _bind_token(df="a" * 128)
    await _bind_lx_token(_make_jwt("read_player"))
    await _bind_wechat("888")
    respx.post(f"{MAIN}/updateUser").mock(
        return_value=Response(200, json={"userMusicList": []})
    )

    event = fake_private_message_event_v11(message="导", user_id=12345678, to_me=True)
    async with app.test_matcher(matchers.update_cmd) as ctx:
        import nonebot
        from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
        from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter))
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message([MessageSegment.text("推分了？你先别急")]),
            result=None,
            bot=bot,
        )
        ctx.should_call_send(
            event,
            Message(
                [
                    MessageSegment.text(
                        "导到水鱼了喵！\n你这次导了0.50秒，很厉害了喵~\n怎么导的：简单的导\n"
                        "检测到你的落雪授权不含成绩写入权限，本次未导出落雪；"
                        "请重新「绑定落雪」完成授权后即可导分"
                    )
                ]
            ),
            result=None,
            bot=bot,
        )


@respx.mock
async def test_lxns_writable_jwt_exports(app: App, stores, monkeypatch):
    """新授权 JWT（scope 含 write_player）→ 正常导落雪。"""
    from fake import fake_private_message_event_v11

    from nonebot_plugin_awmc_score_updater import matchers

    async def fake_run_update(
        client, source, target, *, full, max_retries, pc_hook=None
    ):
        assert len(target) == 2  # 水鱼 + 落雪
        assert target[1][2]["name"] == "落雪"
        return 0.8, 0, []

    monkeypatch.setattr(matchers, "run_update", fake_run_update)
    await _bind_token(df="a" * 128)
    await _bind_lx_token(_make_jwt("read_player read_user_profile write_player"))
    await _bind_wechat("888")
    respx.post(f"{MAIN}/updateUser").mock(
        return_value=Response(200, json={"userMusicList": []})
    )

    event = fake_private_message_event_v11(message="导", user_id=12345678, to_me=True)
    async with app.test_matcher(matchers.update_cmd) as ctx:
        import nonebot
        from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
        from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter))
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message([MessageSegment.text("推分了？你先别急")]),
            result=None,
            bot=bot,
        )
        ctx.should_call_send(
            event,
            Message(
                [
                    MessageSegment.text(
                        "导到水鱼和落雪了喵！\n你这次导了0.80秒，很厉害了喵~\n怎么导的：简单的导"
                    )
                ]
            ),
            result=None,
            bot=bot,
        )


def test_lxns_writable_non_jwt():
    from nonebot_plugin_awmc_score_updater.matchers import _lxns_writable

    assert _lxns_writable("b" * 32)  # 个人 API 密钥（非 JWT）恒可写
    assert _lxns_writable("not-a-jwt!!")


async def test_lxns_401_refresh_then_retry(app: App, stores, monkeypatch):
    """落雪 access_token 过期（401）→ 主插件自动续期 → 新凭据重试成功。"""
    from fake import fake_private_message_event_v11
    from maimai_py.exceptions import InvalidPlayerIdentifierError
    from nonebot_plugin_awmc_helper.config import plugin_config
    from nonebot_plugin_awmc_helper.core.binding import binding_service

    from nonebot_plugin_awmc_score_updater import matchers

    monkeypatch.setattr(plugin_config, "awmc_lxns_client_id", "cid")
    monkeypatch.setattr(plugin_config, "awmc_lxns_client_secret", "sec")
    monkeypatch.setattr(plugin_config, "awmc_lxns_redirect_uri", "oob")
    await _bind_token(df="a" * 128)
    await _bind_lx_token("expired-token")
    # 补 refresh_token（直绑路径没有，OAuth 绑定才有）
    from sqlmodel import select
    from sqlmodel.ext.asyncio.session import AsyncSession
    from nonebot_plugin_awmc_helper.core.store import UserBinding, get_engine

    async with AsyncSession(get_engine()) as session:
        stmt = select(UserBinding).where(
            UserBinding.platform == "OneBot V11", UserBinding.user_id == "12345678"
        )
        row = (await session.exec(stmt)).one()
        row.lxns_refresh_token = "rt-1"
        await session.commit()

    refresh_calls = []

    async def fake_refresh_lxns(binding):
        refresh_calls.append(binding)
        binding.lxns_token = "new-token"
        return "refreshed"

    monkeypatch.setattr(binding_service, "refresh_lxns", fake_refresh_lxns)

    import asyncio as _asyncio

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)

    calls = []

    async def fake_run_update(
        client, source, target, *, full, max_retries, pc_hook=None
    ):
        calls.append([kw["name"] for _, _, kw in target])
        if len(calls) == 1:
            raise InvalidPlayerIdentifierError("Unauthorized")
        return 1.0, 0, []

    monkeypatch.setattr(matchers, "run_update", fake_run_update)
    await _bind_wechat("888")

    event = fake_private_message_event_v11(message="导", user_id=12345678, to_me=True)
    async with app.test_matcher(matchers.update_cmd) as ctx:
        import nonebot
        from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
        from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter))
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message([MessageSegment.text("推分了？你先别急")]),
            result=None,
            bot=bot,
        )
        ctx.should_call_send(
            event,
            Message(
                [
                    MessageSegment.text(
                        "导到水鱼和落雪了喵！\n你这次导了1.00秒，很厉害了喵~\n怎么导的：简单的导"
                    )
                ]
            ),
            result=None,
            bot=bot,
        )
    assert len(refresh_calls) == 1
    assert sleeps == [5]  # 续期后 5s 退避重试（落雪新令牌生效延迟，Q43）
    assert calls == [["水鱼", "落雪"], ["水鱼", "落雪"]]


async def test_help_forward_then_guide_image(app: App, stores, monkeypatch):
    """OneBot 合并转发（4 纯文本节点）+ 转发后单独补发引导图。"""
    from fake import fake_private_message_event_v11

    from nonebot_plugin_awmc_score_updater import matchers

    forwarded, entries = [], None

    async def fake_forward(bot, ents, *, group_id=None, user_id=None):
        nonlocal entries
        forwarded.append(True)
        entries = list(ents)
        return True

    monkeypatch.setattr(matchers, "try_send_forward", fake_forward)
    event = fake_private_message_event_v11(
        message="导帮助", user_id=12345678, to_me=True
    )
    async with app.test_matcher(matchers.help_cmd) as ctx:
        import nonebot
        from nonebot.adapters.onebot.v11 import Bot
        from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter))
        ctx.receive_event(bot, event)
        # 转发成功即 return：无额外发送（引导图在转发内）
    assert forwarded == [True]
    assert len(entries) == 5
    import nonebot_plugin_alconna.uniseg as uniseg

    assert isinstance(entries[3], uniseg.UniMessage)  # 引导图为独立纯图节点
    assert all(isinstance(e, str) for i, e in enumerate(entries) if i != 3)


async def test_help_fallback_two_images(app: App, stores, monkeypatch):
    """非 OneBot / 转发失败：降级为文字渲染图 + 引导图两段图片。"""

    from fake import fake_private_message_event_v11
    from nonebot.adapters.onebot.v11 import Message, MessageSegment
    from nonebot_plugin_awmc_helper.core.render.tools import (
        text_to_image,
        image_to_bytes,
    )

    from nonebot_plugin_awmc_score_updater import matchers

    async def fake_forward(bot, ents, *, group_id=None, user_id=None):
        return False

    monkeypatch.setattr(matchers, "try_send_forward", fake_forward)
    event = fake_private_message_event_v11(
        message="导帮助", user_id=12345678, to_me=True
    )
    expected = Message(
        [
            MessageSegment.image(
                "base64://"
                + b64encode(image_to_bytes(text_to_image(matchers.HELP_TEXT))).decode()
            ),
            MessageSegment.image(
                "base64://"
                + b64encode(matchers._IMPORT_TOKEN_IMG.read_bytes()).decode()
            ),
        ]
    )
    async with app.test_matcher(matchers.help_cmd) as ctx:
        import nonebot
        from nonebot.adapters.onebot.v11 import Bot
        from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter))
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, expected, result=None, bot=bot)


@pytest.mark.asyncio
async def test_run_with_refresh_ladder_and_copies(monkeypatch):
    """续期成功后 5s/10s 两级退避阶梯（Q43）：成功路径、全败兜底文案、
    dead 重绑文案、skip 原样上抛（如水鱼 Import-Token 失效）。"""
    import asyncio

    from maimai_py.exceptions import InvalidPlayerIdentifierError
    from nonebot_plugin_awmc_helper.core.store import UserBinding
    from nonebot_plugin_awmc_helper.core.binding import binding_service

    from nonebot_plugin_awmc_score_updater import matchers

    binding = UserBinding(
        platform="OneBot V11",
        user_id="30001",
        lxns_token="tok",
        lxns_refresh_token="rt",
    )

    async def setup(status: str, n_fail: int):
        sleeps = []
        calls = {"n": 0}

        async def fake_sleep(delay):
            sleeps.append(delay)

        async def fake_refresh(b):
            return status

        async def fake_run_update(
            client, source, target, *, full, max_retries, pc_hook=None
        ):
            calls["n"] += 1
            if calls["n"] <= n_fail:
                raise InvalidPlayerIdentifierError("unauthorized")
            return 1.0, 0, []

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(binding_service, "refresh_lxns", fake_refresh)
        monkeypatch.setattr(matchers, "run_update", fake_run_update)
        return sleeps, calls

    # 5s 后成功
    sleeps, calls = await setup("refreshed", 1)
    result = await matchers._run_with_refresh(binding, [], None, False)
    assert result == (1.0, 0, None, ["落雪"], [])
    assert calls["n"] == 2
    assert sleeps == [5]

    # 10s 后成功；进入 10s 档时慢查询提示触发一次
    notices = []

    async def notify_slow():
        notices.append(1)

    sleeps, calls = await setup("refreshed", 2)
    result = await matchers._run_with_refresh(binding, [], None, False, notify_slow)
    assert result == (1.0, 0, None, ["落雪"], [])
    assert calls["n"] == 3
    assert sleeps == [5, 10]
    assert len(notices) == 1

    # 全败：非技术兜底文案
    sleeps, calls = await setup("refreshed", 99)
    with pytest.raises(matchers.ImportFailed, match="暂时未能同步"):
        await matchers._run_with_refresh(binding, [], None, False, notify_slow)
    assert calls["n"] == 3
    assert sleeps == [5, 10]
    assert len(notices) == 2

    # dead：重绑文案（不进阶梯）
    sleeps, calls = await setup("dead", 99)
    with pytest.raises(matchers.ImportFailed, match="重新绑定落雪"):
        await matchers._run_with_refresh(binding, [], None, False)
    assert calls["n"] == 1
    assert sleeps == []

    # skip：原异常上抛（保留「token 无效」映射给无 rt 场景）
    sleeps, calls = await setup("skip", 99)
    with pytest.raises(InvalidPlayerIdentifierError):
        await matchers._run_with_refresh(binding, [], None, False)
    assert calls["n"] == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_run_with_refresh_skips_renewal_without_lxns_target(monkeypatch):
    """落雪不在本次目标内（纯水鱼绑定）时凭据失效立即上抛（F4）：

    水鱼 Import-Token 失效与落雪同抛 InvalidPlayerIdentifierError 且异常无
    provider 标识——修复前无条件走落雪续期路径，白等两级退避后误报
    「落雪数据暂时未能同步」，而 handler 的「token 无效」文案本就正确。
    """
    import asyncio

    from maimai_py.exceptions import InvalidPlayerIdentifierError
    from nonebot_plugin_awmc_helper.core.store import UserBinding
    from nonebot_plugin_awmc_helper.core.binding import binding_service

    from nonebot_plugin_awmc_score_updater import matchers

    binding = UserBinding(
        platform="OneBot V11",
        user_id="30002",
        divingfish_import_token="a" * 128,
    )

    async def fail_refresh(b):
        raise AssertionError("纯水鱼绑定不得触发落雪续期")

    async def fake_run_update(
        client, source, target, *, full, max_retries, pc_hook=None
    ):
        raise InvalidPlayerIdentifierError("unauthorized")

    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(binding_service, "refresh_lxns", fail_refresh)
    monkeypatch.setattr(matchers, "run_update", fake_run_update)

    with pytest.raises(InvalidPlayerIdentifierError):
        await matchers._run_with_refresh(binding, [], None, False)
    assert sleeps == []


@pytest.mark.asyncio
async def test_run_with_refresh_df_failure_tagged_does_not_renew(monkeypatch):
    """双目标下凭据失效按异常标签判定归属（F4 边界）：run_update 经链内
    回调把报错目标名挂到异常实例上——标签指水鱼时即使落雪也在目标内，
    也不进续期路径（修复前白等两级退避后误报「落雪数据暂时未能同步」）。"""
    import asyncio

    from maimai_py.exceptions import InvalidPlayerIdentifierError
    from nonebot_plugin_awmc_helper.core.store import UserBinding
    from nonebot_plugin_awmc_helper.core.binding import binding_service

    from nonebot_plugin_awmc_score_updater import matchers
    from nonebot_plugin_awmc_score_updater.updater import FAIL_TARGET_ATTR

    binding = UserBinding(
        platform="OneBot V11",
        user_id="30003",
        divingfish_import_token="a" * 128,
        lxns_token="b" * 32,
    )

    async def fail_refresh(b):
        raise AssertionError("水鱼失效不得触发落雪续期")

    async def fake_run_update(
        client, source, target, *, full, max_retries, pc_hook=None
    ):
        exc = InvalidPlayerIdentifierError("unauthorized")
        setattr(exc, FAIL_TARGET_ATTR, "水鱼")
        raise exc

    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(binding_service, "refresh_lxns", fail_refresh)
    monkeypatch.setattr(matchers, "run_update", fake_run_update)

    with pytest.raises(InvalidPlayerIdentifierError):
        await matchers._run_with_refresh(binding, [], None, False)
    assert sleeps == []


@pytest.mark.asyncio
async def test_update_cmd_busy_reply_when_locked(app: App, stores, monkeypatch):
    """同一用户并发「导」互斥（F6）：锁占用时快速回复进行中，
    不再并发拉取/上传（双份成绩、桥接游玩次数双计、observe 撞主键）。"""
    import asyncio

    from fake import fake_private_message_event_v11

    from nonebot_plugin_awmc_score_updater import matchers

    lock = asyncio.Lock()
    await lock.acquire()
    matchers._import_locks[("OneBot V11", "12345678")] = lock

    called = {"n": 0}

    async def fake_run_update(*args, **kwargs):
        called["n"] += 1
        return 1.0, 0, []

    monkeypatch.setattr(matchers, "run_update", fake_run_update)
    await _bind_token(df="a" * 128)
    await _bind_wechat("888")
    event = fake_private_message_event_v11(message="导", user_id=12345678, to_me=True)
    await _send(
        app,
        matchers.update_cmd,
        event,
        "上一次导分还在进行中，请稍等完成后再试",
        private=True,
    )
    assert called["n"] == 0
    # 清理模块级锁表，避免污染其他用例
    matchers._import_locks.pop(("OneBot V11", "12345678"), None)
    lock.release()


async def test_pc_list_unbound_wechat_hint(app: App, stores, monkeypatch):
    """pc列表：已绑数据站但未绑微信 → 引导文案（NET/成绩拉取不触网）。"""
    from types import SimpleNamespace

    from fake import fake_private_message_event_v11

    from nonebot_plugin_awmc_score_updater import matchers

    await _bind_token(df="a" * 128)

    async def fake_get_scores_all(binding, notify_slow=None):
        return SimpleNamespace(scores=[])

    monkeypatch.setattr(
        matchers, "score_service", SimpleNamespace(get_scores_all=fake_get_scores_all)
    )
    event = fake_private_message_event_v11(
        message="13pc列表", user_id=12345678, to_me=True
    )
    await _send(
        app,
        matchers.pc_list_cmd,
        event,
        "尚未绑定微信二维码，暂无游玩次数数据",
        private=True,
    )


async def test_pc_list_no_data_hint(app: App, stores, monkeypatch):
    """pc列表：已绑微信但从未导分 → 引导先导分。"""
    from types import SimpleNamespace

    from fake import fake_private_message_event_v11

    from nonebot_plugin_awmc_score_updater import matchers

    await _bind_token(df="a" * 128)
    await _bind_wechat("888")

    async def fake_get_scores_all(binding, notify_slow=None):
        return SimpleNamespace(scores=[])

    monkeypatch.setattr(
        matchers, "score_service", SimpleNamespace(get_scores_all=fake_get_scores_all)
    )
    event = fake_private_message_event_v11(
        message="13pc列表", user_id=12345678, to_me=True
    )
    await _send(
        app,
        matchers.pc_list_cmd,
        event,
        "暂无游玩次数数据，请先「导」一次；带二维码私聊导分可校准全部次数",
        private=True,
    )


@pytest.mark.asyncio
async def test_build_targets_df_credential_kinds(monkeypatch):
    """水鱼导分凭据判定（2026-09-28 修复）：仅 QQ 号可派生的 ref subject 是
    公开标识，不再单独构成导分凭据——Hoshino 迁移的「仅 QQ」行（consent 至多
    只读、普遍缺失）不再装配注定失败的水鱼目标拖死整链；OAuth consent 标志
    或 Import-Token 才是显式凭据。"""
    from nonebot_plugin_awmc_helper.config import plugin_config
    from nonebot_plugin_awmc_helper.core.store import UserBinding

    from nonebot_plugin_awmc_score_updater.matchers import _build_targets

    monkeypatch.setattr(plugin_config, "awmc_divingfish_oauth_client_id", "cid")
    monkeypatch.setattr(plugin_config, "awmc_divingfish_oauth_client_secret", "sec")

    # 仅 QQ（Hoshino 迁移行，service=lxns 场景同构）：无凭据无授权 → 不装
    targets, note = _build_targets(
        UserBinding(platform="OneBot V11", user_id="449099602", service="lxns")
    )
    assert [kw["name"] for _, _, kw in targets] == []
    assert note is None

    # 仅 Import-Token：照装
    targets, _ = _build_targets(
        UserBinding(
            platform="OneBot V11", user_id="1", divingfish_import_token="a" * 128
        )
    )
    assert [kw["name"] for _, _, kw in targets] == ["水鱼"]
    assert targets[0][1].credentials == "a" * 128

    # OAuth consent 标志：subject 装配（ref: 摘要）
    targets, _ = _build_targets(
        UserBinding(platform="OneBot V11", user_id="1", divingfish_oauth=True)
    )
    assert targets[0][1].credentials.startswith("ref:")

    # oauth + token：consent 优先（subject 换票含写权限，token 写已被水鱼 500 拒绝）
    targets, _ = _build_targets(
        UserBinding(
            platform="OneBot V11",
            user_id="1",
            divingfish_oauth=True,
            divingfish_import_token="a" * 128,
        )
    )
    assert targets[0][1].credentials.startswith("ref:")


async def test_qq_only_binding_rejected(app: App, stores):
    """仅 QQ 号的迁移行（无 token 无 OAuth）→ 视为未绑定水鱼凭据，走「没绑
    数据站」引导（修复前派生 subject 被当凭据放行，导分必败于换票）。"""
    from fake import fake_private_message_event_v11
    from sqlmodel.ext.asyncio.session import AsyncSession
    from nonebot_plugin_awmc_helper.core.store import UserBinding, get_engine

    from nonebot_plugin_awmc_score_updater import matchers

    async with AsyncSession(get_engine()) as session:
        session.add(
            UserBinding(platform="OneBot V11", user_id="12345678", service="divingfish")
        )
        await session.commit()

    event = fake_private_message_event_v11(message="导", user_id=12345678, to_me=True)
    await _send(
        app,
        matchers.update_cmd,
        event,
        "没绑数据站你怎么导。。。先对我说“导帮助”看看怎么绑定喵",
        private=True,
    )


async def _prepare_partial_failure_env(app, monkeypatch, fake_run_update):
    """部分失败用例公共前置：绑定齐备 + SaltNet 拉取 mock + 传分打桩。"""
    from fake import fake_private_message_event_v11
    from nonebot_plugin_awmc_helper.core.songs import song_service

    from nonebot_plugin_awmc_score_updater import matchers

    async def fake_ensure():
        return None

    monkeypatch.setattr(song_service, "ensure_loaded", fake_ensure)
    monkeypatch.setattr(matchers, "run_update", fake_run_update)
    await _bind_token(df="a" * 128, lx="b" * 32)
    await _bind_wechat("888")
    respx.post(f"{MAIN}/updateUser").mock(
        return_value=Response(200, json={"userMusicList": []})
    )
    return fake_private_message_event_v11


@respx.mock
async def test_partial_failure_special_reply(app: App, stores, monkeypatch):
    """部分失败彩蛋文案：「导出来了，但...」+ 失败项原因 + 成功目标照常报喜
    （水鱼写权限缺失不再拖死落雪——落雪成绩正常导出并展示）。"""
    from maimai_py.exceptions import PlayerNotAuthorizedError

    from nonebot_plugin_awmc_score_updater import matchers

    async def fake_run_update(
        client, source, target, *, full, max_retries, pc_hook=None
    ):
        assert [kw["name"] for _, _, kw in target] == ["水鱼", "落雪"]
        return 1.0, 0, [("水鱼", PlayerNotAuthorizedError("consent required"))]

    event_factory = await _prepare_partial_failure_env(
        app, monkeypatch, fake_run_update
    )
    event = event_factory(message="导", user_id=12345678, to_me=True)
    async with app.test_matcher(matchers.update_cmd) as ctx:
        import nonebot
        from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
        from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter))
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message([MessageSegment.text("推分了？你先别急")]),
            result=None,
            bot=bot,
        )
        ctx.should_call_send(
            event,
            Message(
                [
                    MessageSegment.text(
                        "导出来了，但...\n"
                        "· 水鱼没导上去喵：水鱼已要求成绩写入走 OAuth 授权，"
                        "请发送「绑定水鱼」完成一次授权\n"
                        "导到落雪了喵！\n你这次导了1.00秒，很厉害了喵~\n怎么导的：简单的导"
                    )
                ]
            ),
            result=None,
            bot=bot,
        )


@respx.mock
async def test_partial_failure_plain_reply(app: App, stores, monkeypatch):
    """部分失败普通文案（「传分」指令，非彩蛋）：成功目标照报，失败项以
    「未导出」列出原因。"""
    from maimai_py.exceptions import PlayerNotAuthorizedError

    from nonebot_plugin_awmc_score_updater import matchers

    async def fake_run_update(
        client, source, target, *, full, max_retries, pc_hook=None
    ):
        return 1.0, 0, [("水鱼", PlayerNotAuthorizedError("consent required"))]

    event_factory = await _prepare_partial_failure_env(
        app, monkeypatch, fake_run_update
    )
    event = event_factory(message="传分", user_id=12345678, to_me=True)
    async with app.test_matcher(matchers.update_cmd) as ctx:
        import nonebot
        from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
        from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter))
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message([MessageSegment.text("正在上传分数，请稍等...")]),
            result=None,
            bot=bot,
        )
        ctx.should_call_send(
            event,
            Message(
                [
                    MessageSegment.text(
                        "上传分数至落雪成功！\n本次上传用时1.00秒\n"
                        "上传方式：简略上传\n"
                        "· 水鱼未导出：水鱼已要求成绩写入走 OAuth 授权，"
                        "请发送「绑定水鱼」完成一次授权"
                    )
                ]
            ),
            result=None,
            bot=bot,
        )


@pytest.mark.asyncio
async def test_run_with_refresh_partial_lxns_401_renewed(monkeypatch):
    """部分失败续期（新路径）：他站成功 + 落雪 401 进失败列表 → 续期后重试
    成功，落雪从失败列表消失，水鱼失败保留随返回。"""
    import asyncio

    from maimai_py.exceptions import (
        PlayerNotAuthorizedError,
        InvalidPlayerIdentifierError,
    )
    from nonebot_plugin_awmc_helper.core.store import UserBinding
    from nonebot_plugin_awmc_helper.core.binding import binding_service

    from nonebot_plugin_awmc_score_updater import matchers

    binding = UserBinding(
        platform="OneBot V11",
        user_id="30004",
        divingfish_import_token="a" * 128,
        lxns_token="b" * 32,
    )

    sleeps: list[float] = []
    refresh_calls: list = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    async def fake_refresh(b):
        refresh_calls.append(1)
        return "refreshed"

    calls = {"n": 0}

    async def fake_run_update(
        client, source, target, *, full, max_retries, pc_hook=None
    ):
        calls["n"] += 1
        if calls["n"] == 1:
            return 1.0, 0, [("落雪", InvalidPlayerIdentifierError("Unauthorized"))]
        return 2.0, 0, [("水鱼", PlayerNotAuthorizedError("consent required"))]

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(binding_service, "refresh_lxns", fake_refresh)
    monkeypatch.setattr(matchers, "run_update", fake_run_update)

    duration, _skipped, _lx_note, names, failures = await matchers._run_with_refresh(
        binding, [], None, False
    )
    assert duration == 2.0
    assert names == ["水鱼", "落雪"]
    assert [n for n, _ in failures] == ["水鱼"]  # 落雪已救回、水鱼失败保留
    assert sleeps == [5]
    assert len(refresh_calls) == 1


@pytest.mark.asyncio
async def test_run_with_refresh_partial_lxns_dead_rebind_hint(monkeypatch):
    """部分失败 + 续期 dead（rt 已过期）：落雪失败项改记重绑文案随返回，
    不抛整链异常（他站成功结果不受影响）。"""
    import asyncio

    from maimai_py.exceptions import InvalidPlayerIdentifierError
    from nonebot_plugin_awmc_helper.core.store import UserBinding
    from nonebot_plugin_awmc_helper.core.binding import binding_service

    from nonebot_plugin_awmc_score_updater import matchers

    binding = UserBinding(
        platform="OneBot V11",
        user_id="30005",
        divingfish_import_token="a" * 128,
        lxns_token="b" * 32,
    )

    async def fake_sleep(delay):
        raise AssertionError("dead 不进退避阶梯")

    async def fake_refresh(b):
        return "dead"

    async def fake_run_update(
        client, source, target, *, full, max_retries, pc_hook=None
    ):
        return 1.0, 0, [("落雪", InvalidPlayerIdentifierError("Unauthorized"))]

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(binding_service, "refresh_lxns", fake_refresh)
    monkeypatch.setattr(matchers, "run_update", fake_run_update)

    duration, _skipped, _lx_note, _names, failures = await matchers._run_with_refresh(
        binding, [], None, False
    )
    assert duration == 1.0
    assert [n for n, _ in failures] == ["落雪"]
    assert isinstance(failures[0][1], matchers.ImportFailed)
    assert "重新绑定落雪" in str(failures[0][1])
