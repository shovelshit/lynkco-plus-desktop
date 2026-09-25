"""Native login gates every browser session and clears it on lock."""

import unittest
import json
import inspect
import os
import tempfile
import threading
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock


class NativeSessionTests(unittest.TestCase):
    def setUp(self):
        from desktop.native_auth import NativeSession

        self.controller = Mock()
        self.controller.has_license.return_value = True
        self.server = Mock()
        self.server.issue_browser_session.return_value = 'fresh-secret'
        self.server.browser_last_activity.return_value = 100.0
        self.session = NativeSession(self.controller, self.server, 'http://127.0.0.1:8765/')

    def test_login_verifies_before_issuing_browser_session(self):
        events = []
        self.controller.unlock.side_effect = lambda code: events.append('verify') or {'userId': 'u'}
        self.server.issue_browser_session.side_effect = lambda: events.append('issue') or 'fresh-secret'

        url = self.session.unlock('my-code')

        self.assertEqual(events, ['verify', 'issue'])
        self.assertEqual(url, 'http://127.0.0.1:8765/#fresh-secret')

    def test_bad_code_never_issues_browser_session(self):
        self.controller.unlock.side_effect = ValueError('登录码错误')
        with self.assertRaisesRegex(ValueError, '登录码错误'):
            self.session.unlock('wrong')
        self.server.issue_browser_session.assert_not_called()

    def test_lock_revokes_browser_before_discarding_identity(self):
        events = []
        self.server.lock_browser_session.side_effect = lambda: events.append('revoke')
        self.controller.lock_identity.side_effect = lambda: events.append('stop')
        self.session.lock()
        self.assertEqual(events, ['revoke', 'stop'])

    def test_close_stops_desktop_proxy_before_locking_identity(self):
        events = []
        self.controller.force_stop_proxy.side_effect = lambda: events.append('proxy') or {
            'stopped': True, 'phoneMustDisable': True}
        self.server.lock_browser_session.side_effect = lambda: events.append('revoke')
        self.controller.lock_identity.side_effect = lambda: events.append('identity')
        result = self.session.close()
        self.assertEqual(events, ['proxy', 'revoke', 'identity'])
        self.assertEqual(result['phoneMustDisable'], True)

    def test_close_window_error_does_not_claim_phone_proxy_was_running(self):
        from desktop.native_auth import run_window

        self.assertIn("result = {'phoneMustDisable': False}", inspect.getsource(run_window))

    def test_idle_timeout_revokes_browser_access(self):
        self.session.unlock('my-code')
        self.assertFalse(self.session.lock_if_idle(100.0 + 30 * 60 - 1))
        self.assertTrue(self.session.lock_if_idle(100.0 + 30 * 60))
        self.server.lock_browser_session.assert_called_once()
        self.controller.lock_identity.assert_called_once()

    def test_claim_requires_acknowledgement_before_unlock(self):
        self.controller.claim.return_value = {'loginCode': 'new-code'}
        self.assertEqual(self.session.claim('invite'), 'new-code')
        self.server.issue_browser_session.assert_not_called()
        self.controller.unlock.assert_not_called()

    def test_reset_revokes_existing_browser_session(self):
        self.session.unlock('old-code')
        self.controller.reset.return_value = {'loginCode': 'new-code'}
        self.assertEqual(self.session.reset('old-reset-code-1234', 'L1234567890123456'), 'new-code')
        self.server.lock_browser_session.assert_called_once()
        self.assertIsNone(self.session.browser_url)

    def test_reset_passes_vin_as_second_verification_factor(self):
        self.controller.reset.return_value = {'loginCode': 'new-code'}
        self.session.reset('reset-code', 'L1234567890123456')
        self.controller.reset.assert_called_once_with('reset-code', 'L1234567890123456')

    def test_repeated_launcher_cannot_use_stale_browser_url(self):
        from desktop.launcher import running_instance

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'instance.json'
            path.write_text(json.dumps({'pid': os.getpid(), 'port': 8765, 'url': 'http://127.0.0.1:8765/#old'}))
            self.assertTrue(running_instance(path))
            self.assertEqual(json.loads(path.read_text()), {'pid': os.getpid(), 'port': 8765})

    def test_login_url_is_not_persisted_in_instance_record(self):
        from desktop.launcher import instance_record

        record = instance_record(8765)
        self.assertEqual(record['port'], 8765)
        self.assertNotIn('url', record)
        self.assertNotIn('fresh-secret', json.dumps(record))

    def test_bootstrap_reuses_locked_instance_without_opening_browser(self):
        from desktop.bootstrap import existing_instance_url

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'instance.json'
            path.write_text(json.dumps({'pid': os.getpid(), 'port': 8765, 'releaseSha': 'release'}))
            self.assertTrue(existing_instance_url(Path(directory), 'release'))
            self.assertNotIn('url', path.read_text())


class ManualExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        future = Future()
        self.jobs.append((fn, args, future))
        return future

    def complete(self):
        fn, args, future = self.jobs.pop(0)
        try:
            future.set_result(fn(*args))
        except Exception as error:
            future.set_exception(error)


class LoginFlowTests(unittest.TestCase):
    def setUp(self):
        from desktop.native_auth import LoginFlow

        self.session = Mock()
        self.session.controller.has_license.return_value = True
        self.view = Mock()
        self.executor = ManualExecutor()
        self.scheduled = []
        self.opened = []
        self.flow = LoginFlow(self.session, self.view, self.opened.append,
                              lambda callback: self.scheduled.append(callback), self.executor)

    def test_default_tab_depends_on_local_license(self):
        from desktop.native_auth import LoginFlow

        self.assertEqual(self.flow.screen, 'login')
        fresh_session = Mock()
        fresh_session.controller.has_license.return_value = False
        fresh_view = Mock()
        LoginFlow(fresh_session, fresh_view, lambda _: None, lambda _: None, ManualExecutor())
        fresh_view.show_screen.assert_called_once_with('claim')

    def flush_ui(self):
        while self.scheduled:
            self.scheduled.pop(0)()

    def test_login_runs_off_ui_thread_then_opens_browser_on_scheduled_completion(self):
        self.flow.submit('my-code')
        self.view.set_busy.assert_called_with(True)
        self.session.unlock.assert_not_called()
        self.executor.complete()
        self.assertEqual(self.opened, [])
        self.flush_ui()
        self.session.unlock.assert_called_once_with('my-code')
        self.assertEqual(self.opened, [self.session.unlock.return_value])
        self.view.set_busy.assert_called_with(False)

    def test_login_switches_to_reopenable_running_surface(self):
        self.flow.submit('my-code')
        self.executor.complete()
        self.flush_ui()
        self.view.show_running.assert_called_once()
        reopen = self.view.show_running.call_args.args[0]
        reopen()
        self.assertEqual(self.opened, [self.session.unlock.return_value] * 2)

    def test_claim_shows_code_once_and_only_unlocks_after_confirmation(self):
        self.flow.show_screen('claim')
        self.session.claim.return_value = 'one-time-code'
        self.flow.submit('invite')
        self.executor.complete()
        self.flush_ui()
        self.view.show_code.assert_called_once()
        shown, on_saved = self.view.show_code.call_args.args
        self.assertEqual(shown, 'one-time-code')
        self.session.unlock.assert_not_called()
        on_saved()
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.complete()
        self.flush_ui()
        self.session.unlock.assert_called_once_with('one-time-code')

    def test_recovery_opens_a_modal_without_replacing_the_main_screen(self):
        self.flow.show_recovery()
        self.assertEqual(self.flow.screen, 'reset')
        self.view.show_recovery_dialog.assert_called_once_with()

    def test_reset_and_error_keep_browser_locked(self):
        self.flow.show_screen('reset')
        self.session.reset.side_effect = ValueError('登录码错误')
        self.flow.submit('wrong', 'L1234567890123456')
        self.executor.complete()
        self.flush_ui()
        self.view.show_notice.assert_called_with('登录码错误')
        self.session.unlock.assert_not_called()
        self.view.set_busy.assert_called_with(False)

    def test_idle_relocks_and_returns_to_login(self):
        self.session.lock_if_idle.return_value = True
        self.flow.check_idle()
        self.view.show_screen.assert_called_with('login')
        self.view.show_notice.assert_called_with('长时间未操作，请重新登录')

    def test_closed_window_drops_late_cloud_result(self):
        self.flow.submit('code')
        self.flow.closed = True
        self.executor.complete()
        self.flush_ui()
        self.view.show_code.assert_not_called()
        self.assertEqual(self.opened, [])

    def test_daemon_cloud_worker_does_not_hold_process_open(self):
        from desktop.native_auth import DaemonExecutor

        release = threading.Event()
        running = threading.Event()

        def wait_for_network():
            running.set()
            release.wait(2)

        future = DaemonExecutor().submit(wait_for_network)
        self.assertTrue(running.wait(1))
        workers = [thread for thread in threading.enumerate() if thread.name.startswith('lynkco-auth')]
        self.assertTrue(workers)
        self.assertTrue(all(thread.daemon for thread in workers))
        release.set()
        self.assertIsNone(future.result(timeout=1))

    def test_worker_posts_view_change_to_tk_main_loop_without_touching_tk(self):
        from desktop.native_auth import TkDispatcher

        root = Mock()
        dispatcher = TkDispatcher(root)
        callback = root.after.call_args.args[1]
        self.view.show_notice.reset_mock()
        worker = threading.Thread(target=lambda: dispatcher.schedule(lambda: self.view.show_notice('done')))
        worker.start()
        worker.join()
        self.assertEqual(root.after.call_count, 1)
        self.view.show_notice.assert_not_called()
        callback()
        self.view.show_notice.assert_called_once_with('done')
        dispatcher.close()
        dispatcher.schedule(lambda: self.view.show_notice('late'))
        self.assertEqual(self.view.show_notice.call_count, 1)

    def test_empty_submission_uses_screen_specific_prompt(self):
        self.flow.show_screen('reset')
        self.flow.submit('   ')
        self.view.show_notice.assert_called_with('请输入重置码')
        self.flow.show_screen('login')
        self.flow.submit('')
        self.view.show_notice.assert_called_with('请输入登录码')


