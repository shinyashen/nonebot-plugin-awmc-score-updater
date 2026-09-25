"""nonebot-plugin-awmc-score-updater：awmc-helper 第三方扩展。

将 HoshinoBot 版 maimai-score-updater（同作者）的「机台成绩一键导出到
水鱼/落雪」能力移植到 NoneBot2 + awmc-helper 生态：

- 水鱼/落雪凭据**直接复用**主插件 ``user_binding``（在主插件完成
  ``绑定水鱼token`` / ``绑定落雪`` 即可，本插件不重复存储凭据）；
- 机台微信二维码绑定（SaltNet 解析）为本插件自有数据，存独立 SQLite；
- 「导 + 二维码」全量上传受群白名单 ``awmc_su_whitelist_groups`` 控制，
  默认空 = 仅私聊（二维码等价账号凭据，防群内泄露）。

与「主插件仓内嵌套子插件」的差异点（同官方示例插件约定）：
1. 先 require() 主插件，再 import（官方《跨插件访问》规范）；
2. 以绝对模块路径导入 core 公开接口；
3. PluginMetadata 用第三方自有命名，不声明 supported_adapters。

加载方式：放入 bot 工作目录 ``awmc_plugins/``（主插件自动加载，主推），
或 bot 顶层 [tool.nonebot] plugin_dirs / pip 安装后写入加载列表。
"""

__version__ = "0.1.0"

# require 必须先于一切对主插件的 import
from nonebot import require

require("nonebot_plugin_awmc_helper")

from nonebot import get_driver
from nonebot.plugin import PluginMetadata

from .store import init_store
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="awmc-score-updater",
    description="awmc-helper 第三方扩展：绑定机台二维码，将国服成绩一键导出到水鱼/落雪",
    usage=(
        "绑定微信 <二维码内容|链接>（仅私聊）绑定机台账号；"
        "导/传分/上传分数 [二维码内容] 上传成绩（全量上传仅私聊或白名单群）。"
        "水鱼/落雪 token 请先用 awmc-helper 主插件绑定。"
    ),
    type="application",
    homepage="https://github.com/shinyashen/nonebot-plugin-awmc-score-updater",
    config=Config,
)

# matcher 装配放元数据之后（其内部 require 主插件 client 单例）
from . import matchers  # noqa: F401

get_driver().on_startup(init_store)
