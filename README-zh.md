# Accessor

[English](README.md)

Accessor 是一个常驻终端工具：统一维护开发 AWS 角色、所选项目的服务凭证，以及共享
Demand Proxy。它不依赖任何业务项目的本地目录，也不会执行业务项目中的 proxy 脚本。

## 1. 初始化

Accessor 目前支持 macOS，并要求电脑已安装 Homebrew。克隆仓库后执行：

```bash
./bootstrap.sh
```

脚本会在缺失时安装 `aws`、Granted 提供的 `assume`、`sshuttle` 和 `curl`，然后创建或
更新项目内的 `.venv` 并安装 Python 依赖。可以重复执行。

它不会修改 `~/.aws`、不会执行 `aws configure`、不会替你登录 Granted，也不会启动
Proxy。请先配置好自己的 AWS 和 Granted，并确认以下命令可用：

```bash
assume --help
```

## 2. 启动 Accessor

启动控制台：

```bash
./accessor
```

如果要按默认选择开启所有项目的自动刷新：进入后输入 `2` 并回车，然后在项目选择处直接
再按一次回车。第二次空输入表示选择**全部项目**。

默认界面为中文；如需英文：

```bash
./accessor --language en
```

使用 `./accessor --language zh` 可切回中文。界面文案分别维护在
[locales/zh.json](locales/zh.json) 和 [locales/en.json](locales/en.json)。

### 控制台操作

- `1`：只检查角色、所选项目凭证和 Proxy 健康状态，不会修改凭证或启动 Proxy。发现问题后会
  询问是否开启或刷新。
- `2`：开启或刷新。可输入项目编号，例如 `1,3`；直接回车表示选择全部已配置项目。
- `3`：停止自动刷新 job 和由 Accessor 管理的 Demand Proxy；已写入磁盘的 AWS 凭证不会删除。
- `q`：退出控制台，同时停止当前刷新 job。

### “开启 / 刷新”会做什么

1. **AWS 角色**：先检查配置的角色。若角色不可用，会以精确 profile 执行配置中的 Granted
   命令（`assume --wait --export PROFILE`），随后验证该 AWS profile。构建角色、Gradle 使用的
   `beiartf` 旧 profile，以及本地 staging jump role 分别独立维护。
2. **Demand Proxy**：从 `accessor.toml` 配置的 SSM mapping 中解析共享 proxy 主机，然后为所有
   已选项目建立一个 `sshuttle` 隧道。新建隧道前会在当前终端请求 `sudo` 密码，并执行 DNS/PF
   网络准备命令；密码不会回显。
3. **项目凭证**：每个所选服务的凭证独立刷新。正常刷新间隔为 45 分钟；失败后按配置的重试
   间隔执行。刷新凭证不会重启健康的 Proxy。
4. **持续检查**：角色每 10 分钟检查一次；Proxy 每 5 分钟通过配置的私有健康检查地址验证。
   如果由 Accessor 管理的隧道全部健康检查失败，会自动重启；外部启动但已失效的隧道会先被接管
   再替换。

控制台只展示缓存状态与最近活动，后台刷新期间依然可操作；界面重绘本身不会触发 AWS 或网络
调用。

### 日志与常见后续操作

- Proxy 输出：`/tmp/accessor-demand-proxy.log`
- 角色申请输出：`/tmp/accessor-role-refresh.log`
- 服务凭证刷新输出：`/tmp/accessor-credential-refresh.log`
- 状态活动：`/tmp/accessor-activity.log`

长时间运行的 Gradle daemon 可能缓存过期 AWS session。构建角色刷新成功后，执行一次
`./gradlew --stop`，再重新启动构建即可。

## 3. 其他辅助功能

### 查看已配置项目

```bash
./accessor projects
```

### 脚本化、非交互运行

```bash
./accessor run --project fprpapi
./accessor run -p fprpapi -p fprcinv --proxy fprpapi
./accessor run --all-projects --no-proxy
```

`run` 会保持所选凭证刷新，并可选地启动共享 Proxy。`--proxy` 仅指定用于解析共享 proxy 的
connector，不代表该项目拥有 Proxy。`--no-proxy` 则只刷新凭证。

### 单次操作与校验

```bash
./accessor refresh --project fprdapi
./accessor refresh-project --project fprdapi
./accessor check --all-projects
./accessor run --dry-run --project fprpapi
```

- `refresh`：刷新角色和所选项目凭证一次后退出。
- `refresh-project`：只刷新一个服务凭证 profile。
- `check`：验证配置和本地 Python 依赖，不调用 AWS。
- `--dry-run`：只输出计划执行的工作，不产生修改。

### 新增服务项目

在 [accessor.toml](accessor.toml) 新增一个 `[[projects]]` 配置块即可，不需要业务项目
目录，也不需要业务项目中的 proxy 脚本。

```toml
[[projects]]
name = "fprbpf"
description = "fprbpf service credentials"
service_name = "fprbpf"
ec2_cluster_tag = "fprbpf-app"
depends_on_role = "local-staging-jump"
```

`service_name` 是 Accessor 写入 `~/.aws/credentials` 的 profile。项目发现字段用于定位服务
角色；`default_projects` 和 `default_proxy` 决定控制台默认选择与 proxy connector。

### 代码结构

- [cli.py](cli.py)：命令行参数与非交互命令
- [console.py](console.py)：终端 UI 与状态渲染
- [scheduler.py](scheduler.py)：角色、凭证与 Proxy 的独立定时任务
- [permissions.py](permissions.py)：Granted 和 AWS 角色处理
- [credentials.py](credentials.py)：服务角色发现与凭证写入
- [sshuttle.py](sshuttle.py)：Proxy 生命周期与 macOS 网络准备
