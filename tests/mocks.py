"""测试公共工具：真实样例曲构造与曲库缓存注入。

样例曲取真实曲目（2026-09-29 取材，值与 tests/data/snapshots/ 一致）：
チルノのパーフェクトさんすう教室(199，SD+DX+蛸宴)、True Love Song(8)、
KISS CANDY FLAVOR(624)——后两首在柚子别名库共持真实别名「糖糖」，天然构成
多命中场景。别名均为柚子实测；disabled 路径当前真实数据无原型，构造补位。
"""

from typing import TYPE_CHECKING
from pathlib import Path

import pytest
from maimai_py import (
    Song,
    Genre,
    SongType,
    LevelIndex,
    SongDifficulty,
    SongDifficulties,
)
from maimai_py.models import BuddyNotes, CurveObject, SongDifficultyUtage

if TYPE_CHECKING:
    from nonebot_plugin_awmc_helper.core.songs import SongService

# NB 版视觉移植的底图来自本地素材包 static/（永不入库，见 AGENTS.md 硬性规则 5）；
# CI 等无素材环境下跳过依赖底图的渲染测试。
ASSETS_READY = Path("static/mai/pic/prism_plus/b50.png").exists()
requires_assets = pytest.mark.skipif(
    not ASSETS_READY, reason="需本地素材包 static/（不入库），CI 无此环境"
)


def make_diff(
    *,
    type: SongType = SongType.DX,
    level_index: LevelIndex = LevelIndex.MASTER,
    level: str = "13",
    level_value: float = 13.0,
    note_designer: str = "まぐランド",
    version: int = 26000,
    tap_num: int = 457,
    hold_num: int = 43,
    slide_num: int = 107,
    touch_num: int = 49,
    break_num: int = 37,
    curve: CurveObject | None = None,
) -> SongDifficulty:
    """默认谱面 = 199 チルノ DX MASTER 真实值。"""
    return SongDifficulty(
        type=type,
        level=level,
        level_value=level_value,
        level_index=level_index,
        note_designer=note_designer,
        version=version,
        tap_num=tap_num,
        hold_num=hold_num,
        slide_num=slide_num,
        touch_num=touch_num,
        break_num=break_num,
        curve=curve,
    )


def make_utage(
    *,
    diff_id: int = 100199,
    level: str = "12+",
    level_value: float = 12.7,
    kanji: str = "蛸",
    description: str = "パーフェクトホールド教室",
    is_buddy: bool = False,
    buddy_notes: "BuddyNotes | None" = None,
    tap_num: int = 58,
    hold_num: int = 217,
    slide_num: int = 27,
    touch_num: int = 0,
    break_num: int = 7,
    version: int = 24000,
) -> SongDifficultyUtage:
    """默认宴谱 = 100199 [蛸]チルノのパーフェクトさんすう教室 真实值。"""
    return SongDifficultyUtage(
        type=SongType.UTAGE,
        level=level,
        level_value=level_value,
        level_index=LevelIndex.BASIC,
        note_designer="-",
        version=version,
        tap_num=tap_num,
        hold_num=hold_num,
        slide_num=slide_num,
        touch_num=touch_num,
        break_num=break_num,
        curve=None,
        kanji=kanji,
        description=description,
        diff_id=diff_id,
        is_buddy=is_buddy,
        buddy_notes=buddy_notes,
    )


def make_buddy_notes(
    left: tuple[int, int, int, int, int] = (183, 76, 53, 164, 173),
    right: tuple[int, int, int, int, int] = (172, 63, 53, 102, 216),
) -> BuddyNotes:
    """左右手物量（真实 [協]ラグトレイン，五元组 [Tap, Hold, Slide, Touch, Break]）。"""
    return BuddyNotes(
        left_tap_num=left[0],
        left_hold_num=left[1],
        left_slide_num=left[2],
        left_touch_num=left[3],
        left_break_num=left[4],
        right_tap_num=right[0],
        right_hold_num=right[1],
        right_slide_num=right[2],
        right_touch_num=right[3],
        right_break_num=right[4],
    )


