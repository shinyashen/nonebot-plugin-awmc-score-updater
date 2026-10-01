"""传分核心测试：增量算法纯函数 + 链编排。

链编排只消费裸 ``Score``（provider.get_scores_all 直取，不经
``MaimaiScores.configure`` 扩展），对曲库零依赖；仅 SaltArcadeProvider 的
删除曲过滤用到样例曲库（mocks.seed_service）。fake provider 保证不触网络。

插件相关导入一律函数内进行（收集期不触发插件加载链）。
"""

import respx
import pytest
from httpx import Response
from maimai_py.enums import FCType, FSType, SongType, LevelIndex
from maimai_py.models import Score, PlayerIdentifier
from maimai_py.providers.base import IScoreProvider, IScoreUpdateProvider


@pytest.fixture
async def songs():
    """样例曲库注入（曲库服务就绪，绕过网络）。"""
    from mocks import seed_service
    from nonebot_plugin_awmc_helper.core.songs import song_service

    await seed_service(song_service)
    yield
    song_service._ready.clear()


def mk_score(
    song_id: int = 199,
    achievements: float = 100.0,
    dx_score: int = 2000,
    fc: FCType | None = FCType.FC,
    fs: FSType | None = None,
    level_index: LevelIndex = LevelIndex.MASTER,
    song_type: SongType = SongType.DX,
    play_count: int = 1,
) -> Score:
    """样例成绩：默认 199 チルノのパーフェクトさんすう教室 的 DX MASTER（真实谱面）。"""
    return Score(
        id=song_id,
        level=None,
        level_index=level_index,
        achievements=achievements,
        fc=fc,
        fs=fs,
        dx_score=dx_score,
        dx_rating=None,
        play_count=play_count,
        play_time=None,
        rate=None,
        type=song_type,
    )


class FakeUpdateProvider(IScoreProvider, IScoreUpdateProvider):
    """同时满足 IScoreProvider/IScoreUpdateProvider 语义的内存实现。"""

    def __init__(
        self,
        scores: list[Score] | None = None,
        fail: bool = False,
        update_fail: bool = False,
    ) -> None:
        self.scores = list(scores or [])
        self.fail = fail  # 拉取失败
        self.update_fail = update_fail  # 上传失败
        self.updates: list[list[Score]] = []
        self.fetch_count = 0

    def _hash(self) -> str:
        return "fake"

    async def get_scores_all(self, identifier, client) -> list[Score]:
        self.fetch_count += 1
        if self.fail:
            raise RuntimeError("fetch failed")
        return list(self.scores)

    async def update_scores(self, identifier, scores, client) -> None:
        if self.update_fail:
            raise RuntimeError("update failed")
        self.updates.append(list(scores))
        self.scores.extend(scores)


def test_join_rev_takes_conservative_base():
    from nonebot_plugin_awmc_score_updater.updater import _join_rev

    merged = _join_rev(
        [
            mk_score(achievements=99.0, fc=FCType.FC),
            mk_score(achievements=98.0, fc=None),
        ]
    )
    assert merged.achievements == 98.0
    assert merged.fc == FCType.FC  # 有值目标内取更优（任一缺失不再清空基准）
    assert merged.play_count == 1


def test_join_rev_does_not_mutate_input():
    """_join_rev 以 replace 起底真拷贝（L-10）：交集首站条目还在 target_dicts
    里随返回值交 pc 观测作该站基线，原地 min/max 化会把基线污染成假值
    （机台真值对比假基线误判虚增 +1）。"""
    from nonebot_plugin_awmc_score_updater.updater import _join_rev

    a = mk_score(achievements=99.5, dx_score=1990, fc=None, fs=None, play_count=5)
    b = mk_score(
        achievements=99.0, dx_score=2000, fc=FCType.FC, fs=FSType.FS, play_count=3
    )
    merged = _join_rev([a, b])
    # 合成基线：达成率/DX 分/pc 取 min，fc/fs 有值目标内取更优
    assert merged.achievements == 99.0
    assert merged.dx_score == 1990
    assert merged.fc == FCType.FC
    assert merged.fs == FSType.FS
    assert merged.play_count == 3
    # 输入对象保持各站原始值
    assert (a.achievements, a.dx_score, a.fc, a.fs, a.play_count) == (
        99.5,
        1990,
        None,
        None,
        5,
    )
    assert (b.achievements, b.dx_score, b.fc, b.fs, b.play_count) == (
        99.0,
        2000,
        FCType.FC,
        FSType.FS,
        3,
    )


