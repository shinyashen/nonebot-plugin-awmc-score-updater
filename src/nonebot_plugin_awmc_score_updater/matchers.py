"""指令层：导/传分、绑定微信、帮助。

「导 + 二维码」全量上传的群白名单规则：私聊始终可用；群聊仅当群号在
``awmc_su_whitelist_groups`` 白名单（默认空）时放行——二维码内容等价账号
凭据，群内发送有泄露风险。

水鱼/落雪凭据不在此绑定：直接读主插件 ``user_binding``，未绑定时引导用户
去主插件指令。
"""

import asyncio
from typing import Any
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
    InvalidDeveloperTokenError,
    InvalidPlayerIdentifierError,
)
from nonebot_plugin_uninfo import Session, SceneType, UniSession
from maimai_py.providers.base import IScoreUpdateProvider
from nonebot_plugin_alconna.uniseg import UniMessage
from nonebot_plugin_awmc_helper.constants import DEFAULT_THEME
from nonebot_plugin_awmc_helper.core.help import (
    Guide,
    GuidePage,
    GuideStep,
    CommandSpec,
    page_text,
    page_entries,
    help_registry,
)
from nonebot_plugin_awmc_helper.core.score import (
    UserScoreError,
    build_bests,
    score_service,
)
from nonebot_plugin_awmc_helper.core.utils import (
    parse_page,
    slow_notice,
    handle_errors,
    player_display_name,
)
from nonebot_plugin_awmc_helper.core.client import (
    client,
    lxns_provider,
    divingfish_provider,
)
from nonebot_plugin_awmc_helper.core.binding import (
    at_tolerant,
    session_keys,
    binding_service,
    service_display,
    resolve_query_binding,
)
from nonebot_plugin_awmc_helper.core.forward import try_send_forward
from nonebot_plugin_awmc_helper.core.ext.lxns import token_writable
from nonebot_plugin_awmc_helper.core.render.score import (
    SCORE_LIST_HEAD_HEIGHT,
    DrawScore,
    score_list_page,
    score_list_height,
)
from nonebot_plugin_awmc_helper.core.render.tools import text_to_image, image_to_bytes
from nonebot_plugin_awmc_helper.core.render.best50 import best50_bytes

from .store import pc_key, row_key, wechat_store, play_count_store
from .config import plugin_config
from .saltapi import SaltApiError, parse_qrcode, extract_qrcode
from .updater import FAIL_TARGET_ATTR, SaltArcadeProvider, run_update

update_cmd = on_command("导", aliases={"传分", "上传分数", "wmupdate"}, block=True)
help_cmd = on_command("导帮助", aliases={"传分帮助", "上传分数帮助"}, block=True)
bindwx_cmd = on_command("绑定微信", aliases={"bindwx", "微信绑定"}, block=True)
# 分数前缀与主插件分数列表同口径（core.combo numeric_level 裸数字解析，
# parse_combo）：整数=标级（13、13+），小数=定数（13.0）；13pc列表 即
# 标级 13 全部谱面（定数 13.0-13.5）的 pc 排行
pc_list_cmd = on_regex(
    at_tolerant(r"^([0-9]+(?:\.[0-9]+)?\+?)\s?pc列表\s?([0-9]+)?$"),
    block=True,
)
pc50_cmd = on_command("pc50", aliases={"PC50"}, block=True)


"""落雪 OAuth access_token（JWT）形态判定复用 maimai-py 单源。

注意不能以「是否 JWT」判断可写性——重绑后的新授权 token 同样是 JWT，
需解码 payload 的 scope 声明确认（access_token 仅 15 分钟有效，主插件
靠 refresh_token 自动续期，续期签发的 scope 随应用当前权限）。
"""

# 上传目标显示名：目标装配与失败归属判定的比较单源（比较点一律引常量）；
# 文案句子内嵌的「水鱼/落雪」字样是完整句子而非目标名比较，不引此常量
_TARGET_DF = "水鱼"
_TARGET_LX = "落雪"

