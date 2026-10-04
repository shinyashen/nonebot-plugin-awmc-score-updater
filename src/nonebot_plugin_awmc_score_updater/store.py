"""本插件自有存储（localstore 数据目录 ``awmc_score_updater.db``）。

只存主插件绑定体系没有的数据：华立微信 userID（机台二维码解析产物）、
上次传分时间与每谱面游玩次数（``<难度>pc列表`` 数据源，见
``local/reference/saltnet-notes.md``——主仓库笔记）。水鱼/落雪凭据**不在
本表**——直接复用主插件 ``user_binding``（core.binding），用户在主插件
完成绑定后本插件即可传分，不重复存储凭据。

SQLModel 全局 metadata 边界（三仓共知）：SQLModel 表默认注册到同一全局
metadata，任一仓 ``create_all`` 会把其他仓已加载模型的**空表**也建出来。
表名三仓约定不重名（现状已满足：主仓 arcade 族 / arcade 仓 *_entry 族 /
score-updater wechat_binding/play_count 族），空表无行、无实际影响；根治
需独立 MetaData（SQLModel 支持有限，先调研，见主仓
``local/code-review-3rd-deferred-structure.md`` 搁置项）。新表命名保持
跨仓不重名。
"""

from pathlib import Path
from datetime import datetime

from pydantic import NaiveDatetime
from sqlmodel import Field, SQLModel, select
from nonebot.log import logger
from sqlalchemy.exc import IntegrityError
from maimai_py.models import Score
from sqlalchemy.ext.asyncio import AsyncEngine
from nonebot_plugin_localstore import get_data_dir
from sqlmodel.ext.asyncio.session import AsyncSession
from nonebot_plugin_awmc_helper.core.store import init_plugin_db, create_plugin_engine

_engine: AsyncEngine | None = None
_db_file: Path | None = None


def db_file():
    """SQLite 库文件路径（localstore 插件数据目录；测试可重定向）。"""
    return (
        _db_file
        if _db_file is not None
        else get_data_dir("nonebot_plugin_awmc_score_updater") / "awmc_score_updater.db"
    )


async def set_db_file(path: Path | None) -> None:
    """重定向库文件并重置引擎（测试隔离用，生产勿调）。

    旧引擎先 dispose 归还连接池，否则池内连接被 GC 回收时触发
    ResourceWarning（aiosqlite 连接未显式关闭）。
    """
    global _db_file, _engine
    if _engine is not None:
        await _engine.dispose()
    _db_file = path
    _engine = None


def get_engine() -> AsyncEngine:
    """懒创建的异步引擎；引擎构造走主插件 ``create_plugin_engine`` 工厂
    （重定向/dispose/懒创建仍归本仓，``set_db_file`` 的测试重定向机制为本仓自有）。"""
    global _engine
    if _engine is None:
        path = db_file()
        _engine = create_plugin_engine(path.parent, path.name)
    return _engine


async def init_store() -> None:
    """建表（幂等）：走主插件 ``init_plugin_db`` 工厂。

    metadata 传 SQLModel 全局 metadata（口径同主仓 ``init_db``）——任一仓
    ``create_all`` 连带建出其他仓已加载模型的空表，语义见模块 docstring；
    「already exists」按幂等成功忽略的竞态处理（2026-09-28 CI 实测两 worker
    并发首建同炸）已收敛在工厂内。
    """
    await init_plugin_db(get_engine(), SQLModel.metadata)


class WechatBinding(SQLModel, table=True):
    """华立微信绑定：(platform, user_id) → 机台二维码解析出的 userID。"""

    __tablename__ = "wechat_binding"  # type: ignore[reportGeneralTypeIssues]

    platform: str = Field(primary_key=True)
    user_id: str = Field(primary_key=True)
    arcade_user_id: str  # SaltNet /getQRInfo 解析产物（华立微信 userID）
    last_update: str | None = None  # 上次成功传分时间（展示用）
    bound_at: NaiveDatetime = Field(default_factory=datetime.now)


