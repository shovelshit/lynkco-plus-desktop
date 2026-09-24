"""Native account window and the lifecycle of its browser session."""

import time
import signal
import threading
import webbrowser
import os
import sys
from pathlib import Path
from concurrent.futures import Future
from queue import Empty, SimpleQueue


IDLE_SECONDS = 30 * 60
FIRST_RUN_NOTICE_MARKER = 'first-run-notice-v1'
FIRST_RUN_NOTICE = '该软件免费，邀请码也免费。\n如果你花钱了，那么恭喜你被骗了。'


def first_run_notice_needed(state_dir):
    return not (Path(state_dir) / FIRST_RUN_NOTICE_MARKER).is_file()


def mark_first_run_notice(state_dir):
    path = Path(state_dir) / FIRST_RUN_NOTICE_MARKER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('acknowledged\n', encoding='utf-8')
    path.chmod(0o600)


class NativeSession:
    def __init__(self, controller, server, base_url):
        self.controller = controller
        self.server = server
        self.base_url = base_url
        self.browser_url = None

    def unlock(self, login_code):
        self.controller.unlock(login_code)
        token = self.server.issue_browser_session()
        self.browser_url = self.base_url + '#' + token
        return self.browser_url

    def claim(self, invite_code):
        return self.controller.claim(invite_code)['loginCode']

    def reset(self, reset_code, vin):
        result = self.controller.reset(reset_code, vin)['loginCode']
        self.server.lock_browser_session()
        self.browser_url = None
        return result

    def lock(self):
        self.server.lock_browser_session()
        self.browser_url = None
        self.controller.lock_identity()

    def close(self):
        """Stop the desktop proxy before the native client window exits."""
        result = self.controller.force_stop_proxy()
        self.lock()
        return result

    def lock_if_idle(self, now=None):
        if self.browser_url is None:
            return False
        if (time.monotonic() if now is None else now) - self.server.browser_last_activity() < IDLE_SECONDS:
            return False
        self.lock()
        return True


class DaemonExecutor:
    def submit(self, action, *args):
        result = Future()

        def run():
            try:
                result.set_result(action(*args))
            except BaseException as error:
                result.set_exception(error)

        threading.Thread(target=run, name='lynkco-auth', daemon=True).start()
        return result


class TkDispatcher:
    def __init__(self, root):
        self.root = root
        self.pending = SimpleQueue()
        self.closed = False
        root.after(50, self._pump)

    def schedule(self, callback):
        if not self.closed:
            self.pending.put(callback)

    def _pump(self):
        if self.closed:
            return
        while True:
            try:
                callback = self.pending.get_nowait()
            except Empty:
                break
            callback()
        self.root.after(50, self._pump)

    def close(self):
        self.closed = True


