"""指令层：导/传分、绑定微信、帮助。

「导 + 二维码」全量上传的群白名单规则：私聊始终可用；群聊仅当群号在
``awmc_su_whitelist_groups`` 白名单（默认空）时放行——二维码内容等价账号
凭据，群内发送有泄露风险。

水鱼/落雪凭据不在此绑定：直接读主插件 ``user_binding``，未绑定时引导用户
去主插件指令。
"""

import re
import json
import base64
from typing import Any
from pathlib import Path
from datetime import datetime

from nonebot import on_command
from nonebot.log import logger
from nonebot.params import CommandArg
from maimai_py.models import PlayerIdentifier
from nonebot.adapters import Bot, Event, Message
from maimai_py.exceptions import (
    InvalidJsonError,
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
from nonebot_plugin_awmc_helper.core.forward import try_send_forward
from nonebot_plugin_awmc_helper.core.render.tools import text_to_image, image_to_bytes

from .store import wechat_store
from .config import plugin_config
from .saltapi import SaltApiError, parse_qrcode, extract_qrcode
from .updater import SaltArcadeProvider, run_update

# 合并转发节点与降级图片的文字内容（同源）；水鱼节点附 Import-Token 获取
# 位置的引导截图（assets/import_token.jpg，沿用 Hoshino 原版素材）
HELP_SECTIONS = [
    "上传国服 maimaiDX 成绩至水鱼/落雪成绩数据库。\n\n"
    "指令：导/传分/上传分数/wmupdate [二维码内容]\n"
    "· 不带二维码 = 简略上传（仅达成率与 DX 分的增量）\n"
    "· 带二维码 = 全量上传（仅私聊或白名单群）\n"
    "· 「导」字开头的指令有专属回复喵",
    "绑定机台账号（仅私聊）：\n"
    "绑定微信/bindwx <SGWCMAID.../https...>\n"
    "发送二维码识别后的内容（SGWCMAID 开头），或二维码页面的链接",
    "导分依赖主插件 awmc-helper 的绑定，请先在主插件完成（发给 bot 即可）：\n"
    "绑定水鱼token <Import-Token> —— 绑定后才能导分水鱼。\n"
    "获取方式见下图：水鱼查分器个人页 → 设置 → 生成 Import-Token。\n"
    "注意：仅「绑定水鱼 <用户名>」的公开查询档无法导分，必须绑定 Import-Token",
    "绑定落雪：在主插件发送「绑定落雪」，按回复的授权链接完成落雪授权，"
    "再把授权码直接回复给 bot（无需任何前缀，90 秒内有效）。\n"
    "新版授权自带成绩上传权限；旧版授权会在导分时提示重新绑定",
]

HELP_TEXT = "\n\n".join(HELP_SECTIONS)

_IMPORT_TOKEN_IMG = Path(__file__).parent / "assets" / "import_token.jpg"


def _help_entries() -> list[str]:
    """合并转发节点（纯文本）。

    引导图不进转发：转发卡片内的图片段不做富媒体上传，NTQQ 渲染为
    「该消息类型暂不支持查看」（线上实测，raw 字节与路径两种形式皆然）
    ——引导图由 handler 在转发成功后单独以普通图片消息发送。
    """
    return list(HELP_SECTIONS)


update_cmd = on_command("导", aliases={"传分", "上传分数", "wmupdate"}, block=True)
help_cmd = on_command("导帮助", aliases={"传分帮助", "上传分数帮助"}, block=True)
bindwx_cmd = on_command("绑定微信", aliases={"bindwx", "微信绑定"}, block=True)


_LXNS_JWT_RE = re.compile(r"^[a-zA-Z0-9-_]+\.[a-zA-Z0-9-_]+\.[a-zA-Z0-9-_]+$")
"""与 maimai-py is_jwt 同源：落雪 OAuth access_token（JWT）形态。

注意不能以「是否 JWT」判断可写性——重绑后的新授权 token 同样是 JWT，
需解码 payload 的 scope 声明确认（access_token 仅 15 分钟有效，主插件
靠 refresh_token 自动续期，续期签发的 scope 随应用当前权限）。
"""

_LXNS_WRITE_SCOPE = "write_player"
_LXNS_REBIND_HINT = (
    "检测到你的落雪授权不含成绩写入权限，本次未导出落雪；"
    "请重新「绑定落雪」完成授权后即可导分"
)


def _lxns_writable(token: str) -> bool:
    """落雪凭据是否可写成绩。

    个人 API 密钥（非 JWT）恒可写；JWT 解码 payload 的 scope 判断是否含
    ``write_player``；解码失败按可写处理（交由运行时 401 文案兜底）。
    """
    if not _LXNS_JWT_RE.match(token):
        return True
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return True
    return _LXNS_WRITE_SCOPE in str(claims.get("scope", ""))


def _build_targets(
    import_token: str | None, lxns_token: str | None
) -> tuple[
    list[tuple[IScoreUpdateProvider, PlayerIdentifier, dict[str, Any]]], str | None
]:
    """按主插件绑定装配上传目标。

    返回 (targets, 落雪不可导提示)：落雪 token 缺 ``write_player`` scope
    （旧版授权，只读）时跳过落雪目标并给出重绑提示，不影响水鱼导出。
    """
    targets: list[tuple[IScoreUpdateProvider, PlayerIdentifier, dict[str, Any]]] = []
    if import_token:
        targets.append(
            (
                divingfish_provider,
                PlayerIdentifier(credentials=import_token),
                {"name": "水鱼"},
            )
        )
    lx_note: str | None = None
    if lxns_token:
        if _lxns_writable(lxns_token):
            targets.append(
                (
                    lxns_provider,
                    PlayerIdentifier(credentials=lxns_token),
                    {"name": "落雪"},
                )
            )
        else:
            lx_note = _LXNS_REBIND_HINT
    return targets, lx_note


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


async def _run_with_refresh(
    binding, source: list, qrcode: str | None, full: bool
) -> tuple[float, int, str | None, list[str]]:
    """执行一次传分；落雪 access_token 仅 15 分钟有效，上传 401 时用
    refresh_token 续期落库后重试一次（不限主插件 service 语义——传分
    凭据与默认查分器无关）。续期失败则原异常上抛，交由错误文案。
    返回 (用时秒, 跳过条数, 落雪只读提示, 目标名列表)。
    """
    targets, lx_note = _build_targets(
        binding.divingfish_import_token, binding.lxns_token
    )
    try:
        duration, skipped = await run_update(
            client,
            source,
            targets,
            full=full,
            max_retries=plugin_config.awmc_su_max_retries,
        )
        return duration, skipped, lx_note, [kw["name"] for _, _, kw in targets]
    except InvalidPlayerIdentifierError as exc:
        # 落雪 access_token 仅 15 分钟有效：复用主插件自动续期
        # （refresh_token 换新并落库），成功后以新凭据重试一次
        if not await binding_service.refresh_lxns_if_expired(binding, exc):
            raise
    logger.info("落雪 access_token 已续期，重试传分")
    targets, lx_note = _build_targets(
        binding.divingfish_import_token, binding.lxns_token
    )
    duration, skipped = await run_update(
        client,
        source,
        targets,
        full=full,
        max_retries=plugin_config.awmc_su_max_retries,
    )
    return duration, skipped, lx_note, [kw["name"] for _, _, kw in targets]


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
    targets, lx_note = _build_targets(
        binding.divingfish_import_token, binding.lxns_token
    )
    if not targets and lx_note:
        # 只有落雪绑定且为只读旧授权：无目标可导，直接引导重绑
        await UniMessage.text(f" {lx_note}").finish(at_sender=True)

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

    # 水鱼/落雪 update_scores 内部会经 maimai-py client.songs() 取曲库：
    # 必须等主插件曲库预热完成（缓存已填），否则重启后立即导分会触发
    # 现场全量重建，超过请求超时（2026-09-26 线上 ReadTimeout 实测根因）
    from nonebot_plugin_awmc_helper.core.songs import song_service

    await song_service.ensure_loaded()

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
        duration, skipped, lx_note, names = await _run_with_refresh(
            binding, source, qrcode, full=bool(qrcode)
        )
    except InvalidPlayerIdentifierError:
        await UniMessage.text(
            " 成绩导入 token 无效，请到主插件重新绑定水鱼/落雪 token"
        ).finish(at_sender=True)
    except PrivacyLimitationError:
        await UniMessage.text(" 你没有同意数据站的相关用户协议，无法完成该操作").finish(
            at_sender=True
        )
    except InvalidJsonError:
        # 数据站返回非 JSON（500 HTML 等）：多见于凌晨维护窗口，服务端问题非本插件故障
        await UniMessage.text(
            " 数据站服务暂时不可用（可能维护中），成绩可能已部分上传，请稍后再试"
        ).finish(at_sender=True)

    await wechat_store.set_last_update(
        platform, user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    target_str = "和".join(names)
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
    if skipped:
        msg += f"\n另有 {skipped} 条成绩被数据站拒绝（删除曲/未收录），已跳过"
    if lx_note:
        msg += f"\n{lx_note}"
    await UniMessage.text(f" {msg}").finish(at_sender=True)


@help_cmd.handle()
@handle_errors()
async def _(bot: Bot, session: Session = UniSession()):
    # OneBot v11 合并转发（对齐原版帮助形态，水鱼节点附引导图）；失败或
    # 其他适配器降级为文字渲染图片 + 引导图（纯文本字数过多）
    group_id = str(session.scene.id) if session.scene.type == SceneType.GROUP else None
    user_id = None if group_id else str(session.user.id)
    if await try_send_forward(bot, _help_entries(), group_id=group_id, user_id=user_id):
        await UniMessage.image(raw=_IMPORT_TOKEN_IMG.read_bytes()).send()
        return
    guide = UniMessage.image(raw=image_to_bytes(text_to_image(HELP_TEXT)))
    guide += UniMessage.image(raw=_IMPORT_TOKEN_IMG.read_bytes())
    await guide.finish(at_sender=True)


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
