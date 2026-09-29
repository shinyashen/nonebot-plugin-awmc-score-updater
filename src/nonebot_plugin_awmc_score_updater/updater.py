"""传分核心：SaltNet 微信成绩 → 水鱼/落雪。

- :class:`SaltArcadeProvider`：maimai-py ``IScoreProvider`` 的 SaltNet 适配，
  供 updates 链作 source；
- :func:`delta_updates_chain`：增量上传——与目标已有成绩比较，只上传有提升
  或新增的部分（移植自 HoshinoBot 版 maimai-score-updater 的同名扩展方法）；
- :func:`run_update`：主流程编排（指数退避重试 + 计时），全量/增量二态。

目标端复用主插件 core.client 的唯一 ``MaimaiClient`` 单例；水鱼/落雪凭据
直接使用主插件 ``user_binding`` 的导入 token，本插件不存储、不重复绑定。

全链路只消费裸 ``Score``（provider.get_scores_all 直取，不经
``MaimaiScores.configure`` 扩展，对齐老插件「只填必要字段」）：传分只需
比达成率/DX 分绝对值并原样上传，扩展项 dx_star/版本/定数既用不上，其内部
按谱面物量算 dx_star，遇曲库零物量谱面（如缺数据的宴谱）直接除零——
2026-09-26 线上实测炸掉整条导分链。
"""

import time
import asyncio
import hashlib
from typing import Any
from dataclasses import replace
from collections.abc import Callable, Iterable, Awaitable

from maimai_py import LXNSProvider, MaimaiClient
from nonebot.log import logger
from maimai_py.enums import FCType, FSType, RateType
from maimai_py.models import Score, PlayerIdentifier
from maimai_py.exceptions import InvalidJsonError
from maimai_py.providers.base import IScoreProvider, IScoreUpdateProvider

from .store import pc_key
from .saltapi import SaltApiError, deser_score, fetch_score_payload

# 谱面键：(曲目 id, SongType.value, LevelIndex.value)——统一复用 store.pc_key，
# 供游玩次数桥接（observe 按 pc_key 元组取基线）与上传比对共用；字符串键与
# 元组键永不相等，曾致存量行永不 +1（键型静默失效）
ScoreKey = tuple[int, str, int]

ChainCallback = Callable[[list[Score], BaseException | None, dict[str, Any]], None]
# 成功链路的游玩次数观测钩子：(源成绩快照, 各数据站基线字典) —— 见 run_update
PCHook = Callable[[list[Score], list[dict[ScoreKey, Score]]], Awaitable[None]]

FAIL_TARGET_ATTR = "_awmc_fail_target"
"""异常实例上报错目标名的属性键。

maimai_py 异常（InvalidPlayerIdentifierError 等）无 provider 标识，水鱼凭据
失效与落雪同抛同型；链内取数/上传失败时把 kwargs.name 挂到异常实例上（免
包装类型、保持 maimai_py 异常族原样上抛），matchers 的落雪续期归属判定据此
区分是哪一数据站失效。
"""


def _tag_fail_target(exc: BaseException, name: str) -> None:
    try:
        setattr(exc, FAIL_TARGET_ATTR, name)
    except AttributeError:
        pass  # 极少数带 __slots__ 的异常类型挂不上，放弃归属信息


async def _fetch_tagged(name: str, sp, ident, client) -> list[Score]:
    """provider 取数；失败时在异常上挂目标名（见 FAIL_TARGET_ATTR）。"""
    try:
        return await sp.get_scores_all(ident, client)
    except Exception as e:
        _tag_fail_target(e, name)
        raise


async def _update_tagged(name: str, ident, batch: list[Score], tp, client) -> None:
    """provider 上传；失败时在异常上挂目标名（见 FAIL_TARGET_ATTR）。"""
    try:
        await client.updates(ident, batch, tp)
    except Exception as e:
        _tag_fail_target(e, name)
        raise