def test_compare_no_gain_returns_none():
    from nonebot_plugin_awmc_score_updater.updater import _compare

    source = mk_score(achievements=99.0, dx_score=1000)
    target = mk_score(achievements=99.5, dx_score=2000)
    assert _compare(source, target) is None


def test_compare_gain_merges_fields():
    from nonebot_plugin_awmc_score_updater.updater import _compare

    source = mk_score(achievements=100.0, dx_score=2500, fc=FCType.FCP, fs=FSType.FS)
    target = mk_score(achievements=99.0, dx_score=2000, fc=FCType.FC, fs=None)
    delta = _compare(source, target)
    assert delta is not None
    assert delta.achievements == 100.0
    assert delta.dx_score == 2500
    assert delta.fc == FCType.FCP  # 合并保留更优 fc（枚举值小者优先）
    assert delta.fs == FSType.FS


def test_compare_new_score():
    from nonebot_plugin_awmc_score_updater.updater import _compare

    assert _compare(mk_score(), None) is not None


def test_compare_mismatched_level_raises():
    from nonebot_plugin_awmc_score_updater.updater import _compare

    with pytest.raises(ValueError, match="different level indexes"):
        _compare(mk_score(), mk_score(level_index=LevelIndex.EXPERT))


def test_salt_provider_identifier_roundtrip():
    from nonebot_plugin_awmc_score_updater.updater import SaltArcadeProvider

    ident = SaltArcadeProvider.make_identifier("42", "q" * 64)
    assert ident.credentials["userid"] == "42"
    assert ident.credentials["qrcode"] == "q" * 64
    ident2 = SaltArcadeProvider.make_identifier("42")
    assert ident2.credentials["qrcode"] == ""


async def _make_client():
    # 主插件 client 单例：scores/updates 走 fake provider，不触网络
    from nonebot_plugin_awmc_helper.core.client import client

    return client


async def test_delta_chain_uploads_only_delta(songs):
    """增量链：只上传有提升与新增的成绩，无提升者跳过。"""
    from nonebot_plugin_awmc_score_updater.updater import delta_updates_chain

    client = await _make_client()
    source_scores = [
        mk_score(song_id=199, achievements=100.0, dx_score=2500),  # 有提升
        # 624 KISS CANDY FLAVOR 仅 SD 谱（无提升）
        mk_score(
            song_id=624,
            achievements=90.0,
            dx_score=100,
            song_type=SongType.STANDARD,
        ),
        mk_score(
            song_id=199,
            achievements=95.0,
            dx_score=500,
            level_index=LevelIndex.EXPERT,
            song_type=SongType.STANDARD,
        ),  # 目标没有（SD 谱面）
    ]
    source = FakeUpdateProvider(source_scores)
    target = FakeUpdateProvider(
        [
            mk_score(song_id=199, achievements=99.0, dx_score=2000),
            mk_score(
                song_id=624,
                achievements=99.9,
                dx_score=3000,
                song_type=SongType.STANDARD,
            ),
        ]
    )
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "s"})]
    targets = [(target, PlayerIdentifier(credentials="t"), {"name": "t"})]

    await delta_updates_chain(client, src, targets)

    assert len(target.updates) == 1
    uploaded = sorted((s.id, s.type.name) for s in target.updates[0])
    assert uploaded == [(199, "DX"), (199, "STANDARD")]  # 仅新增与有提升者


