# Accessor

[中文版](README-zh.md)

Accessor keeps development AWS roles, selected service credentials, and the
shared Demand Proxy available in one terminal application. It is self-contained:
it does not import a business repository or run a repository-specific proxy
script.

## 1. Initialize

Accessor currently supports macOS and expects Homebrew to be installed. From
the cloned repository, run:

```bash
./bootstrap.sh
```

The script installs missing command dependencies (`aws`, Granted's `assume`,
`sshuttle`, and `curl`), then creates or updates the project-local `.venv` and
installs its Python dependencies. It is safe to rerun.

It deliberately does **not** edit `~/.aws`, run `aws configure`, sign you into
Granted, or start a proxy. Before using Accessor, configure your own AWS and
Granted access and confirm that this works:

```bash
assume --help
```

## 2. Start Accessor

Start the console:

```bash
./accessor
```

Accessor immediately selects **all configured projects** and starts the normal
Start / refresh flow. No menu input is needed. If Granted or `sudo` requires a
password, the console switches to a secure hidden-input prompt in this window.

The default interface language is Chinese. Use English when needed:

```bash
./accessor --language en
```

Use `./accessor --language zh` to switch back. UI copy is maintained in
[locales/en.json](locales/en.json) and [locales/zh.json](locales/zh.json).

### Console actions

- `1` — Check roles, selected project credentials, and Proxy health without
  changing credentials or starting a proxy. If a problem is found, Accessor
  offers to start or refresh it.
- `2` — Run Start / refresh again. Enter project numbers such as `1,3`; an
  empty value selects every configured project.
- `3` — Stop the automatic refresh job and the Accessor-managed Demand Proxy.
  Existing AWS credentials are left on disk.
- `q` — Exit the console. This also stops the active refresh job.

### What “Start / refresh” does

1. **AWS roles** — Accessor checks the configured roles first. When a role is
   unavailable, it invokes the configured Granted command for that exact
   profile (`assume --wait --export PROFILE`) and verifies the resulting AWS
   profile. The build role, its legacy `beiartf` Gradle profile, and the local
   staging jump role are handled independently.
2. **Demand Proxy** — Accessor resolves the shared proxy host from the SSM
   mapping in `accessor.toml`, then starts one `sshuttle` tunnel for all selected
   services. Before a new tunnel it asks for the terminal `sudo` password and
   runs the required DNS/PF preparation commands. The password is not echoed.
3. **Project credentials** — Each selected service credential profile is
   refreshed independently. Their normal cadence is 45 minutes; a failed
   refresh uses the configured retry interval. Updating credentials does not
   restart a healthy Proxy.
4. **Ongoing health** — Roles are checked every 10 minutes. The Proxy is health
   checked every five minutes using the configured private endpoints. If an
   Accessor-managed tunnel has failed every probe, Accessor restarts it; an
   unhealthy external tunnel is taken over before replacement.

The console shows cached status and recent activity while this work runs in the
background. It does not perform AWS or network calls merely to redraw itself.

### Logs and common follow-up

- Proxy output: `/tmp/accessor-demand-proxy.log`
- Role request output: `/tmp/accessor-role-refresh.log`
- Service credential refresh output: `/tmp/accessor-credential-refresh.log`
- Status activity: `/tmp/accessor-activity.log`

Long-lived Gradle daemons can retain an expired AWS session. After a successful
build-role refresh, run `./gradlew --stop` once, then start the build again.

## 3. Other useful commands

### List configured projects

```bash
./accessor projects
```

### Scripted, non-interactive operation

```bash
./accessor run --project fprpapi
./accessor run -p fprpapi -p fprcinv --proxy fprpapi
./accessor run --all-projects --no-proxy
```

`run` keeps the selected credentials refreshed and optionally starts the shared
Proxy. The `--proxy` value only chooses the configured connector used to resolve
the shared proxy; it does not make that project the proxy owner. `--no-proxy`
refreshes credentials only.

### One-off operations and validation

```bash
./accessor refresh --project fprdapi
./accessor refresh-project --project fprdapi
./accessor check --all-projects
./accessor run --dry-run --project fprpapi
```

- `refresh` refreshes roles and the selected project credentials once, then
  exits.
- `refresh-project` refreshes exactly one service credential profile.
- `check` validates configuration and local Python dependencies without calling
  AWS.
- `--dry-run` prints the planned work without making changes.

### Add a service target

Add a `[[projects]]` table to [accessor.toml](accessor.toml). No checkout path
or project proxy script is required.

```toml
[[projects]]
name = "fprbpf"
description = "fprbpf service credentials"
service_name = "fprbpf"
ec2_cluster_tag = "fprbpf-app"
depends_on_role = "local-staging-jump"
```

`service_name` is the profile Accessor writes to `~/.aws/credentials`.
Discovery fields identify the service role; `default_projects` and
`default_proxy` determine the default console selection and proxy connector.

### Implementation layout

- [cli.py](cli.py) — command parsing and non-interactive commands
- [console.py](console.py) — terminal UI and status rendering
- [scheduler.py](scheduler.py) — independent role, credential, and Proxy clocks
- [permissions.py](permissions.py) — Granted and AWS role handling
- [credentials.py](credentials.py) — service-role discovery and credential writing
- [sshuttle.py](sshuttle.py) — Proxy lifecycle and macOS network preparation