def make_song(
    song_id: int,
    title: str,
    *,
    artist: str = "ARM＋夕野ヨシミ(IOSYS)feat. miko",
    genre: Genre = Genre.東方Project,
    bpm: int = 175,
    aliases: list[str] | None = None,
    version: int = 26000,
    disabled: bool = False,
    diffs: list[SongDifficulty] | None = None,
    utage: list[SongDifficultyUtage] | None = None,
) -> Song:
    if diffs is None:
        diffs = [
            # 199 SD EXPERT 真实值
            make_diff(
                type=SongType.STANDARD,
                level_index=LevelIndex.EXPERT,
                level="10",
                level_value=10.4,
                note_designer="はっぴー",
                version=12000,
                tap_num=248,
                hold_num=86,
                slide_num=26,
                touch_num=0,
                break_num=4,
            ),
            # 199 DX MASTER 真实值
            make_diff(),
        ]
    return Song(
        id=song_id,
        title=title,
        artist=artist,
        genre=genre,
        bpm=bpm,
        map=None,
        version=version,
        rights=None,
        aliases=aliases,
        disabled=disabled,
        difficulties=SongDifficulties(
            standard=[d for d in diffs if d.type == SongType.STANDARD],
            dx=[d for d in diffs if d.type == SongType.DX],
            utage=utage or [],
        ),
    )