async def test_delta_chain_utage_score_passes(songs):
    """宴谱成绩（6 位机台 id）直接过链上传。

    回归保护（2026-09-26 线上实测）：旧链路经 ``MaimaiScores.configure``
    扩展，按谱面物量算 dx_star——零物量宴谱（如缺数据的 [匿]匿名M）触发
    ``dx_score / 0`` 崩掉整条导分链。现链路只消费裸 ``Score``，宴谱成绩
    不再要求曲库能映射到谱面。锚 121634 = [協]青春コンプレックス（真实，
    2026-08-07 下架，样例曲库不含该宴体）——曲库无此宴谱也照常上传。
    """
    from nonebot_plugin_awmc_score_updater.updater import delta_updates_chain

    client = await _make_client()
    source = FakeUpdateProvider(
        [
            mk_score(
                song_id=121634,
                song_type=SongType.UTAGE,
                level_index=LevelIndex.BASIC,
            )
        ]
    )
    target = FakeUpdateProvider([])
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "s"})]
    targets = [(target, PlayerIdentifier(credentials="t"), {"name": "t"})]

    await delta_updates_chain(client, src, targets)

    assert len(target.updates) == 1
    assert [s.id for s in target.updates[0]] == [121634]


async def test_delta_chain_source_failure_propagates(songs):
    from nonebot_plugin_awmc_score_updater.updater import delta_updates_chain

    client = await _make_client()
    source = FakeUpdateProvider([], fail=True)
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "s"})]
    targets = [(FakeUpdateProvider(), PlayerIdentifier(credentials="t"), {"name": "t"})]

    with pytest.raises(RuntimeError, match="fetch failed"):
        await delta_updates_chain(client, src, targets)


async def test_run_update_retries_then_succeeds(songs):
    from nonebot_plugin_awmc_score_updater.updater import SaltArcadeProvider, run_update

    client = await _make_client()
    flaky_target = FakeUpdateProvider()
    call_count = {"n": 0}

    class FlakyProvider(SaltArcadeProvider):
        async def get_scores_all(self, identifier, client):
            call_count["n"] += 1
            if call_count["n"] <= 1:
                raise RuntimeError("transient")
            return []

    source = [
        (FlakyProvider("m", "f"), PlayerIdentifier(credentials="x"), {"name": "s"})
    ]
    targets = [(flaky_target, PlayerIdentifier(credentials="t"), {"name": "t"})]
    duration, skipped, failures = await run_update(
        client, source, targets, full=False, max_retries=2
    )
    assert duration >= 0
    assert skipped == 0
    assert failures == []
    assert call_count["n"] == 2


async def test_run_update_full_chain(songs):
    from nonebot_plugin_awmc_score_updater.updater import run_update

    client = await _make_client()
    target = FakeUpdateProvider()
    source_provider = FakeUpdateProvider([mk_score(song_id=199)])
    source = [(source_provider, PlayerIdentifier(credentials="x"), {"name": "s"})]
    targets = [(target, PlayerIdentifier(credentials="t"), {"name": "t"})]
    await run_update(client, source, targets, full=True, max_retries=0)
    # 全量上传：不拉取目标、不比较，源合并成绩原样上传
    assert len(target.updates) == 1
    assert [s.id for s in target.updates[0]] == [199]
    assert target.fetch_count == 0


async def test_run_update_no_targets_raises():
    from nonebot_plugin_awmc_score_updater.saltapi import SaltApiError
    from nonebot_plugin_awmc_score_updater.updater import run_update

    client = await _make_client()
    source_provider = FakeUpdateProvider([])
    source = [(source_provider, PlayerIdentifier(credentials="x"), {"name": "s"})]

    with pytest.raises(SaltApiError):
        await run_update(client, source, [], full=False, max_retries=0)


