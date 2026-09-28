"""指令层：导/传分、绑定微信、帮助。

「导 + 二维码」全量上传的群白名单规则：私聊始终可用；群聊仅当群号在
``awmc_su_whitelist_groups`` 白名单（默认空）时放行——二维码内容等价账号
凭据，群内发送有泄露风险。

水鱼/落雪凭据不在此绑定：直接读主插件 ``user_binding``，未绑定时引导用户
去主插件指令。
"""

import json
import base64
import asyncio
from typing import Any
from pathlib import Path
from datetime import datetime

from nonebot import on_regex, on_command
from nonebot.log import logger
from nonebot.params import CommandArg, RegexGroup
from maimai_py.models import PlayerIdentifier
from nonebot.adapters import Bot, Event, Message
from maimai_py.exceptions import (
    RateLimitError,
    InvalidJsonError,
    PrivacyLimitationError,
    PlayerNotAuthorizedError,
    InvalidPlayerIdentifierError,
)
from nonebot_plugin_uninfo import Session, SceneType, UniSession
from maimai_py.providers.base import IScoreUpdateProvider
from maimai_py.providers.lxns import is_jwt
from nonebot_plugin_alconna.uniseg import UniMessage
from nonebot_plugin_awmc_helper.core.score import UserScoreError, score_service
from nonebot_plugin_awmc_helper.core.utils import (
    parse_page,
    slow_notice,
    handle_errors,
)
from nonebot_plugin_awmc_helper.core.client import (
    client,
    lxns_provider,
    divingfish_provider,
)
from nonebot_plugin_awmc_helper.core.binding import (
    session_keys,
    binding_service,
    service_display,
)
from nonebot_plugin_awmc_helper.core.forward import try_send_forward
from nonebot_plugin_awmc_helper.core.render.score import DrawScore, score_list_height
from nonebot_plugin_awmc_helper.core.render.tools import text_to_image, image_to_bytes

from .store import wechat_store, play_count_store
from .config import plugin_config
from .saltapi import SaltApiError, parse_qrcode, extract_qrcode
from .updater import FAIL_TARGET_ATTR, SaltArcadeProvider, run_update

# 合并转发节点与降级图片的文字内容（同源）；水鱼节点附 Import-Token 获取
# 位置的引导截图（assets/import_token.jpg，沿用 Hoshino 原版素材）
HELP_SECTIONS = [
    "上传国服 maimaiDX 成绩至水鱼/落雪成绩数据库。\n\n"
    "指令：导/传分/上传分数/wmupdate [二维码内容]\n"
    "· 不带二维码 = 简略上传（仅达成率与 DX 分的增量）\n"
    "· 带二维码 = 全量上传（仅私聊或白名单群），并校准游玩次数\n"
    "· 13pc列表 / 13.0pc列表：游玩次数排行（标级/定数前缀与\n"
    "  分数列表同口径，支持页码）\n"
    "· 「导」字开头的指令有专属回复喵",
    "绑定机台账号（仅私聊）：\n"
    "绑定微信/bindwx <SGWCMAID.../https...>\n"
    "发送二维码识别后的内容（SGWCMAID 开头），或二维码页面的链接",
    "导分依赖主插件 awmc-helper 的绑定，请先在主插件完成（发给 bot 即可）：\n"
    "绑定水鱼token <Import-Token> —— 绑定后才能导分水鱼。\n"
    "获取方式见下图：水鱼查分器个人页 → 设置 → 生成 Import-Token。\n"
    "注意：仅「绑定水鱼 <用户名>」的公开查询档无法导分；也可发「绑定水鱼」\n"
    "完成一次 OAuth 授权代替 Import-Token（水鱼现已强制写入走授权）",
    "绑定落雪：在主插件发送「绑定落雪」，按回复的授权链接完成落雪授权，"
    "再把授权码直接回复给 bot（无需任何前缀，90 秒内有效）。\n"
    "新版授权自带成绩上传权限；旧版授权会在导分时提示重新绑定",
]

HELP_TEXT = "\n\n".join(HELP_SECTIONS)

_IMPORT_TOKEN_IMG = Path(__file__).parent / "assets" / "import_token.jpg"


