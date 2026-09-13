# 运行前提

这个 skill 默认假设当前环境已经具备：

- 已安装 `social-auto-upload`
- 可以调用 `sau`，或者至少可以执行 `python sau_cli.py`
- 当前机器首次运行时可以联网访问 GitHub Release

## 关键说明

用户不需要自己安装 `biliup`。

当你执行 `sau bilibili ...` 时：

- 如果本地没有 `biliup`，程序会自动下载
- 下载版本、平台资产、字节数和 SHA-256 由 `uploader/bilibili_uploader/biliup-lock.json` 固定
- 已安装版本只在二进制和 install record 校验一致时复用；运行时不会查询 GitHub latest

## 推荐调用方式

### `sau` 已在 PATH 中

```bash
sau bilibili --help
```

### 仓库内直接调用

PowerShell：

```powershell
.\.venv\Scripts\python.exe sau_cli.py bilibili --help
```

bash / zsh：

```bash
python sau_cli.py bilibili --help
```

## 首次运行注意事项

- 首次运行可能比其他平台更慢，因为要自动准备 `biliup`
- 如果网络无法访问 GitHub Release，Bilibili 命令会失败
- 一旦本地已经准备好 `biliup`，后续命令会直接复用
- `sau bilibili login --account <name>` 需要用户自己在本地真实终端里执行
- 如果终端二维码显示不完整，通常可以直接打开当前目录下的 `qrcode.png` 扫码
- 下载只接受 lock 中的官方 GitHub HTTPS URL及受限 GitHub release asset 跳转；不要改写到代理域名
