"""插件配置项（.env 按 pydantic 字段名大写书写，前缀 ``AWMC_SU_``）。"""

from nonebot import get_plugin_config
from pydantic import BaseModel


class Config(BaseModel):
    # 允许「导 + 二维码」全量上传的群白名单（群号字符串）。
    # 默认空 = 全量上传仅限私聊：二维码内容等价账号凭据，群内发送有泄露风险。
    awmc_su_whitelist_groups: list[str] = []
    # SaltNet API 主/备域名（Realtvop 代理服务：解析机台二维码 + 拉取微信成绩）
    awmc_su_salt_api_url: str = "https://salt_api_main.realtvop.top"
    awmc_su_salt_api_fallback_url: str = "https://salt_api_backup.realtvop.top"
    # 传分失败最大重试次数（指数退避 0.5s 起）
    awmc_su_max_retries: int = 3


plugin_config: Config = get_plugin_config(Config)