@respx.mock
async def test_salt_provider_filters_deleted_songs(songs):
    """SaltNet 源过滤：删除曲/未收录曲（曲库无）在源头剔除。

    用户实测：SaltNet 保留删除曲残留成绩，水鱼 update_records 收到未收录
    曲目 id 返回 500（2026-09-26）。下架锚：青春コンプレックス（根 1634，
    2026-08-07 国服下架），其 DX 谱机台 musicId = 11634。
    """
    import respx

    from nonebot_plugin_awmc_score_updater.updater import SaltArcadeProvider

    detail = {
        "musicId": 0,
        "level": 3,
        "achievement": 1005000,
        "comboStatus": 0,
        "syncStatus": 0,
        "deluxscoreMax": 2000,
    }

    def rows(ids):
        out = []
        for i in ids:
            d = dict(detail, musicId=i)
            out.append(d)
        return out

    respx.post("https://salt_api_main.realtvop.top/updateUser").mock(
        return_value=Response(
            200,
            json={"userMusicList": [{"userMusicDetailList": rows([199, 11634])}]},
        )
    )
    provider = SaltArcadeProvider("https://salt_api_main.realtvop.top", "fallback")
    got = await provider.get_scores_all(
        SaltArcadeProvider.make_identifier("42"),
        None,  # type: ignore[arg-type]——曲库过滤走主插件 song_service，client 未用
    )
    assert [s.id for s in got] == [
        199
    ]  # 11634 折根 1634：真实下架曲不在样例曲库 → 剔除


async def test_salt_provider_requires_userid():
    import pytest
    from maimai_py.models import PlayerIdentifier

    from nonebot_plugin_awmc_score_updater.saltapi import SaltApiError
    from nonebot_plugin_awmc_score_updater.updater import SaltArcadeProvider

    provider = SaltArcadeProvider("m", "f")
    with pytest.raises(SaltApiError):
        await provider.get_scores_all(
            PlayerIdentifier(credentials={"userid": ""}), None
        )  # type: ignore[arg-type]


async def test_delta_chain_borrows_better_fc_fs_from_targets(songs):
    """合并条件对拍（原版语义）：

    机台源达成率更高但无 fc/fs，目标交集基准携带更优 fc/fs（水鱼 FCP+FS、
    落雪 FC+无FS → 基准 fc 取更优 FCP、fs 任一为 None 则基准 fs=None）。
    上传载荷应合并基准的更优 fc/fs（跨目标补达成情况）。
    """
    from nonebot_plugin_awmc_score_updater.updater import delta_updates_chain

    client = await _make_client()
    source = FakeUpdateProvider(
        [
            mk_score(
                song_id=199,
                achievements=100.0,
                dx_score=2500,
                fc=None,
                fs=None,
            )
        ]
    )
    water = FakeUpdateProvider(
        [mk_score(achievements=99.5, dx_score=2000, fc=FCType.FCP, fs=FSType.FS)]
    )
    lxns = FakeUpdateProvider(
        [mk_score(achievements=99.5, dx_score=2000, fc=FCType.FC, fs=None)]
    )
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "s"})]
    targets = [
        (water, PlayerIdentifier(credentials="w"), {"name": "水鱼"}),
        (lxns, PlayerIdentifier(credentials="l"), {"name": "落雪"}),
    ]

    await delta_updates_chain(client, src, targets)

    assert len(water.updates) == 1
    uploaded = water.updates[0][0]
    assert uploaded.achievements == 100.0  # 源更高 → 上传源值
    assert uploaded.fc == FCType.FCP  # 基准取更优 FCP（FCP 优于 FC），借给源上传
    assert uploaded.fs == FSType.FS  # 水鱼独有 FS 合入基准 → 落雪借此补齐 fs


async def test_delta_chain_no_gain_skips_even_with_fc_gap(songs):
    """原版语义边界：达成率与 DX 均无提升时，即使目标缺更优 fc/fs 也不上传。"""
    from nonebot_plugin_awmc_score_updater.updater import delta_updates_chain

    client = await _make_client()
    source = FakeUpdateProvider(
        [mk_score(achievements=99.5, dx_score=2000, fc=None, fs=None)]
    )
    target = FakeUpdateProvider(
        [mk_score(achievements=99.5, dx_score=2000, fc=FCType.FCP, fs=FSType.FS)]
    )
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "s"})]
    targets = [(target, PlayerIdentifier(credentials="t"), {"name": "t"})]

    await delta_updates_chain(client, src, targets)

    assert target.updates[0] == []  # 无提升不传，fc 缺口不触发上传