class SaltArcadeProvider(IScoreProvider):
    """SaltNet 微信成绩源：经 Realtvop 代理拉取华立微信端成绩明细。"""

    def __init__(self, main_url: str, fallback_url: str) -> None:
        self.main_url = main_url
        self.fallback_url = fallback_url

    def _hash(self) -> str:
        return hashlib.md5(b"salt_arcade").hexdigest()

    @staticmethod
    def make_identifier(userid: str, qrcode: str | None = None) -> PlayerIdentifier:
        """构造 SaltNet 源标识（credentials 为 dict：userid 必带，qrcode 全量时带）。"""
        return PlayerIdentifier(credentials={"userid": userid, "qrcode": qrcode or ""})

    async def get_scores_all(
        self, identifier: PlayerIdentifier, client: MaimaiClient
    ) -> list[Score]:
        assert isinstance(identifier.credentials, dict), (
            "SaltNet 源标识的 credentials 应为 dict"
        )
        userid = identifier.credentials.get("userid") or ""
        qrcode = identifier.credentials.get("qrcode") or None
        if not userid:
            raise SaltApiError("未绑定微信二维码，无法拉取机台成绩")
        payload = await fetch_score_payload(
            userid, qrcode, main_url=self.main_url, fallback_url=self.fallback_url
        )
        scores = [deser_score(music) for music in payload]
        # SaltNet 会保留已删除/下架曲目的残留成绩，水鱼 update_records 收到
        # 未收录曲目会服务端 500——以主插件曲库 CN 视图为过滤基准剔除
        # （视图已排除删除曲与禁用曲；handler 前置的 ensure_loaded 保证曲库就绪）
        from nonebot_plugin_awmc_helper.core.songs import song_service

        kept: list[Score] = []
        dropped = 0
        for score in scores:
            if await song_service.by_id(score.id % 10000) is not None:
                kept.append(score)
            else:
                dropped += 1
        if dropped:
            logger.warning(
                f"SaltNet 成绩含删除曲/未收录曲 {dropped} 条，已按主插件曲库剔除"
            )
        return kept


def _join_rev(scores: Iterable[Score]) -> Score:
    """目标多源成绩合并（仅在「各源都有该成绩」的交集上调用）：

    达成率/DX 分取各源最小值作为比较基准（保守：宁可多传不可漏传）；
    fc/fs 在有值目标内取更优（fc min / fs max），使单目标独占的更优达成
    情况也能经上传载荷跨目标补齐；全部缺失仍为 None。
    """
    scores_list = list(scores)
    if not scores_list:
        raise ValueError("至少需要一个 Score")
    # replace 起底真拷贝：首站条目还留在 target_dicts 里随返回值交 pc 观测作
    # 该站基线，原地 min/max 化会把基线污染成假值（机台真值对比假基线误判
    # 虚增 +1），必须不动输入对象
    res = replace(scores_list[0])
    res.achievements = min(s.achievements or 0 for s in scores_list)
    res.dx_score = min(s.dx_score or 0 for s in scores_list)
    # fc/fs 合成语义与 Hoshino 原版不同（2026-09-26 作者拍板改此处）：
    # 原版任一目标缺失即基准缺失（all(...) 门控），单目标独占的更优 fc/fs
    # 永远不会随上传补到缺失的目标上；现改为有值目标内取更优。备查。
    fc_values = [s.fc.value for s in scores_list if s.fc is not None]
    res.fc = FCType(min(fc_values)) if fc_values else None
    fs_values = [s.fs.value for s in scores_list if s.fs is not None]
    res.fs = FSType(max(fs_values)) if fs_values else None
    res.rate = RateType._from_achievement(res.achievements)
    res.play_count = min(s.play_count or 0 for s in scores_list)
    return res


