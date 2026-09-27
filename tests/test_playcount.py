"""play_count 存储测试：权威替换 / 桥接增量 / 守卫与查询。

插件相关导入一律函数内进行（收集期不触发插件加载链）。
"""

import pytest
from maimai_py.enums import SongType, LevelIndex
from maimai_py.models import Score


@pytest.fixture
async def pc_store(tmp_path):
    from nonebot_plugin_awmc_score_updater import store as su_store

    su_store.set_db_file(tmp_path / "pc.db")
    await su_store.init_store()
    yield su_store.play_count_store
    su_store.set_db_file(None)


def mk(
    song_id: int = 231,
    ach: float = 100.0,
    dx: int = 2000,
    pc: int | None = None,
    li: LevelIndex = LevelIndex.MASTER,
    typ: SongType = SongType.DX,
) -> Score:
    return Score(
        id=song_id,
        level=None,
        level_index=li,
        achievements=ach,
        fc=None,
        fs=None,
        dx_score=dx,
        dx_rating=None,
        play_count=pc,
        play_time=None,
        rate=None,
        type=typ,
    )


async def test_anchor_replaces_with_truth(pc_store):
    """扫码全量：playCount 真值整表替换并记 last_full_at。"""
    await pc_store.observe("u1", [mk(pc=5), mk(song_id=500, pc=1)], [], anchored=True)
    rows = {(r.music_id, r.play_count) for r in await pc_store.counts("u1")}
    assert rows == {(231, 5), (500, 1)}
    assert await pc_store.last_full_at("u1") is not None

    # 再次校准覆盖旧值
    await pc_store.observe("u1", [mk(pc=9)], [], anchored=True)
    rows = {(r.music_id, r.play_count) for r in await pc_store.counts("u1")}
    assert rows == {(231, 9), (500, 1)}  # 本次载荷缺失的谱面行保留


async def test_anchor_truncation_guard(pc_store):
    """截断守卫：载荷条目不足既有行数一半 → 放弃替换。"""
    await pc_store.observe("u1", [mk(i, pc=i) for i in (1, 2, 3, 4)], [], anchored=True)
    await pc_store.observe("u1", [mk(1, pc=99)], [], anchored=True)
    rows = {r.play_count for r in await pc_store.counts("u1") if r.music_id == 1}
    assert rows == {1}  # 未被 99 覆盖


async def test_anchor_null_playcount_keeps_old(pc_store):
    """锚定载荷中 playCount 为 null 的谱面保留旧值（不写 0）。"""
    await pc_store.observe("u1", [mk(pc=3)], [], anchored=True)
    await pc_store.observe("u1", [mk(pc=None)], [], anchored=True)
    rows = {r.play_count for r in await pc_store.counts("u1")}
    assert rows == {3}


async def test_bridge_seeds_then_increments_on_diff(pc_store):
    """简略导分桥接：首见播 0 值种子；基线状态有变化 +1；无变化不动。"""
    baseline_dicts = [{"k": mk(ach=99.0, dx=1000)}]
    key = (231, "dx", 3)
    # 源成绩 231/dx/master 与基线同键（mk 默认 231/dx/master）
    baseline_dicts = [{(231, "dx", 3): mk(ach=99.0, dx=1000)}]

    # 第一次简略导分：无任何基线可全缺 → 种子 0
    await pc_store.observe("u1", [mk(ach=99.0, dx=1000)], [], anchored=False)
    assert [r.play_count for r in await pc_store.counts("u1")] == [0]

    # 达成率提升 → +1
    await pc_store.observe(
        "u1", [mk(ach=100.0, dx=1000)], [baseline_dicts[0]], anchored=False
    )
    assert [r.play_count for r in await pc_store.counts("u1")] == [1]

    # 数据站已同步（基线推进到 100.0）且机台状态不变 → 不再 +1
    await pc_store.observe(
        "u1", [mk(ach=100.0, dx=1000)], [{key: mk(ach=100.0, dx=1000)}], anchored=False
    )
    assert [r.play_count for r in await pc_store.counts("u1")] == [1]

    # DX 分变化也算一次游玩
    await pc_store.observe(
        "u1", [mk(ach=100.0, dx=2500)], [{key: mk(ach=100.0, dx=1000)}], anchored=False
    )
    assert [r.play_count for r in await pc_store.counts("u1")] == [2]


async def test_bridge_baseline_missing_no_phantom(pc_store):
    """基线缺该谱（数据站全缺，如站侧删除曲）：只种子不虚增。"""
    await pc_store.observe("u1", [mk(ach=90.0, dx=1)], [], anchored=False)  # 种子 0
    for _ in range(3):  # 反复简略导分，基线始终无该谱
        await pc_store.observe("u1", [mk(ach=95.0, dx=2)], [], anchored=False)
    assert [r.play_count for r in await pc_store.counts("u1")] == [0]


async def test_bridge_multi_target_union_baseline(pc_store):
    """多数据站基线取并集（先到先得）。"""
    await pc_store.observe("u1", [mk(ach=99.0, dx=1000)], [], anchored=False)  # 种子
    d1 = {(231, "dx", 3): mk(ach=99.0, dx=1000)}
    d2 = {(500, "dx", 3): mk(ach=98.0, dx=900)}
    await pc_store.observe(
        "u1",
        [mk(ach=100.0, dx=2000), mk(song_id=500, ach=99.0, dx=950)],
        [d1, d2],
        anchored=False,
    )
    counts = {(r.music_id, r.play_count) for r in await pc_store.counts("u1")}
    assert counts == {(231, 1), (500, 1)}


async def test_empty_source_scores_noop(pc_store):
    """空载荷（拉取失败占位）不落库。"""
    await pc_store.observe("u1", [], [], anchored=True)
    await pc_store.observe("u1", [], [], anchored=False)
    assert await pc_store.counts("u1") == []
    assert await pc_store.last_full_at("u1") is None