async def test_delta_chain_prefilter_allowed_ids(songs):
    """落雪预过滤（target kwargs.allowed_ids）：其曲库没有的曲目（如已删除
    限时宴谱）剔除上传并计入 skipped，其余照传（Q44/#60 盲区回归）。"""
    from nonebot_plugin_awmc_score_updater.updater import delta_updates_chain

    client = await _make_client()
    source = FakeUpdateProvider(
        [
            mk_score(song_id=199, achievements=100.0, dx_score=2500),
            # [協]青春コンプレックス宴体（真实下架限时谱，落雪曲库无原型场景）
            mk_score(
                song_id=121634,
                song_type=SongType.UTAGE,
                achievements=96.0,
                dx_score=0,
            ),
        ]
    )
    target = FakeUpdateProvider(
        [mk_score(song_id=199, achievements=99.0, dx_score=2000)]
    )
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "s"})]
    targets = [
        (
            target,
            PlayerIdentifier(credentials="t"),
            {"name": "落雪", "allowed_ids": {199}},
        )
    ]

    skipped, _, _, failures = await delta_updates_chain(client, src, targets)

    assert skipped == 1
    assert failures == []
    assert [(s.id, s.type.name) for s in target.updates[0]] == [(199, "DX")]


async def test_run_update_prefilters_lxns_target(songs):
    """run_update 对落雪目标按其当前曲库列表预过滤：列表只含 199 → 源里
    624 的提升被剔除（skipped=1），上传载荷只带 199（列表拉取经 respx）。"""
    import json as _json

    from respx import mock as respx_mock
    from maimai_py import LXNSProvider

    from nonebot_plugin_awmc_score_updater.updater import (
        SaltArcadeProvider,
        run_update,
        _lxns_ids_cache_clear,
    )

    _lxns_ids_cache_clear()  # 隔离 TTL 缓存
    client = await _make_client()
    source = FakeUpdateProvider(
        [
            mk_score(song_id=199, achievements=100.0, dx_score=2500),
            # 624 仅 SD 谱
            mk_score(
                song_id=624,
                achievements=100.0,
                dx_score=2500,
                song_type=SongType.STANDARD,
            ),
        ]
    )
    src = [(source, SaltArcadeProvider.make_identifier("42"), {"name": "机台"})]
    targets = [
        (
            LXNSProvider(developer_token="dev"),
            PlayerIdentifier(credentials="tok"),
            {"name": "落雪"},
        )
    ]

    with respx_mock(assert_all_called=False) as m:
        m.post(url__regex=r".*/api/v0/oauth/token").respond(
            200, json={"success": True, "data": {"access_token": "t"}}
        )
        m.get(url__regex=r".*/api/v0/maimai/song/list.*").respond(
            200, json={"success": True, "data": {"songs": [{"id": 199}]}}
        )
        m.get(url__regex=r".*/api/v0/user/maimai/player/scores$").respond(
            200, json={"success": True, "code": 200, "data": []}
        )
        post = m.post(url__regex=r".*/api/v0/user/maimai/player/scores$").respond(
            200, json={"success": True, "code": 200, "data": []}
        )
        _, skipped, failures = await run_update(client, src, targets, full=False)

    assert skipped == 1
    assert failures == []
    body = _json.loads(post.calls.last.request.content)
    assert [s["id"] for s in body["scores"]] == [199]


async def test_run_update_pc_hook_called_once_with_snapshot(songs):
    """pc 观测钩子：成功链路恰好一次；源成绩为 _compare 原地合并前的机台真值，
    基线为上传前的数据站状态。"""
    from nonebot_plugin_awmc_score_updater.updater import run_update

    client = await _make_client()
    target = FakeUpdateProvider([mk_score(achievements=99.0, dx_score=1000)])
    source = FakeUpdateProvider([mk_score(achievements=100.0, dx_score=2000)])
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "s"})]
    targets = [(target, PlayerIdentifier(credentials="t"), {"name": "t"})]
    calls: list = []

    async def hook(source_scores, target_dicts):
        calls.append((source_scores, target_dicts))

    await run_update(client, src, targets, full=False, max_retries=0, pc_hook=hook)
    assert len(calls) == 1
    src_scores, dicts = calls[0]
    assert [s.achievements for s in src_scores] == [100.0]  # 机台真值，非合并值
    assert len(dicts) == 1
    assert next(iter(dicts[0].values())).achievements == 99.0  # 上传前基线