def _compare(score: Score, other: Score | None) -> Score | None:
    """增量判定：与目标已有成绩比较，无提升返回 None，有提升返回合并后的成绩。"""
    if other is not None:
        if score.level_index != other.level_index or score.type != other.type:
            raise ValueError(
                "Cannot compare scores with different level indexes or types"
            )
        if (score.achievements or 0) <= (other.achievements or 0) and (
            score.dx_score or 0
        ) <= (other.dx_score or 0):
            return None
        score.achievements = max(score.achievements or 0, other.achievements or 0)
        score.dx_score = max(score.dx_score or 0, other.dx_score or 0)
        if score.fc != other.fc:
            self_fc = score.fc.value if score.fc is not None else 100
            other_fc = other.fc.value if other.fc is not None else 100
            selected_value = min(self_fc, other_fc)
            score.fc = FCType(selected_value) if selected_value != 100 else None
        if score.fs != other.fs:
            self_fs = score.fs.value if score.fs is not None else -1
            other_fs = other.fs.value if other.fs is not None else -1
            selected_value = max(self_fs, other_fs)
            score.fs = FSType(selected_value) if selected_value != -1 else None
        if score.rate != other.rate:
            # 评级取更优；外源成绩 rate 可能为空，缺省侧直接沿用另一侧
            if score.rate is not None and other.rate is not None:
                score.rate = RateType(min(score.rate.value, other.rate.value))
            elif score.rate is None:
                score.rate = other.rate
        if score.play_count != other.play_count:
            score.play_count = max(score.play_count or 0, other.play_count or 0)
    return score


async def _gather(
    client: MaimaiClient,
    providers: list[tuple[Any, PlayerIdentifier | None, dict[str, Any]]],
    callback: ChainCallback | None,
    mode: str,
) -> list[list[Score]]:
    """并行拉取一组 source/target 的裸成绩，收集成功结果，失败走 callback。

    直接调 ``provider.get_scores_all``，不经 ``client.scores``（后者内部
    ``configure`` 扩展见模块 docstring）。单个提供器失败不会中断整批
    （callback 通知后以空成绩占位），但整体 gather 遇到异常仍会向上传播
    ——由 run_update 的重试循环兜底。
    """
    tasks = []
    for sp, ident, kwargs in providers:
        if ident is None:
            continue
        if mode == "parallel" or (mode == "fallback" and len(tasks) == 0):
            task = asyncio.create_task(
                _fetch_tagged(kwargs.get("name", ""), sp, ident, client)
            )
            if callback is not None:
                task.add_done_callback(
                    lambda t, k=kwargs: callback(
                        t.result() if not t.exception() else [],
                        t.exception(),
                        k,
                    )
                )
            tasks.append(task)
    results = await asyncio.gather(*tasks)
    return [r for r in results if isinstance(r, list)]


_LXNS_IDS_CACHE: tuple[float, set[int]] | None = None
"""落雪曲库 id 集缓存：(拉取时刻, id 集)。TTL 内复用，避免阶梯重试与连续
导分重复拉列表（该拉取位于计时窗口内，重复拉取会虚增报给用户的用时）。"""
_LXNS_IDS_TTL = 180.0


def _lxns_ids_cache_clear() -> None:
    """清空曲库 id 缓存（测试用）。"""
    global _LXNS_IDS_CACHE
    _LXNS_IDS_CACHE = None


async def _lxns_known_song_ids() -> set[int] | None:
    """落雪当前曲库曲目 id 集（已删除曲目不在其中），供落雪目标预过滤。

    落雪对含未收录曲目的上传**整批拒绝**（HTTP 400 ``song not found``）——
    典型如已下架的限时宴谱：SaltNet 源有残留成绩、水鱼库仍收录、主插件
    规范表按「一侧缺失、记录保留」策略保留，maimai_py #60 的本地库 by_id
    守卫对此是盲区。取主插件 core.ext.lxns 的曲库列表（轻载荷 notes=false）
    作过滤基准，TTL 内复用缓存；任一异常返回 None = 本次不做预过滤
    （保持旧行为）。
    """
    global _LXNS_IDS_CACHE
    now = time.monotonic()
    if _LXNS_IDS_CACHE is not None and now - _LXNS_IDS_CACHE[0] < _LXNS_IDS_TTL:
        return _LXNS_IDS_CACHE[1]
    try:
        from nonebot_plugin_awmc_helper.core.ext.lxns import fetch_song_list

        data = await fetch_song_list(notes=False)
        ids = {int(s["id"]) for s in data.get("songs", []) if "id" in s}
    except Exception as e:
        logger.warning(f"落雪曲库列表获取失败，本次导分不做落雪侧预过滤：{e!r}")
        return None
    _LXNS_IDS_CACHE = (now, ids)
    return ids


