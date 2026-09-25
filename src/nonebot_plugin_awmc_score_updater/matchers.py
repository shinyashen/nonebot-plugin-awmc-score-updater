"""指令层：导/传分、绑定微信、帮助。

「导 + 二维码」全量上传的群白名单规则：私聊始终可用；群聊仅当群号在
``awmc_su_whitelist_groups`` 白名单（默认空）时放行——二维码内容等价账号
凭据，群内发送有泄露风险。

水鱼/落雪凭据不在此绑定：直接读主插件 ``user_binding``，未绑定时引导用户
去主插件指令。
"""

from typing import Any
from datetime import datetime

from nonebot import on_command
from nonebot.params import CommandArg
from maimai_py.models import PlayerIdentifier
from nonebot.adapters import Event, Message
from maimai_py.exceptions import (
    PrivacyLimitationError,
    InvalidPlayerIdentifierError,
)
from nonebot_plugin_uninfo import Session, SceneType, UniSession
from maimai_py.providers.base import IScoreUpdateProvider
from nonebot_plugin_alconna.uniseg import UniMessage
from nonebot_plugin_awmc_helper.core.utils import handle_errors
from nonebot_plugin_awmc_helper.core.client import (
    client,
    lxns_provider,
    divingfish_provider,
)
from nonebot_plugin_awmc_helper.core.binding import session_keys, binding_service

from .store import wechat_store
from .config import plugin_config
from .saltapi import SaltApiError, parse_qrcode, extract_qrcode
from .updater import SaltArcadeProvider, run_update

HELP_TEXT = """上传国服 maimaiDX 成绩至水鱼/落雪成绩数据库。

指令列表：
1. 绑定微信/bindwx <SGWCMAID.../https...>：绑定微信公众号二维码（仅私聊）
   可发送二维码识别后的内容，或二维码页面的链接
2. 导/传分/上传分数 [二维码内容]：上传成绩至已绑定的水鱼/落雪
3. 不带二维码 = 简略上传（仅达成率与 DX 分）；带二维码 = 全量上传（仅私聊或白名单群）

水鱼/落雪 token 请使用 awmc-helper 主插件绑定：
· 绑定水鱼token <水鱼成绩导入token>
· 绑定落雪（OAuth 授权绑定）"""

update_cmd = on_command("导", aliases={"传分", "上传分数", "wmupdate"}, block=True)
help_cmd = on_command("导帮助", aliases={"传分帮助", "上传分数帮助"}, block=True)
bindwx_cmd = on_command("绑定微信", aliases={"bindwx", "微信绑定"}, block=True)


def _build_targets(import_token: str | None, lxns_token: str | None):
    """按主插件绑定装配上传目标（水鱼 Import-Token / 落雪个人 token）。"""
    targets: list[tuple[IScoreUpdateProvider, PlayerIdentifier, dict[str, Any]]] = []
    if import_token:
        targets.append(
            (
                divingfish_provider,
                PlayerIdentifier(credentials=import_token),
                {"name": "水鱼"},
            )
        )
    if lxns_token:
        targets.append(
            (lxns_provider, PlayerIdentifier(credentials=lxns_token), {"name": "落雪"})
        )
    return targets


async def _resolve_qrcode(text: str) -> tuple[str, str]:
    """解析二维码参数 → (二维码内容, 华立 userID)，业务失败抛 SaltApiError 子类文案。

    这里直接以 :class:`SaltApiError` 承载用户文案（handle_errors 展示 str(e)）。
    """
    qr = extract_qrcode(text)
    if qr is None:
        raise SaltApiError("请提供正确格式的内容(SGWCMAID.../https...)！")
    arcade_user_id = await parse_qrcode(
        qr,
        main_url=plugin_config.awmc_su_salt_api_url,
        fallback_url=plugin_config.awmc_su_salt_api_fallback_url,
    )
    if arcade_user_id is None:
        raise SaltApiError("二维码/链接解析失败，请检查内容是否正确/是否在有效期内")
    return qr, arcade_user_id


