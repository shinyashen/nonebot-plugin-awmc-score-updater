"""SaltNet API 直连层（Realtvop 的华立微信代理服务）。

maimai-py 未覆盖的外部接口：机台二维码解析（``/getQRInfo``）与微信成绩
拉取（``/updateUser``）。主域名失败自动回退备用域名；业务性拒绝（如二维码
过期）直接返回，不重试备用域名。

⚠️ 华立机台二维码自 2026-10 起是**一次性凭证**：``/getQRInfo`` 与
``/updateUser`` 各会消费同一枚二维码一次，同一枚二维码**不能**先解析再拉取。
两个接口因此各管一条链路：:func:`parse_qrcode` 供「绑定微信」消费一枚；
:func:`fetch_score_payload` 带 ``qrcode`` 时由 ``/updateUser`` 消费一枚（全量
拉取）——调用方不得在同一枚二维码上串用两个接口。

参考实现：HoshinoBot 版 maimai-score-updater（本插件的前身）。
"""

import re
import ssl
from typing import Any

import httpx
from maimai_py.enums import FCType, FSType, RateType, SongType, LevelIndex
from maimai_py.models import Score
from nonebot_plugin_awmc_helper.constants import DX_ID_OFFSET, UTAGE_ID_BASE
from nonebot_plugin_awmc_helper.core.http import create_smart_client

_client: httpx.AsyncClient | None = None

# SaltNet 直连超时（连接/读/写/池，秒）：成绩明细载荷大，读放宽到 60s
_TIMEOUT = httpx.Timeout(connect=10, read=60, write=10, pool=10)

# 二维码协议常数（原版 Hoshino 插件同款）：机台识别原文为 SGWCMAID 前缀
# 84 位、有效内容取尾 64 位；页面链接形态截取 MAID 段后至多 76 字符再取尾 64
_QR_RAW_PREFIX = "SGWCMAID"
_QR_LINK_PREFIX = "http"
_QR_RAW_LEN = 84
_QR_CORE_LEN = 64
_QR_MAID_WINDOW = 76


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
        _client = create_smart_client(timeout=_TIMEOUT, verify=_lenient_verify())
    return _client


class SaltApiError(Exception):
    """SaltNet 主备域名均不可用或业务失败（message 面向用户可读）。"""


class SaltApiBusinessError(SaltApiError):
    """华立/代理业务拒绝（二维码已用/过期/账号不匹配等）：重试无益。"""


# 业务拒绝的退化文案：服务端未给 error 字段时按 errorID 映射。errorID 语义
# 与 awmc-wahlap-api 契约同源（/getQRInfo 与 /updateUser 共用一套）：
# 1/2 无效·已用·过期（一次性凭证）、91 账号不匹配、99 格式无效、
# 54/57 服务端配置异常（缺 titlekey / 请求头不合规，文案需带码便于运维定位）
_BUSINESS_ERROR_MESSAGES: dict[int, str] = {
    1: "二维码无效、已使用或已过期（一次性凭证），请重新获取",
    2: "二维码无效、已使用或已过期（一次性凭证），请重新获取",
    91: "你提供的二维码所对应账号与已绑定的账号不匹配，请检查后重新输入",
    99: "二维码格式无效",
}


def _business_error_message(data: dict[str, Any]) -> str:
    """业务拒绝的用户文案：优先服务端 ``error`` 字段，缺失时按 errorID 退化。

    服务端的 error 文案通常更具体（如「二维码已过期（10 分钟窗口）」），能
    直接展示给用户；字段缺失（旧服务端/上游异常）时按契约码表兜底，54/57
    这类部署侧异常附上 errorID 便于反馈运维，未知码也不吞掉码值。
    """
    if isinstance(error := data.get("error"), str) and error.strip():
        return error.strip()
    try:
        code = int(data.get("errorID"))
    except (TypeError, ValueError):
        return f"二维码解析失败（errorID={data.get('errorID')}）"
    if code in (1, 2):
        return _BUSINESS_ERROR_MESSAGES[1]
    if code in (54, 57):
        return f"二维码服务配置异常（errorID={code}）"
    return _BUSINESS_ERROR_MESSAGES.get(code, f"二维码解析失败（errorID={code}）")


def looks_like_qrcode(text: str) -> bool:
    """输入是否形似二维码内容（``SGWCMAID`` 原文或 http 链接开头）。

    前缀判定与 :func:`extract_qrcode` 同源（共用常量）：「导」指令以此区分
    「形似二维码但格式不合法」（照常提示格式错误）与「跟在导后的无关文本」
    （日常词、口头语等，静默跳过不响应）。
    """
    text = text.strip()
    return text.startswith(_QR_RAW_PREFIX) or text.startswith(_QR_LINK_PREFIX)


def extract_qrcode(text: str) -> str | None:
    """从用户输入提取 64 位二维码内容，不合法返回 None。

    支持两种形态：识别结果原文（``SGWCMAID`` 开头 84 位，取尾 64 位），
    或二维码页面的 https 链接（截取 ``MAID`` 段的尾 64 位）。
    """
    text = text.strip()
    if text.startswith(_QR_RAW_PREFIX) and len(text) == _QR_RAW_LEN:
        return text[-_QR_CORE_LEN:]
    if text.startswith(_QR_LINK_PREFIX):
        if matches := re.findall(rf"MAID.{{0,{_QR_MAID_WINDOW}}}", text):
            return matches[0][-_QR_CORE_LEN:]
    return None


