# Bilibili 常见问题

## 1. 首次运行很慢

这是正常情况。

原因通常是程序正在自动下载 `biliup`。

## 2. 自动下载失败

先检查：

- 当前网络是否能访问 GitHub
- GitHub Release 是否可访问
- 本地目录是否有写权限
- `biliup-lock.json` 中当前平台的 URL、size 和 SHA-256 是否来自同一已审查 release
- 不要用代理域名改写下载 URL；下载器会拒绝越出获准 GitHub release 资产域

## 3. `check` 返回 `invalid`

常见原因：

- 账号文件不存在
- 登录信息已失效
- `biliup renew` 失败

建议：

```bash
sau bilibili login --account <account>
```

## 4. 登录时报 `not a terminal`

常见原因：

- 你是在非交互环境里触发了 `sau bilibili login`
- 例如 agent 的命令执行器、管道环境、被接管标准输出的进程

建议：

- 改成由用户自己在本地真实终端里执行：

```bash
sau bilibili login --account <account>
```

- 如果终端里的二维码显示不完整，直接打开当前目录下的 `qrcode.png` 扫码

## 5. 上传失败

优先检查：

- `--tid` 是否正确
- 视频文件是否真实存在
- 标题、简介、标签是否符合平台要求
- 当前登录信息是否仍然有效

## 6. 上游更新后行为变化

当前 Bilibili 集成不会自动跟随上游 latest。升级由仓库 lock 明确控制。

排障时请同时确认：

- install record 中的版本、平台、资产和二进制 SHA-256
- `biliup-lock.json` 是否经过完整升级流程，而不是只替换其中一个 URL 或哈希