@update_cmd.handle()
@handle_errors(except_with_message=(SaltApiError,))
async def _(
    event: Event,
    session: Session = UniSession(),
    args: Message = CommandArg(),
):
    # 彩蛋文案开关：指令以「导」字开头（原版 raw_message 首字符语义）
    special = event.get_plaintext().startswith("导")
    platform, user_id = session_keys(session)

    parts = args.extract_plain_text().strip().split()
    if parts == ["帮助"]:
        await UniMessage.text(HELP_TEXT).finish(at_sender=True)
    qr_input = parts[0] if parts else None

    # 全量上传（带二维码）的群白名单门禁；简略上传群聊不受限
    if qr_input and session.scene.type == SceneType.GROUP:
        if str(session.scene.id) not in plugin_config.awmc_su_whitelist_groups:
            await UniMessage.text(
                " 二维码包含账号凭据，本群不在全量上传白名单内，请私聊使用"
            ).finish(at_sender=True)

    binding = await binding_service.get(platform, user_id)
    if binding is None or not (binding.divingfish_import_token or binding.lxns_token):
        msg = (
            "没绑数据站你怎么导。。。先对我说“导帮助”看看怎么绑定喵"
            if special
            else "尚未绑定水鱼或落雪 token，请先使用 awmc-helper 主插件绑定"
        )
        await UniMessage.text(f" {msg}").finish(at_sender=True)

    wb = await wechat_store.get(platform, user_id)
    if wb is None or not wb.arcade_user_id:
        msg = (
            "没绑微信二维码你怎么导。。。私聊对我说：绑定微信 <二维码内容>"
            if special
            else "尚未绑定微信二维码，请私聊对我说：绑定微信 <二维码内容>"
        )
        await UniMessage.text(f" {msg}").finish(at_sender=True)

    qrcode: str | None = None
    if qr_input:
        qr, arcade_user_id = await _resolve_qrcode(qr_input)
        if arcade_user_id != wb.arcade_user_id:
            msg = (
                "怎么，还想帮别人导一导？"
                if special
                else "你提供的二维码所对应账号与已绑定的账号不匹配，请检查后重新输入"
            )
            await UniMessage.text(f" {msg}").finish(at_sender=True)
        qrcode = qr

    if wb.last_update:
        hint = (
            f"\n你上次啥时候导的: {wb.last_update}"
            if special
            else f"\n最近上传时间: {wb.last_update}"
        )
    else:
        hint = ""
    prefix = "推分了？你先别急" if special else "正在上传分数，请稍等..."
    await UniMessage.text(f"{prefix}{hint}").send(at_sender=True)

    targets = _build_targets(binding.divingfish_import_token, binding.lxns_token)
    source = [
        (
            SaltArcadeProvider(
                plugin_config.awmc_su_salt_api_url,
                plugin_config.awmc_su_salt_api_fallback_url,
            ),
            SaltArcadeProvider.make_identifier(wb.arcade_user_id, qrcode),
            {"name": "机台"},
        )
    ]
    try:
        duration = await run_update(
            client,
            source,
            targets,
            full=bool(qrcode),
            max_retries=plugin_config.awmc_su_max_retries,
        )
    except InvalidPlayerIdentifierError:
        await UniMessage.text(
            " 成绩导入 token 无效，请到主插件重新绑定水鱼/落雪 token"
        ).finish(at_sender=True)
    except PrivacyLimitationError:
        await UniMessage.text(" 你没有同意数据站的相关用户协议，无法完成该操作").finish(
            at_sender=True
        )

    await wechat_store.set_last_update(
        platform, user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    target_str = "和".join(kw["name"] for _, _, kw in targets)
    if special:
        msg = (
            f"导到{target_str}了喵！\n你这次导了{duration:.2f}秒，很厉害了喵~\n"
            f"怎么导的：{'好好的导' if qrcode else '简单的导'}"
        )
    else:
        msg = (
            f"上传分数至{target_str}成功！\n本次上传用时{duration:.2f}秒\n"
            f"上传方式：{'全量上传' if qrcode else '简略上传'}"
        )
    await UniMessage.text(f" {msg}").finish(at_sender=True)


@help_cmd.handle()
async def _():
    await UniMessage.text(HELP_TEXT).finish(at_sender=True)


@bindwx_cmd.handle()
@handle_errors(except_with_message=(SaltApiError,))
async def _(
    session: Session = UniSession(),
    args: Message = CommandArg(),
):
    platform, user_id = session_keys(session)
    if session.scene.type == SceneType.GROUP:
        await UniMessage.text(" 二维码包含账号凭据，仅支持私聊绑定").finish(
            at_sender=True
        )

    text = args.extract_plain_text().strip()
    if not text:
        await UniMessage.text(
            " 用法：绑定微信 <SGWCMAID.../https...>（二维码识别内容或页面链接）"
        ).finish(at_sender=True)
    _, arcade_user_id = await _resolve_qrcode(text)
    await wechat_store.bind(platform, user_id, arcade_user_id)
    await UniMessage.text(" 绑定微信二维码信息成功").finish(at_sender=True)