def sample_songs() -> list[Song]:
    """一套覆盖各查询路径的真实样例曲库（别名/物量/定数均为实测值）。"""
    return [
        # チルノ：SD+DX 双谱 + 蛸宴（多组宿主）；别名真实（柚子）
        make_song(
            199,
            "チルノのパーフェクトさんすう教室",
            aliases=[
                "琪露诺的完美算术教室",
                "数学课堂",
                "⑨",
                "算数教室",
                "琪露诺",
                "baka",
                "算术教室",
            ],
            version=12000,
            diffs=[
                # SD 四谱（真实）
                make_diff(
                    type=SongType.STANDARD,
                    level_index=LevelIndex.BASIC,
                    level="3",
                    level_value=3.0,
                    note_designer=None,
                    version=12000,
                    tap_num=84,
                    hold_num=30,
                    slide_num=4,
                    touch_num=0,
                    break_num=4,
                ),
                make_diff(
                    type=SongType.STANDARD,
                    level_index=LevelIndex.ADVANCED,
                    level="7",
                    level_value=7.2,
                    note_designer=None,
                    version=12000,
                    tap_num=157,
                    hold_num=27,
                    slide_num=18,
                    touch_num=0,
                    break_num=5,
                ),
                make_diff(
                    type=SongType.STANDARD,
                    level_index=LevelIndex.EXPERT,
                    level="10",
                    level_value=10.4,
                    note_designer="はっぴー",
                    version=12000,
                    tap_num=248,
                    hold_num=86,
                    slide_num=26,
                    touch_num=0,
                    break_num=4,
                ),
                make_diff(
                    type=SongType.STANDARD,
                    level_index=LevelIndex.MASTER,
                    level="13",
                    level_value=13.3,
                    note_designer="某S氏",
                    version=12000,
                    tap_num=402,
                    hold_num=74,
                    slide_num=29,
                    touch_num=0,
                    break_num=21,
                ),
                # DX 四谱（真实，CiRCLE 重制）
                make_diff(
                    type=SongType.DX,
                    level_index=LevelIndex.BASIC,
                    level="3",
                    level_value=3.0,
                    note_designer=None,
                    version=26000,
                    tap_num=147,
                    hold_num=9,
                    slide_num=4,
                    touch_num=6,
                    break_num=2,
                ),
                make_diff(
                    type=SongType.DX,
                    level_index=LevelIndex.ADVANCED,
                    level="6",
                    level_value=6.5,
                    note_designer=None,
                    version=26000,
                    tap_num=262,
                    hold_num=12,
                    slide_num=4,
                    touch_num=11,
                    break_num=6,
                ),
                make_diff(
                    type=SongType.DX,
                    level_index=LevelIndex.EXPERT,
                    level="9+",
                    level_value=9.7,
                    note_designer="サファ太",
                    version=26000,
                    tap_num=186,
                    hold_num=122,
                    slide_num=19,
                    touch_num=26,
                    break_num=9,
                ),
                make_diff(
                    type=SongType.DX,
                    level_index=LevelIndex.MASTER,
                    level="13",
                    level_value=13.0,
                    note_designer="まぐランド",
                    version=26000,
                    tap_num=457,
                    hold_num=43,
                    slide_num=107,
                    touch_num=49,
                    break_num=37,
                ),
            ],
            utage=[make_utage()],
        ),
        # True Love Song：别名「糖糖」（与 624 共持，真实多命中锚）
        make_song(
            8,
            "True Love Song",
            aliases=[
                "true love song",
                "会员制餐厅",
                "真的爱情歌",
                "糖糖",
                "小管弦乐",
                "真爱歌",
                "真爱",
                "真情歌",
            ],
            artist="Kai/クラシック「G線上のアリア」",
            genre=Genre.maimai,
            bpm=150,
            version=10000,
            diffs=[
                make_diff(
                    type=SongType.STANDARD,
                    level_index=LevelIndex.BASIC,
                    level="5",
                    level_value=5.0,
                    note_designer=None,
                    version=10000,
                    tap_num=63,
                    hold_num=23,
                    slide_num=8,
                    touch_num=0,
                    break_num=2,
                ),
                make_diff(
                    type=SongType.STANDARD,
                    level_index=LevelIndex.ADVANCED,
                    level="7",
                    level_value=7.2,
                    note_designer=None,
                    version=10000,
                    tap_num=85,
                    hold_num=27,
                    slide_num=6,
                    touch_num=0,
                    break_num=4,
                ),
                make_diff(
                    type=SongType.STANDARD,
                    level_index=LevelIndex.EXPERT,
                    level="10",
                    level_value=10.2,
                    note_designer="譜面-100号",
                    version=10000,
                    tap_num=110,
                    hold_num=56,
                    slide_num=9,
                    touch_num=0,
                    break_num=2,
                ),
                make_diff(
                    type=SongType.STANDARD,
                    level_index=LevelIndex.MASTER,
                    level="12",
                    level_value=12.4,
                    note_designer="ニャイン",
                    version=10000,
                    tap_num=263,
                    hold_num=14,
                    slide_num=19,
                    touch_num=0,
                    break_num=6,
                ),
            ],
        ),
        # KISS CANDY FLAVOR：与 8 共持别名「糖糖」；同持「kcf」等
        make_song(
            624,
            "KISS CANDY FLAVOR",
            aliases=[
                "kiss candy flavor",
                "小女孩福瑞",
                "kcf",
                "糖糖",
                "亲甜滴",
                "糖的味道",
                "小红帽",
                "亲糖口味",
            ],
            artist="Ao",
            genre=Genre.ゲームバラエティ,
            bpm=140,
            version=18500,
            diffs=[
                make_diff(
                    type=SongType.STANDARD,
                    level_index=LevelIndex.BASIC,
                    level="4",
                    level_value=4.0,
                    note_designer=None,
                    version=18500,
                    tap_num=184,
                    hold_num=6,
                    slide_num=6,
                    touch_num=0,
                    break_num=7,
                ),
                make_diff(
                    type=SongType.STANDARD,
                    level_index=LevelIndex.MASTER,
                    level="13",
                    level_value=13.4,
                    note_designer="Moon Strix",
                    version=18500,
                    tap_num=558,
                    hold_num=39,
                    slide_num=119,
                    touch_num=0,
                    break_num=14,
                ),
            ],
        ),
        # 构造补位（快照无原型）：disabled 路径（落雪 disabled 当前实测为 0）
        make_song(902, "（构造）下架样例", disabled=True),
    ]


async def seed_service(
    service: "SongService", songs: list[Song] | None = None
) -> list[Song]:
    """向曲库服务注入样例数据（绕过网络）。"""
    songs = songs if songs is not None else sample_songs()
    service._ready.clear()
    await service.inject(songs)
    return songs