# pc 指令（pc列表/pc50）共用文案：前置检查收敛于 _resolve_pc_context，
# 收尾兜底（pc50 全零行）与校准提示小助手亦引用
_PC_NO_WECHAT_HINT = "尚未绑定微信二维码，暂无游玩次数数据"
_PC_NO_DATA_HINT = "暂无游玩次数数据，请先「导」一次；带二维码私聊导分可校准全部次数"
_PC_UNCALIBRATED_HINT = (
    " 提示：尚未扫码校准，次数为导分增量估算；带二维码私聊「导」一次可校准"
)

# 落雪续期退避阶梯（秒，Q43 新令牌生效延迟）：5s/10s 两级，末档触发慢查询提示
_LXNS_RETRY_LADDER = (5, 10)

# 定数匹配容差（13.0pc列表 挑谱）：与主插件 combo 定数过滤（core.combo
# _ds_cond 的 round 一位小数口径）在 x.x5 边界行为不同（本处 abs 差 < 容差），
# 各自产品语义，改一侧须核对另一侧
_DS_MATCH_TOL = 0.05

_LXNS_REBIND_HINT = (
    "检测到你的落雪授权不含成绩写入权限，本次未导出落雪；"
    "请重新「绑定落雪」完成授权后即可导分"
)


def _lxns_writable(token: str) -> bool:
    """落雪凭据是否可写成绩（scope 知识单源主插件 ``core.ext.lxns.token_writable``）。

    个人 API 密钥（非 JWT）与解码失败均按可写处理（交由运行时 401 文案
    兜底）；JWT payload 的 scope 不含 write_player 不可写。
    """
    writable = token_writable(token)
    return True if writable is None else writable


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
                {"name": _TARGET_DF},
            )
        )
    lx_note: str | None = None
    if binding.lxns_token:
        if _lxns_writable(binding.lxns_token):
            targets.append(
                (
                    lxns_provider,
                    PlayerIdentifier(credentials=binding.lxns_token),
                    {"name": _TARGET_LX},
                )
            )
        else:
            lx_note = _LXNS_REBIND_HINT
    return targets, lx_note


async def _finish_if_jp_view(binding) -> None:
    """pc 次数排行对日服（jp 视图）数据源整链不可用（2026-09-30 拍板）。

    pc 数据采集自国服机台导分，日服数据源无权威游玩次数——官方
    musicDetail 全量抓取（每曲一跳、数百请求）风控代价过高，不立项
    （调研留档 dxrating-net-notes §6）。入口即拦，不给 pc/微信前置提示误导。
    """
    if score_service.view_of(binding.service) == "jp":
        await UniMessage.text(
            " 游玩次数排行仅支持国服数据源（次数采集自国服机台导分），"
            "日服 NET 暂不支持该指令"
        ).finish(at_sender=True)


async def _resolve_pc_context(session: Session, event: Event | None):
    """pc 指令（pc列表/pc50）共用前置：绑定解析 → 日服视图拦截 → 微信绑定
    与 pc 数据检查，返回 (binding, who, wb, pc_map) 供两 handler 继续。

    @ 代查（2026-09-30）：绑定/微信/pc 数据全部取查询目标（at 只读解析）；
    微信绑定与 pc 数据的前置检查先于全量成绩拉取（未绑微信/从未导分的用户
    不必白等数据站的慢查询）。前置不满足直接 .finish() 终止
    （FinishedException 上穿本函数），不存在不可达的返回路径。
    """
    platform, user_id = session_keys(session)
    binding, at_target = await resolve_query_binding(session, event)
    await _finish_if_jp_view(binding)
    target_id = at_target or user_id
    who = "对方" if at_target else ""
    wb = await wechat_store.get(platform, target_id)
    if wb is None or not wb.arcade_user_id:
        await UniMessage.text(f" {who}{_PC_NO_WECHAT_HINT}").finish(at_sender=True)
    pc_map = {
        row_key(r): r.play_count
        for r in await play_count_store.counts(wb.arcade_user_id)
    }
    if not pc_map:
        await UniMessage.text(f" {who}{_PC_NO_DATA_HINT}").finish(at_sender=True)
    return binding, who, wb, pc_map


