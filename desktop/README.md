# 领+ 桌面客户端（试用版）

双击启动程序，先在原生窗口输入登录码；验证成功后才会打开浏览器后台。电脑负责手机配对与登录态提取，用户确认绑定后，云端负责每日任务。原有命令行脚本保持独立。

## 使用

1. 从 [GitHub Releases](https://github.com/shovelshit/lynkco-plus-desktop/releases) 下载对应平台的 `LynkCoHelper-cloud-*.zip`。Windows 解压后运行 `LynkCoHelper/LynkCoHelper.exe`；macOS 解压后打开 `LynkCoHelper.app`。应用自带 Python 运行时和全部客户端资源，不需要额外启动器或 Python。
2. 首次使用在原生窗口粘贴管理员发来的邀请码，领取登录码并保存。重置登录码时，新码只显示一次，确认保存后旧码立即失效。
3. 电脑、手机连接同一局域网。在「绑定账号」选择手机系统和电脑网络。
4. 手机扫码配对，下载名为 `LynkCoHelper-CA.cer` 的本机证书并安装。iPhone 安装描述文件后，还要在「关于本机 → 证书信任设置」开启完全信任。
5. 按页面显示的服务器和端口设置手机 Wi-Fi 手动代理，然后打开领克 App。电脑换 Wi-Fi 后需要重新配对，旧二维码不会继续使用。
6. 页面依次显示「登录态」「设备信息」「分享信息」。打开 App 后未识别到完整资料时，请退出账号后使用手机验证码重新登录；缺少分享信息时，在 App 首页打开一篇文章，分享一次。三项资料齐全后，在 30 分钟内点击「确认保存」，助手才会上传并验证、保存云端绑定；补抓信息不会延长期限。
7. 关闭手机 Wi-Fi 代理，移除本次证书，再在助手中确认断开连接。电脑此后可以关机。

关闭浏览器不会退出后台；重新双击 App 不会绕过原生登录，也不会复用旧 bearer。关闭原生窗口会撤销浏览器会话、停止电脑代理；手机端 Wi-Fi 代理开关仍需用户自行关闭。闲置 30 分钟后自动锁定，旧页面请求失效，重新输入登录码后才会发放新的浏览器会话。

认证窗口关闭后客户端进程随之停止。macOS 是可双击的 `.app`，Windows 是窗口模式 `.exe`；从 SSH 或无图形环境直接运行时才回退到终端输出。

发布包同时提供 `.sha256` 文件供人工校验。当前包不再拆分资源下载器，首次运行不依赖 GitHub 资源下载，也不会创建临时解压目录。未签名 App 本身仍可能被替换，此校验文件不等同于发布者数字签名。macOS 首次运行可能受系统安全策略限制。

每日分享默认关闭，可在保存时自行开启。绑定需要完整的本机设备信息和分享请求资料，不要求额外获取 IMEI。不同安卓 App/系统版本是否接受用户证书仍需真机验证。

## 数据

登录码不会写入日志或实例记录。本地 License 使用登录码与本机标识派生密钥加密保存管理令牌；复制到另一台机器无法解密。Cloud 只保存登录码和管理令牌的哈希，重置时同时轮换两者。首次登录和每次解锁都向 Cloud 验证；登录码错误或已重置时不会打开浏览器。

手机 access token 仅在本地内存及浏览器 sessionStorage 中短暂保存，不显示在界面、不写抓包日志、不上传云端。云端使用 AES-GCM 加密保存 refreshToken、设备信息和分享所需快照，不保存 access token 或其有效期；任务执行时重新续期。分享快照来自手机实际请求，后续重复使用，重新抓包绑定时更新。每台电脑独立生成 CA，手机只下载公开证书，私钥不打包、不上传。解除绑定删除在线登录态与运行记录；云平台历史备份依其保留政策到期删除。

## 开发与打包

普通用户无需 Python/Node；以下命令供开发者使用，Python 3.12、Node.js 22。打包使用 PyInstaller 的单个用户可见 `--windowed --onedir` 应用，原生登录窗口由 CustomTkinter 实现，登录后在系统浏览器中打开后台；手机代理由同一应用包内隐藏的 mitmproxy 子进程负责，用户无需单独启动它。源码启动与打包入口使用同一套桌面业务代码。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r desktop/requirements.lock
.venv/bin/python -m unittest discover -s desktop/tests -v
.venv/bin/python -m desktop.launcher
.venv/bin/python desktop/packaging/build.py
.venv/bin/python desktop/packaging/build_release.py --tag cloud-v0.1.0 --platform macos-arm64
.venv/bin/python desktop/tests/packaged_smoke.py dist/LynkCoHelper.app/Contents/MacOS/LynkCoHelper
```

本地 Worker 运行在 `http://127.0.0.1:8787` 时，可以一次同时构建正式包和独立的本地包：

```sh
.venv/bin/python desktop/packaging/build.py --with-local-worker
```

产物包括正式包 `dist/LynkCoHelper.app` 和本地包 `dist/local-worker/LynkCoHelper-dev.app`；Windows 正式包是 `dist/LynkCoHelper/LynkCoHelper.exe`。本地包内置回环地址、Tkinter 登录窗口及匹配的完整性清单；两套构建使用独立工作目录和状态目录。若正式客户端也在运行，请用 `dist/local-worker/LynkCoHelper-dev.app/Contents/MacOS/LynkCoHelper-dev --state-dir <独立目录>` 启动本地包，避免复用正式客户端的单实例状态。需要只构建本地包时，仍可使用 `--cloud-url http://127.0.0.1:8787`。

Windows 使用 `.venv\Scripts\python.exe`。推送到 `main` 的客户端代码或构建流程改动会自动运行测试和 Windows x64、macOS ARM64 / Intel 打包检查。

### 本地分享字段省略实验

在源码客户端中完成三项采集后，先不要确认保存（保存后本地捕获资料会清理），运行：

```sh
.venv/bin/python -m desktop.share_probe
```

工具通过受保护的本机接口读取内存中的同一账号抓包，使用 `LynkCoHelper/env.json` 的 `secrets.nativeAppKey/nativeAppSecret` 签名。可用 `--instance-file` 指定另一个本地实例记录、`--config` 指定应用配置文件；不要把账号凭据作为命令行参数。无需启动 Worker。

实验仅调用获取分享码接口，先验证完整快照，再尝试省略风控快照或电量、设备环境、网络、定位等字段。裁剪失败时复测完整基准；凭证失效、限流、网络故障均记为无法判断。标准输出只包含字段名、结果类别、平台和版本，不包含凭证、设备标识、网络值、文章 ID 或分享码。缺少新鲜成功抓包时返回 `capture_required`，不会用固定模拟器数据代替基准。

`accepted_for_code_only` 只说明取码接受该请求，不证明分享加分不依赖被省略字段。报告不会自动修改采集策略或删除快照字段；尚未充分验证的字段保留实际抓包值。工具不执行签到或分享上报，也不向云端上传资料。

正式发布时，手动触发 `Build desktop assistant` 并填写新版本号，例如 `v0.1.9` 或 `cloud-v0.1.9`（统一发布为 `cloud-v0.1.9`）；也可以推送新的 `cloud-v*` 标签触发发布。三个平台全部构建成功后发布 GitHub Release，包含每个平台的单一 App 压缩包和校验文件；Actions artifact 只是发布过程的中间产物。已有版本禁止覆盖，更新需发布新版本 App。日常任务不依赖 GitHub Actions。当前发行包没有付费开发者签名/公证，首次分发可能遇到系统安全确认，不应关闭系统安全功能。

`desktop/service.json` 仅包含公开服务地址。云端 Worker、管理后台及数据库结构独立维护在私有仓库 `shovelshit/LynkCoHelper-Cloud`，客户端构建不需要访问该仓库。发布包在启动本地服务或代理前校验网页、服务地址和代理插件的 SHA-256 清单，清单摘要编译进程序，发现缺失或被修改时拒绝启动。源码运行不执行发布校验，发布版没有环境变量跳过入口。此机制不保护可执行文件本身，也不能阻止修改程序以绕过检查，不等同于发布者数字签名。`desktop/tests/ui_harness.py` 只使用测试账号和积分，绝不打包进应用；浏览器测试通过不代表真实账号签到成功。
Windows App 在创建窗口前启用 Per-Monitor DPI awareness，并在打包文件中附带 DPI manifest，避免系统位图缩放造成文字模糊。网页中的本机状态校验使用临时 Bearer token，云端只保存哈希和加密登录态；本地调试与发布包的完整性校验边界不同，校验失败应重新获取可信发布包。

## 验收边界

已进行桌面单元/HTTP 集成测试、真实代理及证书测试、云端真实 D1 集成测试、桌面与 390px 浏览器布局测试、当前 macOS ARM64 打包测试。

概览内可设置两小时执行区间，并从 Bark、Server 酱中选择一个结果推送渠道。每区间默认 10 个名额，显示剩余数量，满额不可新选；暂停保留名额，解绑释放。后端并发校验防止超配。密钥加密保存在云端，不回显；运行记录显示推送状态。账号名称沿用领克个人信息，不提供名称输入框。

“签到时同时完成分享”也适用于立即执行。领克当天已签到时跳过签到写入，仍可继续待完成的分享；助手当天任务已结束则不重复执行。登录码用于恢复助手管理权限，与领克登录态续期无关。

尚需 Windows/Intel 实机、免费 CPU/子请求测量及 7 天运行观察。推送联调需要用户自己的 Device Key 或 SendKey。通过这些验收前，不宣称所有用户均可零成本即用。