def _help_entries() -> list["str | UniMessage"]:
    """合并转发节点：引导图为独立纯图节点（不与文字混节点）。

    节点构造已对齐 Hoshino 原版实测可用形态（name/uin 键 + file:/// 图片
    URI，见主插件 core.forward 升级记录）；此前「消息类型暂不支持查看」
    实为图文混合单节点 + 图片路径形式不规范所致。"""
    return [
        HELP_SECTIONS[0],
        HELP_SECTIONS[1],
        HELP_SECTIONS[2],
        UniMessage.image(path=_IMPORT_TOKEN_IMG),
        HELP_SECTIONS[3],
    ]


update_cmd = on_command("导", aliases={"传分", "上传分数", "wmupdate"}, block=True)
help_cmd = on_command("导帮助", aliases={"传分帮助", "上传分数帮助"}, block=True)
bindwx_cmd = on_command("绑定微信", aliases={"bindwx", "微信绑定"}, block=True)
# 分数前缀与主插件分数列表同口径（DS_RE 同款）：整数=标级（13、13+），
# 小数=定数（13.0）；13pc列表 即标级 13 全部谱面（定数 13.0-13.5）的 pc 排行
pc_list_cmd = on_regex(
    r"^([0-9]+(?:\.[0-9]+)?\+?)\s?pc列表\s?([0-9]+)?$",
    block=True,
)