async def _iter_post(
    path: str,
    payload: dict[str, str],
    *,
    main_url: str,
    fallback_url: str,
):
    """主备域名逐域 POST 的传输骨架：网络失败跳备域，逐个产出响应。

    非 200/业务拒绝/JSON 形态的处置两链路刻意不同（见 parse_qrcode 与
    fetch_score_payload 各自注释），全部域名失败时循环自然结束。
    """
    for url in (main_url, fallback_url):
        try:
            resp = await get_client().post(f"{url}{path}", json=payload)
        except httpx.RequestError:
            continue
        yield resp


async def parse_qrcode(qr_code: str, *, main_url: str, fallback_url: str) -> str | None:
    """调 ``/getQRInfo`` 解析二维码，成功返回华立 userID（字符串）。

    ⚠️ 本接口会**消费一枚二维码**（一次性凭证），仅供「绑定微信」链路使用；
    「导 <二维码>」不得调用本函数（否则同一枚二维码被消费后 ``/updateUser``
    必然被拒）。

    业务拒绝（errorID != 0，如已使用/已过期/格式无效）抛
    :class:`SaltApiBusinessError`——备用域名结果相同，不重试；主备域名网络/
    HTTP 均失败抛 :class:`SaltApiError`。errorID=0 但缺 userID 的异常响应仍
    返回 None（网关异常响应，不能 ``str(None)`` 绑成 "None" 字符串落库）。
    """
    payload = {"qrCode": qr_code}
    async for resp in _iter_post(
        "/getQRInfo", payload, main_url=main_url, fallback_url=fallback_url
    ):
        if resp.status_code != 200:
            continue
        try:
            data = resp.json()
        except ValueError:
            # 200+HTML（网关劫持页/反代错误页）：按该域名失败继续试备域
            # （与非 200 同语义），不能让 JSONDecodeError 逃出 SaltApiError 语义
            continue
        if data.get("errorID", 0) == 0:
            user_id = data.get("userID")
            # errorID=0 但缺 userID（网关异常响应）：按业务失败返回 None，
            # 不能 str(None) 绑成 "None" 字符串落库
            return str(user_id) if user_id is not None else None
        # 业务拒绝：200+非 0 errorID（一次性二维码已用/过期等）。备用域名
        # 结果相同，直接抛出终止回退
        raise SaltApiBusinessError(_business_error_message(data))
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
    ⚠️ 传 ``qrcode`` 时 ``/updateUser`` 会**消费这枚二维码**（一次性凭证）；
    业务拒绝（errorID != 0，如二维码已使用/过期/账号不匹配）抛
    :class:`SaltApiBusinessError`——此前会被静默当成空成绩列表，用户只看到
    「导了 0 条」而不知失败原因。
    """
    payload: dict[str, str] = {"userId": userid, "importToken": ""}
    if qrcode:
        payload["qrCode"] = qrcode
    last_status = 0
    async for resp in _iter_post(
        "/updateUser", payload, main_url=main_url, fallback_url=fallback_url
    ):
        if resp.status_code != 200:
            last_status = resp.status_code
            continue
        try:
            raw = resp.json()
        except ValueError as e:
            # 200+HTML（网关劫持页）：立即计为失败并保持 SaltApiError 语义，
            # 不再试备域——此类页面经同一反代/出口，主备同现，逐域重试只会
            # 叠加 run_update 的退避轮次白跑
            raise SaltApiError("成绩拉取响应非 JSON（网关异常），请稍后再试") from e
        # 业务拒绝：200 + 非 0 errorID + error 文案（awmc-wahlap-api 契约）。
        # 一次性凭证下重试/回退备域都改不了结果，直接抛业务异常（run_update
        # 对 SaltApiBusinessError 不重试）
        if raw.get("errorID", 0) != 0:
            raise SaltApiBusinessError(_business_error_message(raw))
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

    - id：DX 谱（id>DX_ID_OFFSET）折回曲目 id；宴明细（>UTAGE_ID_BASE）
      保留 6 位机台内部 id；
    - level_index：宴明细的 level 值为 10，超常规枚举范围一律取 LevelIndex(0)
      （maimai-py 约定宴谱 level_index 恒为 0）；
    - fc：SaltNet 状态码 1-4 按 ``4 - n`` 映射 FCType；101.0000% 视为 APP；
    - fs：状态码对 5 取模落入 FSType 枚举；
    - play_count：机台协议 UserMusicDetail.playCount——SaltNet 仅在扫码
      （全量）拉取时透传真值，简略拉取恒为 null → None（2026-09-28 实测）。
    """
    song_id = int(raw["musicId"])
    achievement = int(raw["achievement"]) / 10000
    level_value = int(raw["level"])
    combo = int(raw["comboStatus"])
    sync = int(raw["syncStatus"])
    raw_pc = raw.get("playCount")
    return Score(
        id=song_id if song_id > UTAGE_ID_BASE else song_id % DX_ID_OFFSET,
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
        play_count=int(raw_pc) if raw_pc is not None else None,
        play_time=None,
        rate=RateType._from_achievement(achievement),
        type=SongType._from_id(song_id),
    )