async def test_delta_chain_snapshot_is_true_copy(songs):
    """源成绩快照须为真拷贝（F5）：_compare 原地合并目标基准值（fc 借用、
    play_count 取大），浅拷贝列表与源对象同引用，快照会被合并污染——
    观测到的不再是机台真值。"""
    from nonebot_plugin_awmc_score_updater.updater import delta_updates_chain

    client = await _make_client()
    source = FakeUpdateProvider(
        [
            mk_score(
                song_id=199,
                achievements=100.0,
                dx_score=2500,
                fc=None,
                fs=None,
                play_count=1,
            )
        ]
    )
    target = FakeUpdateProvider(
        [
            mk_score(
                achievements=99.0, dx_score=2000, fc=FCType.FCP, fs=None, play_count=5
            )
        ]
    )
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "s"})]
    targets = [(target, PlayerIdentifier(credentials="t"), {"name": "t"})]

    _skipped, snapshot, _dicts, _failures = await delta_updates_chain(
        client, src, targets
    )

    assert snapshot[0].fc is None  # 机台真值：合并借用的 FCP 不进快照
    assert snapshot[0].play_count == 1  # 机台真值：基线 pc=5 不取大进快照
    # 上传载荷仍是合并后的（跨目标补达成情况的语义不变）
    assert target.updates[0][0].fc == FCType.FCP


async def test_run_update_tags_fail_target(songs):
    """链内取数/上传失败在异常上挂报错目标名（F4 边界）：

    maimai_py 异常无 provider 标识，matchers 的落雪续期归属判定靠该标签
    区分水鱼/落雪失效（用户点破：错误回调 ctx.name 可作归属依据）。
    """
    from maimai_py.exceptions import InvalidPlayerIdentifierError

    from nonebot_plugin_awmc_score_updater.updater import FAIL_TARGET_ATTR, run_update

    client = await _make_client()
    src = [
        (
            FakeUpdateProvider([mk_score()]),
            PlayerIdentifier(credentials="x"),
            {"name": "机台"},
        )
    ]

    class FailingFetch(FakeUpdateProvider):
        async def get_scores_all(self, identifier, client):
            raise InvalidPlayerIdentifierError("fetch unauthorized")

    class FailingUpdate(FakeUpdateProvider):
        async def update_scores(self, identifier, scores, client):
            raise InvalidPlayerIdentifierError("update unauthorized")

    fetch_fail = FailingFetch([])
    with pytest.raises(InvalidPlayerIdentifierError) as ei:
        await run_update(
            client,
            src,
            [(fetch_fail, PlayerIdentifier(credentials="t"), {"name": "落雪"})],
            full=False,
            max_retries=0,
        )
    assert getattr(ei.value, FAIL_TARGET_ATTR, None) == "落雪"

    update_fail = FailingUpdate([])
    with pytest.raises(InvalidPlayerIdentifierError) as ei2:
        await run_update(
            client,
            src,
            [(update_fail, PlayerIdentifier(credentials="t"), {"name": "落雪"})],
            full=False,
            max_retries=0,
        )
    assert getattr(ei2.value, FAIL_TARGET_ATTR, None) == "落雪"


async def test_run_update_pc_hook_full_mode_no_baseline(songs):
    """全量模式：不拉目标 → 基线为空列表（扫码锚定不依赖基线）。"""
    from nonebot_plugin_awmc_score_updater.updater import run_update

    client = await _make_client()
    target = FakeUpdateProvider()
    source = FakeUpdateProvider([mk_score()])
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "s"})]
    targets = [(target, PlayerIdentifier(credentials="t"), {"name": "t"})]
    calls: list = []

    async def hook(source_scores, target_dicts):
        calls.append(target_dicts)

    await run_update(client, src, targets, full=True, max_retries=0, pc_hook=hook)
    assert calls == [[]]


