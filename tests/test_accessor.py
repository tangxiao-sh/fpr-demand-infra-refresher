from __future__ import annotations

from pathlib import Path
import signal
import subprocess
import tempfile
import textwrap
import threading
import unittest
from unittest import mock

import config
import cli
import console
import permissions
import sshuttle
import scheduler

# Activity output is a real user-facing diagnostic file. Keep test events out
# of it so local test runs cannot be mistaken for a running Accessor session.
console.ACTIVITY_LOG = Path(tempfile.gettempdir()) / "accessor-test-activity.log"


class SettingsTest(unittest.TestCase):
    def test_cli_defaults_to_interactive_console(self) -> None:
        self.assertEqual(cli.parse_arguments([]).action, "console")

    @mock.patch("console.RoleRefresher")
    def test_console_stops_before_proxy_when_foreground_role_refresh_fails(
        self, refresher_class: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = config.load_settings(self.write_config(Path(temporary)))
            panel = console.AccessorConsole(settings)
            refresher_class.return_value.refresh.return_value = False
            with mock.patch.object(panel, "_choose_projects"), mock.patch("builtins.input", return_value=""):
                panel.enable_or_refresh()

        self.assertFalse(panel.active)

    @mock.patch("console.prepare_network_before_proxy", return_value=True)
    @mock.patch("console.RefreshScheduler")
    @mock.patch("console.RoleRefresher")
    def test_console_allows_proxy_when_only_build_role_is_unavailable(
        self,
        refresher_class: mock.Mock,
        scheduler_class: mock.Mock,
        _network_prepare: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = config.load_settings(self.write_config(Path(temporary)))
            panel = console.AccessorConsole(settings)
            # The first role (build) fails; the selected project's jump role succeeds.
            refresher_class.return_value.refresh.side_effect = [False, True]
            with mock.patch.object(panel, "_choose_projects"), mock.patch("console.threading.Thread"):
                panel.enable_or_refresh()

        scheduler_class.assert_called_once()

    def test_refresh_job_reports_results_to_console_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = config.load_settings(self.write_config(Path(temporary)))
            panel = console.AccessorConsole(settings)
            panel._update_refresh_status("role", "jump", "有效")
            panel._update_refresh_status("project", "papi", "刷新失败")
            panel._update_refresh_status("proxy", "demand", "运行中（Accessor 管理）")

        self.assertEqual(panel.role_status["jump"], "有效")
        self.assertEqual(panel.project_status["papi"], "刷新失败")
        self.assertEqual(panel.proxy_status, "运行中（Accessor 管理）")

    def test_status_update_records_timestamped_refresh_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = config.load_settings(self.write_config(Path(temporary)))
            panel = console.AccessorConsole(settings)
            panel._update_refresh_status("role", "jump", "有效", "刷新")

        self.assertIn("检查了 jump，状态：有效，行为：刷新", panel.activity[-1])

    @mock.patch("console.check_project_credentials", return_value=(True, "service"))
    @mock.patch("sshuttle.SshuttleProcess.has_pf_sshuttle_anchor", return_value=False)
    @mock.patch("sshuttle.SshuttleProcess.find_external_proxy_pids", return_value=())
    @mock.patch("console.RoleRefresher.check", return_value=True)
    def test_initial_display_check_probes_every_role_and_project(
        self,
        role_check: mock.Mock,
        _external_proxy: mock.Mock,
        _pf_anchor: mock.Mock,
        project_check: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = config.load_settings(self.write_config(Path(temporary)))
            panel = console.AccessorConsole(settings)
            failures = panel._refresh_display_status()

        self.assertEqual(failures, ["Demand Proxy"])
        self.assertEqual(role_check.call_count, len(settings.roles))
        self.assertEqual(project_check.call_count, len(settings.projects))

    @mock.patch("sshuttle.SshuttleProcess.has_pf_sshuttle_anchor", return_value=True)
    @mock.patch("sshuttle.SshuttleProcess.find_external_proxy_pids", return_value=())
    def test_pf_anchor_alone_is_not_reported_as_a_healthy_proxy(
        self, _external_proxy: mock.Mock, _pf_anchor: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = config.load_settings(self.write_config(Path(temporary)))
            panel = console.AccessorConsole(settings)
            status = panel._probe_proxy_status()

        self.assertEqual(status, "疑似残留 PF 路由（proxy 未验证）")
        self.assertFalse(panel._proxy_is_usable(status))

    def test_dynamic_ui_draws_status_before_handling_a_key(self) -> None:
        class FakeScreen:
            def __init__(self) -> None:
                self.added: list[str] = []

            def nodelay(self, _enabled: bool) -> None:
                pass

            def getmaxyx(self) -> tuple[int, int]:
                return 40, 120

            def erase(self) -> None:
                pass

            def addnstr(self, _row: int, _column: int, text: str, _width: int) -> None:
                self.added.append(text)

            def refresh(self) -> None:
                pass

            def clear(self) -> None:
                pass

            def getch(self) -> int:
                return ord("q")

        with tempfile.TemporaryDirectory() as temporary:
            settings = config.load_settings(self.write_config(Path(temporary)))
            panel = console.AccessorConsole(settings)
            screen = FakeScreen()
            with (
                mock.patch.object(panel, "_probe_proxy_status", return_value="已停止"),
                mock.patch.object(panel, "close"),
            ):
                self.assertEqual(panel._run_dynamic_ui(screen), 0)

        self.assertIn("Accessor 控制台", screen.added)

    @mock.patch.object(console.AccessorConsole, "_probe_proxy_status")
    def test_rendering_reads_cached_proxy_status_without_network_probe(
        self, proxy_probe: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = config.load_settings(self.write_config(Path(temporary)))
            panel = console.AccessorConsole(settings)
            panel.proxy_status = "运行中（健康 3/3）"
            with mock.patch.object(panel, "_clear"), mock.patch("builtins.print"):
                panel.show_status()

        proxy_probe.assert_not_called()

    @mock.patch.object(console.AccessorConsole, "_refresh_display_status", return_value=[])
    def test_status_refresh_runs_outside_the_ui_thread(self, refresh: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = config.load_settings(self.write_config(Path(temporary)))
            panel = console.AccessorConsole(settings)
            panel._start_status_refresh()
            assert panel._status_check_thread is not None
            panel._status_check_thread.join(timeout=2)

        self.assertFalse(panel._status_check_thread.is_alive())
        refresh.assert_called_once()

    def write_config(self, directory: Path, extra: str = "") -> Path:
        config_path = directory / "accessor.toml"
        config_path.write_text(
            textwrap.dedent(
                f"""
                [general]
                auto_request = true
                default_projects = ["papi"]
                default_proxy = "papi"
                prepare_network_before_proxy = true
                sshuttle_check_seconds = 300

                [[roles]]
                name = "build"
                profile = "BuildRole@example"

                [[roles]]
                name = "jump"
                profile = "JumpRole@example"

                [[projects]]
                name = "papi"
                service_name = "papi"
                depends_on_role = "jump"
                {extra}

                [[projects]]
                name = "cinv"
                service_name = "cinv"
                depends_on_role = "jump"
                """
            ),
            encoding="utf-8",
        )
        return config_path

    def test_loads_projects_and_selects_one_proxy_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            settings = config.load_settings(self.write_config(directory))
            selected = config.select_projects(settings, ["papi", "cinv"], False)
            proxy = config.select_proxy_project(settings, selected, "papi", False)

        self.assertEqual(settings.sshuttle_check_seconds, 300)
        self.assertTrue(settings.prepare_network_before_proxy)
        self.assertEqual(settings.projects[0].service_name, "papi")
        self.assertEqual([project.name for project in selected], ["papi", "cinv"])
        self.assertEqual(proxy.name, "papi")

    def test_allows_shared_default_proxy_connector_without_selecting_its_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = config.load_settings(self.write_config(Path(temporary)))
            selected = config.select_projects(settings, ["cinv"], False)
            proxy = config.select_proxy_project(settings, selected, "papi", False)

        self.assertEqual(proxy.name, "papi")

    def test_rejects_unknown_role_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            content = self.write_config(directory).read_text(encoding="utf-8")
            (directory / "accessor.toml").write_text(
                content.replace('depends_on_role = "jump"', 'depends_on_role = "missing"'),
                encoding="utf-8",
            )
            with self.assertRaises(config.ConfigError):
                config.load_settings(directory / "accessor.toml")


class PermissionsTest(unittest.TestCase):
    def test_syncs_build_session_into_legacy_gradle_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            aws = home / ".aws"
            aws.mkdir()
            credentials = aws / "credentials"
            credentials.write_text(
                "[BuildRole@example]\n"
                "aws_access_key_id = key\n"
                "aws_secret_access_key = secret\n"
                "aws_session_token = session\n",
                encoding="utf-8",
            )
            role = config.RoleConfig(
                "build", "BuildRole@example", 600, 60, True, ("beiartf",)
            )
            with mock.patch("permissions.Path.home", return_value=home):
                self.assertTrue(permissions.RoleRefresher._sync_credential_aliases(role))

            written = credentials.read_text(encoding="utf-8")

        self.assertIn("[beiartf]", written)
        self.assertIn("aws_session_token = session", written)

    def test_gradle_profile_refreshes_from_its_separate_source_role(self) -> None:
        settings = config.Settings(
            config_path=Path("/tmp/accessor.toml"), auto_request=True,
            request_command=("assume", "--wait", "--export"), command_timeout_seconds=30,
            post_request_delay_seconds=1, prepare_network_before_proxy=True, sshuttle_check_seconds=300,
            lock_file=Path("/tmp/accessor.lock"), default_projects=(), default_proxy=None,
            roles=(), projects=(),
        )
        role = config.RoleConfig(
            "gradle-beiartf", "beiartf", 600, 60, False,
            credential_source_profile="BuildRole@example",
        )
        refresher = permissions.RoleRefresher(settings)
        with (
            mock.patch.object(refresher, "_check_profile", side_effect=[True, True]),
            mock.patch.object(refresher, "_copy_credential_profiles", return_value=True) as copy,
        ):
            self.assertTrue(refresher.refresh(role))

        copy.assert_called_once_with("BuildRole@example", ("beiartf",))
        self.assertEqual(refresher.last_action, "刷新")

    def test_requests_after_exact_profile_attempt_then_rechecks(self) -> None:
        settings = config.Settings(
            config_path=Path("/tmp/accessor.toml"), auto_request=True,
            request_command=("assume", "--wait", "--export"), command_timeout_seconds=30,
            post_request_delay_seconds=1, prepare_network_before_proxy=True, sshuttle_check_seconds=300,
            lock_file=Path("/tmp/accessor.lock"), default_projects=(), default_proxy=None,
            roles=(), projects=(),
        )
        role = config.RoleConfig("build", "BuildRole@example", 600, 60, True)
        with mock.patch("permissions.time.sleep"), mock.patch("permissions.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 1),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0),
            ]
            self.assertTrue(permissions.RoleRefresher(settings).refresh(role))

        self.assertEqual(run.call_args_list[0].args[0][4], "BuildRole@example")
        self.assertEqual(
            tuple(run.call_args_list[1].args[0]),
            ("/bin/zsh", "-ic", "assume --wait --export BuildRole@example"),
        )

    @mock.patch("permissions.subprocess.run")
    def test_background_project_refresh_writes_child_output_to_log(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess([], 0)
        settings = config.Settings(
            config_path=Path("/tmp/accessor.toml"), auto_request=False,
            request_command=(), command_timeout_seconds=30, post_request_delay_seconds=1,
            prepare_network_before_proxy=False, sshuttle_check_seconds=300,
            lock_file=Path("/tmp/accessor.lock"), default_projects=(), default_proxy=None,
            roles=(), projects=(),
        )
        project = self.project(Path("/tmp/example-proxy.py"))

        self.assertTrue(permissions.run_project_refresh(settings, project))

        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.STDOUT)
        self.assertIn("stdout", run.call_args.kwargs)

    @mock.patch("permissions.subprocess.run")
    def test_background_role_request_writes_granted_output_to_log(self, run: mock.Mock) -> None:
        settings = config.Settings(
            config_path=Path("/tmp/accessor.toml"), auto_request=True,
            request_command=("assume", "--wait", "--export"), command_timeout_seconds=30,
            post_request_delay_seconds=1, prepare_network_before_proxy=True, sshuttle_check_seconds=300,
            lock_file=Path("/tmp/accessor.lock"), default_projects=(), default_proxy=None,
            roles=(), projects=(),
        )
        role = config.RoleConfig("build", "BuildRole@example", 600, 60, True)
        run.side_effect = [
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
        ]
        with tempfile.TemporaryDirectory() as temporary, mock.patch("permissions.time.sleep"):
            log = Path(temporary) / "role.log"
            self.assertTrue(
                permissions.RoleRefresher(settings, request_log_path=log).refresh(role)
            )

        self.assertEqual(run.call_args_list[1].kwargs["stderr"], subprocess.STDOUT)
        self.assertIn("stdout", run.call_args_list[1].kwargs)

    @staticmethod
    def project(proxy: Path) -> config.ProjectConfig:
        return config.ProjectConfig(
            name="test-project", description="", service_name="example-service",
            depends_on_role="jump",
            credential_refresh_seconds=2700, credential_retry_seconds=60,
            restart_delay_seconds=10, shutdown_grace_seconds=15,
        )

    @mock.patch("permissions.refresh_service_credentials", return_value="example-service")
    def test_service_refresh_uses_accessor_owned_implementation(self, refresh: mock.Mock) -> None:
        project = self.project(Path("/tmp/unused-project-script.py"))
        settings = config.Settings(
            config_path=Path("/tmp/accessor.toml"), auto_request=False,
            request_command=(), command_timeout_seconds=30, post_request_delay_seconds=1,
            prepare_network_before_proxy=False, sshuttle_check_seconds=300,
            lock_file=Path("/tmp/accessor.lock"), default_projects=(), default_proxy=None,
            roles=(), projects=(project,),
        )
        self.assertEqual(permissions.refresh_project_credentials(settings, project), "example-service")
        refresh.assert_called_once_with(settings, project)


class SshuttleProcessTest(unittest.TestCase):
    @mock.patch("sshuttle.subprocess.run")
    @mock.patch("sshuttle.shutil.which", return_value="/usr/bin/curl")
    def test_health_check_bypasses_http_proxy(self, _which: mock.Mock, run: mock.Mock) -> None:
        run.side_effect = [
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 22),
        ]

        self.assertEqual(
            sshuttle.SshuttleProcess.check_health(("https://one/health", "https://two/health")),
            (1, 2),
        )

        command = run.call_args_list[0].args[0]
        self.assertEqual(command[:7], ["/usr/bin/curl", "--fail", "--silent", "--show-error", "--location", "--noproxy", "*"])

    @mock.patch("sshuttle.subprocess.run")
    def test_detects_root_sshuttle_through_pf_anchor(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="com.apple\nsshuttle-123\nsshuttle6-123\n"
        )

        self.assertTrue(sshuttle.SshuttleProcess.has_pf_sshuttle_anchor())
        self.assertEqual(
            run.call_args.args[0], ["sudo", "-n", "pfctl", "-s", "Anchors"]
        )

    @mock.patch("sshuttle.subprocess.run")
    def test_detects_external_proxy_processes(self, run: mock.Mock) -> None:
        run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout="111\n222\n"),
            subprocess.CompletedProcess([], 0, stdout="222\n333\n"),
        ]

        self.assertEqual(sshuttle.SshuttleProcess.find_external_proxy_pids(), (111, 222, 333))

    @mock.patch("sshuttle.os.kill")
    @mock.patch("sshuttle.time.sleep")
    def test_stops_external_proxy_before_takeover(
        self, _sleep: mock.Mock, kill: mock.Mock
    ) -> None:
        kill.side_effect = [None, ProcessLookupError()]

        remaining = sshuttle.SshuttleProcess().stop_external_proxy((123,))

        self.assertEqual(remaining, ())
        self.assertEqual(kill.call_args_list[0].args, (123, signal.SIGTERM))

    def test_proxy_mapping_accepts_nested_and_record_shapes(self) -> None:
        self.assertEqual(
            sshuttle.SshuttleProcess._mapped_instance_name(
                {"proxy-a": ["fprpapi", "other"]}, "fprpapi"
            ),
            "proxy-a",
        )
        self.assertEqual(
            sshuttle.SshuttleProcess._mapped_instance_name(
                {"mapping": {"proxy-b": {"services": ["fprpapi"]}}}, "fprpapi"
            ),
            "proxy-b",
        )
        self.assertEqual(
            sshuttle.SshuttleProcess._mapped_instance_name(
                [{"instanceName": "proxy-c", "services": ["fprpapi"]}], "fprpapi"
            ),
            "proxy-c",
        )
        self.assertEqual(
            sshuttle.SshuttleProcess._mapped_instance_name(
                {"fprpapi": "proxy-d"}, "fprpapi"
            ),
            "proxy-d",
        )
        self.assertEqual(
            sshuttle.SshuttleProcess._mapped_instance_name(
                {"fprprxy-proxy-demand-01": {"fprpapi": "sg-0123456789abcdef0"}},
                "fprpapi",
            ),
            "fprprxy-proxy-demand-01",
        )
        self.assertIsNone(
            sshuttle.SshuttleProcess._mapped_instance_name(
                {"sg-0123456789abcdef0": ["fprpapi"]}, "fprpapi"
            )
        )

    @mock.patch("sshuttle.shutil.which", return_value="/usr/bin/sshuttle")
    @mock.patch("sshuttle.subprocess.Popen")
    def test_proxy_is_detached_from_the_interactive_console(self, popen: mock.Mock, _which: mock.Mock) -> None:
        manager = sshuttle.SshuttleProcess()
        with mock.patch.object(manager, "_find_proxy_instance", return_value="i-123"):
            self.assertTrue(manager.start(config.ProxyConfig(), prepare_network=False))

        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.STDOUT)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.kwargs["env"]["AWS_REGION"], "ap-southeast-1")
        self.assertEqual(popen.call_args.kwargs["env"]["AWS_DEFAULT_REGION"], "ap-southeast-1")
        if manager.log_handle is not None:
            manager.log_handle.close()

    @mock.patch("sshuttle.subprocess.run")
    def test_network_prepare_uses_one_terminal_prompt_then_noninteractive_sudo(
        self, run: mock.Mock
    ) -> None:
        run.side_effect = [subprocess.CompletedProcess([], 0) for _ in range(4)]

        self.assertTrue(sshuttle.prepare_network_before_proxy())

        self.assertEqual(
            run.call_args_list[0].args[0],
            ["sudo", "-p", "Accessor sudo password: ", "-v"],
        )
        self.assertNotIn("SUDO_ASKPASS", run.call_args_list[0].kwargs["env"])
        self.assertEqual(run.call_args_list[1].args[0], ["sudo", "-n", "dscacheutil", "-flushcache"])
        self.assertEqual(run.call_args_list[3].args[0], ["sudo", "-n", "pfctl", "-f", "/etc/pf.conf"])

    @mock.patch("sshuttle.subprocess.run")
    def test_network_prepare_accepts_a_hidden_ui_password(self, run: mock.Mock) -> None:
        run.side_effect = [subprocess.CompletedProcess([], 0) for _ in range(4)]

        self.assertTrue(sshuttle.prepare_network_before_proxy(lambda: "not-logged"))

        self.assertEqual(run.call_args_list[0].args[0], ["sudo", "-S", "-p", "", "-v"])
        self.assertEqual(run.call_args_list[0].kwargs["input"], "not-logged\n")
        self.assertEqual(run.call_args_list[0].kwargs["stderr"], subprocess.PIPE)
        self.assertEqual(run.call_args_list[1].kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(run.call_args_list[3].kwargs["stderr"], subprocess.PIPE)

    @mock.patch("sshuttle.subprocess.run")
    def test_network_prepare_retries_a_rejected_ui_password(self, run: mock.Mock) -> None:
        run.side_effect = [
            subprocess.CompletedProcess([], 1, stderr="Sorry, try again."),
            *[subprocess.CompletedProcess([], 0) for _ in range(4)],
        ]
        passwords = mock.Mock(side_effect=("wrong", "correct"))

        self.assertTrue(sshuttle.prepare_network_before_proxy(passwords))

        self.assertEqual(passwords.call_count, 2)
        self.assertEqual(run.call_args_list[0].kwargs["input"], "wrong\n")
        self.assertEqual(run.call_args_list[1].kwargs["input"], "correct\n")

    @mock.patch("sshuttle.subprocess.run")
    def test_network_prepare_allows_four_password_retries(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess([], 1, stderr="Sorry, try again.")
        passwords = mock.Mock(return_value="wrong")

        self.assertFalse(sshuttle.prepare_network_before_proxy(passwords))

        self.assertEqual(passwords.call_count, 5)
        self.assertIn("已重试 4 次", sshuttle.network_prepare_error() or "")

    def test_liveness_is_just_a_process_poll(self) -> None:
        manager = sshuttle.SshuttleProcess()
        process = mock.Mock()
        process.poll.return_value = None
        manager.process = process
        self.assertTrue(manager.is_alive())
        process.poll.return_value = 1
        self.assertFalse(manager.is_alive())


class SchedulerThreadTest(unittest.TestCase):
    def test_unhealthy_external_proxy_is_taken_over_and_restarted(self) -> None:
        project = config.ProjectConfig(
            name="fprpapi", description="", service_name="fprpapi",
            depends_on_role=None, credential_refresh_seconds=2700,
            credential_retry_seconds=60, restart_delay_seconds=10, shutdown_grace_seconds=15,
        )
        settings = config.Settings(
            config_path=Path("/tmp/accessor.toml"), auto_request=False,
            request_command=(), command_timeout_seconds=30, post_request_delay_seconds=1,
            prepare_network_before_proxy=False, sshuttle_check_seconds=300,
            lock_file=Path("/tmp/accessor.lock"), default_projects=("fprpapi",),
            default_proxy="fprpapi", roles=(), projects=(project,),
            proxy_health_urls=("https://private/health",),
        )
        worker = scheduler.RefreshScheduler(settings, (project,), project, manage_proxy=False)
        worker.role_ready = {}
        worker.sshuttle.check_health = mock.Mock(return_value=(0, 1))
        worker.sshuttle.find_external_proxy_pids = mock.Mock(return_value=(123,))
        worker.sshuttle.stop_external_proxy = mock.Mock(return_value=())
        worker.sshuttle.is_alive = mock.Mock(return_value=False)
        worker.sshuttle.start = mock.Mock(return_value=True)

        worker._check_sshuttle(100.0)

        self.assertTrue(worker.manage_proxy)
        worker.sshuttle.stop_external_proxy.assert_called_once_with((123,))
        worker.sshuttle.start.assert_called_once_with(settings.proxy, prepare_network=False)

    def test_proxy_start_does_not_delay_connector_first_refresh(self) -> None:
        project = config.ProjectConfig(
            name="fprpapi",
            description="",
            service_name="fprpapi",
            depends_on_role=None,
            credential_refresh_seconds=2700,
            credential_retry_seconds=60,
            restart_delay_seconds=10,
            shutdown_grace_seconds=15,
        )
        settings = config.Settings(
            config_path=Path("/tmp/accessor.toml"), auto_request=False,
            request_command=(), command_timeout_seconds=30, post_request_delay_seconds=1,
            prepare_network_before_proxy=False, sshuttle_check_seconds=300,
            lock_file=Path("/tmp/accessor.lock"), default_projects=("fprpapi",),
            default_proxy="fprpapi", roles=(), projects=(project,),
        )
        worker = scheduler.RefreshScheduler(settings, (project,), project)
        with (
            mock.patch.object(worker, "_check_sshuttle"),
            mock.patch.object(worker, "_start_due_projects") as start_projects,
        ):
            worker.sshuttle.is_alive = mock.Mock(return_value=True)
            worker._initial_refreshes(123.0)

        self.assertEqual(worker.next_credential_refresh["fprpapi"], 0.0)
        start_projects.assert_called_once()

    def test_scheduler_keeps_auto_role_renewal_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = config.Settings(
                config_path=Path(temporary) / "accessor.toml",
                auto_request=True,
                request_command=("assume", "--wait", "--export"),
                command_timeout_seconds=30,
                post_request_delay_seconds=1,
                prepare_network_before_proxy=False,
                sshuttle_check_seconds=300,
                lock_file=Path(temporary) / "accessor.lock",
                default_projects=(),
                default_proxy=None,
                roles=(),
                projects=(),
            )
            worker = scheduler.RefreshScheduler(settings, (), None)

        self.assertTrue(worker.roles.settings.auto_request)

    def test_background_scheduler_does_not_register_signal_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = config.Settings(
                config_path=Path(temporary) / "accessor.toml",
                auto_request=False,
                request_command=(),
                command_timeout_seconds=30,
                post_request_delay_seconds=1,
                prepare_network_before_proxy=False,
                sshuttle_check_seconds=300,
                lock_file=Path(temporary) / "accessor.lock",
                default_projects=(),
                default_proxy=None,
                roles=(),
                projects=(),
            )
            worker = scheduler.RefreshScheduler(settings, (), None)
            worker.request_stop()
            thread = threading.Thread(target=worker.run)
            thread.start()
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