class WechatStore:
    """微信绑定读写（查询与写入同会话完成，全异步）。"""

    @staticmethod
    async def _query(
        session: AsyncSession, platform: str, user_id: str
    ) -> WechatBinding | None:
        stmt = select(WechatBinding).where(
            WechatBinding.platform == platform,
            WechatBinding.user_id == user_id,
        )
        return (await session.exec(stmt)).first()

    async def get(self, platform: str, user_id: str) -> WechatBinding | None:
        async with AsyncSession(get_engine()) as session:
            return await self._query(session, platform, user_id)

    async def bind(self, platform: str, user_id: str, arcade_user_id: str) -> None:
        """绑定/换绑微信 userID（已有记录则覆盖，保留 last_update）。

        并发首绑双 INSERT 竞态对齐主插件 ``binding.ensure``：败方捕
        IntegrityError 重读后覆盖更新（两方 userID 同源同一二维码，内容
        一致）；重读仍无行则原样上抛（非竞态异常不吞）。
        """
        async with AsyncSession(get_engine()) as session:
            if row := await self._query(session, platform, user_id):
                row.arcade_user_id = arcade_user_id
                await session.commit()
                return
            session.add(
                WechatBinding(
                    platform=platform,
                    user_id=user_id,
                    arcade_user_id=arcade_user_id,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if row := await self._query(session, platform, user_id):
                    row.arcade_user_id = arcade_user_id
                    await session.commit()
                else:
                    raise

    async def set_last_update(self, platform: str, user_id: str, when: str) -> None:
        """记录上次成功传分时间。"""
        async with AsyncSession(get_engine()) as session:
            if row := await self._query(session, platform, user_id):
                row.last_update = when
                await session.commit()


wechat_store = WechatStore()


def pc_key(score) -> tuple[int, str, int]:
    """成绩 → PC 表谱面键（与传分链 delta_updates_chain 的谱面键同构）。

    id：DX 谱折回曲目 id、宴谱保留 6 位机台内部 id（deser_score 产物）；
    type/level_index 存枚举原始值（value），渲染侧按 ScoreExtend 同构取回。
    """
    return (score.id, score.type.value, score.level_index.value)


class PlayCount(SQLModel, table=True):
    """每谱面游玩次数（导分观测落地）。计数主体是华立账号。

    数值语义二态：扫码全量导分 → SaltNet 透传的机台累计真值（权威替换）；
    简略导分 → 桥接增量（与数据站已有成绩比对，状态有变化 +1，近似：
    无成绩变化的纯练习不可见，由下次扫码校准自愈）。
    """

    __tablename__ = "play_count"  # type: ignore[reportGeneralTypeIssues]

    arcade_user_id: str = Field(primary_key=True)
    music_id: int = Field(primary_key=True)  # 宴谱保留 6 位机台 id（同 deser_score）
    type: str = Field(primary_key=True)  # SongType.value：standard/dx/utage
    level_index: int = Field(primary_key=True)  # 0-4；宴谱恒 0（maimai-py 约定）
    play_count: int = 0


def row_key(row: PlayCount) -> tuple[int, str, int]:
    """PC 表行 → 谱面键（行内 type/level_index 已是原始值，不再取 .value）。

    与 ：func:`pc_key` 同构（谱面键的行侧读法），导出供 matchers 的 pc 列表
    查询把行转回谱面键，避免第三处内联元组漂移。
    """
    return (row.music_id, row.type, row.level_index)


class PlayCountSync(SQLModel, table=True):
    """PC 数据的用户级同步状态（跟华立 id：一次导分全谱面更新，时间无行级意义）。"""

    __tablename__ = "play_count_sync"  # type: ignore[reportGeneralTypeIssues]

    arcade_user_id: str = Field(primary_key=True)
    last_full_at: NaiveDatetime | None = None  # 最近全量（扫码）导分时间；NULL=从未校准


# 全量导分截断守卫：现有 PC 行数超过载荷的该倍数视为 SaltNet 分页事故
# （防清库，见 PlayCountStore.observe docstring）
_FULL_LOAD_TRUNCATE_FACTOR = 2


class PlayCountStore:
    """游玩次数读写：导分链观测落地（observe）与 pc 列表查询（counts）。"""

    @staticmethod
    async def _sync_row(
        session: AsyncSession, arcade_user_id: str, when: datetime
    ) -> None:
        stmt = select(PlayCountSync).where(
            PlayCountSync.arcade_user_id == arcade_user_id
        )
        if row := (await session.exec(stmt)).first():
            row.last_full_at = when
        else:
            session.add(PlayCountSync(arcade_user_id=arcade_user_id, last_full_at=when))

    async def observe(
        self,
        arcade_user_id: str,
        source_scores: list[Score],
        baselines: list[dict[tuple[int, str, int], Score]],
        *,
        anchored: bool,
        now: datetime | None = None,
    ) -> None:
        """导分成功后落地游玩次数（每条导分链只调用一次）。

        - ``anchored=True``（扫码全量）：权威累计值整表替换并记
          ``last_full_at``；载荷中 playCount 缺失（null）的谱面保留旧值。
          现有行数超过载荷两倍（_FULL_LOAD_TRUNCATE_FACTOR 倍判定）视为
          异常截断（SaltNet 分页事故），放弃替换防清库。
        - ``anchored=False``（简略）：桥接增量——基线取各数据站已有成绩的
          并集（先到先得）；基线有该谱且 (达成率, DX 分) 任一变化 → +1
          （首见行直接以 1 落地）；基线无该谱（数据站全缺，如站侧删除曲）
          只播 0 值种子行，防「每次导分都算一次」的虚增。
        - 空载荷直接跳过（拉取失败的占位回调不落库）。

        并发边界（不加锁）：本函数按 arcade_user_id 无互斥，跨平台绑定同一
        华立账号并发导分理论上可撞主键（IntegrityError），该异常由 run_update
        的 pc_hook 兜底吞为 warning（不虚增次数）；同平台同用户并发已被
        matchers 的 _import_locks 互斥。
        """
        if not source_scores:
            return
        when = now or datetime.now()
        async with AsyncSession(get_engine()) as session:
            stmt = select(PlayCount).where(PlayCount.arcade_user_id == arcade_user_id)
            rows = (await session.exec(stmt)).all()
            existing: dict[tuple[int, str, int], PlayCount] = {
                row_key(r): r for r in rows
            }

            if anchored:
                if existing and len(source_scores) * _FULL_LOAD_TRUNCATE_FACTOR < len(
                    existing
                ):
                    logger.warning(
                        f"华立账号 {arcade_user_id} 全量导分载荷 "
                        f"{len(source_scores)} 条远小于既有 PC 行数 "
                        f"{len(existing)}，疑似截断，放弃替换"
                    )
                    return
                for s in source_scores:
                    if s.play_count is None:
                        continue
                    if row := existing.get(pc_key(s)):
                        row.play_count = s.play_count
                    else:
                        session.add(
                            PlayCount(
                                arcade_user_id=arcade_user_id,
                                music_id=s.id,
                                type=s.type.value,
                                level_index=s.level_index.value,
                                play_count=s.play_count,
                            )
                        )
                await self._sync_row(session, arcade_user_id, when)
                await session.commit()
                return

            baseline: dict[tuple[int, str, int], Score] = {}
            for d in baselines:
                for k, v in d.items():
                    baseline.setdefault(k, v)

            def _changed(b: Score | None, s: Score) -> bool:
                return b is not None and (
                    (b.achievements or 0) != (s.achievements or 0)
                    or (b.dx_score or 0) != (s.dx_score or 0)
                )

            for s in source_scores:
                k = pc_key(s)
                if row := existing.get(k):
                    if _changed(baseline.get(k), s):
                        row.play_count += 1
                else:
                    # 首见行：基线有该谱且状态有变化 → 1（存量用户首导的增量）；
                    # 基线缺该谱（数据站看不到，如站侧删除曲）→ 0，不虚计
                    session.add(
                        PlayCount(
                            arcade_user_id=arcade_user_id,
                            music_id=s.id,
                            type=s.type.value,
                            level_index=s.level_index.value,
                            play_count=1 if _changed(baseline.get(k), s) else 0,
                        )
                    )
            await session.commit()

    async def counts(self, arcade_user_id: str) -> list[PlayCount]:
        """取该华立账号全部游玩次数行（标级/定数过滤在渲染侧按曲库数据做）。"""
        stmt = select(PlayCount).where(PlayCount.arcade_user_id == arcade_user_id)
        async with AsyncSession(get_engine()) as session:
            return list((await session.exec(stmt)).all())

    async def last_full_at(self, arcade_user_id: str) -> datetime | None:
        """最近全量（扫码）导分时间；从未校准返回 None。"""
        stmt = select(PlayCountSync).where(
            PlayCountSync.arcade_user_id == arcade_user_id
        )
        async with AsyncSession(get_engine()) as session:
            if row := (await session.exec(stmt)).first():
                return row.last_full_at
        return None


play_count_store = PlayCountStore()
