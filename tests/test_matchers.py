"""指令层测试：白名单门禁、绑定链路、传分流程编排（run_update 打桩）。

插件相关导入一律函数内进行（收集期不触发插件加载链）。
"""

import respx
import pytest
from httpx import Response
from nonebug import App

MAIN = "https://salt_api_main.realtvop.top"

SGWCMAID = "SGWCMAID" + "0" * (84 - len("SGWCMAID"))


@pytest.fixture
async def stores(tmp_path):
    """主插件库与本插件库各自重定向到临时文件。"""
    from nonebot_plugin_awmc_helper.core import store as awmc_store

    from nonebot_plugin_awmc_score_updater import store as su_store

    awmc_store.set_db_file(tmp_path / "awmc.db")
    await awmc_store.init_db()
    su_store.set_db_file(tmp_path / "su.db")
    await su_store.init_store()
    yield
    awmc_store.set_db_file(None)
    su_store.set_db_file(None)


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


async def test_help(app: App, stores):
    from fake import fake_private_message_event_v11

    from nonebot_plugin_awmc_score_updater import matchers

    event = fake_private_message_event_v11(
        message="导帮助", user_id=12345678, to_me=True
    )
    await _send(app, matchers.help_cmd, event, matchers.HELP_TEXT, private=True)


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

    async def fake_run_update(client, source, target, *, full, max_retries):
        assert full is False
        assert len(target) == 2  # 水鱼 + 落雪
        return 1.23

    monkeypatch.setattr(matchers, "run_update", fake_run_update)
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