class NativeWindowTests(unittest.TestCase):
    def test_native_window_uses_windows_safe_theme_and_font(self):
        import inspect
        import desktop.native_auth as native_auth

        self.assertTrue(hasattr(native_auth, 'NATIVE_THEME'))
        self.assertTrue(hasattr(native_auth, 'NATIVE_FONT_FAMILY'))
        self.assertIn('text', native_auth.NATIVE_THEME)
        self.assertIn('muted', native_auth.NATIVE_THEME)
        self.assertIn('accent', native_auth.NATIVE_THEME)
        self.assertIn('accent_hover', native_auth.NATIVE_THEME)
        self.assertIn('enable_windows_dpi_awareness', inspect.getsource(native_auth.run_window))

    def test_native_window_keeps_controls_inside_content_at_minimum_size(self):
        import inspect
        from desktop.native_auth import TkLoginView

        source = inspect.getsource(TkLoginView.__init__)
        self.assertIn("content.place(relx=.5, rely=0, anchor='n', relwidth=.82, relheight=1)", source)

    def test_login_window_uses_preview_tabs_and_static_footer(self):
        import customtkinter as ctk
        from desktop.native_auth import TkLoginView

        root = ctk.CTk()
        root.withdraw()
        try:
            view = TkLoginView(root)
            self.assertEqual(view.login_tab.cget('text'), '登录')
            self.assertEqual(view.claim_tab.cget('text'), '首次领取')
            self.assertEqual(view.footer_label.cget('text'), '安全登录 · 开发版 · GitHub @shovelshit')
            self.assertEqual(view.entry.cget('placeholder_text'), '请输入登录码')
            view.show_screen('login')
            self.assertEqual(view.recover.cget('text'), '恢复登录码')
            view.flow = Mock()
            view.recover.invoke()
            view.flow.show_recovery.assert_called_once_with()

            view.show_screen('claim')
            self.assertEqual(view.entry.cget('placeholder_text'), '请输入邀请码')
            view.show_screen('reset')
            self.assertEqual(view.entry.cget('placeholder_text'), '请输入重置码')
            self.assertEqual(view.label.cget('text'), '重置码')
        finally:
            root.destroy()

    def test_recovery_dialog_submits_and_closes_as_a_modal(self):
        import customtkinter as ctk
        from desktop.native_auth import TkLoginView

        root = ctk.CTk()
        root.withdraw()
        try:
            view = TkLoginView(root)
            view.flow = Mock()
            view.show_recovery_dialog()
            dialog = view.modal
            self.assertEqual((dialog._current_width, dialog._current_height), (520, 410))
            self.assertEqual(view.recovery_entry.cget('placeholder_text'), '重置码')
            self.assertEqual(view.recovery_vin_entry.cget('placeholder_text'), '车架号（17 位）')
            view.recovery_entry.insert(0, 'recovery-value')
            view.recovery_vin_entry.insert(0, 'L1234567890123456')
            view.recovery_action.invoke()
            view.flow.submit.assert_called_once_with('recovery-value', 'L1234567890123456')
            view.flow.screen = 'reset'
            view.close_recovery_dialog()
            self.assertIsNone(view.modal)
            self.assertFalse(dialog.winfo_exists())
            self.assertEqual(view.flow.screen, 'login')
        finally:
            root.destroy()

    def test_login_tabs_use_larger_readable_type(self):
        import customtkinter as ctk
        from desktop.native_auth import TkLoginView

        root = ctk.CTk()
        root.withdraw()
        try:
            view = TkLoginView(root)
            self.assertEqual(view.login_tab.cget('font').cget('size'), 16)
            self.assertEqual(view.claim_tab.cget('font').cget('size'), 16)
        finally:
            root.destroy()

    def test_login_window_uses_compact_geometry(self):
        import customtkinter as ctk
        from desktop.native_auth import TkLoginView

        root = ctk.CTk()
        root.withdraw()
        try:
            TkLoginView(root)
            self.assertEqual((root._current_width, root._current_height), (640, 600))
            self.assertEqual((root._min_width, root._min_height), (600, 560))
        finally:
            root.destroy()

    def test_tabs_and_form_share_content_column_without_overlap(self):
        import customtkinter as ctk
        from desktop.native_auth import TkLoginView

        root = ctk.CTk()
        root.withdraw()
        try:
            view = TkLoginView(root)
            view.show_screen('login')
            root.deiconify()
            root.update_idletasks()
            view.content.update_idletasks()
            self.assertEqual(view.navigation.winfo_x(), view.form.winfo_x())
            self.assertEqual(view.navigation.winfo_width(), view.form.winfo_width())
            navigation_bottom = view.navigation.winfo_rooty() + view.navigation.winfo_height()
            self.assertGreaterEqual(view.form.winfo_rooty(), navigation_bottom + 40)
            self.assertEqual(view.tab_indicator.winfo_x(), view.login_tab.winfo_x())
            view.show_screen('claim')
            root.update_idletasks()
            self.assertEqual(view.tab_indicator.winfo_x(), view.claim_tab.winfo_x())
        finally:
            root.destroy()

    def test_login_controls_have_round_corners(self):
        import customtkinter as ctk
        from desktop.native_auth import TkLoginView

        root = ctk.CTk()
        root.withdraw()
        try:
            view = TkLoginView(root)
            for widget in (view.entry, view.action, view.recover, view.progress):
                self.assertEqual(widget.cget('corner_radius'), 12)
            self.assertEqual(view.reveal.cget('corner_radius'), 4)
        finally:
            root.destroy()

    def test_running_surface_keeps_window_visible_and_reopens_backend(self):
        import customtkinter as ctk
        from desktop.native_auth import TkLoginView

        root = ctk.CTk()
        root.withdraw()
        try:
            view = TkLoginView(root)
            opened = []
            view.show_running(lambda: opened.append(True))
            root.deiconify()
            root.update_idletasks()
            self.assertTrue(root.winfo_viewable())
            self.assertEqual(view.running_title.cget('text'), '客户端运行中')
            view.running_open.invoke()
            self.assertEqual(opened, [True])
            view.show_screen('login')
            self.assertFalse(view.running.winfo_ismapped())
        finally:
            root.destroy()

    def test_busy_state_can_start_and_stop_progress(self):
        import customtkinter as ctk
        from desktop.native_auth import TkLoginView

        root = ctk.CTk()
        root.withdraw()
        try:
            view = TkLoginView(root)
            view.set_busy(True)
            view.set_busy(False)
            self.assertEqual(view.progress.cget('mode'), 'indeterminate')
        finally:
            root.destroy()

    def test_pending_one_time_code_blocks_window_close_and_shortcut(self):
        from desktop.native_auth import TkLoginView

        view = TkLoginView.__new__(TkLoginView)
        view.root = Mock()
        view.modal = Mock()
        self.assertEqual(view.request_close(), 'break')
        view.root.destroy.assert_not_called()
        view.modal.lift.assert_called_once()
        view.modal = None
        self.assertEqual(view.request_close(), 'break')
        view.root.destroy.assert_called_once()

    def test_window_close_uses_close_handler_when_no_modal_is_open(self):
        from desktop.native_auth import TkLoginView

        view = TkLoginView.__new__(TkLoginView)
        view.root = Mock()
        view.modal = None
        closed = []
        view.close_handler = lambda: closed.append(True)
        self.assertEqual(view.request_close(), 'break')
        self.assertEqual(closed, [True])
        view.root.destroy.assert_not_called()

    def test_actual_one_time_code_dialog_requires_acknowledgement(self):
        import customtkinter as ctk
        from desktop.native_auth import TkLoginView

        root = ctk.CTk()
        root.withdraw()
        try:
            view = TkLoginView(root)
            root.update()
            saved = []
            view.show_code('one-time', lambda: saved.append(True))
            dialog = view.modal
            self.assertEqual((dialog._current_width, dialog._current_height), (520, 320))
            self.assertEqual(view.modal_content.cget('corner_radius'), 12)
            view.request_close()
            self.assertTrue(root.winfo_exists())
            self.assertTrue(dialog.winfo_exists())
            self.assertEqual(saved, [])

            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)

            checkbox = next(child for child in descendants(dialog) if isinstance(child, ctk.CTkCheckBox))
            complete = next(child for child in descendants(dialog)
                            if isinstance(child, ctk.CTkButton) and child.cget('text') == '完成并打开后台')
            copy = next(child for child in descendants(dialog)
                        if isinstance(child, ctk.CTkButton) and child.cget('text') == '复制')
            self.assertEqual(complete.cget('state'), 'disabled')
            self.assertEqual(copy.cget('corner_radius'), 12)
            self.assertEqual(complete.cget('corner_radius'), 12)
            self.assertEqual(checkbox.cget('corner_radius'), 4)
            checkbox.toggle()
            complete.invoke()
            self.assertEqual(saved, [True])
            self.assertIsNone(view.modal)
        finally:
            root.destroy()

    def test_shutdown_poll_destroys_tk_on_main_loop(self):
        from desktop.native_auth import watch_shutdown

        root = Mock()
        stopped = threading.Event()
        watch_shutdown(root, stopped)
        callback = root.after.call_args.args[1]
        callback()
        root.destroy.assert_not_called()
        stopped.set()
        callback = root.after.call_args.args[1]
        callback()
        root.destroy.assert_called_once()

    def test_shutdown_poll_uses_close_callback(self):
        from desktop.native_auth import watch_shutdown

        root = Mock()
        stopped = threading.Event()
        closed = []
        watch_shutdown(root, stopped, lambda: closed.append(True))
        callback = root.after.call_args.args[1]
        stopped.set()
        callback()
        self.assertEqual(closed, [True])
        root.destroy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
