# 领+桌面客户端

领+桌面客户端负责手机抓包、登录态绑定和本地运行控制；每日任务由独立的 Cloudflare Worker 执行。

## 下载

从 [GitHub Releases](https://github.com/shovelshit/lynkco-plus-desktop/releases) 下载对应平台的 `LynkCoHelper-cloud-*.zip`。应用包名和可执行文件仍为 `LynkCoHelper`，这样不会影响已有安装目录、状态目录或自动更新识别。

## 开发

客户端源码位于 [`desktop/`](desktop/)，开发和打包说明见 [`desktop/README.md`](desktop/README.md)。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r desktop/requirements.lock
.venv/bin/python -m unittest discover -s desktop/tests -v
.venv/bin/python desktop/packaging/build.py
```

云端 Worker 和管理后台位于独立仓库 [`LynkCoHelper-Cloud`](https://github.com/shovelshit/LynkCoHelper-Cloud)。
