"""AWS/Granted role checks and service-credential refresh operations."""

from __future__ import annotations

import inspect
import logging
import os
import configparser
from pathlib import Path
import runpy
import shlex
import subprocess
import time
from typing import Any, Sequence

from config import ProjectConfig, RoleConfig, Settings


LOG = logging.getLogger("accessor.permissions")
CREDENTIAL_REFRESH_LOG = Path("/tmp/accessor-credential-refresh.log")
ROLE_REFRESH_LOG = Path("/tmp/accessor-role-refresh.log")


class RoleRefresher:
    """Refresh roles serially so `granted request latest` cannot target another role."""

    def __init__(
        self,
        settings: Settings,
        dry_run: bool = False,
        request_log_path: Path | None = None,
    ):
        self.settings = settings
        self.dry_run = dry_run
        # Foreground requests inherit the terminal so Granted can guide the
        # developer. The scheduler instead records the same output in a log,
        # keeping the interactive menu usable while it refreshes automatically.
        self.request_log_path = request_log_path
        self.last_action = "检查"

    def _run(self, command: Sequence[str], quiet: bool) -> bool:
        """Run without capturing any credential_process output or secret values."""
        LOG.log(logging.DEBUG if quiet else logging.INFO, "Executing: %s", shlex.join(command))
        if self.dry_run:
            return True
        try:
            if not quiet and self.request_log_path is not None:
                with self.request_log_path.open("a", encoding="utf-8") as log_file:
                    result = subprocess.run(
                        command,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        timeout=self.settings.command_timeout_seconds,
                        check=False,
                    )
            else:
                result = subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL if quiet else None,
                    stderr=subprocess.DEVNULL if quiet else None,
                    timeout=self.settings.command_timeout_seconds,
                    check=False,
                )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            LOG.error("Command timed out after %ss", self.settings.command_timeout_seconds)
        except OSError as error:
            LOG.error("Unable to run %s: %s", command[0], error)
        return False

    def check(self, role: RoleConfig) -> bool:
        """Read the main and compatibility AWS profiles without requesting access."""
        return all(self._check_profile(profile) for profile in (role.profile, *role.credential_aliases))

    def _check_profile(self, profile: str, quiet: bool = True) -> bool:
        return self._run(
            ("aws", "sts", "get-caller-identity", "--profile", profile, "--output", "json"),
            quiet=quiet,
        )

    def _aliases_available(self, role: RoleConfig) -> bool:
        return all(self._check_profile(alias) for alias in role.credential_aliases)

    @staticmethod
    def _sync_credential_aliases(role: RoleConfig) -> bool:
        """Copy a refreshed session into legacy profiles used by local build tools.

        Granted exports the session under the modern role profile. Some Gradle
        builds still reference a short, historical profile (for example
        ``beiartf``), so both must contain the exact same temporary session.
        Secret values are copied locally and never logged.
        """
        return RoleRefresher._copy_credential_profiles(role.profile, role.credential_aliases)

    @staticmethod
    def _copy_credential_profiles(source_profile: str, targets: Sequence[str]) -> bool:
        """Copy one local temporary session into one or more named profiles."""
        if not targets:
            return True
        credentials_path = Path.home() / ".aws" / "credentials"
        parser = configparser.RawConfigParser()
        parser.read(credentials_path, encoding="utf-8")
        if not parser.has_section(source_profile):
            LOG.error("Cannot synchronize aliases: profile %s is missing", source_profile)
            return False
        required = ("aws_access_key_id", "aws_secret_access_key", "aws_session_token")
        if any(not parser.has_option(source_profile, field) for field in required):
            LOG.error("Cannot synchronize aliases: profile %s has no complete session", source_profile)
            return False
        session = {field: parser.get(source_profile, field) for field in required}
        for alias in targets:
            if not parser.has_section(alias):
                parser.add_section(alias)
            for field, value in session.items():
                parser.set(alias, field, value)
        temporary_path = credentials_path.with_suffix(".accessor-tmp")
        try:
            with temporary_path.open("w", encoding="utf-8") as credentials_file:
                parser.write(credentials_file)
            os.chmod(temporary_path, 0o600)
            temporary_path.replace(credentials_path)
        except OSError as error:
            LOG.error("Unable to synchronize credential aliases: %s", error)
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True

    def refresh(self, role: RoleConfig) -> bool:
        """Check one profile, request access if needed, then check it again."""
        self.last_action = "检查"
        LOG.info("Refreshing role %s (%s)", role.name, role.profile)
        if role.credential_source_profile is not None:
            if self._check_profile(role.profile):
                return True
            self.last_action = "刷新"
            return (
                self._copy_credential_profiles(role.credential_source_profile, (role.profile,))
                and self._check_profile(role.profile)
            )
        primary_ready = self._check_profile(role.profile)
        if primary_ready and self._aliases_available(role):
            return True
        if primary_ready:
            # The role itself is valid, but a legacy profile used by a build
            # tool has expired. Re-export the same session into that alias.
            self.last_action = "刷新"
            return self._sync_credential_aliases(role) and self._aliases_available(role)
        if not (
            self.settings.auto_request
            and role.request_on_failure
            and self.settings.request_command
        ):
            LOG.warning("Role %s is not currently available", role.name)
            return False

        # Granted's `assume` is installed as a shell alias that sources the
        # actual executable. Running the binary directly bypasses that alias
        # and causes Granted to ask for alias installation again. Use the
        # developer's interactive shell so ~/.zshenv is loaded first. The
        # configured ``--export`` stores the approved session in the requested
        # AWS profile: an export to this short-lived shell alone would disappear
        # before the AWS/Boto processes that Accessor starts afterwards.
        shell = os.environ.get("SHELL", "/bin/zsh")
        self.last_action = "刷新"
        shell_command = shlex.join((*self.settings.request_command, role.profile))
        request_command = (shell, "-ic", shell_command)
        LOG.info("Requesting access for role %s", role.name)
        if not self._run(request_command, quiet=False):
            LOG.error("Granted could not obtain role %s", role.name)
            return False
        if not self.dry_run:
            time.sleep(self.settings.post_request_delay_seconds)
        if self._check_profile(role.profile):
            return self._sync_credential_aliases(role) and self._aliases_available(role)

        # The initial/retry probes are quiet to keep the menu readable. If a
        # request did complete but credentials still fail, show AWS's precise
        # (non-secret) diagnostic in the terminal instead of a vague status.
        LOG.error("Role %s remains unavailable after the Granted request", role.name)
        self._check_profile(role.profile, quiet=False)
        return False