class LoginFlow:
    """Keep network work off Tk's event loop and publish results on its scheduler."""

    def __init__(self, session, view, open_browser, schedule, executor):
        self.session = session
        self.view = view
        self.open_browser = open_browser
        self.schedule = schedule
        self.executor = executor
        self.screen = None
        self.busy = False
        self.closed = False
        self.show_screen('login' if session.controller.has_license() else 'claim')

    def show_screen(self, screen):
        if self.busy or screen not in ('login', 'claim', 'reset'):
            return
        self.screen = screen
        self.view.show_screen(screen)
        self.view.show_notice('')

    def show_recovery(self):
        if self.busy or self.closed:
            return
        self.screen = 'reset'
        self.view.show_recovery_dialog()
        self.view.show_notice('')

    def submit(self, value, vin=None):
        if self.busy or self.closed:
            return
        value = value.strip()
        if not value:
            self.view.show_notice({
                'login': '请输入登录码',
                'claim': '请输入邀请码',
                'reset': '请输入重置码',
            }.get(self.screen, '请输入内容'))
            return
        if self.screen == 'reset' and not (vin or '').strip():
            self.view.show_notice('请输入车架号')
            return
        self.busy = True
        self.view.set_busy(True)
        self.view.show_notice('')
        action = {'login': self.session.unlock, 'claim': self.session.claim,
                  'reset': self.session.reset}[self.screen]
        screen = self.screen
        future = self.executor.submit(action, value, vin.strip()) if screen == 'reset' else self.executor.submit(action, value)
        future.add_done_callback(lambda finished: self.schedule(lambda: self._finish(screen, finished)))

    def _finish(self, screen, future):
        if self.closed:
            return
        self.busy = False
        self.view.set_busy(False)
        try:
            result = future.result()
        except ValueError as error:
            self.view.show_notice(str(error))
            return
        except Exception:
            self.view.show_notice('操作失败，请稍后重试')
            return
        self.view.clear_input()
        if screen == 'login':
            self.open_browser(result)
            # Keep the native window available as a small control surface. The
            # browser may be closed and reopened with the same issued session.
            show_running = getattr(self.view, 'show_running', None)
            if show_running is not None:
                show_running(lambda: self.open_browser(result))
        else:
            if screen == 'reset':
                self.view.close_recovery_dialog()
            self.view.show_code(result, lambda: self._confirm_code(result))

    def _confirm_code(self, code):
        if self.closed:
            return
        self.show_screen('login')
        self.submit(code)

    def check_idle(self):
        if not self.closed and self.session.lock_if_idle():
            self.show_screen('login')
            self.view.show_notice('长时间未操作，请重新登录')


