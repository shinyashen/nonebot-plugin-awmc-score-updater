# nonebot-plugin-awmc-score-updater

[![python3](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://opensource.org/licenses/MIT)

[nonebot-plugin-awmc-helper](https://github.com/shinyashen/nonebot-plugin-awmc-helper) 的第三方扩展插件：在 QQ 聊天环境绑定机台二维码后，一句话把国服 maimaiDX 成绩上传到[水鱼](https://www.diving-fish.com/maimaidx/prober/)与[落雪](https://maimai.lxns.net/)成绩数据库。移植自同作者的 HoshinoBot 插件 [maimai-score-updater](https://github.com/shinyashen/maimai-score-updater)，数据层基于 [maimai.py](https://github.com/TrueRou/maimai.py)。

## 特性

- **绑定零重复**：水鱼/落雪凭据直接复用 awmc-helper 主插件的绑定数据——在主插件完成「绑定水鱼token」「绑定落雪」即可，本插件不再存一份 token；
- **机台二维码绑定**：微信公众号二维码（`SGWCMAID...` 或页面链接）经 SaltNet 服务解析出机台 userID，仅私聊绑定；
- **增量/全量二态上传**：不带二维码为简略上传（仅上传有变化的达成率与 DX 分），带二维码为全量上传；多目标（水鱼+落雪）并行；
- **群白名单**：带二维码的全量上传默认仅限私聊，可配置群白名单放开（二维码等价账号凭据，默认不在群内使用）。

## 要求

- NoneBot2 ≥ 2.4.3，Python ≥ 3.10
- [nonebot-plugin-awmc-helper](https://github.com/shinyashen/nonebot-plugin-awmc-helper)（主插件，需已部署并完成水鱼/落雪绑定）

## 安装

**方式一（主推）：awmc_plugins/ 目录即装**

主插件支持 `awmc_plugins/` 固定目录的「clone 即安装」：

```bash
cd <bot 工作目录>
git clone https://github.com/shinyashen/nonebot-plugin-awmc-score-updater.git awmc_plugins/nonebot-plugin-awmc-score-updater
```

重启 bot 即自动加载（目录名加 `_` 前缀或写入 `awmc_disabled_plugins` 可停用）。

**方式二：pip / PyPI**

```bash
pip install nonebot-plugin-awmc-score-updater
```

随后将 `"nonebot_plugin_awmc_score_updater"` 加入 bot 的加载列表。

## 配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `awmc_su_whitelist_groups` | `[]` | 允许「导 + 二维码」全量上传的群白名单；**默认空 = 仅私聊** |
| `awmc_su_salt_api_url` | `https://salt_api_main.realtvop.top` | SaltNet API 主域名 |
| `awmc_su_salt_api_fallback_url` | `https://salt_api_backup.realtvop.top` | SaltNet API 备用域名 |
| `awmc_su_max_retries` | `3` | 传分失败重试次数（指数退避） |

```dotenv
AWMC_SU_WHITELIST_GROUPS='["123456789"]'
```

## 指令

| 指令 | 说明 |
|---|---|
| `绑定微信` / `bindwx` / `微信绑定` `<二维码内容\|链接>` | 绑定机台微信二维码（**仅私聊**） |
| `导` / `传分` / `上传分数` / `wmupdate` | 简略上传（仅达成率与 DX 分的增量） |
| `导 <二维码内容\|链接>` | 全量上传（仅私聊或白名单群；校验与已绑机台账号一致） |
| `导帮助` / `传分帮助` / `上传分数帮助` | 帮助 |

「导」字开头的指令带原版彩蛋文案。上传成功后记录最近上传时间并在下次提示。

## 开源许可

[MIT](LICENSE)。鸣谢 [maimai.py](https://github.com/TrueRou/maimai.py)、[nonebot-plugin-awmc-helper](https://github.com/shinyashen/nonebot-plugin-awmc-helper) 与本插件的前身 [maimai-score-updater](https://github.com/shinyashen/maimai-score-updater)。