def refresh_project_credentials(project: ProjectConfig) -> str:
    """Reuse a proxy module's credential functions without starting sshuttle.

    The project script is run under a non-``__main__`` name, so its bottom main
    block never executes. This is the important separation: rotating service
    credentials does not recreate or disturb an established sshuttle tunnel.
    """
    namespace = runpy.run_path(
        str(project.script_path), run_name="accessor_project_proxy_module"
    )
    required = ("boto3", "aws_profile", "service_name", "get_credential", "write_credential")
    missing = [name for name in required if name not in namespace]
    if missing:
        raise RuntimeError(
            f"project {project.name}: proxy script is missing expected symbols: {', '.join(missing)}"
        )

    # New scripts usually obtain the region from AWS_REGION; old ones expose a
    # `region` global. Their original default is ap-southeast-1.
    region = namespace.get("region") or os.environ.get("AWS_REGION", "ap-southeast-1")
    namespace["boto3"].setup_default_session(
        profile_name=namespace["aws_profile"], region_name=region
    )

    arguments: list[Any] = []
    for parameter in inspect.signature(namespace["get_credential"]).parameters.values():
        if parameter.kind not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            raise RuntimeError(f"project {project.name}: unsupported parameter {parameter.name}")
        if parameter.name == "session_name" and "read_aws_config" in namespace:
            arguments.append(namespace["read_aws_config"](profile_name=namespace["aws_profile"]))
        elif parameter.name in namespace:
            arguments.append(namespace[parameter.name])
        elif parameter.default is inspect.Parameter.empty:
            raise RuntimeError(
                f"project {project.name}: cannot resolve parameter {parameter.name}"
            )

    credential = namespace["get_credential"](*arguments)
    service_name = namespace["service_name"]
    # Preserve the project's own ~/.aws/credentials writing behavior. Secrets
    # are never logged or stored by Accessor outside that existing writer.
    namespace["write_credential"](service_name, credential)
    return service_name


def check_project_credentials(project: ProjectConfig) -> tuple[bool, str]:
    """Check the existing service profile without writing credentials or starting proxy."""
    namespace = runpy.run_path(
        str(project.script_path), run_name="accessor_project_proxy_check"
    )
    service_name = namespace.get("service_name")
    if not isinstance(service_name, str) or not service_name:
        return False, "proxy script has no service_name"
    try:
        result = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--profile", service_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    return result.returncode == 0, service_name


def run_project_refresh(
    settings: Settings, project: ProjectConfig, dry_run: bool = False
) -> bool:
    """Run a project refresh using that project's Python interpreter."""
    command = (
        project.python,
        str(Path(__file__).with_name("cli.py")),
        "refresh-project",
        "--config",
        str(settings.config_path),
        "--project",
        project.name,
    )
    LOG.info("Refreshing credentials for project %s", project.name)
    if dry_run:
        LOG.info("Would execute: %s", shlex.join(command))
        return True
    try:
        # Scheduler refreshes run in the background. Keep project-script output
        # out of the interactive menu; failures remain visible in this log.
        with CREDENTIAL_REFRESH_LOG.open("a", encoding="utf-8") as log_file:
            result = subprocess.run(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                # Project scripts normally complete in seconds.  Do not let
                # one stalled service call monopolize the serialized worker
                # for the five-minute Granted command timeout.
                timeout=settings.project_command_timeout_seconds,
                check=False,
            )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as error:
        LOG.error("Project %s credential refresh failed: %s", project.name, error)
        return False
