"""Timed orchestration: roles, service credentials, and sshuttle liveness."""

from __future__ import annotations

import fcntl
import logging
import math
import os
from pathlib import Path
import signal
import threading
import time
import traceback
from typing import Any, Callable, Sequence

from config import ProjectConfig, ProxyConfig, RoleConfig, Settings
from i18n import t
from permissions import ROLE_REFRESH_LOG, RoleRefresher, run_project_refresh
from sshuttle import SshuttleProcess, proxy_start_error


LOG = logging.getLogger("accessor.scheduler")
StatusReporter = Callable[[str, str, str, str], None]
ProxyFailureNotifier = Callable[[], None]
SCHEDULER_LOG = Path("/tmp/accessor-scheduler.log")
LOCK_CONFLICT_EXIT_CODE = 2


class AccessorInstanceRunning(RuntimeError):
    """Raised when another refresh worker already owns the shared lock."""


class SingleInstance:
    """Prevent two schedulers from each trying to own the same sshuttle routes."""

    def __init__(self, lock_file: Path):
        self.lock_file = lock_file
        self.handle: Any = None

    def __enter__(self) -> "SingleInstance":
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.lock_file.open("a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.handle.seek(0)
            owner = self.handle.read().strip() or "unknown"
            raise AccessorInstanceRunning(
                f"another Accessor instance is running (pid {owner})"
            ) from error
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, *_: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


class RefreshScheduler:
    """Own independent clocks for roles, service credentials, and sshuttle."""

    def __init__(
        self,
        settings: Settings,
        projects: Sequence[ProjectConfig],
        proxy_project: ProjectConfig | None,
        proxy_config: ProxyConfig | None = None,
        proxy_group: str | None = None,
        status_reporter: StatusReporter | None = None,
        manage_proxy: bool = True,
        network_prepared: bool = False,
        sudo_password_provider: Callable[[], str] | None = None,
        proxy_failure_notifier: ProxyFailureNotifier | None = None,
    ):
        self.settings = settings
        self.projects = tuple(projects)
        self.proxy_project = proxy_project
        # ``proxy_config`` has the first selected project's service name.  It
        # deliberately differs from settings.proxy when that project belongs
        # to a non-default Demand Proxy group.
        self.proxy_config = proxy_config or settings.proxy
        self.proxy_group = proxy_group
        self.manage_proxy = manage_proxy
        self.network_prepared = network_prepared
        # The prompt_toolkit console implements this callback by switching the
        # current screen to a hidden password field. Command-line mode leaves
        # it unset, so a background job can never read from terminal stdin.
        self.sudo_password_provider = sudo_password_provider
        self.proxy_failure_notifier = proxy_failure_notifier
        self._proxy_failure_notified = False
        # Keep automatic Granted output out of the console. The request still
        # runs with the exact configured role and is verified afterwards; its
        # diagnostics remain available in this local log.
        self.roles = RoleRefresher(settings, request_log_path=ROLE_REFRESH_LOG)
        self.sshuttle = SshuttleProcess()
        self.role_ready = {role.name: False for role in settings.roles}
        self.next_role_refresh = {role.name: 0.0 for role in settings.roles}
        self.next_credential_refresh = {project.name: 0.0 for project in projects}
        self.next_sshuttle_check = 0.0
        self.stop_requested = False
        self.lock_conflict_message: str | None = None
        # AWS service discovery can spend minutes waiting for AWS. Keep that
        # slow work off the scheduler loop so the role and tunnel clocks still
        # fire on time.  Credential writers remain serialized below: project
        # credential operations rewrite ~/.aws/credentials as a whole file.
        self._schedule_lock = threading.RLock()
        self._credential_write_lock = threading.Lock()
        self._project_refresh_thread: threading.Thread | None = None
        self._role_refresh_thread: threading.Thread | None = None
        # The interactive console supplies this callback. It receives actual
        # outcomes from this refresh job, so the UI never needs a second status
        # polling thread that repeats AWS or proxy checks.
        self.status_reporter = status_reporter

    def _report_status(self, kind: str, name: str, status: str, action: str) -> None:
        """Publish a non-sensitive status update without risking the refresh job."""
        if self.status_reporter is None:
            return
        try:
            self.status_reporter(kind, name, status, action)
        except Exception:
            LOG.exception("Unable to publish %s status for %s", kind, name)

    def _notify_proxy_failure_once(self) -> None:
        """Notify only when the proxy newly enters a total health failure."""
        if self._proxy_failure_notified:
            return
        self._proxy_failure_notified = True
        if self.proxy_failure_notifier is None:
            return
        try:
            self.proxy_failure_notifier()
        except Exception:
            LOG.exception("Unable to show Demand Proxy failure notification")

    def _request_stop(self, signum: int, _frame: Any) -> None:
        LOG.info("Received signal %s; shutting down", signum)
        self.stop_requested = True

    def request_stop(self) -> None:
        """Allow the interactive console to stop the background scheduler."""
        self.stop_requested = True

    def replace_projects(self, projects: Sequence[ProjectConfig]) -> None:
        """Make a new menu selection eligible for an immediate refresh.

        The scheduler remains alive, so a same-group selection does not tear
        down and recreate the shared sshuttle route.
        """
        with self._schedule_lock:
            self.projects = tuple(projects)
            self.next_credential_refresh = {
                project.name: 0.0 for project in self.projects
            }

    def update_role_ready(self, states: dict[str, bool]) -> None:
        """Reuse role results already obtained by a foreground menu action."""
        with self._schedule_lock:
            self.role_ready.update(states)

    def switch_proxy(
        self,
        projects: Sequence[ProjectConfig],
        proxy_project: ProjectConfig,
        proxy_config: ProxyConfig,
        proxy_group: str,
        network_prepared: bool,
    ) -> None:
        """Replace the one managed tunnel after the console chose a new group."""
        with self._schedule_lock:
            previous_project = self.proxy_project
            if self.manage_proxy and previous_project is not None:
                self.sshuttle.stop(previous_project)
            self.projects = tuple(projects)
            self.next_credential_refresh = {
                project.name: 0.0 for project in self.projects
            }
            self.proxy_project = proxy_project
            self.proxy_config = proxy_config
            self.proxy_group = proxy_group
            self.manage_proxy = True
            self.network_prepared = network_prepared
            self.next_sshuttle_check = 0.0
            self._proxy_failure_notified = False

    def _project_role_ready(self, project: ProjectConfig) -> bool:
        dependency = project.depends_on_role
        with self._schedule_lock:
            return dependency is None or self.role_ready.get(dependency, False)

    def _refresh_role(self, role: RoleConfig, now: float) -> None:
        # Granted may write a new session to ~/.aws/credentials, so it must not
        # race a project's own credential writer.
        with self._credential_write_lock:
            ready = self.roles.refresh(role)
        completed_at = time.monotonic()
        with self._schedule_lock:
            self.role_ready[role.name] = ready
            self.next_role_refresh[role.name] = completed_at + (
                role.refresh_seconds if ready else role.retry_seconds
            )
        self._report_status(
            "role", role.name, t("status.valid") if ready else t("status.invalid"), self.roles.last_action
        )

    def _schedule_credential(self, project: ProjectConfig, now: float, success: bool) -> None:
        delay = project.credential_refresh_seconds if success else project.credential_retry_seconds
        with self._schedule_lock:
            self.next_credential_refresh[project.name] = now + delay

    def _refresh_project_credentials(self, project: ProjectConfig, now: float) -> None:
        """Credential rotation is independent from sshuttle's lifecycle."""
        if not self._project_role_ready(project):
            LOG.warning("Project %s is waiting for role %s", project.name, project.depends_on_role)
            self._report_status(
                "project", project.name, t("scheduler.wait_role", role=project.depends_on_role), t("action.wait")
            )
            self._schedule_credential(project, now, success=False)
            return
        # Service refreshes may involve slow AWS discovery. Serialize their
        # writes, but run the batch in a worker
        # so this cannot freeze proxy or role monitoring.
        with self._credential_write_lock:
            refreshed = run_project_refresh(self.settings, project)
        self._report_status(
            "project", project.name, t("status.valid") if refreshed else t("status.refresh_failed"), t("action.refresh")
        )
        # Count the interval from completion: a slow credential process must
        # not make the same project immediately due again.
        self._schedule_credential(project, time.monotonic(), refreshed)

    def _run_due_roles(self, roles: Sequence[RoleConfig]) -> None:
        """Refresh one due-role batch without delaying Proxy checks."""
        for role in roles:
            if self.stop_requested:
                return
            self._refresh_role(role, time.monotonic())

    def _start_due_roles(self, now: float) -> None:
        """Start a single serialized role worker when any role is due."""
        if self._role_refresh_thread is not None and self._role_refresh_thread.is_alive():
            return
        with self._schedule_lock:
            due = [
                role for role in self.settings.roles
                if now >= self.next_role_refresh[role.name]
            ]
            # Mark jobs in-flight before the worker starts; otherwise the main
            # loop would launch the same due role repeatedly.
            for role in due:
                self.next_role_refresh[role.name] = math.inf
        if not due:
            return
        self._role_refresh_thread = threading.Thread(
            target=self._run_due_roles,
            args=(tuple(due),),
            name="accessor-role-refresh",
            daemon=True,
        )
        self._role_refresh_thread.start()

    def _run_due_projects(self, projects: Sequence[ProjectConfig]) -> None:
        """Refresh one due-project batch without delaying Proxy checks."""
        for project in projects:
            if self.stop_requested:
                return
            self._refresh_project_credentials(project, time.monotonic())

    def _start_due_projects(self, now: float) -> None:
        """Start one serialized project worker when selected projects are due."""
        if self._project_refresh_thread is not None and self._project_refresh_thread.is_alive():
            return
        with self._schedule_lock:
            due = [
                project for project in self.projects
                if now >= self.next_credential_refresh[project.name]
            ]
            for project in due:
                self.next_credential_refresh[project.name] = math.inf
        if not due:
            return
        self._project_refresh_thread = threading.Thread(
            target=self._run_due_projects,
            args=(tuple(due),),
            name="accessor-project-refresh",
            daemon=True,
        )
        self._project_refresh_thread.start()

    def _check_sshuttle(self, now: float) -> None:
        """Long-interval liveness check and recovery for the shared tunnel."""
        project = self.proxy_project
        if project is None or now < self.next_sshuttle_check:
            return
        if not self.manage_proxy:
            healthy, total = self.sshuttle.check_health(self.settings.proxy_health_urls)
            if healthy:
                self._proxy_failure_notified = False
            if total and healthy == 0:
                self._notify_proxy_failure_once()
                external_pids = self.sshuttle.find_external_proxy_pids()
                if external_pids:
                    self._report_status(
                        "proxy", "demand",
                        t("scheduler.external_takeover", total=total),
                        t("action.takeover"),
                    )
                    remaining = self.sshuttle.stop_external_proxy(external_pids)
                    if remaining:
                        self._report_status(
                            "proxy", "demand",
                            t("scheduler.external_wait_exit", pids=", ".join(map(str, remaining))),
                            t("action.restart"),
                        )
                        self.next_sshuttle_check = now + project.restart_delay_seconds
                        return
                # It is now safe to replace the failed external connection.
                self.manage_proxy = True
            else:
                if total and healthy < total:
                    status = t("scheduler.external_partial", healthy=healthy, total=total)
                elif total:
                    status = t("scheduler.external_healthy", healthy=healthy, total=total)
                else:
                    status = t("scheduler.external_no_health")
                self._report_status("proxy", "demand", status, t("action.check"))
                self.next_sshuttle_check = now + self.settings.sshuttle_check_seconds
                return
        if self.sshuttle.is_alive():
            healthy, total = self.sshuttle.check_health(self.settings.proxy_health_urls)
            if healthy:
                self._proxy_failure_notified = False
            if total and healthy == 0:
                self._notify_proxy_failure_once()
                LOG.warning("sshuttle for %s failed every health probe; restarting", project.name)
                self._report_status(
                    "proxy", "demand", t("scheduler.restarting", total=total), t("action.restart")
                )
                self.sshuttle.stop(project)
                self.network_prepared = False
            else:
                LOG.debug("sshuttle for %s is alive", project.name)
                health = t("proxy.health", healthy=healthy, total=total) if total else ""
                if total and healthy < total:
                    health = t("proxy.partial_health", healthy=healthy, total=total)
                self._report_status(
                    "proxy", "demand", t("proxy.managed", health=health), t("action.check")
                )
                self.next_sshuttle_check = now + self.settings.sshuttle_check_seconds
                return
        if not self._project_role_ready(project):
            LOG.warning("sshuttle for %s waits for role %s", project.name, project.depends_on_role)
            self._report_status(
                "proxy", "demand", t("scheduler.wait_role", role=project.depends_on_role), t("action.wait")
            )
            self.next_sshuttle_check = now + project.restart_delay_seconds
            return
        LOG.info("sshuttle for %s is not running; starting it", project.name)
        # Proxy startup is intentionally independent from service credentials.
        # The tunnel only uses the configured jump-role AWS profile; selected
        # service profiles are refreshed by the separate project worker below.
        started = self.sshuttle.start(
            self.proxy_config,
            prepare_network=(
                self.settings.prepare_network_before_proxy and not self.network_prepared
            ),
            allow_sudo_prompt=self.sudo_password_provider is not None,
            sudo_password_provider=self.sudo_password_provider,
        )
        if started:
            self.network_prepared = True
        # SSM-backed SSH startup can take noticeably longer than a normal
        # process restart. Do not kill a tunnel that is still connecting; wait
        # at least one minute before its first health check.
        self.next_sshuttle_check = now + max(60, project.restart_delay_seconds)
        if started:
            proxy_status = t("scheduler.starting")
        else:
            reason = proxy_start_error()
            proxy_status = t("scheduler.start_failed", reason=reason) if reason else t("scheduler.start_failed_generic")
        self._report_status("proxy", "demand", proxy_status, t("action.start") if started else t("action.restart"))

    def _initial_refreshes(self, now: float) -> None:
        """Queue credentials immediately; Proxy startup must not delay them."""
        # Creating the project worker is non-blocking. Do it before SSM/EC2
        # discovery and sshuttle startup, which can take minutes while a proxy
        # instance is waking up or an SSH connection is being established.
        self._start_due_projects(now)
        self._check_sshuttle(now)

    def _sleep(self, now: float) -> None:
        with self._schedule_lock:
            deadlines = [*self.next_role_refresh.values(), *self.next_credential_refresh.values()]
        if self.proxy_project is not None:
            deadlines.append(self.next_sshuttle_check)
        future = [deadline for deadline in deadlines if deadline > now]
        time.sleep(min(5.0, max(0.2, min(future) - now)) if future else 1.0)

    def _record_fatal_error(self, error: BaseException) -> None:
        """Make a background failure visible even when UI logging is muted."""
        message = t("scheduler.job_stopped", type=type(error).__name__, error=error)
        self._report_status("job", "refresh", message, t("action.failure"))
        try:
            with SCHEDULER_LOG.open("a", encoding="utf-8") as log_file:
                log_file.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
                traceback.print_exc(file=log_file)
        except OSError:
            pass

    def run(self) -> int:
        """Run all schedules until interrupted; sshuttle has its own long clock."""
        # The interactive console runs this method in a background thread.
        # Python only permits signal registration in the main interpreter thread;
        # console shutdown instead calls request_stop() directly.
        previous_handlers: dict[int, Any] = {}
        if threading.current_thread() is threading.main_thread():
            previous_handlers = {
                signum: signal.getsignal(signum)
                for signum in (signal.SIGINT, signal.SIGTERM)
            }
            for signum in previous_handlers:
                signal.signal(signum, self._request_stop)
        try:
            with SingleInstance(self.settings.lock_file):
                now = time.monotonic()
                for role in self.settings.roles:
                    self._refresh_role(role, now)
                self._initial_refreshes(now)
                while not self.stop_requested:
                    now = time.monotonic()
                    self._start_due_roles(now)
                    self._check_sshuttle(now)
                    self._start_due_projects(now)
                    self._sleep(now)
        except AccessorInstanceRunning as error:
            # Lock contention is an expected safety guard, not a transient
            # refresh failure.  The caller must not retry once per minute.
            self.lock_conflict_message = str(error)
            LOG.info("%s", error)
            return LOCK_CONFLICT_EXIT_CODE
        except RuntimeError as error:
            LOG.error("%s", error)
            self._record_fatal_error(error)
            return 1
        except Exception as error:
            LOG.exception("Accessor refresh job crashed")
            self._record_fatal_error(error)
            return 1
        finally:
            if self.proxy_project is not None and self.manage_proxy:
                self.sshuttle.stop(self.proxy_project)
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        return 0
