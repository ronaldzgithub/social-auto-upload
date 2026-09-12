# Foundry—Huaxiaobao 接入说明

## 基线与边界

- 上游：`dreammis/social-auto-upload`
- 基线提交：`0012d2c355f88f683cc38dde2a2db209e14091bc`
- 改造分支：`codex/foundry-huaxiaobao-integration`
- 项目版本：`0.1.0`
- 许可证：MIT。升级和分发时保留许可证与版权声明。

Huaxiaobao 是各平台自动化能力的唯一执行所有者，分别管理平台连接、账号状态、登录/HITL、能力审核、执行权限和回执。Foundry 不直接运行平行浏览器执行器，不保存账号文件，也不把一个平台的成功扩展为其他平台可用。

Foundry 拥有内容计划、销售目标、客户/商机语义、预算、外部动作批准、履约验收和会计裁决。上传成功或平台排程成功不是客户验收、收入或净值证据。

## 平台能力与副作用

当前主线入口是 `sau` CLI；历史 Web 端和 `examples/` 不作为稳定集成合同。

| 平台 | 当前 CLI 能力 | 主要人工依赖 | 外部动作 |
| --- | --- | --- | --- |
| 抖音 | 登录、检查、视频、图文、定时 | 扫码、短信/安全验证 | 发布、定时发布、获取验证码 |
| 快手 | 登录、检查、视频、图文、定时 | 扫码及页面验证 | 发布、定时发布 |
| 小红书 | 登录、检查、视频、图文、定时 | 扫码 | 发布、定时发布 |
| Bilibili | 登录、检查、视频、定时 | 交互终端扫码 | 发布、定时发布；运行时下载 `biliup` |
| 视频号 | 登录、检查、视频、定时、草稿 | 微信扫码、管理员实名验证 | 发表、定时发表或保存草稿 |
| 百家号、支付宝生活号、微博、虎扑 | 登录、检查、视频 | 对应平台登录/验证 | 发布 |
| YouTube | 交互登录、检查、视频 | Google 浏览器登录 | 发布并设置可见性/播放列表 |
| TikTok | 历史示例 | 登录 | 未纳入统一 CLI 合同 |

上述能力必须按平台、账号和动作分别注册。小红书默认由 `xiaohongshu-mcp` 执行；本仓库的小红书上传只有在主执行器不可用且显式选择 fallback 时才运行，禁止双执行。

## 安全 adapter surface 与外发门禁

安装后可运行 `sau-huaxiaobao describe`；`sau-huaxiaobao execute` 从标准输入读取 `foundry.huaxiaobao.tool-request.v1` JSON。当前按十个平台分别开放 `account.status`，复用对应原生 `check_*_account`，返回请求哈希、平台级稳定账号对象引用和 `READY`/`BLOCKED`/`UNKNOWN` typed result。账号对象引用故意不含 executor 名，以便小红书主执行器和 fallback 锁定同一对象。小红书描述固定标为 fallback，并指向主执行器 `xiaohongshu-mcp`；调用方必须以同一稳定 operation/账号引用落实互斥选择。

安全 adapter 不提供发布能力，也不从请求接收 Cookie、Token、密码或会话材料。旧 `sau` 的十三个上传 wrapper 新增 `SAU_ENABLE_EXTERNAL_ACTIONS` fail-closed 门禁：默认、空值和未知值在账号/浏览器操作前拒绝；仅 `1`、`true`、`yes`、`on` 显式开启。该兼容开关不构成 Foundry 批准，也不能绕过 Huaxiaobao 的能力审核和执行门。

## 账号与人工入口

账号状态文件位于运行目录的 `cookies/`，只能存放在 Huaxiaobao 管理的执行边界。Foundry 任务只保存账号不透明引用。

扫码、短信验证、管理员实名验证或交互登录应转成持久化人工依赖，说明阻塞步骤、所需身份、受限入口、完成条件、期限和失败处理：

- 账号所有者完成本人/组织账号登录和扫码；
- Huaxiaobao/工具管理员完成连接配置、能力激活和管理员实名验证；
- 普通工作人员不得获得账号所有权、管理员权或批准权；
- 完成后由对应平台 `check` 或更强的账号/租户检查重新验证，不能只信人工点击“完成”。

二维码、短信验证码、Cookie、Token 和可复用会话链接不得写入任务正文、普通日志、提交或 Foundry ledger。根目录临时 `verify_code.txt` 和 `qrcode.png` 已加入 Git 忽略；集成适配器还应使用权限受限的临时存储并在完成、取消、失败或过期后清理。

## 执行、ACK 与恢复

当前 CLI 成功只表示 uploader 返回，尚不构成 Foundry typed outcome。Huaxiaobao adapter 需要补充：

- 版本化 capability request/result、请求哈希和幂等键；
- 执行时重新校验批准仍有效；
- 平台对象 ID、账号/租户指纹、排程时间和可复查状态；
- `BLOCKED`/`UNKNOWN` 时停止重复点击和预算消耗；
- 人工结果经工具复查后回传，原组件 ACK 消费，再从原检查点恢复；
- 重复、迟到、过期和任务版本不匹配的结果失败关闭。

部分 uploader 目前存在无界发布重试。接入生产前必须由 adapter 隔离并改为有界状态机；结果未知时先查询，不能重新发布。

## 部署与升级

部署由 VolvenceDeploy 按平台隔离运行，持久化账号状态和临时验证材料，限制文件权限与网络边界。不得自动重启、迁移或替换正在运行的 Foundry、Huaxiaobao 或其他生产实例。

升级时记录 upstream commit、Fork commit、Python/Patchright/浏览器版本；Bilibili 的 `biliup` 还需记录 release 版本、资产哈希和许可证。先在隔离环境执行 CLI、单元测试、浏览器 fixture 和无外发验证，再通过独立发布门。

## 当前验收状态

- 源码基线：已记录。
- 本地运行：未就绪；当前缺少 `conf.py`、虚拟环境和账号状态。
- 测试：安全 adapter/门禁针对性测试已离线通过；完整 `pytest` 仍在收集阶段因缺少 `conf.py` 失败，尚无全量通过证据。
- Foundry—Huaxiaobao 合同：已增加按平台隔离的 `account.status` 最小 adapter；发布/排程执行与持久恢复合同仍未开放。
- 真实账号及获批外部动作：均未验证。
- 人工 ACK、重启恢复、重复结果、权限撤销与下游自动恢复：无真实证据。

所有 fixture 和 DOM 模拟只能标记为 `simulation`，不得声称真实发布、获客或收入。