async def test_run_update_pc_hook_failure_swallowed(songs):
    """钩子异常只记日志，不影响导分结果。"""
    from nonebot_plugin_awmc_score_updater.updater import run_update

    client = await _make_client()
    target = FakeUpdateProvider()
    source = FakeUpdateProvider([mk_score()])
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "s"})]
    targets = [(target, PlayerIdentifier(credentials="t"), {"name": "t"})]

    async def boom(source_scores, target_dicts):
        raise RuntimeError("hook failed")

    duration, skipped, failures = await run_update(
        client, src, targets, full=False, max_retries=0, pc_hook=boom
    )
    assert duration >= 0
    assert skipped == 0
    assert failures == []
    assert len(target.updates) == 1


async def test_run_update_pc_hook_not_called_on_failure(songs):
    """上传失败且重试耗尽：钩子不触发（防重试轮次重复计数）。"""
    import pytest

    from nonebot_plugin_awmc_score_updater.updater import run_update

    client = await _make_client()
    target = FakeUpdateProvider(update_fail=True)
    source = FakeUpdateProvider([mk_score()])
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "s"})]
    targets = [(target, PlayerIdentifier(credentials="t"), {"name": "t"})]
    calls: list = []

    async def hook(source_scores, target_dicts):
        calls.append(1)

    with pytest.raises(RuntimeError, match="update failed"):
        await run_update(client, src, targets, full=False, max_retries=1, pc_hook=hook)
    assert calls == []


async def test_delta_chain_invalid_json_retry_narrowed_to_failed_targets(songs):
    """InvalidJsonError 降级重传只针对返回该错误的目标（L-42）：

    已成功站不重传（重复上传纯浪费配额），failures 按目标合并两轮结果——
    修复前对全部目标重传且首轮结果被整体覆盖。"""
    from maimai_py.exceptions import InvalidJsonError

    from nonebot_plugin_awmc_score_updater.updater import delta_updates_chain

    client = await _make_client()
    source = FakeUpdateProvider(
        [
            mk_score(song_id=199, achievements=100.0, dx_score=2500),
            # 未收录曲（624 不在任何目标基线里）→ 降级时剔除，skipped=1
            mk_score(
                song_id=624,
                achievements=90.0,
                dx_score=100,
                song_type=SongType.STANDARD,
            ),
        ]
    )

    class JsonFlakyProvider(FakeUpdateProvider):
        """首轮上传抛 InvalidJsonError（500 HTML），重传成功。"""

        def __init__(self, scores=None):
            super().__init__(scores)
            self.update_calls = 0

        async def update_scores(self, identifier, scores, client):
            self.update_calls += 1
            if self.update_calls == 1:
                raise InvalidJsonError("<html>500</html>")
            self.updates.append(list(scores))
            self.scores.extend(scores)

    water = JsonFlakyProvider([mk_score(achievements=99.0, dx_score=2000)])
    lxns = FakeUpdateProvider([mk_score(achievements=99.0, dx_score=2000)])
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "机台"})]
    targets = [
        (water, PlayerIdentifier(credentials="w"), {"name": "水鱼"}),
        (lxns, PlayerIdentifier(credentials="l"), {"name": "落雪"}),
    ]

    skipped, _, _, failures = await delta_updates_chain(client, src, targets)

    assert skipped == 1
    assert failures == []
    # 落雪首轮已成功：仅 1 次上传（含 199+624），不参与重传
    assert len(lxns.updates) == 1
    assert sorted(s.id for s in lxns.updates[0]) == [199, 624]
    # 水鱼首轮 InvalidJson：重传仅含已收录部分
    assert water.update_calls == 2
    assert [s.id for s in water.updates[0]] == [199]


