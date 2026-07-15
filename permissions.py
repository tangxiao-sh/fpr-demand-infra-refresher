"""AWS/Granted role checks and service-credential refresh operations."""

from __future__ import annotations

import logging
import os
import configparser
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any, Sequence

from config import ProjectConfig, RoleConfig, Settings
from credentials import refresh_service_credentials
from i18n import t


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
        self.last_action = t("action.check")

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
        self.last_action = t("action.check")
        LOG.info("Refreshing role %s (%s)", role.name, role.profile)
        if role.credential_source_profile is not None:
            # A long-lived consumer such as a Gradle daemon may keep using an
            # older session even while `aws sts` can still validate the alias.
            # Re-copy the source session on every role cycle so the profile on
            # disk always matches the freshly granted source role.
            if not self._check_profile(role.credential_source_profile):
                LOG.warning(
                    "Source profile %s is unavailable for %s",
                    role.credential_source_profile,
                    role.profile,
                )
                return False
            self.last_action = t("action.refresh")
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
            self.last_action = t("action.refresh")
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
        self.last_action = t("action.refresh")
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


def refresh_project_credentials(settings: Settings, project: ProjectConfig) -> str:
    """Refresh a service profile using Accessor's standalone AWS implementation."""
    return refresh_service_credentials(settings, project)


def check_project_credentials(project: ProjectConfig) -> tuple[bool, str]:
    """Check the existing service profile without writing credentials or starting proxy."""
    service_name = project.service_name
    if not service_name:
        return False, "project has no service_name"
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
    """Run Accessor's standalone service refresh in an isolated child process."""
    command = (
        sys.executable,
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
        # Scheduler refreshes run in the background. Keep boto3 output out of
        # the interactive menu; failures remain visible in this log.
        with CREDENTIAL_REFRESH_LOG.open("a", encoding="utf-8") as log_file:
            result = subprocess.run(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                # AWS service discovery normally completes in seconds. Do not let
                # one stalled service call monopolize the serialized worker
                # for the five-minute Granted command timeout.
                timeout=settings.project_command_timeout_seconds,
                check=False,
            )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as error:
        LOG.error("Project %s credential refresh failed: %s", project.name, error)
        return False
