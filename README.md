# Accessor CLI

Accessor keeps selected development AWS roles and service credentials ready.
It contains its own AWS role-discovery, credential-rotation, Demand Proxy, and
sshuttle implementation. The business repositories that were used as reference
are not imported or executed at runtime; they can be absent from the machine.

One Accessor process can refresh credentials for multiple service targets. It
starts at most one sshuttle connection because the shared Demand Proxy routes
overlap. Credentials refresh on their own schedule; sshuttle is only checked
every five minutes and restarted if dead.

## Commands

```bash
cd Accessor
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
`check` verifies Accessor's own commands and boto3 without calling AWS.
`--dry-run` prints the planned external commands without changing state.

## Add or switch service targets

Add another `[[projects]]` block to `accessor.toml`; no local checkout path or
Proxy script is needed:

```toml
[[projects]]
name = "fprbpf"
description = "fprbpf service credentials"
service_name = "fprbpf"
ec2_cluster_tag = "fprbpf-app"
depends_on_role = "local-staging-jump"
```

`service_name` is the AWS profile written to `~/.aws/credentials`.
`ec2_cluster_tag`, `discovery_tag`, and `discovery_value` describe where the
service IAM role is found. Use `default_projects` and `default_proxy` to choose
what `./accessor run` does when no project flag is supplied.

## Permission and proxy behavior

Every 10 minutes, Accessor runs `aws sts get-caller-identity --profile ...`; this
invokes the configured Granted `credential_process`. If a role is unavailable, it
foreground refresh runs `assume --wait PROFILE` for that exact profile, then
checks it again through your interactive zsh shell, so the existing Granted
alias in `.zshenv` is used. Background refreshes remain quiet and only update menu status;
use `检查` followed by `开启 / 刷新` to approve an expired entitlement. Use
`--no-auto-request` if you want to see failures without requesting access.

The initial Granted approval and terminal sudo preparation can still require
interaction. On `Ctrl-C`, Accessor stops its sshuttle process and children.

After the terminal sudo preparation finishes, Accessor resolves the shared
Demand Proxy instance from the configured SSM parameter and starts sshuttle in
its own session. Output is written to `/tmp/accessor-demand-proxy.log`; it does
not take over the interactive Accessor console. Use `tail -f
/tmp/accessor-demand-proxy.log` when you need to inspect proxy output.

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

- [cli.py](cli.py): argument parsing and one-off commands.
- [scheduler.py](scheduler.py): independent timed loops for roles, credentials, and liveness checks.
- [permissions.py](permissions.py): Granted/AWS role requests and service credential rotation.
- [credentials.py](credentials.py): standalone service-role discovery and credential writing.
- [sshuttle.py](sshuttle.py): start, long-interval liveness state, and cleanup of sshuttle.
- [config.py](config.py): TOML loading, models, and local validation.

Python 3.11+ and `boto3` in Accessor's own environment are required. Accessor
never prints credentials or creates another credential store; it writes the
temporary service profiles to the standard `~/.aws/credentials` file.
# Accessor

`./accessor` uses the project-local `.venv` and launches a `prompt_toolkit`
terminal UI. Its refresh worker performs AWS, credential and tunnel checks;
the UI only renders cached state and is notified through thread-safe redraws.
