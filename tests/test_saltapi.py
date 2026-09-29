"""SaltNet 直连层测试（respx mock）。插件相关导入一律函数内进行。

成绩明细 musicId 锚定真实曲（机台 id 空间）：199 チルノのパーフェクトさんすう教室
（SD=199、DX MASTER=10199、蛸宴=100199）与 624 KISS CANDY FLAVOR（SD）。
"""

import respx
import pytest
from httpx import Response

MAIN = "https://salt_api_main.realtvop.top"
FALLBACK = "https://salt_api_backup.realtvop.top"

SGWCMAID = "SGWCMAID" + "0" * (84 - len("SGWCMAID"))
QR64 = SGWCMAID[-64:]


def test_extract_qrcode_sgwcmaid():
    from nonebot_plugin_awmc_score_updater.saltapi import extract_qrcode

    assert extract_qrcode(SGWCMAID) == QR64


def test_extract_qrcode_url():
    from nonebot_plugin_awmc_score_updater.saltapi import extract_qrcode

    # 链接形态：截取 MAID 段的尾 64 位（二维码内容恰为 64 字符）
    url = f"https://mai.paradproject.com/?t=MAID{'x' * 64}"
    assert extract_qrcode(url) == "x" * 64


def test_extract_qrcode_invalid():
    from nonebot_plugin_awmc_score_updater.saltapi import extract_qrcode

    assert extract_qrcode("随便什么") is None
    assert extract_qrcode("SGWCMAID123") is None  # 长度不符
    assert extract_qrcode("") is None


@respx.mock
async def test_parse_qrcode_ok():
    from nonebot_plugin_awmc_score_updater.saltapi import parse_qrcode

    respx.post(f"{MAIN}/getQRInfo").mock(
        return_value=Response(200, json={"errorID": 0, "userID": "12345"})
    )
    assert await parse_qrcode(QR64, main_url=MAIN, fallback_url=FALLBACK) == "12345"


@respx.mock
async def test_parse_qrcode_business_reject_no_fallback():
    """业务拒绝（errorID != 0）不重试备用域名。"""
    from nonebot_plugin_awmc_score_updater.saltapi import parse_qrcode

    main = respx.post(f"{MAIN}/getQRInfo").mock(
        return_value=Response(200, json={"errorID": 1})
    )
    fallback = respx.post(f"{FALLBACK}/getQRInfo").mock(
        return_value=Response(200, json={"errorID": 0})
    )
    assert await parse_qrcode(QR64, main_url=MAIN, fallback_url=FALLBACK) is None
    assert main.called
    assert not fallback.called


@respx.mock
async def test_parse_qrcode_fallback():
    from nonebot_plugin_awmc_score_updater.saltapi import parse_qrcode

    respx.post(f"{MAIN}/getQRInfo").mock(return_value=Response(500))
    respx.post(f"{FALLBACK}/getQRInfo").mock(
        return_value=Response(200, json={"errorID": 0, "userID": "42"})
    )
    assert await parse_qrcode(QR64, main_url=MAIN, fallback_url=FALLBACK) == "42"


@respx.mock
async def test_parse_qrcode_all_down():
    from nonebot_plugin_awmc_score_updater.saltapi import SaltApiError, parse_qrcode

    respx.post(f"{MAIN}/getQRInfo").mock(return_value=Response(500))
    respx.post(f"{FALLBACK}/getQRInfo").mock(return_value=Response(500))
    with pytest.raises(SaltApiError):
        await parse_qrcode(QR64, main_url=MAIN, fallback_url=FALLBACK)


@respx.mock
async def test_parse_qrcode_html_body_falls_back():
    """200+HTML（网关劫持页）→ JSONDecodeError 不逃出 SaltApiError 语义，
    按该域名失败继续试备域（L-17）。"""
    from nonebot_plugin_awmc_score_updater.saltapi import parse_qrcode

    respx.post(f"{MAIN}/getQRInfo").mock(
        return_value=Response(200, text="<html>gateway error</html>")
    )
    respx.post(f"{FALLBACK}/getQRInfo").mock(
        return_value=Response(200, json={"errorID": 0, "userID": "77"})
    )
    assert await parse_qrcode(QR64, main_url=MAIN, fallback_url=FALLBACK) == "77"


