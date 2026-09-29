"""play_count 存储测试：权威替换 / 桥接增量 / 守卫与查询。

插件相关导入一律函数内进行（收集期不触发插件加载链）。
"""

import pytest
from maimai_py.enums import SongType, LevelIndex
from maimai_py.models import Score


@pytest.fixture
async def pc_store(tmp_path):
    import conftest

    from nonebot_plugin_awmc_score_updater import store as su_store

    su_store.set_db_file(tmp_path / "pc.db")
    await su_store.init_store()
    yield su_store.play_count_store
    su_store.set_db_file(conftest._session_db["su"])


def mk(
    song_id: int = 199,
    ach: float = 100.0,
    dx: int = 2000,
    pc: int | None = None,
    li: LevelIndex = LevelIndex.MASTER,
    typ: SongType = SongType.DX,
) -> Score:
    """样例成绩：默认 199 DX MASTER（真实谱面）；8/624 仅 SD 谱，用 typ 指明。"""
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
    await pc_store.observe(
        "u1",
        [mk(pc=5), mk(song_id=624, pc=1, typ=SongType.STANDARD)],
        [],
        anchored=True,
    )
    rows = {(r.music_id, r.play_count) for r in await pc_store.counts("u1")}
    assert rows == {(199, 5), (624, 1)}
    assert await pc_store.last_full_at("u1") is not None

    # 再次校准覆盖旧值
    await pc_store.observe("u1", [mk(pc=9)], [], anchored=True)
    rows = {(r.music_id, r.play_count) for r in await pc_store.counts("u1")}
    assert rows == {(199, 9), (624, 1)}  # 本次载荷缺失的谱面行保留


async def test_anchor_truncation_guard(pc_store):
    """截断守卫：载荷条目不足既有行数一半 → 放弃替换。"""
    await pc_store.observe(
        "u1",
        [
            mk(8, pc=1, typ=SongType.STANDARD),
            mk(pc=2),
            mk(624, pc=3, typ=SongType.STANDARD),
        ],
        [],
        anchored=True,
    )
    await pc_store.observe("u1", [mk(pc=99)], [], anchored=True)
    rows = {r.play_count for r in await pc_store.counts("u1") if r.music_id == 199}
    assert rows == {2}  # 未被 99 覆盖


async def test_anchor_null_playcount_keeps_old(pc_store):
    """锚定载荷中 playCount 为 null 的谱面保留旧值（不写 0）。"""
    await pc_store.observe("u1", [mk(pc=3)], [], anchored=True)
    await pc_store.observe("u1", [mk(pc=None)], [], anchored=True)
    rows = {r.play_count for r in await pc_store.counts("u1")}
    assert rows == {3}


async def test_bridge_seeds_then_increments_on_diff(pc_store):
    """简略导分桥接：首见播 0 值种子；基线状态有变化 +1；无变化不动。"""
    # 199 DX MASTER（谱面键 (199, "dx", 3)，与 mk 默认同键）
    key = (199, "dx", 3)
    baseline_dicts = [{key: mk(ach=99.0, dx=1000)}]

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
    d1 = {(199, "dx", 3): mk(ach=99.0, dx=1000)}
    # 624 仅 SD 谱（(624, "standard", 3) = SD MASTER）
    d2 = {(624, "standard", 3): mk(ach=98.0, dx=900, typ=SongType.STANDARD)}
    await pc_store.observe(
        "u1",
        [
            mk(ach=100.0, dx=2000),
            mk(song_id=624, ach=99.0, dx=950, typ=SongType.STANDARD),
        ],
        [d1, d2],
        anchored=False,
    )
    counts = {(r.music_id, r.play_count) for r in await pc_store.counts("u1")}
    assert counts == {(199, 1), (624, 1)}


async def test_empty_source_scores_noop(pc_store):
    """空载荷（拉取失败占位）不落库。"""
    await pc_store.observe("u1", [], [], anchored=True)
    await pc_store.observe("u1", [], [], anchored=False)
    assert await pc_store.counts("u1") == []
    assert await pc_store.last_full_at("u1") is None


async def test_run_update_observe_bridge_increments(pc_store):
    """全链集成（L-9 回归）：run_update(pc_hook=...) → observe 桥接增量。

    链返回基线字典键曾为字符串，observe 按 pc_key 元组取恒 miss → 存量行
    永不 +1、首见行恒播 0（键型静默失效，仅扫码校准兜底）。目标站已有
    99.0 基线、机台 100.0：导分成功后存量行必须 +1。"""
    from test_updater import FakeUpdateProvider, mk_score, _make_client
    from maimai_py.models import PlayerIdentifier

    from nonebot_plugin_awmc_score_updater.updater import run_update

    # 预置存量行（上次简略导分的种子 0）
    await pc_store.observe(
        "u-bridge", [mk_score(achievements=99.0, dx_score=1000)], [], anchored=False
    )
    assert [r.play_count for r in await pc_store.counts("u-bridge")] == [0]

    client = await _make_client()
    target = FakeUpdateProvider([mk_score(achievements=99.0, dx_score=1000)])
    source = FakeUpdateProvider([mk_score(achievements=100.0, dx_score=2000)])
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "机台"})]
    targets = [(target, PlayerIdentifier(credentials="t"), {"name": "水鱼"})]

    async def pc_hook(source_scores, target_dicts):
        await pc_store.observe("u-bridge", source_scores, target_dicts, anchored=False)

    await run_update(client, src, targets, full=False, max_retries=0, pc_hook=pc_hook)

    assert [r.play_count for r in await pc_store.counts("u-bridge")] == [1]


async def test_pc_bridge_no_phantom_on_intersect_min(pc_store):
    """交集谱各站基线不同（水鱼 99.5 / 落雪 99.0）时不得虚增（LOGIC-2 钉子）：

    机台真值 99.5 与水鱼一致并非新游玩，桥接不得 +1——_join_rev 原地改写曾
    把随返回值交出的水鱼基线拉低成 min 99.0，误判虚增。同时断言返回基线
    == 各站原始值（L-10）。"""
    from test_updater import FakeUpdateProvider, mk_score, _make_client
    from maimai_py.models import PlayerIdentifier

    from nonebot_plugin_awmc_score_updater.updater import delta_updates_chain

    key = (199, "dx", 3)
    # 存量行：已按机台真值校准到 5 次
    await pc_store.observe(
        "u1",
        [mk_score(achievements=99.5, dx_score=2000, play_count=5)],
        [],
        anchored=True,
    )

    client = await _make_client()
    source = FakeUpdateProvider([mk_score(achievements=99.5, dx_score=2000)])
    water = FakeUpdateProvider([mk_score(achievements=99.5, dx_score=2000)])
    lxns = FakeUpdateProvider([mk_score(achievements=99.0, dx_score=2000)])
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "机台"})]
    targets = [
        (water, PlayerIdentifier(credentials="w"), {"name": "水鱼"}),
        (lxns, PlayerIdentifier(credentials="l"), {"name": "落雪"}),
    ]

    _skipped, _snapshot, baselines, _failures = await delta_updates_chain(
        client, src, targets
    )

    # 返回基线 == 各站原始值（_join_rev 真拷贝，不污染输入）
    assert baselines[0][key].achievements == 99.5
    assert baselines[1][key].achievements == 99.0

    await pc_store.observe(
        "u1", [mk_score(achievements=99.5, dx_score=2000)], baselines, anchored=False
    )
    assert [r.play_count for r in await pc_store.counts("u1")] == [5]  # 无新游玩不 +1
