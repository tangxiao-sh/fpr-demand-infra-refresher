# Accessor CLI

Accessor keeps selected development AWS roles and project service credentials
ready. It reuses each project's existing `establish_proxy_connection.py` rather
than reimplementing AWS role assumption or sshuttle setup.

One Accessor process can refresh credentials for multiple projects. It starts at
most one sshuttle connection: project proxy scripts install overlapping routes,
so multiple simultaneous tunnels would conflict. Credentials refresh on their
own schedule; sshuttle is only checked every five minutes and restarted if dead.

## Commands

```bash
cd /Users/tang.xiao/IdeaProjects/tool/Accessor
./accessor
```

打开常驻控制台，显示 Demand Proxy、权限和各项目凭证的状态：

- `1`：检查状态；发现失效或未开启时会询问是否立即开启/刷新。
- `2`：选择项目并开启或手动刷新。
- `3`：停止所有自动刷新与共享 Proxy。

```bash
./accessor projects
```

列出可选项目。

```bash
./accessor run --project fprpapi
```

Keep the two shared roles refreshed, start the `fprpapi` tunnel, and refresh its
service credentials every 45 minutes without restarting sshuttle.

```bash
./accessor run -p fprpapi -p fprcinv --proxy fprpapi
```

刷新两个项目的凭证，同时通过共享 Demand Proxy 建立 sshuttle。`fprpapi`
在这里仅作为内部 connector，用来定位共享 proxy，并不表示它拥有 proxy 或会被刷新。

```bash
./accessor run --all-projects --no-proxy
```

Refresh every configured project without creating a tunnel. This is useful when
you only need build/writelock credentials or already have a tunnel running.

```bash
./accessor refresh --project fprdapi
./accessor check --all-projects
./accessor run --dry-run --project fprpapi
```

`refresh` performs one role and service-credential refresh, then exits.
`check` is local-only: it verifies paths, commands, and `boto3` without calling
AWS. `--dry-run` prints the planned external commands without changing state.

## Add or switch projects

Add another `[[projects]]` block to [accessor.toml](/Users/tang.xiao/IdeaProjects/tool/Accessor/accessor.toml:31):

```toml
[[projects]]
name = "fprbpf"
description = "my local fprbpf checkout"
directory = "/Users/tang.xiao/IdeaProjects/fpr-fprbpf"
depends_on_role = "local-staging-jump"
```

The script and Python defaults are the usual proxy-script values, so they can be
omitted. Use `default_projects` and `default_proxy` to choose what `./accessor run`
does when no project flag is supplied.

## Permission and proxy behavior

Every 10 minutes, Accessor runs `aws sts get-caller-identity --profile ...`; this
invokes the existing Granted `credential_process`. If a role is unavailable, it
foreground refresh runs `assume --wait PROFILE` for that exact profile, then
checks it again through your interactive zsh shell, so the existing Granted
alias in `.zshenv` is used. Background refreshes remain quiet and only update menu status;
use `检查` followed by `开启 / 刷新` to approve an expired entitlement. Use
`--no-auto-request` if you want to see failures without requesting access.

The initial Granted approval and terminal sudo preparation can still require
interaction. On `Ctrl-C`, Accessor stops the project script plus its shell and
sshuttle children.

After the terminal sudo preparation finishes, the project proxy runs in its own
session and writes output to `/tmp/accessor-demand-proxy.log`; it does not take
over the interactive Accessor console. Use `tail -f /tmp/accessor-demand-proxy.log`
when you need to inspect proxy output.

Background service-credential refresh output is similarly written to
`/tmp/accessor-credential-refresh.log`, keeping the status menu usable.

Before every new proxy start, Accessor runs `sudo -v` in the terminal, then:
`dscacheutil -flushcache`, `killall -HUP mDNSResponder`, and
`pfctl -f /etc/pf.conf`. The password is entered directly in the terminal with
no echo. The following commands use `sudo -n`, so they cannot show an askpass
dialog or request the password again. Accessor also removes `SUDO_ASKPASS` for
these commands; if there is no interactive terminal, startup fails rather than
falling back to a GUI prompt. If any preparation step fails, sshuttle is not
started. Set `prepare_network_before_proxy = false` in `accessor.toml` to disable
this sequence.

## Code layout

- [cli.py](/Users/tang.xiao/IdeaProjects/tool/Accessor/cli.py): argument parsing and one-off commands.
- [scheduler.py](/Users/tang.xiao/IdeaProjects/tool/Accessor/scheduler.py): independent timed loops for roles, credentials, and liveness checks.
- [permissions.py](/Users/tang.xiao/IdeaProjects/tool/Accessor/permissions.py): Granted/AWS role requests and service credential rotation.
- [sshuttle.py](/Users/tang.xiao/IdeaProjects/tool/Accessor/sshuttle.py): start, long-interval liveness state, and cleanup of sshuttle.
- [config.py](/Users/tang.xiao/IdeaProjects/tool/Accessor/config.py): TOML loading, models, and local validation.

Python 3.11+ and `boto3` in each configured project's Python environment are
required. Accessor never prints credentials or creates another credential store;
it delegates the final write to the project's existing `write_credential`.
# Accessor

`./accessor` uses the project-local `.venv` and launches a `prompt_toolkit`
terminal UI. Its refresh worker performs AWS, credential and tunnel checks;
the UI only renders cached state and is notified through thread-safe redraws.