@respx.mock
async def test_fetch_score_payload_html_body_raises_saltapi_error():
    """200+HTML → 立即抛 SaltApiError（计入失败、不试备域），JSONDecodeError
    不再白跑 run_update 的 3 轮退避后落通用兜底（L-17）。"""
    from nonebot_plugin_awmc_score_updater.saltapi import (
        SaltApiError,
        fetch_score_payload,
    )

    respx.post(f"{MAIN}/updateUser").mock(
        return_value=Response(200, text="<html>gateway error</html>")
    )
    fallback = respx.post(f"{FALLBACK}/updateUser").mock(return_value=Response(200))
    with pytest.raises(SaltApiError, match="非 JSON"):
        await fetch_score_payload("42", None, main_url=MAIN, fallback_url=FALLBACK)
    assert not fallback.called


def _detail(
    music_id: int, level: int = 3, achievement: int = 1005000, combo: int = 0
) -> dict:
    """SaltNet 成绩明细（level 默认 3=MASTER：锚定的 199/10199/624 均有 MASTER 谱）。"""
    return {
        "musicId": music_id,
        "level": level,
        "achievement": achievement,
        "comboStatus": combo,
        "syncStatus": 0,
        "deluxscoreMax": 2000,
    }


@respx.mock
async def test_fetch_score_payload_flatten():
    from nonebot_plugin_awmc_score_updater.saltapi import fetch_score_payload

    payload = {
        "userMusicList": [
            {"userMusicDetailList": [_detail(199, combo=1), _detail(10199)]},
            {"userMusicDetailList": [_detail(624)]},
        ]
    }
    respx.post(f"{MAIN}/updateUser").mock(return_value=Response(200, json=payload))
    rows = await fetch_score_payload("42", None, main_url=MAIN, fallback_url=FALLBACK)
    assert len(rows) == 3


@respx.mock
async def test_fetch_score_payload_with_qrcode_sends_field():
    from nonebot_plugin_awmc_score_updater.saltapi import fetch_score_payload

    route = respx.post(f"{MAIN}/updateUser").mock(
        return_value=Response(200, json={"userMusicList": []})
    )
    rows = await fetch_score_payload("42", QR64, main_url=MAIN, fallback_url=FALLBACK)
    assert rows == []
    import json

    body = json.loads(route.calls.last.request.content)
    assert body["qrCode"] == QR64
    assert body["userId"] == "42"
    assert body["importToken"] == ""


def test_deser_score_standard():
    from nonebot_plugin_awmc_score_updater.saltapi import deser_score

    # 199 SD（机台 musicId = 根 id）
    score = deser_score(_detail(199, achievement=1005000, combo=1))
    assert score.id == 199
    assert score.type.name == "STANDARD"
    assert score.achievements == 100.5
    assert score.fc is not None
    assert score.fc.value == 3  # 4 - 1
    assert score.dx_score == 2000


def test_deser_score_dx_folds_id():
    from nonebot_plugin_awmc_score_updater.saltapi import deser_score

    # 真实折根对：199 DX MASTER 机台 musicId = 10199（根 199 + 10000）
    score = deser_score(_detail(10199))
    assert score.id == 199
    assert score.type.name == "DX"


def test_deser_score_utage_keeps_id_and_level0():
    from nonebot_plugin_awmc_score_updater.saltapi import deser_score

    # 蛸チルノ（100199，真实 6 位宴谱机台 id）保留不折根
    score = deser_score(_detail(100199, level=10))
    assert score.id == 100199
    assert score.level_index.value == 0  # 宴谱恒取 LevelIndex(0)


def test_deser_score_app_theoretical():
    from nonebot_plugin_awmc_score_updater.saltapi import deser_score

    # comboStatus=0 且达成率 101.0000% → APP（理论值）
    score = deser_score(_detail(199, achievement=1010000))
    assert score.achievements == 101.0
    assert score.fc is not None
    assert score.fc.name == "APP"


def test_deser_score_play_count_mapped():
    """扫码全量载荷：playCount 真值映射进 Score.play_count。"""
    from nonebot_plugin_awmc_score_updater.saltapi import deser_score

    raw = {**_detail(199), "playCount": 42}
    assert deser_score(raw).play_count == 42


def test_deser_score_play_count_null_and_absent():
    """简略载荷 playCount 恒 null（或缺失）→ None。"""
    from nonebot_plugin_awmc_score_updater.saltapi import deser_score

    assert deser_score({**_detail(199), "playCount": None}).play_count is None
    assert deser_score(_detail(199)).play_count is None