async def delta_updates_chain(
    client: MaimaiClient,
    source: list[tuple[IScoreProvider, PlayerIdentifier | None, dict[str, Any]]],
    target: list[tuple[IScoreUpdateProvider, PlayerIdentifier | None, dict[str, Any]]],
    source_mode: str = "fallback",
    target_mode: str = "parallel",
    source_gather_callback: ChainCallback | None = None,
    target_gather_callback: ChainCallback | None = None,
    target_update_callback: ChainCallback | None = None,
    compare_target: bool = True,
) -> tuple[int, list[Score], list[dict[ScoreKey, Score]], list[tuple[str, Exception]]]:
    """增量/全量版 ``MaimaiClient.updates_chain``（裸成绩版）。

    ``compare_target=True``：源成绩与目标已有成绩比较后仅上传增量；
    ``compare_target=False``：全量——不拉取目标、全部上传（原全量走
    maimai_py ``updates_chain``，其经 ``client.scores`` 触发 configure
    扩展，同样会除零，2026-09-26 起弃用）。

    返回 (因数据站拒绝（未收录曲目触发 500）而跳过的成绩条数 + 落雪预
    过滤剔除数, 源成绩快照, 各数据站基线字典列表, 失败目标列表)。源成绩为
    机台真值（_compare 原地合并前的独立拷贝）、基线为上传前的数据站状态，
    二者供游玩次数观测（store.observe）比对；基线字典键为谱面元组键
    （pc_key 同构），基线列表可能为空（全量模式 / 目标拉取全败，此时桥接
    无基准）。

    部分成功语义（2026-09-28）：单目标上传失败不再上抛拖死整链——他站已
    写入的成绩不能因某一站（典型：水鱼写权限缺失）失败而白费。失败目标以
    (目标名, 异常) 收集返回，异常实例自带 FAIL_TARGET_ATTR 归属标签；**全部**
    目标失败仍上抛第一个异常，保持调用方的整链错误映射路径。

    目标 provider 必须同时支持拉取（IScoreProvider）与上传（IScoreUpdateProvider）。
    """
    for tp, _, _ in target:
        if not isinstance(tp, IScoreProvider):
            raise ValueError("Target provider does not support score fetching.")
        if not isinstance(tp, IScoreUpdateProvider):
            raise ValueError("Target provider does not support score updating.")

    # 源成绩拉取并合并（_join：同谱面取最高记录）
    source_scores_list = await _gather(
        client, source, source_gather_callback, source_mode
    )
    source_scores_unique: dict[ScoreKey, Score] = {}
    for scores in source_scores_list:
        for score in scores:
            key = pc_key(score)
            source_scores_unique[key] = score._join(source_scores_unique.get(key, None))
    # PC 观测用源成绩快照：必须在 _compare 之前取且须真拷贝——_compare 原地
    # 合并目标基准值，浅拷贝列表仍引用同一 Score 对象，快照会被污染失真
    source_scores = [replace(s) for s in source_scores_unique.values()]

    # 目标成绩拉取并取交集合并（_join_rev：保守基准）
    target_dicts: list[dict[ScoreKey, Score]] = []
    if compare_target:
        target_scores_list = await _gather(
            client, target, target_gather_callback, target_mode
        )
        target_dicts = [
            {pc_key(score): score for score in scores} for scores in target_scores_list
        ]
    if target_dicts:
        common_keys = set(target_dicts[0].keys())
        for d in target_dicts[1:]:
            common_keys.intersection_update(d.keys())
        merged_targets = {k: _join_rev(d[k] for d in target_dicts) for k in common_keys}
    else:
        merged_targets = {}

    # 增量判定并上传
    delta_scores: list[Score] = []
    for key, score in source_scores_unique.items():
        if delta := _compare(score, merged_targets.get(key, None)):
            delta_scores.append(delta)

    # 水鱼未收录新曲的成绩会让 update_records 服务端 500（2026-09-26 线上实测：
    # 空载荷 200、含未收录曲目 id 的载荷 500）。过滤基准 = 目标已有成绩出现过的
    # 曲目 id（目标确认收录）；500 后自动降级为仅传已收录部分并报告跳过数。
    # 全量模式不拉取目标、降级不可用，未收录直接报错（与原 updates_chain 一致）。
    known_song_ids = {score.id % 10000 for d in target_dicts for score in d.values()}
    skipped_unknown = 0

    # 落雪预过滤（kwargs.allowed_ids，见 _lxns_known_song_ids）：其曲库已删除
    # 的曲目（如过期宴谱）会让上传整批 400，按其曲库提前剔除并计入 skipped
    prefilter_dropped = 0
    for _, _, kwargs in target:
        allowed = kwargs.get("allowed_ids")
        if allowed is not None:
            prefilter_dropped = max(
                prefilter_dropped,
                sum(1 for s in delta_scores if s.id not in allowed),
            )
    if prefilter_dropped:
        logger.info(
            f"落雪曲库不含的曲目成绩 {prefilter_dropped} 条，已剔除（静默跳过）"
        )

    upload_tasks: list[asyncio.Task] = []
    # 与 upload_tasks 对齐的各上传任务目标名（降级重传按目标名单收窄用）
    upload_names: list[str] = []

    async def _schedule_upload(
        batch: list[Score], only_names: set[str] | None = None
    ) -> None:
        for tp, ident, kwargs in target:
            if ident is None:
                continue
            if only_names is not None and kwargs.get("name", "") not in only_names:
                continue
            batch_t = batch
            allowed = kwargs.get("allowed_ids")
            if allowed is not None:
                batch_t = [s for s in batch if s.id in allowed]
            if target_mode == "parallel" or (
                target_mode == "fallback" and not upload_tasks
            ):
                upload_tasks.append(
                    asyncio.create_task(
                        _update_tagged(
                            kwargs.get("name", ""), ident, batch_t, tp, client
                        )
                    )
                )
                upload_names.append(kwargs.get("name", ""))
                if (cb := target_update_callback) is not None:
                    # 闭包内变量收窄失效，回调经默认参数固定为非 None 局部
                    upload_tasks[-1].add_done_callback(
                        lambda t, k=kwargs, b=batch_t, cb=cb: cb(b, t.exception(), k)
                    )

    await _schedule_upload(delta_scores)
    results = await asyncio.gather(*upload_tasks, return_exceptions=True)

    if any(isinstance(r, InvalidJsonError) for r in results):
        filtered = [s for s in delta_scores if s.id % 10000 in known_song_ids]
        skipped_unknown = len(delta_scores) - len(filtered)
        if filtered and skipped_unknown:
            # 仅对返回 InvalidJsonError 的目标降级重传已收录部分：已成功站
            # 重传纯浪费（配额/去重），其他原因失败站重传也改不了结果；
            # failures 按目标合并两轮结果（首轮其他目标结果保留）
            retry_idx = [
                i for i, r in enumerate(results) if isinstance(r, InvalidJsonError)
            ]
            retry_names = {upload_names[i] for i in retry_idx}
            logger.warning(
                f"{'、'.join(retry_names)}拒绝上传（含未收录曲目成绩），"
                f"剔除 {skipped_unknown} 条后重传已收录部分"
            )
            upload_tasks.clear()
            upload_names.clear()
            await _schedule_upload(filtered, only_names=retry_names)
            retry_results = await asyncio.gather(*upload_tasks, return_exceptions=True)
            merged = list(results)
            # 重传轮沿用 target 遍历序，与 retry_idx 的首轮顺序一一对应
            for local_i, global_i in enumerate(retry_idx):
                merged[global_i] = retry_results[local_i]
            results = merged

    # 部分成功语义：失败目标收集返回（异常自带归属标签）；全失败仍上抛，
    # 由调用方既有映射给出整链错误文案
    failures: list[tuple[str, Exception]] = [
        (getattr(r, FAIL_TARGET_ATTR, "?"), r)
        for r in results
        if isinstance(r, Exception)
    ]
    total_targets = sum(1 for _, ident, _ in target if ident is not None)
    if failures and len(failures) >= total_targets:
        raise failures[0][1]
    return skipped_unknown + prefilter_dropped, source_scores, target_dicts, failures


