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
from collections.abc import Callable, Iterable

from maimai_py import MaimaiClient
from nonebot.log import logger
from maimai_py.enums import FCType, FSType, RateType
from maimai_py.models import Score, PlayerIdentifier
from maimai_py.exceptions import InvalidJsonError
from maimai_py.providers.base import IScoreProvider, IScoreUpdateProvider

from .saltapi import SaltApiError, deser_score, fetch_score_payload

ChainCallback = Callable[[list[Score], BaseException | None, dict[str, Any]], None]


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
    res = scores_list[0]
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
        if (
            score.achievements <= other.achievements
            and score.dx_score <= other.dx_score
        ):
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
            task = asyncio.create_task(sp.get_scores_all(ident, client))
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
) -> int:
    """增量/全量版 ``MaimaiClient.updates_chain``（裸成绩版）。

    ``compare_target=True``：源成绩与目标已有成绩比较后仅上传增量；
    ``compare_target=False``：全量——不拉取目标、全部上传（原全量走
    maimai_py ``updates_chain``，其经 ``client.scores`` 触发 configure
    扩展，同样会除零，2026-09-26 起弃用）。

    返回因数据站拒绝（未收录曲目触发 500）而跳过的成绩条数（常规为 0）。

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
    source_scores_unique: dict[str, Score] = {}
    for scores in source_scores_list:
        for score in scores:
            key = f"{score.id} {score.type} {score.level_index}"
            source_scores_unique[key] = score._join(source_scores_unique.get(key, None))

    # 目标成绩拉取并取交集合并（_join_rev：保守基准）
    target_dicts: list[dict[str, Score]] = []
    if compare_target:
        target_scores_list = await _gather(
            client, target, target_gather_callback, target_mode
        )
        target_dicts = [
            {f"{score.id} {score.type} {score.level_index}": score for score in scores}
            for scores in target_scores_list
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

    upload_tasks: list[asyncio.Task] = []

    async def _schedule_upload(batch: list[Score]) -> None:
        for tp, ident, kwargs in target:
            if ident is None:
                continue
            if target_mode == "parallel" or (
                target_mode == "fallback" and not upload_tasks
            ):
                upload_tasks.append(
                    asyncio.create_task(client.updates(ident, batch, tp))
                )
                if target_update_callback is not None:
                    upload_tasks[-1].add_done_callback(
                        lambda t, k=kwargs, b=batch: target_update_callback(
                            b, t.exception(), k
                        )
                    )

    await _schedule_upload(delta_scores)
    results = await asyncio.gather(*upload_tasks, return_exceptions=True)

    if any(isinstance(r, InvalidJsonError) for r in results):
        filtered = [s for s in delta_scores if s.id % 10000 in known_song_ids]
        skipped_unknown = len(delta_scores) - len(filtered)
        if filtered and skipped_unknown:
            logger.warning(
                f"数据站拒绝上传（含未收录曲目成绩），"
                f"剔除 {skipped_unknown} 条后重传已收录部分"
            )
            upload_tasks.clear()
            await _schedule_upload(filtered)
            results = await asyncio.gather(*upload_tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            raise r
    return skipped_unknown


async def run_update(
    client: MaimaiClient,
    source: list[tuple[IScoreProvider, PlayerIdentifier, dict[str, Any]]],
    target: list[tuple[IScoreUpdateProvider, PlayerIdentifier, dict[str, Any]]],
    *,
    full: bool,
    max_retries: int = 3,
    gather_log_name: str = "salt",
) -> tuple[float, int]:
    """执行一次传分：全量（跳过目标比对）或增量（与目标比对只传提升）。

    失败按指数退避重试（0.5s 起），重试耗尽后抛最后一次的异常，由调用方
    映射为用户文案。返回 (用时秒, 被数据站拒绝而跳过的成绩条数)。
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
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        skipped = 0
        try:
            skipped = await delta_updates_chain(
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
            return time.monotonic() - start, skipped
        except Exception as e:  # 统一退避重试后交给调用方
            last_exc = e
            if attempt >= max_retries:
                raise
            delay = 0.5 * (2**attempt)
            logger.warning(
                f"传分第 {attempt + 1}/{max_retries} 次重试（等待 {delay}s）：{e!r}"
            )
            await asyncio.sleep(delay)
    raise last_exc  # pragma: no cover——循环内必然 return 或 raise
