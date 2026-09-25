"""测试公共工具：假曲目数据构造与曲库缓存注入。"""

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
    note_designer: str = "サルミ",
    version: int = 25000,
    tap_num: int = 500,
    hold_num: int = 50,
    slide_num: int = 50,
    touch_num: int = 50,
    break_num: int = 10,
    curve: CurveObject | None = None,
) -> SongDifficulty:
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
    diff_id: int = 100001,
    level: str = "11",
    level_value: float = 11.5,
    kanji: str = "宴",
    description: str = "新年の宴",
    is_buddy: bool = False,
    buddy_notes: "BuddyNotes | None" = None,
    tap_num: int = 300,
    hold_num: int = 10,
    slide_num: int = 10,
    touch_num: int = 10,
    break_num: int = 5,
    version: int = 24000,
) -> SongDifficultyUtage:
    return SongDifficultyUtage(
        type=SongType.UTAGE,
        level=level,
        level_value=level_value,
        level_index=LevelIndex.BASIC,
        note_designer="宴譜",
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
    left: tuple[int, int, int, int, int] = (474, 61, 59, 28, 15),
    right: tuple[int, int, int, int, int] = (470, 59, 59, 28, 15),
) -> BuddyNotes:
    """左右手物量（01 文档双人谱口径五元组 [Tap, Hold, Slide, Touch, Break]）。"""
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
    artist: str = "曲师A",
    genre: Genre = Genre.maimai,
    bpm: int = 200,
    aliases: list[str] | None = None,
    version: int = 25000,
    disabled: bool = False,
    diffs: list[SongDifficulty] | None = None,
    utage: list[SongDifficultyUtage] | None = None,
) -> Song:
    if diffs is None:
        diffs = [
            make_diff(
                type=SongType.STANDARD,
                level_index=LevelIndex.EXPERT,
                level="10",
                level_value=10.5,
            ),
            make_diff(
                type=SongType.DX,
                level_index=LevelIndex.MASTER,
                level="13",
                level_value=13.2,
            ),
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
    """一套覆盖各查询路径的样例曲库。"""
    return [
        make_song(231, "PENGUIN", aliases=["企鹅舞"], artist="DwikkoAengae"),
        make_song(
            500,
            "Preferences",
            aliases=["普瑞", "普雷呃伦斯"],
            artist="uzz",
            bpm=180,
            diffs=[
                make_diff(
                    type=SongType.STANDARD,
                    level_index=LevelIndex.BASIC,
                    level="4",
                    level_value=4.0,
                ),
                make_diff(
                    type=SongType.DX,
                    level_index=LevelIndex.MASTER,
                    level="13+",
                    level_value=13.7,
                ),
            ],
        ),
        make_song(
            901,
            "バーチャルダム_w/UTAGE",
            genre=Genre.宴会場,
            bpm=100,
            disabled=False,
            diffs=[],
            utage=[make_utage()],
        ),
        make_song(902, "disabled曲", disabled=True),
    ]


async def seed_service(
    service: "SongService", songs: list[Song] | None = None
) -> list[Song]:
    """向曲库服务注入样例数据（绕过网络）。"""
    songs = songs if songs is not None else sample_songs()
    service._ready.clear()
    await service.inject(songs)
    return songs
