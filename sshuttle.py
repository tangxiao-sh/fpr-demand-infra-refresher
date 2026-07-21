"""Lifecycle management for one long-lived project sshuttle process."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Sequence
import shutil
import signal
import subprocess
import json
import time

import boto3

from config import ProjectConfig, ProxyConfig
from i18n import t


LOG = logging.getLogger("accessor.sshuttle")

# These commands are intentionally fixed in code rather than read from a shell
# string. That avoids command injection and makes their privileged scope clear.
NETWORK_PREP_COMMANDS = (
    ("dscacheutil", "-flushcache"),
    ("killall", "-HUP", "mDNSResponder"),
    ("pfctl", "-f", "/etc/pf.conf"),
)
LAST_NETWORK_PREP_ERROR: str | None = None
LAST_PROXY_START_ERROR: str | None = None
SUDO_AUTH_ATTEMPTS = 5  # First entry plus four retries.


def network_prepare_error() -> str | None:
    """Return the last human-readable sudo/network preparation failure."""
    return LAST_NETWORK_PREP_ERROR


def proxy_start_error() -> str | None:
    """Return the last proxy discovery/start failure without exposing secrets."""
    return LAST_PROXY_START_ERROR


def prepare_network_before_proxy(
    password_provider: Callable[[], str] | None = None, allow_prompt: bool = True
) -> bool:
    """Run required macOS network setup, optionally without a sudo prompt.

    The foreground UI supplies a hidden password when needed. Background
    recovery uses ``allow_prompt=False``: every sudo command is noninteractive,
    so an expired sudo cache is reported instead of stealing terminal input.
    """
    global LAST_NETWORK_PREP_ERROR
    LAST_NETWORK_PREP_ERROR = None
    LOG.info("Network preparation requires sudo")
    # A user may have configured SUDO_ASKPASS globally. Remove it so a missing
    # terminal fails safely instead of falling back to a graphical password UI.
    terminal_env = os.environ.copy()
    terminal_env.pop("SUDO_ASKPASS", None)
    try:
        validated: subprocess.CompletedProcess[object] | None = None
        if not allow_prompt:
            validated = subprocess.run(
                ["sudo", "-n", "-v"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=terminal_env,
                check=False,
            )
        else:
            for attempt in range(SUDO_AUTH_ATTEMPTS):
                if password_provider is None:
                    LOG.info("Enter the password in this terminal")
                    validated = subprocess.run(
                        ["sudo", "-p", "Accessor sudo password: ", "-v"],
                        env=terminal_env,
                        check=False,
                    )
                else:
                    password = password_provider()
                    # `-S` consumes the password from stdin. This lets the UI
                    # keep its screen active while using sudo's timestamp.
                    validated = subprocess.run(
                        ["sudo", "-S", "-p", "", "-v"],
                        input=f"{password}\n",
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=terminal_env,
                        check=False,
                    )
                if validated.returncode == 0:
                    break
                if attempt + 1 < SUDO_AUTH_ATTEMPTS:
                    LOG.info("sudo password was rejected; retrying (%s/%s)", attempt + 1, SUDO_AUTH_ATTEMPTS - 1)
        if validated is None or validated.returncode != 0:
            detail = (getattr(validated, "stderr", "") or "").strip()
            if allow_prompt:
                LAST_NETWORK_PREP_ERROR = t("sshuttle.sudo_failed", detail=f": {detail}" if detail else "")
            else:
                LAST_NETWORK_PREP_ERROR = t("sshuttle.sudo_expired")
            LOG.error("%s; proxy will not start", LAST_NETWORK_PREP_ERROR)
            return False
        for command in NETWORK_PREP_COMMANDS:
            # In the prompt_toolkit UI a password provider is supplied. Never
            # let privileged command output write directly to the terminal: a
            # single sudo/PF diagnostic would corrupt the live screen.
            hidden_output = (
                {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
                if password_provider is not None or not allow_prompt
                else {}
            )
            result = subprocess.run(
                ["sudo", "-n", *command], env=terminal_env, check=False, **hidden_output
            )
            if result.returncode != 0:
                detail = (getattr(result, "stderr", "") or "").strip()
                LAST_NETWORK_PREP_ERROR = t(
                    "sshuttle.command_failed",
                    command=" ".join(command), detail=f": {detail}" if detail else "",
                )
                LOG.error("%s", t("sshuttle.network_prepare_failed", detail=LAST_NETWORK_PREP_ERROR))
                return False
    except OSError as error:
        LAST_NETWORK_PREP_ERROR = t("sshuttle.network_prepare_unavailable", error=error)
        LOG.error("%s", LAST_NETWORK_PREP_ERROR)
        return False
    return True


class SshuttleProcess:
    """Start, inspect, and stop Accessor's shared Demand Proxy tunnel."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[object] | None = None
        self.log_handle: object | None = None
        self.log_path: Path | None = None

    def is_alive(self) -> bool:
        """Do not restart on every credential rotation; only inspect on schedule."""
        return self.process is not None and self.process.poll() is None

    @staticmethod
    def check_health(urls: Sequence[str]) -> tuple[int, int]:
        """Return successful/total private endpoint probes without exposing output.

        The health URLs are routed through sshuttle. ``--noproxy '*'`` is
        important on developer laptops: a corporate HTTP proxy could otherwise
        make a healthy response bypass the tunnel and give a false green result.
        """
        if not urls:
            return 0, 0
        curl = shutil.which("curl")
        if curl is None:
            return 0, len(urls)
        successful = 0
        for url in urls:
            try:
                result = subprocess.run(
                    [
                        curl,
                        "--fail",
                        "--silent",
                        "--show-error",
                        "--location",
                        "--noproxy",
                        "*",
                        "--connect-timeout",
                        "5",
                        "--max-time",
                        "15",
                        url,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            successful += result.returncode == 0
        return successful, len(urls)

    @staticmethod
    def find_external_proxy_pids() -> tuple[int, ...]:
        """Find an sshuttle/proxy launched outside this Accessor instance.

        A developer may start proxy from a project terminal before opening
        Accessor. Such a connection is usable but must never be stopped or
        duplicated by this process, so it is reported separately.
        """
        pgrep = shutil.which("pgrep")
        if pgrep is None:
            return ()
        process_ids: set[int] = set()
        for pattern in ("sshuttle", "establish_proxy_connection.py"):
            try:
                result = subprocess.run(
                    [pgrep, "-f", pattern],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            process_ids.update(
                int(line) for line in result.stdout.splitlines() if line.isdigit()
            )
        return tuple(sorted(process_ids))

    @staticmethod
    def has_pf_sshuttle_anchor() -> bool:
        """Check macOS PF state for sshuttle rules without prompting for sudo.

        sshuttle's forwarding helper commonly runs as root, which macOS may
        hide from a regular user's process list. Its PF anchors remain visible
        through `sudo -n` while the existing sudo timestamp is valid. `-n`
        deliberately returns false instead of interrupting the menu for a
        password prompt.
        """
        try:
            result = subprocess.run(
                ["sudo", "-n", "pfctl", "-s", "Anchors"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and any(
            "sshuttle" in line.lower() for line in result.stdout.splitlines()
        )

    @staticmethod
    def resolve_proxy_group(proxy: ProxyConfig) -> str:
        """Return the Demand Proxy group that serves ``proxy.service_name``.

        The Parameter Store mapping is the source of truth.  Resolving the
        group independently lets the console reuse an existing sshuttle when
        two selected services belong to the same group.
        """
        session = boto3.Session(profile_name=proxy.profile, region_name=proxy.region)
        ssm = session.client("ssm")
        value = ssm.get_parameter(Name=proxy.parameter_mapping)["Parameter"]["Value"]
        mapping = json.loads(value)
        instance_name = SshuttleProcess._mapped_instance_name(mapping, proxy.service_name)
        if not instance_name:
            raise RuntimeError(f"service {proxy.service_name} is absent from proxy mapping")
        return instance_name

    @staticmethod
    def _find_proxy_instance(proxy: ProxyConfig) -> str:
        """Resolve and start the EC2 instance for the selected proxy group."""
        instance_name = SshuttleProcess.resolve_proxy_group(proxy)
        session = boto3.Session(profile_name=proxy.profile, region_name=proxy.region)

        autoscaling = session.client("autoscaling")
        groups = autoscaling.describe_auto_scaling_groups(
            Filters=[{"Name": "tag:Cluster", "Values": [instance_name]}]
        ).get("AutoScalingGroups", [])
        if not groups:
            raise RuntimeError(f"no Auto Scaling Group found for proxy {instance_name}")
        group = groups[0]
        if group.get("DesiredCapacity", 0) == 0:
            autoscaling.update_auto_scaling_group(
                AutoScalingGroupName=group["AutoScalingGroupName"], DesiredCapacity=1
            )

        ec2 = session.client("ec2")
        running_filter = [
            {"Name": "tag:Name", "Values": [instance_name]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
        ec2.get_waiter("instance_running").wait(Filters=running_filter)
        response = ec2.describe_instances(Filters=running_filter)
        instances = [
            instance
            for reservation in response.get("Reservations", [])
            for instance in reservation.get("Instances", [])
        ]
        if not instances:
            raise RuntimeError(f"proxy instance {instance_name} did not become available")
        instance_id = instances[0]["InstanceId"]
        ec2.get_waiter("instance_status_ok").wait(InstanceIds=[instance_id])
        return instance_id

    @staticmethod
    def _mapped_instance_name(mapping: object, service_name: str) -> str | None:
        """Extract a valid proxy instance/Cluster name from the SSM mapping."""
        target = service_name.strip().lower()

        def contains_service(value: object) -> bool:
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized == target:
                    return True
                if normalized.startswith(f"{target}-") or normalized.startswith(f"{target}_"):
                    return True
                # A few Parameter Store revisions serialized the service list
                # as a comma/whitespace-delimited string instead of JSON.
                return target in {
                    item.strip()
                    for item in normalized.replace(",", " ").split()
                    if item.strip()
                }
            if isinstance(value, (list, tuple, set)):
                return any(contains_service(item) for item in value)
            if isinstance(value, dict):
                # The actual Parameter Store shape is
                # {proxy_cluster: {service_name: security_group_id}}. Match
                # service names stored as dictionary keys as well as the older
                # list/object value forms.
                return any(
                    contains_service(key) or contains_service(item)
                    for key, item in value.items()
                )
            return False

        def is_instance_name(value: object) -> bool:
            """Reject AWS resource IDs such as ``sg-...`` as Cluster names."""
            return isinstance(value, str) and bool(value) and not value.lower().startswith(
                ("sg-", "subnet-", "vpc-", "i-")
            )

        instance_fields = (
            "instance", "instance_name", "instanceName", "proxy_instance", "proxyInstance",
            "proxy_instance_name", "proxyInstanceName", "cluster",
        )
        ignored_fields = {*instance_fields, "name"}
        wrapper_fields = {"mapping", "mappings", "items", "entries", "data", "proxies"}

        if isinstance(mapping, dict):
            instance_field = next(
                (
                    mapping.get(field)
                    for field in instance_fields
                    if is_instance_name(mapping.get(field))
                ),
                None,
            )
            if instance_field:
                service_values = [
                    value
                    for key, value in mapping.items()
                    if key not in ignored_fields
                ]
                if any(contains_service(value) for value in service_values):
                    return instance_field

            for instance_name, services in mapping.items():
                # Also accept the reverse form: {"service": "instance"}.
                if str(instance_name).strip().lower() == target:
                    if is_instance_name(services):
                        return services
                    if isinstance(services, list) and services and is_instance_name(services[0]):
                        return services[0]
                if (
                    str(instance_name).lower() not in wrapper_fields
                    and is_instance_name(instance_name)
                    and contains_service(services)
                ):
                    return str(instance_name)
            # Recurse only through explicit wrapper fields. Recursing through
            # every nested value previously selected a security-group ID.
            for key, value in mapping.items():
                if str(key).lower() not in wrapper_fields:
                    continue
                found = SshuttleProcess._mapped_instance_name(value, service_name)
                if found:
                    return found
        elif isinstance(mapping, list):
            for record in mapping:
                if not isinstance(record, dict):
                    continue
                instance_name = next(
                    (
                        record.get(field)
                        for field in instance_fields
                        if is_instance_name(record.get(field))
                    ),
                    None,
                )
                service_values = [
                    value
                    for key, value in record.items()
                    if key not in ignored_fields
                ]
                if instance_name and any(contains_service(value) for value in service_values):
                    return instance_name
        return None

    def start(
        self,
        proxy: ProxyConfig,
        prepare_network: bool = True,
        allow_sudo_prompt: bool = True,
        sudo_password_provider: Callable[[], str] | None = None,
    ) -> bool:
        """Start Accessor's own sshuttle command without a project checkout."""
        global LAST_PROXY_START_ERROR
        LAST_PROXY_START_ERROR = None
        # Discovery failures happen before sshuttle creates its child log
        # stream, so keep the manager diagnostics in the same file users
        # already inspect for proxy failures.
        self.log_path = Path("/tmp/accessor-demand-proxy.log")
        if self.is_alive():
            return True
        if prepare_network and not prepare_network_before_proxy(
            password_provider=sudo_password_provider, allow_prompt=allow_sudo_prompt
        ):
            LAST_PROXY_START_ERROR = network_prepare_error() or t("sshuttle.network_prepare_failed", detail="")
            self._write_manager_error(LAST_PROXY_START_ERROR)
            return False
        try:
            instance_id = self._find_proxy_instance(proxy)
            sshuttle = shutil.which("sshuttle")
            if sshuttle is None:
                raise OSError("sshuttle executable was not found")
            # sudo -v has completed before this point. The child can therefore
            # be detached: it cannot steal menu input or render over the status
            # screen, while its sshuttle output remains available in a log file.
            self.log_handle = self.log_path.open("a", encoding="utf-8")
            command = [
                sshuttle,
                "--dns",
                "-x",
                proxy.exclude_cidr,
                "-Hvr",
                f"{proxy.ssh_user}@{instance_id}",
                *proxy.subnets,
                "--ssh-cmd",
                "ssh -oStrictHostKeyChecking=no",
            ]
            environment = os.environ.copy()
            environment["AWS_PROFILE"] = proxy.profile
            # sshuttle starts an SSH child which can invoke the AWS profile's
            # credential process. boto3 above receives ``region_name``
            # explicitly, but that child does not; without these variables a
            # machine lacking a default AWS region fails with ``NoRegion`` and
            # drops the just-created tunnel.
            environment["AWS_REGION"] = proxy.region
            environment["AWS_DEFAULT_REGION"] = proxy.region
            self.process = subprocess.Popen(
                command,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=self.log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            LOG.info("Proxy output is written to %s", self.log_path)
            return True
        except OSError as error:
            LAST_PROXY_START_ERROR = f"{type(error).__name__}: {error}"
            self._write_manager_error(LAST_PROXY_START_ERROR)
            LOG.error("Unable to start sshuttle: %s", error)
            self.process = None
            if self.log_handle is not None:
                self.log_handle.close()
                self.log_handle = None
            return False
        except Exception as error:
            LAST_PROXY_START_ERROR = f"{type(error).__name__}: {error}"
            self._write_manager_error(LAST_PROXY_START_ERROR)
            LOG.error("Unable to resolve/start sshuttle: %s", error)
            self.process = None
            if self.log_handle is not None:
                self.log_handle.close()
                self.log_handle = None
            return False

    def _write_manager_error(self, message: str) -> None:
        """Persist pre-child startup failures beside sshuttle's own output."""
        try:
            self.log_path = self.log_path or Path("/tmp/accessor-demand-proxy.log")
            with self.log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} Accessor proxy start failed: {message}\n"
                )
        except OSError:
            pass

    @staticmethod
    def _descendant_pids(root_pid: int) -> list[int]:
        """Return descendants before the parent exits and reparents them."""
        pgrep = shutil.which("pgrep")
        if pgrep is None:
            return []
        descendants, pending = [], [root_pid]
        while pending:
            parent = pending.pop()
            try:
                result = subprocess.run(
                    [pgrep, "-P", str(parent)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            children = [int(line) for line in result.stdout.splitlines() if line.isdigit()]
            descendants.extend(children)
            pending.extend(children)
        return descendants

    @staticmethod
    def _signal(process_ids: Sequence[int], signum: int) -> None:
        for process_id in process_ids:
            try:
                os.kill(process_id, signum)
            except (ProcessLookupError, PermissionError):
                pass

    def stop_external_proxy(self, process_ids: Sequence[int]) -> tuple[int, ...]:
        """Request shutdown of an unhealthy external proxy before replacing it."""
        process_ids = tuple(sorted({pid for pid in process_ids if pid > 0}))
        LOG.warning("Stopping unhealthy external sshuttle processes: %s", process_ids)
        self._signal(process_ids, signal.SIGTERM)
        time.sleep(0.2)
        remaining: list[int] = []
        for process_id in process_ids:
            try:
                os.kill(process_id, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            remaining.append(process_id)
        return tuple(remaining)

    def stop(self, project: ProjectConfig) -> None:
        """Stop the sshuttle process tree when Accessor exits."""
        process = self.process
        if process is None or process.poll() is not None:
            self.process = None
            return
        process_ids = [*reversed(self._descendant_pids(process.pid)), process.pid]
        LOG.info("Stopping sshuttle process tree rooted at %s", process.pid)
        try:
            self._signal(process_ids, signal.SIGTERM)
            process.wait(timeout=project.shutdown_grace_seconds)
        except subprocess.TimeoutExpired:
            LOG.warning("sshuttle did not stop gracefully; sending SIGKILL")
            self._signal(process_ids, signal.SIGKILL)
            process.wait(timeout=5)
        finally:
            self.process = None
            if self.log_handle is not None:
                self.log_handle.close()
                self.log_handle = None
