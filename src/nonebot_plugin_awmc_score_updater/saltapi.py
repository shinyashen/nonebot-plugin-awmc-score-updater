"""SaltNet API 直连层（Realtvop 的华立微信代理服务）。

maimai-py 未覆盖的外部接口：机台二维码解析（``/getQRInfo``）与微信成绩
拉取（``/updateUser``）。主域名失败自动回退备用域名；业务性拒绝（如二维码
过期）直接返回，不重试备用域名。

参考实现：HoshinoBot 版 maimai-score-updater（本插件的前身）。
"""

import re
import ssl
from typing import Any

import httpx
from maimai_py.enums import FCType, FSType, RateType, SongType, LevelIndex
from maimai_py.models import Score
from nonebot_plugin_awmc_helper.core.http import build_smart_transport

_client: httpx.AsyncClient | None = None


def _lenient_verify() -> ssl.SSLContext:
    """SaltNet 证书现状（2026-09 实测）：主备域名证书均主机名不匹配（签给
    其他域名），原版 Hoshino 插件因此裸 ``verify=False``。这里保留 CA 链
    验证、仅放开主机名校验——比原版少暴露一层 MITM 面；上游修证书后可还原。
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    return ctx


def get_client() -> httpx.AsyncClient:
    """SaltNet 直连共享客户端（懒创建，代理传输与主插件 ext 层同源）。"""
    global _client
    if _client is None:
        verify = _lenient_verify()
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10),
            follow_redirects=True,
            verify=verify,
            transport=build_smart_transport(verify=verify),
        )
    return _client


class SaltApiError(Exception):
    """SaltNet 主备域名均不可用或业务失败（message 面向用户可读）。"""


def extract_qrcode(text: str) -> str | None:
    """从用户输入提取 64 位二维码内容，不合法返回 None。

    支持两种形态：识别结果原文（``SGWCMAID`` 开头 84 位，取尾 64 位），
    或二维码页面的 https 链接（截取 ``MAID`` 段的尾 64 位）。
    """
    text = text.strip()
    if text.startswith("SGWCMAID") and len(text) == 84:
        return text[-64:]
    if text.startswith("http"):
        if matches := re.findall(r"MAID.{0,76}", text):
            return matches[0][-64:]
    return None


async def parse_qrcode(qr_code: str, *, main_url: str, fallback_url: str) -> str | None:
    """调 ``/getQRInfo`` 解析二维码，成功返回华立 userID（字符串）。

    二维码无效/过期（errorID != 0）返回 None——业务性拒绝，备用域名结果
    相同，不重试；主备域名网络/HTTP 均失败抛 :class:`SaltApiError`。
    """
    payload = {"qrCode": qr_code}
    for url in (main_url, fallback_url):
        try:
            resp = await get_client().post(f"{url}/getQRInfo", json=payload)
        except httpx.RequestError:
            continue
        if resp.status_code != 200:
            continue
        data = resp.json()
        if data.get("errorID") == 0:
            return str(data.get("userID"))
        return None
    raise SaltApiError("二维码解析服务暂不可用，请稍后再试")


async def fetch_score_payload(
    userid: str,
    qrcode: str | None,
    *,
    main_url: str,
    fallback_url: str,
) -> list[dict[str, Any]]:
    """拉取微信成绩明细，返回平铺后的 userMusicDetailList。

    ``qrcode`` 传入时为全量拉取（带当日游玩记录），否则为常规拉取。
    """
    payload: dict[str, str] = {"userId": userid, "importToken": ""}
    if qrcode:
        payload["qrCode"] = qrcode
    last_status = 0
    for url in (main_url, fallback_url):
        try:
            resp = await get_client().post(f"{url}/updateUser", json=payload)
        except httpx.RequestError:
            continue
        if resp.status_code != 200:
            last_status = resp.status_code
            continue
        raw = resp.json()
        return [
            music
            for entry in raw.get("userMusicList", [])
            for music in entry.get("userMusicDetailList", [])
        ]
    if last_status:
        raise SaltApiError(f"成绩拉取失败（HTTP {last_status}），请稍后再试")
    raise SaltApiError("成绩拉取服务暂不可用，请稍后再试")


def deser_score(raw: dict[str, Any]) -> Score:
    """SaltNet 成绩明细 → maimai-py :class:`Score`。

    - id：DX 谱（id>10000）折回曲目 id；宴谱（>100000）保留 6 位机台内部 id；
    - level_index：宴明细的 level 值为 10，超常规枚举范围一律取 LevelIndex(0)
      （maimai-py 约定宴谱 level_index 恒为 0）；
    - fc：SaltNet 状态码 1-4 按 ``4 - n`` 映射 FCType；101.0000% 视为 APP；
    - fs：状态码对 5 取模落入 FSType 枚举。
    """
    song_id = int(raw["musicId"])
    achievement = int(raw["achievement"]) / 10000
    level_value = int(raw["level"])
    combo = int(raw["comboStatus"])
    sync = int(raw["syncStatus"])
    return Score(
        id=song_id if song_id > 100000 else song_id % 10000,
        # maimai_py 注解 level: str 偏紧：上传序列化（水鱼/落雪 _ser_score）只
        # 消费 level_index，不读该字段，None 运行时安全
        level=None,  # type: ignore[reportArgumentType]
        level_index=LevelIndex(level_value) if level_value < 5 else LevelIndex(0),
        achievements=achievement,
        fc=FCType(4 - combo)
        if combo
        else (FCType.APP if achievement == 101.0 else None),
        fs=FSType(sync % 5) if sync else None,
        dx_score=int(raw["deluxscoreMax"]),
        dx_rating=None,
        play_count=None,
        play_time=None,
        rate=RateType._from_achievement(achievement),
        type=SongType._from_id(song_id),
    )
