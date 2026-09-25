"""SaltNet 直连层测试（respx mock）。插件相关导入一律函数内进行。"""

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


def _detail(
    music_id: int, level: int = 4, achievement: int = 1005000, combo: int = 0
) -> dict:
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
            {"userMusicDetailList": [_detail(200, combo=1), _detail(10001)]},
            {"userMusicDetailList": [_detail(300)]},
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

    score = deser_score(_detail(200, achievement=1005000, combo=1))
    assert score.id == 200
    assert score.type.name == "STANDARD"
    assert score.achievements == 100.5
    assert score.fc is not None
    assert score.fc.value == 3  # 4 - 1
    assert score.dx_score == 2000


def test_deser_score_dx_folds_id():
    from nonebot_plugin_awmc_score_updater.saltapi import deser_score

    score = deser_score(_detail(10001))
    assert score.id == 1
    assert score.type.name == "DX"


def test_deser_score_utage_keeps_id_and_level0():
    from nonebot_plugin_awmc_score_updater.saltapi import deser_score

    score = deser_score(_detail(100231, level=10))
    assert score.id == 100231
    assert score.level_index.value == 0  # 宴谱恒取 LevelIndex(0)


def test_deser_score_app_theoretical():
    from nonebot_plugin_awmc_score_updater.saltapi import deser_score

    # comboStatus=0 且达成率 101.0000% → APP（理论值）
    score = deser_score(_detail(200, achievement=1010000))
    assert score.achievements == 101.0
    assert score.fc is not None
    assert score.fc.name == "APP"