async def _hint_if_uncalibrated(wb, *, page: int = 1) -> None:
    """从未扫码校准（last_full_at 空）时提示次数为导分增量估算；分页列表仅
    首页提示（pc50 无分页概念，恒满足）。仅 send 不 finish，不影响出图。"""
    if page == 1 and await play_count_store.last_full_at(wb.arcade_user_id) is None:
        await UniMessage.text(_PC_UNCALIBRATED_HINT).send(at_sender=True)


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
        if name == _TARGET_DF:
            return "水鱼已要求成绩写入走 OAuth 授权，请发送「绑定水鱼」完成一次授权"
        return f"{name}未授权成绩写入，请重新「绑定{name}」"
    if isinstance(exc, InvalidPlayerIdentifierError):
        if name == _TARGET_DF:
            return "水鱼 Import-Token 已失效，请到主插件重新绑定"
        return f"{name}凭据已失效，请重新「绑定{name}」"
    if isinstance(exc, InvalidDeveloperTokenError):
        # 1.6.0 起 dev 端点已从库中删除，此异常只剩 OAuth 应用凭据缺失/无效
        # 一类部署问题（文案与主插件 core.score 同口径）
        return "水鱼 OAuth 应用凭据无效或缺失，请联系管理员检查部署配置"
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
    full: bool,
    notify_slow=None,
    pc_hook=None,
) -> tuple[float, int, str | None, list[str], list[tuple[str, Exception]]]:
    """执行一次传分；落雪 access_token 仅 15 分钟有效，凭据失效（401）时用
    refresh_token 续期落库后重试（不限主插件 service 语义）。返回 (用时秒,
    跳过条数, 落雪只读提示, 目标名列表, 部分失败目标列表)。

    落雪续期两条入口汇入同一阶梯（_LXNS_RETRY_LADDER 退避，Q43 新令牌生效
    延迟）：
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
                if name == _TARGET_LX and isinstance(exc, InvalidPlayerIdentifierError)
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
        if fail_name is not None and fail_name != _TARGET_LX:
            raise
        if _TARGET_LX not in [kw["name"] for _, _, kw in _build_targets(binding)[0]]:
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
                result, _TARGET_LX, ImportFailed("落雪授权已过期，请重新绑定落雪")
            )
        if status != "refreshed":
            return result
    # 落雪续期阶梯（refreshed 已确认，:data:`_LXNS_RETRY_LADDER`）：进入末档
    # 时触发慢查询提示（整链/部分失败两态共用）
    last: InvalidPlayerIdentifierError | None = None
    notified = False
    for delay in _LXNS_RETRY_LADDER:
        if delay >= _LXNS_RETRY_LADDER[-1] and notify_slow is not None and not notified:
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
        return replace_failure(result, _TARGET_LX, exhausted)
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
        _g = help_registry.guides["导分"]
        await UniMessage.text(
            f"{_g.intro}\n前置条件：{_g.prerequisites}\n发送「导帮助」查看分步流程"
        ).finish(at_sender=True)
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
    if not targets:
        # 无目标可导：有落雪只读提示用之；否则（OAuth 标志在但 subject 派生
        # 不出且无 Import-Token 等装配盲区）给通用引导——不能落进后面
        # run_update 的「没有可用的成绩数据库」兜底误导已绑用户
        await UniMessage.text(
            f" {lx_note or '当前绑定没有可用的导出目标，请检查水鱼/落雪绑定'}"
        ).finish(at_sender=True)

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
                full=bool(qrcode),
                # 落雪续期进入 10s 档的慢查询提示复用 core 单源（发送失败静默）
                notify_slow=slow_notice(),
                pc_hook=pc_hook,
            )
        except (
            InvalidPlayerIdentifierError,
            PlayerNotAuthorizedError,
            PrivacyLimitationError,
            InvalidJsonError,
            RateLimitError,
            InvalidDeveloperTokenError,
        ) as exc:
            # 整链失败与部分失败共用 _failure_hint 单一映射（T-13，防两套文案
            # 漂移）；目标名读链内挂的归属标签（FAIL_TARGET_ATTR），无标签
            # （理论不可达，run_update 直抛等）退回中性「数据站」
            name = getattr(exc, FAIL_TARGET_ATTR, None) or "数据站"
            await UniMessage.text(f" {_failure_hint(name, exc)}").finish(at_sender=True)

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
    # OneBot v11 合并转发（M10「导分」指南页）；失败或其他适配器降级为
    # 文字渲染图片（纯文本字数过多）
    page = GuidePage(guide=help_registry.guides["导分"])
    entries = page_entries(help_registry, page)
    group_id = str(session.scene.id) if session.scene.type == SceneType.GROUP else None
    user_id = None if group_id else str(session.user.id)
    if await try_send_forward(bot, entries, group_id=group_id, user_id=user_id):
        return
    await UniMessage.image(
        raw=image_to_bytes(text_to_image(page_text(help_registry, page)))
    ).finish(at_sender=True)


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
    event: Event | None = None,
    groups: tuple = RegexGroup(),
):
    """<定数/等级>pc列表：游玩次数降序成绩列表（行卡副行 pc: N，复用主插件版式）。

    分数前缀处理与主插件分数列表一致：带小数点按定数匹配（13.0），否则按
    标级匹配（13 / 13+）；宴谱按其定数（.0/.7）自然入列。成绩展示字段来自
    当前数据源，次数来自本插件 play_count 表——次数采集自国服机台导分，
    故日服（jp 视图）数据源整链不可用（:meth:`_finish_if_jp_view` 入口拦截）；
    支持 @某人 代查（前置收敛于 :func:`_resolve_pc_context`）。
    """
    ds_raw, page_raw = groups
    page = parse_page(page_raw)

    binding, _, wb, pc_map = await _resolve_pc_context(session, event)
    scores = await score_service.get_scores_all(binding, notify_slow=slow_notice())

    # 分数前缀同主插件分数列表口径：带小数点=定数，否则=标级；
    # 宴谱按其定数（.0/.7）自然入列
    if "." in ds_raw:
        ds = float(ds_raw)
        matched = [s for s in scores.scores if abs(s.level_value - ds) < _DS_MATCH_TOL]
    else:
        matched = [s for s in scores.scores if s.level == ds_raw]
    matched = [s for s in matched if pc_key(s) in pc_map]
    if not matched:
        await UniMessage.text(" 没有找到符合条件的成绩").finish(at_sender=True)

    def pc_of(s) -> int:
        return pc_map[pc_key(s)]

    matched.sort(key=lambda s: (-pc_of(s), -(s.achievements or 0)))

    await _hint_if_uncalibrated(wb, page=page)

    end_page, real = score_list_page(len(matched), page)
    card = DrawScore(
        SCORE_LIST_HEAD_HEIGHT + score_list_height(len(matched), real, end_page),
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


@pc50_cmd.handle()
@handle_errors("查询失败", except_with_message=(UserScoreError,))
async def _(
    session: Session = UniSession(),
    event: Event | None = None,
):
    """pc50：游玩次数 Top50（旧版本 35 + 新版本 15，B50 版式）。

    行为**谱面**粒度：一个谱面只对应一个难度，排序键 = 该谱面自身的 pc
    （平手比达成率，与 13pc列表 同口径），全难度谱面同池竞争；35/15 版本
    拆分走主插件公共 build_bests，副行经 sub_of 钩子显示 pc（同 13pc列表
    格式）。pc 表值为 0 的种子行（导分桥接未知次数）不入榜。头部 rating
    三数字沿用模板占位口径（所列成绩 RA 之和，与 ap50 一致）。次数采集自
    国服机台导分，故日服（jp 视图）数据源整链不可用
    （:meth:`_finish_if_jp_view` 入口拦截），且需先「导」过；
    支持 @某人 代查（前置收敛于 :func:`_resolve_pc_context`）。
    """
    binding, who, wb, pc_map = await _resolve_pc_context(session, event)

    scores = await score_service.get_scores_all(binding, notify_slow=slow_notice())
    rows = [s for s in scores.scores if pc_map.get(pc_key(s), 0) > 0]
    if not rows:
        await UniMessage.text(f" {who}{_PC_NO_DATA_HINT}").finish(at_sender=True)
    bests = build_bests(
        rows,
        key=lambda s: (pc_map[pc_key(s)], s.achievements or 0),
    )

    await _hint_if_uncalibrated(wb)

    player = await score_service.get_player(binding, notify_slow=slow_notice())
    png = await best50_bytes(
        player_display_name(player),
        bests.rating,
        bests.rating_b35,
        bests.rating_b15,
        bests.scores_b35,
        bests.scores_b15,
        player=player,
        qqid=binding_service.qq_of(binding),
        service=binding.service,
        theme=binding.theme or DEFAULT_THEME,
        sub_of=lambda s: f"pc: {pc_map[pc_key(s)]}",
    )
    await UniMessage.image(raw=png).finish(at_sender=True)


# ---------------------------------------------------------------- 帮助声明
# 指令按功能就近入主插件类别（M10 拍板⑦）：绑定微信→绑定、pc 排行→查分、
# 导→工具；「导分」指南为流程轴首个真实消费者

_SU_PLUGIN = "nonebot_plugin_awmc_score_updater"

help_registry.declare(
    plugin=_SU_PLUGIN,
    title="成绩导分",
    category="bind",
    commands=[
        CommandSpec(
            matcher=bindwx_cmd,
            name="绑定微信",
            aliases=("bindwx", "微信绑定"),
            scope="仅私聊",
            brief="绑定机台账号（二维码识别内容或页面链接）",
            detail=(
                "格式：绑定微信 <SGWCMAID.../https...>；"
                "二维码含账号凭据，禁止群聊使用。"
            ),
        ),
    ],
)
help_registry.declare(
    plugin=_SU_PLUGIN,
    title="成绩导分",
    category="score",
    commands=[
        # scope 手写（非 capability 自动派生）：限制来自「pc 次数为国服机台
        # 导分数据」这一本地数据边界，不是数据源的能力域
        CommandSpec(
            matcher=pc50_cmd,
            name="pc50",
            aliases=("PC50",),
            scope="仅国服数据源",
            brief="游玩次数 Top50（旧版本 35 + 新版本 15，B50 版式；@某人=代查）",
        ),
        CommandSpec(
            matcher=pc_list_cmd,
            name="<等级|定数>pc列表",
            scope="仅国服数据源",
            brief="游玩次数排行（口径同主插件分数列表，支持页码；@某人=代查）",
            detail="整数=标级（13pc列表），小数=定数（13.0pc列表）。",
        ),
    ],
)
help_registry.declare(
    plugin=_SU_PLUGIN,
    title="成绩导分",
    category="tools",
    commands=[
        CommandSpec(
            matcher=update_cmd,
            name="导",
            aliases=("传分", "上传分数", "wmupdate"),
            scope="全量上传仅私聊/白名单群",
            brief="上传国服成绩至水鱼/落雪（简略增量 / 带二维码全量并校准游玩次数）",
            detail="「导」字开头的指令有专属回复喵。",
        ),
    ],
)

help_registry.declare_guide(
    Guide(
        key="导分",
        title="导分",
        aliases=("传分",),
        intro=(
            "把国服成绩一键导出到水鱼/落雪成绩数据库。\n"
            "「导」不带二维码=简略上传（仅达成率与 DX 分增量）；带二维码=全量上传"
            "并校准游玩次数（仅私聊或白名单群）。\n"
            "pc50 / pc列表 可查看上传后的游玩次数排行。"
        ),
        prerequisites=(
            "机台账号 + 数据站绑定（至少其一）：① 绑定微信（仅私聊）；"
            "② 主插件 awmc-helper 的水鱼/落雪绑定，发指令给 bot 即可。"
        ),
        steps=[
            GuideStep(
                text="绑定机台账号（仅私聊，发二维码识别内容或页面链接）：",
                commands=("绑定微信",),
            ),
            GuideStep(
                text=(
                    "绑定水鱼（OAuth 授权——写成绩的唯一途径）："
                    "发「绑定水鱼」按引导完成设备码授权即可导分。\n"
                    "Import-Token 仅剩读取基线价值（写入会失败并提示授权），"
                    "需要全量成绩/牌子读取时才发「绑定水鱼token」绑定。"
                ),
                commands=("绑定水鱼", "绑定水鱼token"),
            ),
            GuideStep(
                text=(
                    "绑定落雪（OAuth 授权码 90 秒内直接回复给 bot；"
                    "也可好友码/Token 直绑）："
                ),
                commands=("绑定落雪",),
            ),
            GuideStep(
                text=("开导：全量上传发二维码，简略增量直接发；导完可查游玩次数排行："),
                commands=("导", "pc50", "<等级|定数>pc列表"),
            ),
        ],
        source=_SU_PLUGIN,
    )
)