class TkLoginView:
    screens = {
        'login': ('账号访问', '登录领+', '输入登录码，验证后打开后台。', '登录码', '验证并打开后台'),
        'claim': ('新用户', '首次领取', '输入管理员提供的邀请码，在这台电脑上领取领+。', '邀请码', '领取并登录'),
        'reset': ('账号恢复', '恢复登录', '输入管理员提供的重置码和车架号。', '重置码', '验证并重置登录码'),
    }

    def __init__(self, root):
        import tkinter as tk
        import customtkinter as ctk

        self.root, self.tk, self.ctk = root, tk, ctk
        self.flow = None
        self.modal = None
        self.modal_kind = None
        self.modal_content = None
        self.close_handler = None
        root.title('领+ · 桌面客户端')
        root.geometry('620x560')
        root.minsize(560, 520)
        root.configure(fg_color='#f7faf8')
        font = ctk.CTkFont(size=14)
        heading = ctk.CTkFont(size=15, weight='bold')
        title_font = ctk.CTkFont(size=27, weight='bold')

        header = ctk.CTkFrame(root, fg_color='#ffffff', corner_radius=0, height=82)
        header.pack(fill='x')
        header.pack_propagate(False)
        ctk.CTkLabel(header, text='领+', fg_color='#16664e', text_color='#ffffff',
                     corner_radius=9, font=heading, width=40, height=40).pack(side='left', padx=24, pady=20)
        ctk.CTkLabel(header, text='桌面客户端', text_color='#687672', font=font).pack(side='right', padx=24)
        ctk.CTkFrame(root, fg_color='#dce4e0', corner_radius=0, height=1).pack(fill='x')

        body = ctk.CTkFrame(root, fg_color='#f7faf8', corner_radius=0)
        body.pack(fill='both', expand=True)
        # Keep navigation and form in one content column so the tabs do not
        # read as a separate full-width header or overlap the form on compact windows.
        content = ctk.CTkFrame(body, fg_color='#f7faf8', corner_radius=0)
        self.content = content
        content.place(relx=.5, rely=0, anchor='n', relwidth=.82, relheight=1)
        navigation = ctk.CTkFrame(content, fg_color='#f7faf8', corner_radius=0, height=60)
        self.navigation = navigation
        navigation.pack_propagate(False)
        navigation.pack(fill='x', pady=(20, 0))
        self.login_tab = ctk.CTkButton(
            navigation, text='登录', width=58, height=38, corner_radius=0,
            fg_color='transparent', hover_color='#eef5f0', text_color='#687672',
            font=font, command=lambda: self._select_tab('login'))
        self.login_tab.pack(side='left', padx=(0, 54))
        self.claim_tab = ctk.CTkButton(
            navigation, text='首次领取', width=88, height=38, corner_radius=0,
            fg_color='transparent', hover_color='#eef5f0', text_color='#687672',
            font=font, command=lambda: self._select_tab('claim'))
        self.claim_tab.pack(side='left')
        ctk.CTkFrame(navigation, fg_color='#d8e0dc', corner_radius=0, height=1).place(
            relx=0, rely=1, relwidth=1, anchor='sw')
        self.tab_indicator = ctk.CTkFrame(navigation, fg_color='#16664e', corner_radius=1,
                                          width=58, height=3)
        self.tab_indicator.place(x=0, y=57)

        form = ctk.CTkFrame(content, fg_color='#f7faf8', corner_radius=0)
        form.pack(fill='x', pady=(40, 0))
        self.form = form
        form.grid_columnconfigure(0, weight=1)
        self.eyebrow = ctk.CTkLabel(form, text='', text_color='#a66e28', font=heading)
        self.eyebrow.grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 16))
        self.eyebrow.grid_remove()
        self.title = ctk.CTkLabel(form, text='', text_color='#202d2b', font=title_font)
        self.title.grid(row=1, column=0, columnspan=2, sticky='w', pady=(0, 8))
        self.description = ctk.CTkLabel(form, text='', text_color='#687672', font=font, wraplength=450)
        self.description.grid(row=2, column=0, columnspan=2, sticky='w', pady=(0, 32))
        self.label = ctk.CTkLabel(form, text='', text_color='#202d2b', font=font)
        self.label.grid(row=3, column=0, columnspan=2, sticky='w', pady=(0, 8))
        field = ctk.CTkFrame(form, fg_color='transparent', corner_radius=0, height=50)
        field.grid(row=4, column=0, columnspan=2, sticky='ew')
        field.grid_propagate(False)
        self.entry = ctk.CTkEntry(field, show='*', corner_radius=12, height=50,
                                  border_width=2, border_color='#16664e', fg_color='#ffffff',
                                  placeholder_text='请输入登录码', font=font)
        self.entry.pack(fill='both', expand=True)
        self.entry.bind('<Return>', lambda _: self.flow.submit(self.entry.get()))
        self.reveal_var = tk.BooleanVar(value=False)
        self.reveal = ctk.CTkCheckBox(field, text='显示', variable=self.reveal_var,
                                      command=self._toggle_reveal, fg_color='#16664e',
                                      corner_radius=4, checkbox_width=0, checkbox_height=0,
                                      border_width=0, font=font, width=50, height=30)
        self.reveal.place(relx=1, rely=.5, anchor='e', x=-10)
        self.action = ctk.CTkButton(form, text='', corner_radius=12, width=220, height=48,
                                    fg_color='#16664e', hover_color='#11533e',
                                    font=heading, command=lambda: self.flow.submit(self.entry.get()))
        self.action.grid(row=5, column=0, sticky='w', pady=(26, 0))
        self.recover = ctk.CTkButton(form, text='', corner_radius=12, width=140, height=48,
                                     fg_color='#e1eee8', text_color='#16664e', hover_color='#d5e8df',
                                     command=self._recover)
        self.recover.grid(row=5, column=1, padx=(12, 0), sticky='w', pady=(26, 0))
        self.progress = ctk.CTkProgressBar(form, mode='indeterminate', height=4, corner_radius=12,
                                           progress_color='#16664e', fg_color='#e5eae7')
        self.progress.grid(row=6, column=0, columnspan=2, sticky='ew', pady=(16, 0))
        self.progress.grid_remove()
        self.notice = ctk.CTkLabel(form, text='', text_color='#a66e28', font=font, wraplength=470)
        self.notice.grid(row=7, column=0, columnspan=2, sticky='w', pady=(16, 0))

        self.running = ctk.CTkFrame(content, fg_color='#f7faf8', corner_radius=0)
        self.running_title = ctk.CTkLabel(
            self.running, text='客户端运行中', text_color='#202d2b',
            font=title_font)
        self.running_title.pack(anchor='w', pady=(56, 8))
        self.running_description = ctk.CTkLabel(
            self.running, text='后台已启动，手机请求会在后台自动处理。',
            text_color='#687672', font=font, wraplength=450)
        self.running_description.pack(anchor='w', pady=(0, 28))
        self.running_status = ctk.CTkLabel(
            self.running, text='运行状态：正常', text_color='#16664e', font=heading)
        self.running_status.pack(anchor='w', pady=(0, 24))
        self.running_open = ctk.CTkButton(
            self.running, text='打开后台', width=220, height=48, corner_radius=12,
            fg_color='#16664e', hover_color='#11533e', font=heading)
        self.running_open.pack(anchor='w')
        self.running.pack_forget()

        footer = ctk.CTkFrame(root, fg_color='#ffffff', corner_radius=0, height=60)
        footer.pack(fill='x')
        footer.pack_propagate(False)
        from desktop.version import current_version
        self.footer_label = ctk.CTkLabel(footer, text=f'安全登录 · {current_version()} · GitHub @shovelshit',
                                         text_color='#687672', font=font)
        self.footer_label.pack(side='left', padx=24, pady=20)
        self.footer_label.bind('<Button-1>', lambda _: webbrowser.open('https://github.com/shovelshit/lynkco-plus-desktop'))
        root.bind('<Escape>', lambda _: self.flow.show_screen('login') if self.flow.screen != 'login' else None)
        root.bind('<Command-q>', lambda _: self.request_close())
        root.bind('<Control-q>', lambda _: self.request_close())
        root.protocol('WM_DELETE_WINDOW', self.request_close)
        self._update_tabs('login')

    def request_close(self):
        if self.modal is not None:
            self.modal.lift()
            self.modal.focus_set()
        else:
            close_handler = getattr(self, 'close_handler', None)
            if close_handler is not None:
                close_handler()
            else:
                self.root.destroy()
        return 'break'

    def hide(self):
        """Compatibility no-op; the post-login window remains available."""
        return None

    def _toggle_reveal(self):
        self.entry.configure(show='' if self.reveal_var.get() else '*')

    def _select_tab(self, screen):
        if self.flow is not None:
            self.flow.show_screen(screen)

    def _recover(self):
        if self.flow is None:
            return
        self.flow.show_recovery()

    def _update_tabs(self, screen):
        active = 'claim' if screen == 'claim' else 'login'
        for name, button in (('login', self.login_tab), ('claim', self.claim_tab)):
            selected = name == active
            button.configure(text_color='#16664e' if selected else '#7a8580',
                             font=self.ctk.CTkFont(size=16, weight='bold' if selected else 'normal'))
        self.tab_indicator.place_configure(x=0 if active == 'login' else 112,
                                           width=58 if active == 'login' else 88)

    def show_screen(self, screen):
        self.running.pack_forget()
        self.navigation.pack_forget()
        self.form.pack_forget()
        self.navigation.pack(fill='x', pady=(20, 0))
        self.form.pack(fill='x', pady=(40, 0))
        eyebrow, title, description, label, action = self.screens[screen]
        for widget, value in ((self.eyebrow, eyebrow), (self.title, title), (self.description, description),
                              (self.label, label), (self.action, action)):
            widget.configure(text=value)
        self.entry.delete(0, 'end')
        self.reveal_var.set(False)
        self.entry.configure(show='' if screen == 'claim' else '*')
        self.entry.configure(placeholder_text={
            'login': '请输入登录码',
            'claim': '请输入邀请码',
            'reset': '请输入重置码',
        }[screen])
        if screen == 'claim':
            self.reveal.place_forget()
            self.recover.grid_remove()
        else:
            self.reveal.place(relx=1, rely=.5, anchor='e', x=-10)
            self.recover.grid()
            self.recover.configure(text='返回登录' if screen == 'reset' else '恢复登录码')
        self._update_tabs(screen)
        from desktop.version import current_version
        self.footer_label.configure(text=f'安全登录 · {current_version()} · GitHub @shovelshit')
        self.entry.focus_set()

    def show_running(self, open_browser):
        """Switch to the persistent post-login control surface."""
        self.navigation.pack_forget()
        self.form.pack_forget()
        self.running_open.configure(command=open_browser, state='normal')
        self.running.pack(fill='x', pady=(40, 0))
        from desktop.version import current_version
        self.footer_label.configure(text=f'安全运行 · {current_version()} · GitHub @shovelshit')
        self.running_open.focus_set()

    def show_notice(self, message):
        if self.modal is not None and getattr(self, 'modal_kind', None) == 'recovery':
            self.recovery_notice.configure(text=message)
        else:
            self.notice.configure(text=message)

    def clear_input(self):
        self.entry.delete(0, 'end')

    def set_busy(self, busy):
        for widget in (self.entry, self.reveal, self.action, self.recover,
                       self.login_tab, self.claim_tab):
            widget.configure(state='disabled' if busy else 'normal')
        if busy:
            self.progress.grid()
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.grid_remove()

        if self.modal is not None and getattr(self, 'modal_kind', None) == 'recovery':
            state = 'disabled' if busy else 'normal'
            self.recovery_entry.configure(state=state)
            self.recovery_vin_entry.configure(state=state)
            self.recovery_action.configure(state=state)
            self.recovery_cancel.configure(state=state)

    def show_recovery_dialog(self):
        if self.modal is not None:
            self.modal.lift()
            self.modal.focus_set()
            return

        dialog = self.ctk.CTkToplevel(self.root)
        self.modal = dialog
        self.modal_kind = 'recovery'
        dialog.title('恢复登录码')
        dialog.geometry('480x380')
        dialog.minsize(440, 360)
        dialog.transient(self.root)
        dialog.configure(fg_color='#ffffff')

        content = self.ctk.CTkFrame(dialog, fg_color='#ffffff', corner_radius=12)
        self.modal_content = content
        content.pack(fill='both', expand=True, padx=24, pady=20)
        self.ctk.CTkLabel(
            content, text='恢复登录码', text_color='#202d2b',
            font=self.ctk.CTkFont(size=20, weight='bold')).pack(anchor='w')
        self.ctk.CTkLabel(
            content, text='输入管理员提供的重置码及已绑定车辆的车架号。',
            text_color='#687672', wraplength=400).pack(anchor='w', pady=(8, 18))

        self.recovery_entry = self.ctk.CTkEntry(
            content, height=44, corner_radius=12,
            placeholder_text='重置码', font=self.ctk.CTkFont(size=14))
        self.recovery_entry.pack(fill='x')
        self.recovery_entry.bind('<Return>', lambda _: submit())
        self.recovery_vin_entry = self.ctk.CTkEntry(
            content, height=44, corner_radius=12,
            placeholder_text='车架号（17 位）', font=self.ctk.CTkFont(size=14))
        self.recovery_vin_entry.pack(fill='x', pady=(12, 0))
        self.recovery_vin_entry.bind('<Return>', lambda _: submit())
        self.recovery_notice = self.ctk.CTkLabel(
            content, text='', text_color='#a66e28', wraplength=400,
            anchor='w')
        self.recovery_notice.pack(fill='x', pady=(10, 0))

        buttons = self.ctk.CTkFrame(content, fg_color='transparent', corner_radius=0)
        buttons.pack(fill='x', side='bottom', pady=(18, 0))

        def cancel():
            self.close_recovery_dialog()
            if self.flow is not None and self.flow.screen == 'reset':
                self.flow.show_screen('login')

        def submit():
            if self.flow is not None:
                self.flow.submit(self.recovery_entry.get(), self.recovery_vin_entry.get())

        self.recovery_cancel = self.ctk.CTkButton(
            buttons, text='取消', width=90, height=40, corner_radius=12,
            fg_color='#e1eee8', text_color='#16664e', hover_color='#d5e8df',
            command=cancel)
        self.recovery_cancel.pack(side='right')
        self.recovery_action = self.ctk.CTkButton(
            buttons, text='验证并重置', width=130, height=40, corner_radius=12,
            fg_color='#16664e', hover_color='#11533e', command=submit)
        self.recovery_action.pack(side='right', padx=(0, 10))

        dialog.protocol('WM_DELETE_WINDOW', cancel)
        dialog.bind('<Escape>', lambda _: cancel())
        dialog.grab_set()
        dialog.focus_set()
        self.recovery_entry.focus_set()

    def close_recovery_dialog(self):
        if self.modal is None or getattr(self, 'modal_kind', None) != 'recovery':
            return
        dialog = self.modal
        try:
            dialog.grab_release()
        except self.tk.TclError:
            pass
        dialog.destroy()
        self.modal = None
        self.modal_kind = None
        self.modal_content = None
        if self.flow is not None and getattr(self.flow, 'screen', None) == 'reset':
            self.flow.screen = 'login'
        for name in ('recovery_entry', 'recovery_vin_entry', 'recovery_notice', 'recovery_action', 'recovery_cancel'):
            setattr(self, name, None)

    def show_code(self, code, on_saved):
        dialog = self.ctk.CTkToplevel(self.root)
        self.modal = dialog
        self.modal_kind = 'code'
        dialog.title('保存新的登录码')
        dialog.geometry('480x300')
        dialog.minsize(480, 280)
        dialog.transient(self.root)
        dialog.configure(fg_color='#ffffff')
        dialog.protocol('WM_DELETE_WINDOW', lambda: None)
        content = self.ctk.CTkFrame(dialog, fg_color='#ffffff', corner_radius=12)
        self.modal_content = content
        content.pack(fill='both', expand=True, padx=24, pady=20)
        self.ctk.CTkLabel(content, text='保存新的登录码', text_color='#a66e28',
                          font=self.ctk.CTkFont(size=18, weight='bold')).pack(anchor='w', pady=(0, 16))
        self.ctk.CTkLabel(content, text='旧码立即失效。新码只显示一次，请先保存。',
                          text_color='#202d2b').pack(anchor='w', pady=(0, 16))
        row = self.ctk.CTkFrame(content, fg_color='#ffffff', corner_radius=0)
        row.pack(fill='x')
        displayed = self.ctk.CTkEntry(row, corner_radius=12, height=40)
        displayed.insert(0, code)
        displayed.configure(state='disabled')
        displayed.pack(side='left', fill='x', expand=True)

        def copy_code():
            dialog.clipboard_clear()
            dialog.clipboard_append(code)
            dialog.update_idletasks()

        self.ctk.CTkButton(row, text='复制', command=copy_code, corner_radius=12,
                           fg_color='#16664e', hover_color='#11533e', width=70).pack(side='left', padx=(8, 0))
        acknowledged = self.tk.BooleanVar(value=False)
        self.ctk.CTkCheckBox(content, text='我已保存新的登录码', variable=acknowledged,
                             corner_radius=4, fg_color='#16664e',
                             command=lambda: complete.configure(state='normal' if acknowledged.get() else 'disabled')).pack(anchor='w', pady=(20, 12))

        def finish():
            if not acknowledged.get():
                return
            dialog.grab_release()
            dialog.destroy()
            self.modal = None
            self.modal_kind = None
            self.modal_content = None
            on_saved()

        complete = self.ctk.CTkButton(content, text='完成并打开后台', command=finish,
                                      corner_radius=12, fg_color='#16664e', hover_color='#11533e')
        complete.configure(state='disabled')
        complete.pack(anchor='e')
        dialog.bind('<Escape>', lambda _: 'break')
        dialog.bind('<Command-q>', lambda _: 'break')
        dialog.bind('<Control-q>', lambda _: 'break')
        dialog.grab_set()
        dialog.focus_set()


