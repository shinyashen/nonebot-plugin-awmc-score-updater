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


def test_looks_like_qrcode():
    from nonebot_plugin_awmc_score_updater.saltapi import looks_like_qrcode

    # 形似 = 前缀命中（长度/链接格式不合法也视作「尝试过导二维码」）
    assert looks_like_qrcode(SGWCMAID)
    assert looks_like_qrcode("SGWCMAID123")  # 长度不符也形似
    assert looks_like_qrcode(f"https://mai.paradproject.com/?t=MAID{'x' * 64}")
    assert looks_like_qrcode("http://example.com/MAIDxx")
    # 不形似：日常词、普通文本（导指令据此静默跳过不响应）
    assert not looks_like_qrcode("随便什么")
    assert not looks_like_qrcode("导演")
    assert not looks_like_qrcode("")


def test_looks_like_qrcode_covers_extract_success():
    """一致性：extract_qrcode 能解析的输入必形似（前缀常量共用，防漂移）。"""
    from nonebot_plugin_awmc_score_updater.saltapi import (
        extract_qrcode,
        looks_like_qrcode,
    )

    for text in (SGWCMAID, f"https://mai.paradproject.com/?t=MAID{'x' * 64}"):
        assert extract_qrcode(text) is not None
        assert looks_like_qrcode(text)


@respx.mock
async def test_parse_qrcode_ok():
    from nonebot_plugin_awmc_score_updater.saltapi import parse_qrcode

    respx.post(f"{MAIN}/getQRInfo").mock(
        return_value=Response(200, json={"errorID": 0, "userID": "12345"})
    )
    assert await parse_qrcode(QR64, main_url=MAIN, fallback_url=FALLBACK) == "12345"


@respx.mock
async def test_parse_qrcode_business_reject_no_fallback():
    """业务拒绝（errorID != 0）抛 SaltApiBusinessError，不重试备用域名。

    一次性凭证下「已使用/已过期」由服务端同码返回（1/2），文案提示重新获取。
    """
    from nonebot_plugin_awmc_score_updater.saltapi import (
        SaltApiBusinessError,
        parse_qrcode,
    )

    main = respx.post(f"{MAIN}/getQRInfo").mock(
        return_value=Response(200, json={"errorID": 1})
    )
    fallback = respx.post(f"{FALLBACK}/getQRInfo").mock(
        return_value=Response(200, json={"errorID": 0, "userID": "42"})
    )
    with pytest.raises(
        SaltApiBusinessError, match="二维码无效、已使用或已过期（一次性凭证）"
    ):
        await parse_qrcode(QR64, main_url=MAIN, fallback_url=FALLBACK)
    assert main.called
    assert not fallback.called


@respx.mock
async def test_parse_qrcode_business_reject_prefers_server_message():
    """业务拒绝文案优先透传服务端 ``error`` 字段（比契约码表更具体）。"""
    from nonebot_plugin_awmc_score_updater.saltapi import (
        SaltApiBusinessError,
        parse_qrcode,
    )

    respx.post(f"{MAIN}/getQRInfo").mock(
        return_value=Response(
            200, json={"errorID": 2, "error": "二维码已过期（10 分钟窗口），请重新获取"}
        )
    )
    with pytest.raises(SaltApiBusinessError, match="二维码已过期（10 分钟窗口）"):
        await parse_qrcode(QR64, main_url=MAIN, fallback_url=FALLBACK)


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
    按该域名失败继续试备域。"""
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
    不再白跑 run_update 的 3 轮退避后落通用兜底。"""
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


@respx.mock
async def test_fetch_score_payload_business_reject_prefers_server_message():
    """200+errorID=2（二维码一次性已用/过期）：抛 SaltApiBusinessError，文案取
    ``error`` 字段——修复前业务拒绝被静默当成空成绩列表，用户只看到「导了 0 条」。"""
    from nonebot_plugin_awmc_score_updater.saltapi import (
        SaltApiBusinessError,
        fetch_score_payload,
    )

    respx.post(f"{MAIN}/updateUser").mock(
        return_value=Response(
            200, json={"errorID": 2, "error": "二维码已使用，请重新获取"}
        )
    )
    fallback = respx.post(f"{FALLBACK}/updateUser").mock(
        return_value=Response(200, json={"errorID": 0, "userMusicList": []})
    )
    with pytest.raises(SaltApiBusinessError, match="二维码已使用，请重新获取"):
        await fetch_score_payload("42", QR64, main_url=MAIN, fallback_url=FALLBACK)
    # 业务拒绝不回退备域（备用域名结果相同，且二维码已被消费）
    assert not fallback.called


@respx.mock
@pytest.mark.parametrize(
    ("error_id", "expected"),
    [
        (1, "二维码无效、已使用或已过期（一次性凭证）"),
        (91, "你提供的二维码所对应账号与已绑定的账号不匹配，请检查后重新输入"),
        (99, "二维码格式无效"),
        (54, "二维码服务配置异常（errorID=54）"),
        (57, "二维码服务配置异常（errorID=57）"),
        (50, "二维码解析失败（errorID=50）"),
    ],
)
async def test_fetch_score_payload_business_reject_fallback_message(error_id, expected):
    """无 ``error`` 字段时按 errorID 退化：1/2、91、99、54/57 及未知码。"""
    from nonebot_plugin_awmc_score_updater.saltapi import (
        SaltApiBusinessError,
        fetch_score_payload,
    )

    respx.post(f"{MAIN}/updateUser").mock(
        return_value=Response(200, json={"errorID": error_id})
    )
    with pytest.raises(SaltApiBusinessError, match=expected):
        await fetch_score_payload("42", QR64, main_url=MAIN, fallback_url=FALLBACK)


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
        "errorID": 0,
        "userMusicList": [
            {"userMusicDetailList": [_detail(199, combo=1), _detail(10199)]},
            {"userMusicDetailList": [_detail(624)]},
        ],
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
