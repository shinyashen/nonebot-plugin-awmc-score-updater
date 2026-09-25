"""本插件自有存储（localstore 数据目录 ``awmc_score_updater.db``）。

只存主插件绑定体系没有的数据：华立微信 userID（机台二维码解析产物）与
上次传分时间。水鱼/落雪凭据**不在本表**——直接复用主插件 ``user_binding``
（core.binding），用户在主插件完成绑定后本插件即可传分，不重复存储凭据。
"""

from pathlib import Path
from datetime import datetime

from pydantic import NaiveDatetime
from sqlmodel import Field, SQLModel, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from nonebot_plugin_localstore import get_data_dir
from sqlmodel.ext.asyncio.session import AsyncSession

_engine: AsyncEngine | None = None
_db_file: Path | None = None


def db_file():
    """SQLite 库文件路径（localstore 插件数据目录；测试可重定向）。"""
    return (
        _db_file
        if _db_file is not None
        else get_data_dir("nonebot_plugin_awmc_score_updater") / "awmc_score_updater.db"
    )


def set_db_file(path: Path | None) -> None:
    """重定向库文件并重置引擎（测试隔离用，生产勿调）。"""
    global _db_file, _engine
    _db_file = path
    _engine = None


def get_engine() -> AsyncEngine:
    """懒创建的异步引擎（与主插件 core.store 同款模式）。"""
    global _engine
    if _engine is None:
        _engine = create_async_engine(f"sqlite+aiosqlite:///{db_file()}")
    return _engine


async def init_store() -> None:
    """建表（create_all 起步）。"""
    async with get_engine().begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


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
        """绑定/换绑微信 userID（已有记录则覆盖，保留 last_update）。"""
        async with AsyncSession(get_engine()) as session:
            if row := await self._query(session, platform, user_id):
                row.arcade_user_id = arcade_user_id
            else:
                session.add(
                    WechatBinding(
                        platform=platform,
                        user_id=user_id,
                        arcade_user_id=arcade_user_id,
                    )
                )
            await session.commit()

    async def set_last_update(self, platform: str, user_id: str, when: str) -> None:
        """记录上次成功传分时间。"""
        async with AsyncSession(get_engine()) as session:
            if row := await self._query(session, platform, user_id):
                row.last_update = when
                await session.commit()


wechat_store = WechatStore()