def watch_shutdown(root, stop_event, on_shutdown=None):
    def poll():
        if stop_event.is_set():
            if on_shutdown is not None:
                on_shutdown()
            else:
                root.destroy()
        else:
            root.after(100, poll)
    root.after(100, poll)


def run_window(session, open_browser, stop_event=None):
    import customtkinter as ctk

    ctk.set_appearance_mode('light')
    root = ctk.CTk()
    if stop_event is not None:
        # Tk can reset the native signal disposition while initializing Cocoa.
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, signal.getsignal(signum))
    view = TkLoginView(root)
    store = getattr(session.controller, 'store', None)
    state_dir = getattr(session.controller, 'state_dir', None) or getattr(store, 'root', None)
    if state_dir is not None and first_run_notice_needed(state_dir):
        from tkinter import messagebox
        acknowledged = messagebox.askokcancel('使用提示', FIRST_RUN_NOTICE, parent=root,
                                              default='cancel')
        if acknowledged:
            mark_first_run_notice(state_dir)
    dispatcher = TkDispatcher(root)
    flow = LoginFlow(session, view, open_browser, dispatcher.schedule, DaemonExecutor())
    view.flow = flow
    close_started = False

    def close_window():
        nonlocal close_started
        if close_started:
            return
        close_started = True
        try:
            result = session.close()
        except Exception:
            result = {'phoneMustDisable': False}
        try:
            from tkinter import messagebox
            message = '电脑代理已关闭，请手动关闭手机 Wi-Fi 代理，否则可能网络异常。'
            if isinstance(result, dict) and result.get('phoneMustDisable'):
                messagebox.showinfo('关闭客户端', message, parent=root)
        except Exception:
            pass
        root.destroy()

    view.close_handler = close_window

    def check_updates():
        try:
            from desktop.version import current_version
            from desktop.updater import (download_and_prepare, fetch_latest,
                                         is_newer, launch_updater, platform_assets,
                                         should_check_for_updates)
            version = current_version()
            if not should_check_for_updates(version):
                return
            latest = fetch_latest()
            if is_newer(latest.version, version):
                def ask_download():
                    from tkinter import messagebox
                    if not messagebox.askyesno('发现新版本',
                                               f'发现新版本 {latest.version}，是否下载并更新？',
                                               parent=root):
                        view.show_notice(f'发现新版本 {latest.version}，可稍后手动更新。')
                        return
                    view.show_notice('正在下载更新，请保持客户端运行。')

                    def download():
                        try:
                            archive_url, checksum_url = platform_assets(latest)
                            import tempfile
                            incoming = download_and_prepare(
                                archive_url, checksum_url,
                                Path(tempfile.mkdtemp(prefix='lynkco-update-')))
                            current = Path(sys.executable)
                            if sys.platform == 'darwin':
                                current = next((item for item in current.parents
                                                if item.suffix == '.app'), current.parent)
                            else:
                                current = current.parent
                            launch_updater(incoming, current, os.getpid())
                            dispatcher.schedule(close_window)
                        except Exception:
                            dispatcher.schedule(lambda: view.show_notice('更新下载失败，当前版本保持不变。'))

                    threading.Thread(target=download, name='lynkco-update-download', daemon=True).start()
                dispatcher.schedule(ask_download)
        except Exception:
            # Update checks are advisory and must never block login or capture.
            return

    threading.Thread(target=check_updates, name='lynkco-update-check', daemon=True).start()

    def idle_check():
        flow.check_idle()
        root.after(1000, idle_check)

    root.after(1000, idle_check)
    if stop_event is not None:
        watch_shutdown(root, stop_event, close_window)
    try:
        root.mainloop()
    finally:
        flow.closed = True
        dispatcher.close()