async def run_update(
    client: MaimaiClient,
    # 与 maimai_py updates_chain 同款签名：允许 None 占位（链内跳过）；
    # list 对元素类型不型变，收窄为 PlayerIdentifier 会与链函数不兼容
    source: list[tuple[IScoreProvider, PlayerIdentifier | None, dict[str, Any]]],
    target: list[tuple[IScoreUpdateProvider, PlayerIdentifier | None, dict[str, Any]]],
    *,
    full: bool,
    max_retries: int = 3,
    gather_log_name: str = "salt",
    pc_hook: PCHook | None = None,
) -> tuple[float, int, list[tuple[str, Exception]]]:
    """执行一次传分：全量（跳过目标比对）或增量（与目标比对只传提升）。

    失败按指数退避重试（0.5s 起），重试耗尽后抛最后一次的异常，由调用方
    映射为用户文案。返回 (用时秒, 被数据站拒绝而跳过的成绩条数, 失败目标
    列表)。目标部分失败（至少一个站成功）不算链路失败：直接随返回值交出
    （写权限缺失等确定性失败重试无益），pc_hook 照常触发（观测按各站已有
    基线桥接，缺一站只是漏计不虚增）；全部目标失败/源失败仍上抛走重试。

    ``pc_hook``：游玩次数观测钩子，链路成功后以 (源成绩快照, 数据站基线)
    恰好调用一次（重试轮次只在成功那轮触发，不会重复计数）；钩子异常只
    记日志、不影响导分结果。
    """
    if not target:
        raise SaltApiError("没有可用的成绩数据库，请先绑定水鱼或落雪")

    def gather_callback(
        scores: list[Score], err: BaseException | None, ctx: dict[str, Any]
    ) -> None:
        if err:
            logger.error(
                f"从{ctx.get('name', gather_log_name)}源获取数据失败:\n{err!r}"
            )
        else:
            logger.info(
                f"从{ctx.get('name', gather_log_name)}源获取数据成功，"
                f"共 {len(scores)} 条成绩"
            )

    def update_callback(
        scores: list[Score], err: BaseException | None, ctx: dict[str, Any]
    ) -> None:
        if err:
            logger.error(f"更新到目标{ctx.get('name', '?')}失败:\n{err!r}")
        else:
            logger.info(
                f"更新到目标{ctx.get('name', '?')}成功，共 {len(scores)} 条成绩"
            )

    start = time.monotonic()
    # 落雪目标按其当前曲库预过滤（已删除曲目会上传整批 400；获取失败不过滤）
    if any(isinstance(tp, LXNSProvider) for tp, _, _ in target):
        known = await _lxns_known_song_ids()
        if known is not None:
            target = [
                (tp, ident, {**kw, "allowed_ids": known})
                if isinstance(tp, LXNSProvider)
                else (tp, ident, kw)
                for tp, ident, kw in target
            ]
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        skipped = 0
        try:
            skipped, source_scores, target_dicts, failures = await delta_updates_chain(
                client,
                source,
                target,
                "parallel",
                "parallel",
                gather_callback,
                None if full else gather_callback,
                update_callback,
                compare_target=not full,
            )
            if pc_hook is not None:
                try:
                    await pc_hook(source_scores, target_dicts)
                except Exception:
                    logger.warning("游玩次数观测失败（不影响本次导分）")
            return time.monotonic() - start, skipped, failures
        except Exception as e:  # 统一退避重试后交给调用方
            last_exc = e
            if attempt >= max_retries:
                raise
            delay = 0.5 * (2**attempt)
            logger.warning(
                f"传分第 {attempt + 1}/{max_retries} 次重试（等待 {delay}s）：{e!r}"
            )
            await asyncio.sleep(delay)
    # 循环内必然 return 或 raise，此处不可达；last_exc 收窄对类型检查器不可证
    raise last_exc  # type: ignore  # pragma: no cover