"""落雪 OAuth access_token（JWT）形态判定复用 maimai-py 单源。

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
    if not is_jwt.match(token):
        return True
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return True
    return _LXNS_WRITE_SCOPE in str(claims.get("scope", ""))


def _build_targets(
    binding,
) -> tuple[
    list[tuple[IScoreUpdateProvider, PlayerIdentifier | None, dict[str, Any]]],
    str | None,
]:
    """按主插件绑定装配上传目标。

    水鱼写入 2026-09-28 起强制 OAuth（Import-Token 带真实数据的写入被服务
    端 500 拒绝），导分凭据必须是**用户显式建立的凭据**：OAuth consent
    （``divingfish_oauth``，设备码授权 scope 一次带齐 read+write → subject
    换票含写权限）或 Import-Token（读基线仍有效，写入将失败并得到引导授权
    的部分失败提示）。仅 QQ 号/用户名可派生的 ref subject 只是公开标识，
    不再单独构成导分凭据——存量「仅 QQ」行（consent 至多多读、普遍缺失）
    换票/写入必败，装配它只会把本来成功的他站导分拖死。落雪 token 缺
    ``write_player`` scope（旧版授权，只读）时跳过落雪目标并给出重绑提示，
    不影响水鱼导出。标识类型含 None 占位与 run_update/链函数签名对齐
    （list 不型变）。
    """
    targets: list[
        tuple[IScoreUpdateProvider, PlayerIdentifier | None, dict[str, Any]]
    ] = []
    if binding.divingfish_oauth:
        subject = binding_service.divingfish_subject(binding)
        df_credentials = subject or binding.divingfish_import_token
    else:
        df_credentials = binding.divingfish_import_token
    if df_credentials:
        targets.append(
            (
                divingfish_provider,
                PlayerIdentifier(credentials=df_credentials),
                {"name": "水鱼"},
            )
        )
    lx_note: str | None = None
    if binding.lxns_token:
        if _lxns_writable(binding.lxns_token):
            targets.append(
                (
                    lxns_provider,
                    PlayerIdentifier(credentials=binding.lxns_token),
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


class ImportFailed(Exception):
    """导分失败的准确用户文案（handle_errors 直接展示 message）。"""


def _failure_hint(name: str, exc: Exception) -> str:
    """部分失败目标的用户文案：按异常类型映射，目标名点明是哪一站没导上。"""
    if isinstance(exc, ImportFailed):
        return str(exc)  # 续期阶梯产物（重绑/暂时未能同步），文案已完整
    if isinstance(exc, PlayerNotAuthorizedError):
        if name == "水鱼":
            return "水鱼已要求成绩写入走 OAuth 授权，请发送「绑定水鱼」完成一次授权"
        return f"{name}未授权成绩写入，请重新「绑定{name}」"
    if isinstance(exc, InvalidPlayerIdentifierError):
        if name == "水鱼":
            return "水鱼 Import-Token 已失效，请到主插件重新绑定"
        return f"{name}凭据已失效，请重新「绑定{name}」"
    if isinstance(exc, PrivacyLimitationError):
        return f"未同意{name}的相关用户协议，无法完成该操作"
    if isinstance(exc, InvalidJsonError):
        return f"{name}服务暂时不可用（可能维护中），请稍后再试"
    if isinstance(exc, RateLimitError):
        return f"{name}今日请求配额已用完，请明天再试"
    return f"{name}导出失败：{exc!r}"


_import_locks: dict[tuple[str, str], asyncio.Lock] = {}
"""按 (platform, user_id) 的导分互斥锁：同一用户并发「导」会各自按同一旧
基线判增量（双份拉取/上传）、桥接游玩次数双计、observe 撞主键被吞。锁表
按真实用户键增长，进程内有界，不回收。"""


async def _run_with_refresh(
    binding,
    source: list,
    qrcode: str | None,
    full: bool,
    notify_slow=None,
    pc_hook=None,
) -> tuple[float, int, str | None, list[str], list[tuple[str, Exception]]]:
    """执行一次传分；落雪 access_token 仅 15 分钟有效，凭据失效（401）时用
    refresh_token 续期落库后重试（不限主插件 service 语义）。返回 (用时秒,
    跳过条数, 落雪只读提示, 目标名列表, 部分失败目标列表)。

    落雪续期两条入口汇入同一阶梯（5s/10s 退避，Q43 新令牌生效延迟）：
    - 整链失败（run_update 上抛：全部目标失败/源失败）且归属落雪——续期后
      重试，耗尽抛 ImportFailed「暂时未能同步」（handler 映射为整链文案）；
    - 部分失败（他站成功、落雪 401 进失败列表）——同样续期重试，耗尽后
      落雪失败项改记「暂时未能同步」随返回，他站成功结果不受影响。
    续期 dead（rt 已过期）分两态：整链抛「重新绑定落雪」；部分失败把落雪
    失败项改记同款重绑文案。skip（无凭据/OAuth 未配置）整链原样上抛、
    部分失败原样保留（如水鱼 Import-Token 失效文案本就准确）。
    """

    # 落雪 token 过期预检（Q43）：导分前 JWT exp 已过/临近就先续期，省掉
    # 写端点上的必败 401 首跳；best-effort，失败走下方 401 驱动链路
    await binding_service.preflight_lxns(binding)

    async def attempt():
        targets, lx_note = _build_targets(binding)
        duration, skipped, failures = await run_update(
            client,
            source,
            targets,
            full=full,
            max_retries=plugin_config.awmc_su_max_retries,
            pc_hook=pc_hook,
        )
        return (
            duration,
            skipped,
            lx_note,
            [kw["name"] for _, _, kw in targets],
            failures,
        )

    def lx_auth_failure(
        failures: list[tuple[str, Exception]],
    ) -> InvalidPlayerIdentifierError | None:
        """部分失败列表中的落雪凭据失效项（401 类，续期重试对象）。"""
        return next(
            (
                exc
                for name, exc in failures
                if name == "落雪" and isinstance(exc, InvalidPlayerIdentifierError)
            ),
            None,
        )

    def replace_failure(result, name: str, exc: Exception):
        """把失败列表中指定目标的异常替换为定文案（其他目标结果不动）。"""
        duration, skipped, lx_note, names, failures = result
        failures = [(n, exc if n == name else e) for n, e in failures]
        return duration, skipped, lx_note, names, failures

    try:
        result = await attempt()
    except InvalidPlayerIdentifierError as exc:
        # 整链失败：水鱼凭据失效同抛此异常且 maimai_py 异常无 provider 标识，
        # 优先读 updater 链内挂到异常上的报错目标名（FAIL_TARGET_ATTR）；无
        # 标签时退回按目标装配判定——落雪不在目标内必然不是落雪失效。
        # 两者均非落雪则立即上抛，不白等续期退避（handler 的 token 无效
        # 文案本就对）。
        fail_name = getattr(exc, FAIL_TARGET_ATTR, None)
        if fail_name is not None and fail_name != "落雪":
            raise
        if "落雪" not in [kw["name"] for _, _, kw in _build_targets(binding)[0]]:
            raise
        status = await binding_service.refresh_lxns(binding)
        if status == "dead":
            raise ImportFailed("落雪授权已过期，请重新绑定落雪") from exc
        if status != "refreshed":
            raise
        result = None
    else:
        fail = lx_auth_failure(result[4])
        if fail is None:
            return result
        status = await binding_service.refresh_lxns(binding)
        if status == "dead":
            return replace_failure(
                result, "落雪", ImportFailed("落雪授权已过期，请重新绑定落雪")
            )
        if status != "refreshed":
            return result
    # 落雪续期阶梯（refreshed 已确认）：新令牌生效有短延迟，5s/10s 两级
    # 退避重试；进入 10s 档时触发慢查询提示（整链/部分失败两态共用）
    last: InvalidPlayerIdentifierError | None = None
    notified = False
    for delay in (5, 10):
        if delay >= 10 and notify_slow is not None and not notified:
            notified = True
            try:
                await notify_slow()
            except Exception:
                logger.debug("慢查询提示发送失败（不影响导分）")
        await asyncio.sleep(delay)
        logger.info("落雪 access_token 已续期，重试传分")
        try:
            result = await attempt()
        except InvalidPlayerIdentifierError as exc:
            last = exc
            continue
        if lx_auth_failure(result[4]) is None:
            return result  # 落雪已救回；水鱼侧部分失败（若有）保留随返回
        last = lx_auth_failure(result[4])
    # 阶梯耗尽：部分失败态把落雪失败项改记「暂时未能同步」随返回；整链态
    # （阶梯内仍全失败上抛）保持 ImportFailed 上抛由 handler 映射
    exhausted = ImportFailed("落雪数据暂时未能同步，请一分钟后再试")
    if result is not None:
        return replace_failure(result, "落雪", exhausted)
    raise exhausted from last


@update_cmd.handle()
@handle_errors(except_with_message=(SaltApiError, ImportFailed))
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
    has_df = binding is not None and bool(
        binding.divingfish_import_token or binding.divingfish_oauth
    )
    if binding is None or not (has_df or binding.lxns_token):
        msg = (
            "没绑数据站你怎么导。。。先对我说“导帮助”看看怎么绑定喵"
            if special
            else "尚未绑定水鱼或落雪 token，请先使用 awmc-helper 主插件绑定"
        )
        await UniMessage.text(f" {msg}").finish(at_sender=True)
    targets, lx_note = _build_targets(binding)
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

    # 互斥覆盖二维码探测 → 上传 → 游玩次数观测 → 最近时间落库全程；
    # 进行中的第二次指令快速回复而非静默排队（FinishedException 穿过
    # async with 释放锁，无泄漏路径）
    lock = _import_locks.setdefault((platform, user_id), asyncio.Lock())
    if lock.locked():
        await UniMessage.text(" 上一次导分还在进行中，请稍等完成后再试").finish(
            at_sender=True
        )
    async with lock:
        qrcode: str | None = None
        if qr_input:
            qr, arcade_user_id = await _resolve_qrcode(qr_input)
            if arcade_user_id != wb.arcade_user_id:
                msg = (
                    "怎么，还想帮别人导一导？"
                    if special
                    else "你提供的二维码所对应账号与已绑定的账号不匹配，"
                    "请检查后重新输入"
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

        async def notify_slow():
            await UniMessage.text(" 比预期时间要长，再稍等一下…").send(at_sender=True)

        # 游玩次数观测：链路成功后恰好调用一次（扫码全量=权威替换，简略=桥接）
        async def pc_hook(source_scores, target_dicts):
            await play_count_store.observe(
                wb.arcade_user_id,
                source_scores,
                target_dicts,
                anchored=bool(qrcode),
            )

        try:
            # skipped 仅服务端统计口径，删除曲静默跳过、不向用户提示
            duration, _skipped, lx_note, names, failures = await _run_with_refresh(
                binding,
                source,
                qrcode,
                full=bool(qrcode),
                notify_slow=notify_slow,
                pc_hook=pc_hook,
            )
        except InvalidPlayerIdentifierError:
            await UniMessage.text(
                " 成绩导入 token 无效，请到主插件重新绑定水鱼/落雪 token"
            ).finish(at_sender=True)
        except PlayerNotAuthorizedError:
            await UniMessage.text(
                " 水鱼已要求所有成绩写入走 OAuth 授权："
                "请发送「绑定水鱼」完成一次授权后重试"
            ).finish(at_sender=True)
        except PrivacyLimitationError:
            await UniMessage.text(
                " 你没有同意数据站的相关用户协议，无法完成该操作"
            ).finish(at_sender=True)
        except InvalidJsonError:
            # 数据站返回非 JSON（500 HTML 等）：多见于凌晨维护窗口，
            # 服务端问题非本插件故障
            await UniMessage.text(
                " 数据站服务暂时不可用（可能维护中），成绩可能已部分上传，请稍后再试"
            ).finish(at_sender=True)

        await wechat_store.set_last_update(
            platform, user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        # 部分成功语义：failures 非空 = 至少一站成功 + 若干站失败（全失败在
        # updater 层已上抛走上面的整链映射）。成功目标照常报喜，失败目标逐
        # 一给出可行动原因（水鱼写权限引导授权等），lx_note 追加在最后。
        failed_names = {name for name, _ in failures}
        ok_names = [n for n in names if n not in failed_names]
        target_str = "和".join(ok_names)
        fail_lines = "".join(
            f"\n· {name}没导上去喵：{_failure_hint(name, exc)}"
            if special
            else f"\n· {name}未导出：{_failure_hint(name, exc)}"
            for name, exc in failures
        )
        if special:
            # 彩蛋部分失败：以「导出来了，但...」开头列失败项，报喜段照旧
            head = ("导出来了，但..." + fail_lines + "\n") if failures else ""
            msg = (
                head
                + f"导到{target_str}了喵！\n你这次导了{duration:.2f}秒，很厉害了喵~\n"
                f"怎么导的：{'好好的导' if qrcode else '简单的导'}"
            )
        else:
            msg = (
                f"上传分数至{target_str}成功！\n本次上传用时{duration:.2f}秒\n"
                f"上传方式：{'全量上传' if qrcode else '简略上传'}"
            ) + fail_lines
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


@pc_list_cmd.handle()
@handle_errors("查询失败", except_with_message=(UserScoreError,))
async def _(
    session: Session = UniSession(),
    groups: tuple = RegexGroup(),
):
    """<定数/等级>pc列表：游玩次数降序成绩列表（行卡副行 pc: N，复用主插件版式）。

    分数前缀处理与主插件分数列表一致：带小数点按定数匹配（13.0），否则按
    标级匹配（13 / 13+）；宴谱按其定数（.0/.7）自然入列。成绩展示字段来自
    当前数据源（NET 数据源在 get_scores_all 内被 _guard_cn 拦截，仅国服源
    可用），次数来自本插件 play_count 表。
    """
    ds_raw, page_raw = groups
    page = parse_page(page_raw)
    platform, user_id = session_keys(session)

    binding = await binding_service.ensure(*session_keys(session))
    scores = await score_service.get_scores_all(binding, notify_slow=slow_notice())

    wb = await wechat_store.get(platform, user_id)
    if wb is None or not wb.arcade_user_id:
        await UniMessage.text(" 尚未绑定微信二维码，暂无游玩次数数据").finish(
            at_sender=True
        )
    pc_map = {
        (r.music_id, r.type, r.level_index): r.play_count
        for r in await play_count_store.counts(wb.arcade_user_id)
    }
    if not pc_map:
        await UniMessage.text(
            " 暂无游玩次数数据，请先「导」一次；带二维码私聊导分可校准全部次数"
        ).finish(at_sender=True)

    # 分数前缀同主插件分数列表口径：带小数点=定数，否则=标级；
    # 宴谱按其定数（.0/.7）自然入列
    if "." in ds_raw:
        ds = float(ds_raw)
        matched = [s for s in scores.scores if abs(s.level_value - ds) < 0.05]
    else:
        matched = [s for s in scores.scores if s.level == ds_raw]
    matched = [
        s for s in matched if (s.id, s.type.value, s.level_index.value) in pc_map
    ]
    if not matched:
        await UniMessage.text("  没有找到符合条件的成绩").finish(at_sender=True)

    def pc_of(s) -> int:
        return pc_map[(s.id, s.type.value, s.level_index.value)]

    matched.sort(key=lambda s: (-pc_of(s), -(s.achievements or 0)))

    if page == 1 and await play_count_store.last_full_at(wb.arcade_user_id) is None:
        await UniMessage.text(
            " 提示：尚未扫码校准，次数为导分增量估算；带二维码私聊「导」一次可校准"
        ).send(at_sender=True)

    end_page = max(1, -(-len(matched) // 80))
    real = min(max(page, 1), end_page)
    card = DrawScore(
        280 + score_list_height(len(matched), real, end_page),
        service=service_display(binding),
    )
    png = card.draw_score_list(
        ds_raw,
        matched,
        real,
        end_page,
        sub_of=lambda s: f"pc: {pc_of(s)}",
    )
    await UniMessage.image(raw=png).finish(at_sender=True)