async def test_delta_chain_full_mode_invalid_json_goes_failures(songs):
    """全量模式（compare_target=False）InvalidJsonError 走 failures 不虚报
    skipped：无目标基线时过滤集恒空、重传不可用——旧实现把整批计成
    skipped_unknown（口径失真）且静默不重传。"""
    from maimai_py.exceptions import InvalidJsonError

    from nonebot_plugin_awmc_score_updater.updater import delta_updates_chain

    client = await _make_client()
    source = FakeUpdateProvider(
        [mk_score(song_id=199, achievements=100.0, dx_score=2500)]
    )

    class JsonBrokenProvider(FakeUpdateProvider):
        """上传恒抛 InvalidJsonError。"""

        def __init__(self):
            super().__init__([])
            self.update_calls = 0

        async def update_scores(self, identifier, scores, client):
            self.update_calls += 1
            raise InvalidJsonError("<html>500</html>")

    broken = JsonBrokenProvider()
    healthy = FakeUpdateProvider([])
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "机台"})]
    # 双目标：单目标全失败会整链上抛，配一个健康站承载部分成功语义
    targets = [
        (broken, PlayerIdentifier(credentials="w"), {"name": "水鱼"}),
        (healthy, PlayerIdentifier(credentials="l"), {"name": "落雪"}),
    ]

    skipped, _, _, failures = await delta_updates_chain(
        client, src, targets, compare_target=False
    )

    assert skipped == 0  # 不得虚报「因未收录跳过」
    assert [(n, type(e).__name__) for n, e in failures] == [("水鱼", "InvalidJsonError")]
    assert broken.update_calls == 1  # 无重传（重传需目标基线，全量模式不可用）
    assert [s.id for s in healthy.updates[0]] == [199]  # 健康站照常收到上传


async def test_delta_chain_partial_failure_collected(songs):
    """部分成功语义（2026-09-28）：单目标失败不上抛拖死整链——失败目标
    收集进返回值（异常自带归属标签），他站照常收到上传。"""

    from nonebot_plugin_awmc_score_updater.updater import (
        FAIL_TARGET_ATTR,
        delta_updates_chain,
    )

    client = await _make_client()
    source = FakeUpdateProvider([mk_score()])
    failed = FakeUpdateProvider(update_fail=True)
    ok = FakeUpdateProvider([])
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "机台"})]
    targets = [
        (failed, PlayerIdentifier(credentials="w"), {"name": "水鱼"}),
        (ok, PlayerIdentifier(credentials="l"), {"name": "落雪"}),
    ]

    skipped, _, _, failures = await delta_updates_chain(client, src, targets)

    assert skipped == 0
    assert len(failures) == 1
    name, exc = failures[0]
    assert name == "水鱼"
    assert isinstance(exc, RuntimeError)
    assert getattr(exc, FAIL_TARGET_ATTR, None) == "水鱼"
    assert len(ok.updates) == 1  # 他站成功结果不受失败目标影响


async def test_delta_chain_all_targets_fail_raises(songs):
    """全部目标失败仍上抛第一个异常（保持调用方整链错误映射路径）。"""
    from nonebot_plugin_awmc_score_updater.updater import delta_updates_chain

    client = await _make_client()
    source = FakeUpdateProvider([mk_score()])
    failed_a = FakeUpdateProvider(update_fail=True)
    failed_b = FakeUpdateProvider(update_fail=True)
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "机台"})]
    targets = [
        (failed_a, PlayerIdentifier(credentials="w"), {"name": "水鱼"}),
        (failed_b, PlayerIdentifier(credentials="l"), {"name": "落雪"}),
    ]

    with pytest.raises(RuntimeError, match="update failed"):
        await delta_updates_chain(client, src, targets)


async def test_run_update_partial_failure_returns_failures(songs):
    """run_update 层：部分失败直接返回失败列表（不触发指数退避重试），
    pc_hook 照常触发（观测按各站已有基线桥接，缺一站只是漏计不虚增）。"""
    from nonebot_plugin_awmc_score_updater.updater import run_update

    client = await _make_client()
    failed = FakeUpdateProvider(update_fail=True)
    ok = FakeUpdateProvider([])
    source = FakeUpdateProvider([mk_score()])
    src = [(source, PlayerIdentifier(credentials="x"), {"name": "机台"})]
    targets = [
        (failed, PlayerIdentifier(credentials="w"), {"name": "水鱼"}),
        (ok, PlayerIdentifier(credentials="l"), {"name": "落雪"}),
    ]
    hook_calls: list = []

    async def hook(source_scores, target_dicts):
        hook_calls.append(target_dicts)

    duration, skipped, failures = await run_update(
        client, src, targets, full=False, max_retries=3, pc_hook=hook
    )
    assert duration >= 0
    assert skipped == 0
    assert [name for name, _ in failures] == ["水鱼"]
    assert len(hook_calls) == 1
    assert len(ok.updates) == 1
