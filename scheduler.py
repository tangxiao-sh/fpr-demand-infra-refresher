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

from config import ProjectConfig, RoleConfig, Settings
from permissions import ROLE_REFRESH_LOG, RoleRefresher, run_project_refresh
from sshuttle import SshuttleProcess


LOG = logging.getLogger("accessor.scheduler")
StatusReporter = Callable[[str, str, str, str], None]
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
        status_reporter: StatusReporter | None = None,
        manage_proxy: bool = True,
    ):
        self.settings = settings
        self.projects = tuple(projects)
        self.proxy_project = proxy_project
        self.manage_proxy = manage_proxy
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

    def _request_stop(self, signum: int, _frame: Any) -> None:
        LOG.info("Received signal %s; shutting down", signum)
        self.stop_requested = True

    def request_stop(self) -> None:
        """Allow the interactive console to stop the background scheduler."""
        self.stop_requested = True

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
            "role", role.name, "有效" if ready else "失效", self.roles.last_action
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
                "project", project.name, f"等待权限 {project.depends_on_role}", "等待"
            )
            self._schedule_credential(project, now, success=False)
            return
        # Service refreshes may involve slow AWS discovery. Serialize their
        # writes, but run the batch in a worker
        # so this cannot freeze proxy or role monitoring.
        with self._credential_write_lock:
            refreshed = run_project_refresh(self.settings, project)
        self._report_status(
            "project", project.name, "有效" if refreshed else "刷新失败", "刷新"
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
        """Long-interval liveness check; restart only if the tunnel is dead."""
        project = self.proxy_project
        if project is None or now < self.next_sshuttle_check:
            return
        if not self.manage_proxy:
            healthy, total = self.sshuttle.check_health(self.settings.proxy_health_urls)
            if total and healthy == 0:
                external_pids = self.sshuttle.find_external_proxy_pids()
                if external_pids:
                    status = f"不可用（外部 Proxy 健康检查 0/{total}）"
                else:
                    # We must not silently start a privileged proxy from a
                    # daemon thread: sudo may need a password and has no safe
                    # prompt there.  The menu's 开启/刷新 path collects it in
                    # the terminal, then starts the connector safely.
                    status = "外部 Proxy 已退出（请执行开启/刷新重新建立）"
            elif total and healthy < total:
                status = f"运行中（外部 Proxy，部分健康 {healthy}/{total}）"
            elif total:
                status = f"运行中（外部 Proxy，健康 {healthy}/{total}）"
            else:
                status = "运行中（外部 Proxy，未配置健康检查）"
            self._report_status("proxy", "demand", status, "检查")
            self.next_sshuttle_check = now + self.settings.sshuttle_check_seconds
            return
        if self.sshuttle.is_alive():
            healthy, total = self.sshuttle.check_health(self.settings.proxy_health_urls)
            if total and healthy == 0:
                LOG.warning("sshuttle for %s failed every health probe; restarting", project.name)
                self._report_status(
                    "proxy", "demand", f"不可用（健康检查 0/{total}，正在重启）", "重启"
                )
                self.sshuttle.stop(project)
            else:
                LOG.debug("sshuttle for %s is alive", project.name)
                health = f"（健康 {healthy}/{total}）" if total else ""
                if total and healthy < total:
                    health = f"（部分健康 {healthy}/{total}）"
                self._report_status(
                    "proxy", "demand", f"运行中（Accessor 管理）{health}", "检查"
                )
                self.next_sshuttle_check = now + self.settings.sshuttle_check_seconds
                return
        if not self._project_role_ready(project):
            LOG.warning("sshuttle for %s waits for role %s", project.name, project.depends_on_role)
            self._report_status(
                "proxy", "demand", f"等待权限 {project.depends_on_role}", "等待"
            )
            self.next_sshuttle_check = now + project.restart_delay_seconds
            return
        LOG.info("sshuttle for %s is not running; starting it", project.name)
        # The old reference script refreshed this service profile as a side
        # effect of starting sshuttle. Keep that behavior explicit and owned by
        # Accessor now that no project script is executed.
        with self._credential_write_lock:
            credential_ready = run_project_refresh(self.settings, project)
        self._report_status(
            "project", project.name,
            "有效" if credential_ready else "刷新失败",
            "刷新",
        )
        if not credential_ready:
            self.next_sshuttle_check = now + project.restart_delay_seconds
            self._report_status("proxy", "demand", "启动失败（Proxy 凭证刷新失败）", "重试")
            return
        started = self.sshuttle.start(
            self.settings.proxy, prepare_network=self.settings.prepare_network_before_proxy
        )
        self.next_sshuttle_check = now + (
            self.settings.sshuttle_check_seconds if started else project.restart_delay_seconds
        )
        self._report_status(
            "proxy", "demand", "运行中（Accessor 管理）" if started else "启动失败（将重试）",
            "启动" if started else "重启",
        )
        if started:
            # Starting the connector already refreshed this service credential.
            # Do not run a duplicate refresh immediately.
            self._schedule_credential(project, now, success=True)

    def _initial_refreshes(self, now: float) -> None:
        """Start the tunnel once and refresh all other selected project profiles."""
        self._check_sshuttle(now)
        # The connector refreshes its own service credential when Accessor owns
        # the tunnel. Keep the remaining target work asynchronous.
        if self.proxy_project is not None and self.sshuttle.is_alive():
            with self._schedule_lock:
                self.next_credential_refresh[self.proxy_project.name] = (
                    now + self.proxy_project.credential_refresh_seconds
                )
        self._start_due_projects(now)

    def _sleep(self, now: float) -> None:
        with self._schedule_lock:
            deadlines = [*self.next_role_refresh.values(), *self.next_credential_refresh.values()]
        if self.proxy_project is not None:
            deadlines.append(self.next_sshuttle_check)
        future = [deadline for deadline in deadlines if deadline > now]
        time.sleep(min(5.0, max(0.2, min(future) - now)) if future else 1.0)

    def _record_fatal_error(self, error: BaseException) -> None:
        """Make a background failure visible even when UI logging is muted."""
        message = f"刷新 job 已停止：{type(error).__name__}: {error}"
        self._report_status("job", "refresh", message, "失败")
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
