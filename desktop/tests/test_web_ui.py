from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / 'desktop' / 'web' / 'index.html').read_text(encoding='utf-8')
JS = (ROOT / 'desktop' / 'web' / 'app.js').read_text(encoding='utf-8')
CSS = (ROOT / 'desktop' / 'web' / 'style.css').read_text(encoding='utf-8')


class WebUIContractTests(unittest.TestCase):
    def test_confirmed_overview_sections_are_kept_in_preview_order(self):
        ids = ['account-overview', 'points', 'asset-expiring-points', 'sign-cards', 'energy',
               'member-medals-title', 'settings-form',
               'push-title', 'today-status', 'recent-title']
        positions = [HTML.index(f'id="{item}"') for item in ids]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('class="overview-settings-row"', HTML)
        self.assertIn('class="asset-metric"', HTML)
        self.assertIn('class="overview-settings-row"', HTML)
        self.assertIn('class="overview-today-row"', HTML)
        self.assertIn('id="medal-count"', HTML)
        self.assertIn('即将过期', HTML)
        self.assertNotIn('class="continue-metric"', HTML)
        self.assertNotIn('id="continue-days"', HTML)
        self.assertIn('data-ui-version="user-dashboard-v3"', HTML)
        self.assertRegex(CSS, r'#view-overview\s*\{[^}]*border-top:\s*0', re.S)

    def test_dashboard_assets_render_expiring_points_and_medal_count(self):
        self.assertIn('id="asset-expiring-points"', HTML)
        self.assertRegex(JS, r'text\("asset-expiring-points"')
        self.assertRegex(JS, r'text\("medal-count"')

    def test_local_token_survives_refresh_only_in_session_storage(self):
        self.assertIn('sessionStorage', JS)
        self.assertNotIn('localStorage', JS)
        self.assertRegex(JS, r'location\.hash\.slice\(1\).*sessionStorage',
                         'URL token must be copied into sessionStorage before the hash is removed')

    def test_native_login_gate_hides_identity_controls_from_dashboard(self):
        self.assertNotIn('id="identity-panel"', HTML)
        self.assertNotIn('/api/claim', JS)
        self.assertNotIn('/api/recover', JS)

    def test_dashboard_has_no_cloud_claim_form(self):
        self.assertNotIn('claimCode', JS)
        self.assertNotIn('recoveryCode', JS)

    def test_capture_copy_uses_the_required_app_instructions(self):
        self.assertIn('请打开领克 App', HTML)
        self.assertIn('请退出账号后，使用手机验证码重新登录', JS)
        self.assertIn('请在领克 App 首页打开一篇文章，分享一次', JS)

    def test_capture_waits_for_explicit_confirmation_before_cloud_upload(self):
        self.assertNotIn('upload-consent', HTML)
        self.assertNotIn('upload-consent', JS)
        self.assertIn('/api/candidates/prepare', JS)
        self.assertIn('确认保存', HTML)
        self.assertIn('请先完成登录态、设备信息、分享信息和车架号识别', (ROOT / 'desktop' / 'binding.py').read_text(encoding='utf-8'))

    def test_new_capture_replaces_only_capture_session_storage(self):
        self.assertIn('capture.accessRevision', JS)
        self.assertIn('sessionStorage.removeItem?.(captureAccessKey)', JS)
        self.assertIn('sessionStorage.removeItem?.(captureExpireKey)', JS)
        self.assertIn('sessionStorage.removeItem?.(captureIdKey)', JS)
        self.assertIn('sessionStorage.removeItem(tokenKey)', JS)

    def test_verification_states_show_progress_retry_and_continue_to_save(self):
        self.assertIn('id="capture-verifying"', HTML)
        self.assertIn('id="capture-verification-error"', HTML)
        self.assertIn('stage === "verifying"', JS)
        self.assertIn('stage === "verification_failed"', JS)
        self.assertIn('capture.verificationError', JS)
        self.assertIn('selectedBindingStep = 2', JS)

    def test_binding_step_three_contains_cloud_save_and_settings_controls(self):
        self.assertIn('id="bind-confirm"', HTML)
        self.assertIn('登录态已验证', HTML)
        self.assertIn('id="capture-vehicle-state"', HTML)
        self.assertIn('id="activate-form"', HTML)
        self.assertIn('id="bind-push-settings"', HTML)
        self.assertIn('id="bind-push-none"', HTML)
        self.assertIn('api("/api/binding/settings"', JS)
        self.assertIn('selectedPush', JS)
        self.assertIn('保存到云端', JS)

    def test_binding_push_choice_hides_optional_fields_until_channel_selected(self):
        self.assertIn('id="bind-push-fields"', HTML)
        self.assertIn('id="bind-bark-field"', HTML)
        self.assertIn('id="bind-serverchan-field"', HTML)
        self.assertIn('function updateBindPushChoice()', JS)
        self.assertIn('show("bind-push-fields", selected !== "none")', JS)
        self.assertIn('input[name="bind-push-channel"]', JS)

    def test_overview_exposes_independent_share_action_contract(self):
        self.assertIn('id="run-share-now"', HTML)
        self.assertIn('立即分享一次', HTML)
        self.assertIn('share-now', JS)
        self.assertIn('runTaskNow($("run-share-now"), "share", "/api/binding/run"', JS)
        self.assertNotIn('/api/binding/share', JS)

    def test_history_has_a_local_return_action_without_global_navigation(self):
        self.assertIn('id="history-back"', HTML)
        handler = JS[JS.index('$("history-back")'):JS.index('$("bind-back")')]
        self.assertIn('navigate("overview")', handler)
        self.assertNotIn('<nav', HTML)
        self.assertNotIn('class="sidebar"', HTML)

    def test_disconnected_and_recovery_errors_are_page_local(self):
        self.assertIn('id="disconnected-state"', HTML)
        self.assertNotIn('id="identity-error"', HTML)
        self.assertIn('renderDisconnectedState', JS)
        self.assertIn('localDisconnected', JS)
        self.assertIn('localDisconnected = true', JS)
        self.assertNotIn('identityError', JS)

    def test_binding_prerequisites_disable_actions_before_the_request(self):
        self.assertIn('id="proxy-next"', HTML)
        self.assertIn('id="stop-proxy"', HTML)
        self.assertIn('id="bind-save"', HTML)
        self.assertIn('!proxy.running || !proxy.paired || proxy.needsReconfigure', JS)
        self.assertIn('/api/candidates/prepare', JS)
        self.assertIn('disabled = !$("proxy-removed").checked', JS)

    def test_pairing_exposes_wifi_rebind_and_proxy_port_controls(self):
        self.assertIn('id="proxy-port"', HTML)
        self.assertIn('data-copy="proxy-port"', HTML)
        self.assertIn('/api/capture/rebind', JS)
        self.assertIn('/api/capture/start', JS)
        self.assertNotIn('proxy-port-input', JS)
        self.assertNotIn('proxyPort', JS)

    def test_replace_binding_resets_local_flow_before_navigation(self):
        handler = re.search(r'\$\("binding-replace"\).*?\n\s*\}\);', JS, re.S)
        self.assertIsNotNone(handler)
        self.assertIn('resetBindingFlow', handler.group(0))

    def test_stale_qr_failure_does_not_leak_into_the_dashboard(self):
        qr_handler = JS[JS.index('if (step === 0 && proxy.pairUrl && qrPair !== proxy.pairUrl)'):JS.index('text(\n      "phone-instructions-title"')]
        self.assertIn('requestedPair', qr_handler)
        self.assertIn('view === "bind"', qr_handler)
        self.assertIn('state?.proxy?.pairUrl === requestedPair', qr_handler)
        self.assertIn('step === 0', qr_handler)

    def test_reload_only_forces_capture_after_worker_requires_rebinding(self):
        startup = JS[JS.index('async function start()'):]
        self.assertIn('routeRequiredRebind()', startup)
        self.assertNotIn('state?.proxy.running && !state.binding', startup)
        self.assertIn('id="proxy-start"', HTML)

    def test_activation_returns_to_overview_without_waiting_for_proxy_disconnect(self):
        handler = JS[JS.index('$("activate-form").addEventListener'):JS.index('$("stop-proxy").addEventListener')]
        self.assertIn('navigate("overview")', handler)

    def test_long_operations_have_stable_visible_loading_state(self):
        self.assertIn('setButtonLoading', JS)
        self.assertIn('data-loading-label', CSS)
        self.assertIn('.is-loading', CSS)
        self.assertIn('@keyframes button-spin', CSS)

    def test_cloud_and_browser_deadlines_leave_backend_time_to_reply(self):
        self.assertIn('AbortSignal.timeout(path === "/api/binding/run" ? 130000 : 45000)', JS)
        self.assertNotIn('AbortSignal.timeout(path === "/api/binding/run" ? 160000 : 60000)', JS)

    def test_only_the_initiating_control_becomes_busy(self):
        perform_start = JS.index('async function perform(')
        perform_end = JS.index('  function navigate(', perform_start)
        handler = JS[perform_start:perform_end]
        self.assertIn('activeOperations', handler)
        self.assertIn('operationBusy', handler)
        self.assertNotIn('buttons.forEach((button) => (button.disabled = true))', handler)

    def test_startup_keeps_local_state_visible_when_cloud_refresh_fails(self):
        start = JS[JS.index('async function start()'):]
        self.assertIn('await poll()', start)
        refresh_failure = start[start.index('try {\n      await api("/api/refresh"'):]
        self.assertIn('render();', refresh_failure)

    def test_binding_actions_wrap_without_overlapping(self):
        self.assertIn('class="binding-step-actions"', HTML)
        self.assertIn('id="bind-back"', HTML)
        self.assertIn('$("bind-back").addEventListener', JS)
        self.assertRegex(CSS, r'\.binding-step-actions\s*\{[^}]*flex-wrap:\s*wrap', re.S)
        self.assertNotRegex(CSS, r'\.binding-step-actions\s*\{[^}]*flex-direction:\s*column', re.S)

    def test_desktop_layout_keeps_actions_inline_and_reflows_at_1100px(self):
        self.assertRegex(CSS, r'\.form-actions\s*\{[^}]*flex-wrap:\s*nowrap', re.S)
        self.assertRegex(CSS, r'@media\s*\(max-width:\s*900px\)\s*\{[\s\S]*?\.overview-settings-row\s*\{[^}]*grid-template-columns:\s*1fr', re.S)
        self.assertRegex(CSS, r'\.muted\s*\{[^}]*font-size:\s*12px', re.S)

    def test_push_configuration_shows_one_channel_and_saves_then_tests(self):
        self.assertIn('id="push-form"', HTML)
        self.assertIn('id="bark-field"', HTML)
        self.assertIn('id="serverchan-field"', HTML)
        self.assertIn('id="push-save"', HTML)
        self.assertIn('/api/binding/notification-test', JS)
        self.assertIn('保存并测试', JS)
        self.assertRegex(JS, r'show\(`\$\{channel\}-field`,\s*active\)')

    def test_binding_stages_are_navigable_and_cleanup_is_exit_only(self):
        self.assertEqual(HTML.count('data-binding-step='), 3)
        self.assertIn('连接与配置</button>', HTML)
        self.assertNotIn('配置代理</button>', HTML)
        self.assertNotIn('关闭代理并断开</button>', HTML)
        self.assertNotIn('id="cancel-capture"', HTML)
        self.assertNotIn('放弃本次绑定', HTML)
        self.assertNotIn('exitCapture', JS)
        self.assertIn('selectedBindingStep', JS)
        self.assertIn('document.querySelectorAll("[data-binding-step]")', JS)

    def test_binding_first_step_combines_pairing_and_proxy_configuration(self):
        self.assertRegex(HTML, r'id="bind-start"[\s\S]*id="bind-connect"')
        self.assertIn('id="proxy-runtime-state"', HTML)
        self.assertIn('id="proxy-reconfigure"', HTML)
        self.assertIn('重置网络', HTML)
        self.assertIn('needsReconfigure', JS)
        self.assertIn('networkChanged', JS)
        self.assertRegex(CSS, r'#view-bind\.binding-start-layout\s*\{[^}]*grid-template-columns', re.S)

    def test_binding_first_step_uses_four_step_nav_and_shared_grid_row(self):
        self.assertRegex(CSS, r'\.steps\s*\{[^}]*grid-template-columns:\s*repeat\(3,', re.S)
        layout = CSS[CSS.index('#view-bind.binding-start-layout'):CSS.index('.proxy-runtime')]
        self.assertIn('grid-template-rows:', layout)
        self.assertRegex(layout, r'> #bind-start\s*\{[^}]*grid-row:\s*2', re.S)
        self.assertRegex(layout, r'> #bind-connect\s*\{[^}]*grid-row:\s*2', re.S)

    def test_binding_first_step_auto_starts_proxy_after_networks_are_loaded(self):
        self.assertIn('ensureProxyStarted', JS)
        self.assertIn('await ensureProxyStarted', JS)
        self.assertRegex(JS, r'if \(!networks\.length\)[\s\S]{0,500}return', re.S)
        self.assertIn('proxyStartPromise', JS)
        self.assertIn('proxyStarting', JS)
        self.assertIn('启动中', JS)

    def test_pairing_status_bar_belongs_to_capture_step_and_has_state_dots(self):
        self.assertLess(HTML.index('id="capture-step"'), HTML.index('id="pairing-state-grid"'))
        for item in ('proxy-running', 'phone-request', 'tls', 'certificate'):
            self.assertIn(f'id="{item}-dot"', HTML)
        self.assertIn('show("pairing-step", step === 0)', JS)
        self.assertIn('show("bind-connect", [0, 1].includes(step)', JS)
        self.assertIn('status-dot', CSS)

    def test_proxy_start_does_not_confirm_phone_configuration(self):
        start = JS[JS.index('async function ensureProxyStarted'):JS.index('async function autoStartProxy')]
        self.assertNotIn('proxyConfirmed = true', start)
        self.assertNotIn('selectedBindingStep = 1', start)
        confirm = JS[JS.index('$("proxy-next").addEventListener'):JS.index('async function loadNetworks')]
        self.assertIn('proxyConfirmed = true', confirm)
        self.assertIn('selectedBindingStep = 1', confirm)

    def test_proxy_runtime_keeps_server_only_and_qr_loads_after_auto_start(self):
        self.assertIn('id="proxy-address"', HTML)
        self.assertIn('id="proxy-port"', HTML)
        self.assertIn('data-copy="proxy-port"', HTML)
        self.assertIn('fetch("/api/capture/qr"', JS)
        self.assertIn('await ensureProxyStarted', JS)

    def test_proxy_start_controls_live_in_runtime_state(self):
        self.assertNotIn('id="capture-start-submit"', HTML)
        self.assertNotIn('代理已启动', HTML)
        self.assertNotIn('id="proxy-port-input"', HTML)
        runtime = HTML[HTML.index('id="proxy-runtime-state"'):HTML.index('</section>', HTML.index('id="proxy-runtime-state"'))]
        self.assertIn('id="proxy-port"', runtime)
        self.assertIn('data-copy="proxy-port"', runtime)
        self.assertLess(runtime.index('id="proxy-runtime-status"'), runtime.index('id="proxy-address"'))
        self.assertLess(runtime.index('id="proxy-address"'), runtime.index('id="proxy-port"'))
        self.assertNotIn('capture-start-submit', JS)
        self.assertNotIn('proxy-port-input', JS)
        self.assertIn('proxy-runtime-status', JS)

    def test_qr_reconfigure_action_explains_regeneration_for_expired_qr(self):
        self.assertNotIn('重新生成二维码', HTML)
        self.assertNotIn('id="refresh-pairing"', HTML)
        self.assertIn('重置网络', HTML)
        self.assertRegex(HTML, r'重置网络[^。]*二维码', re.S)
        self.assertIn('id="pair-qr-retry"', HTML)
        self.assertNotIn('async function refreshPairingQr', JS)
        self.assertNotIn('/api/capture/reissue', JS)

    def test_network_refresh_preserves_running_proxy_selection(self):
        refresh_handler = JS[JS.index('$("refresh-network")'):JS.index('async function rebindProxy')]
        self.assertIn('loadNetworks', refresh_handler)
        self.assertNotIn('/api/capture/rebind', refresh_handler)
        self.assertIn('proxy.address', JS)

    def test_pending_tasks_do_not_render_success_checkmarks(self):
        self.assertIn('id="sign-task-icon"', HTML)
        self.assertIn('id="share-task-icon"', HTML)
        self.assertIn('function taskIcon', JS)
        self.assertRegex(JS, r'taskIcon\("sign-task-icon"')
        self.assertRegex(JS, r'taskIcon\("share-task-icon"')
        self.assertIn('.task-state-icon.success', CSS)
        task_helper = JS[JS.index('function taskIcon'):JS.index('const show =')]
        self.assertNotIn('className =', task_helper)

    def test_member_info_assets_include_dynamic_details_and_medals(self):
        self.assertIn('id="member-details"', HTML)
        self.assertIn('id="member-medals"', HTML)
        self.assertIn('id="medal-list"', HTML)
        self.assertIn('function renderMemberAssets', JS)
        self.assertIn('inventory.details', JS)
        self.assertIn('inventory.medals', JS)
        self.assertIn('.slice(0, 48)', JS)
        self.assertIn('function safeImageUrl', JS)
        self.assertIn('data-lucide="medal"', HTML)
        self.assertIn('memberVisual(item.iconUrl, "medal")', JS)
        self.assertIn('.member-details', CSS)
        self.assertIn('.medal-list', CSS)

    def test_tablet_layout_does_not_reserve_removed_sidebar_space(self):
        self.assertRegex(CSS, r'@media\s*\(max-width:\s*1050px\)\s*and\s*\(min-width:\s*721px\)[^{]*\{[\s\S]*?main\s*\{[^}]*margin:\s*0 auto[^}]*width:\s*100%', re.S)

    def test_preview_no_longer_carries_a_second_dashboard_implementation(self):
        preview = ROOT / 'desktop' / 'preview'
        for path in (preview / 'index.html', preview / 'preview.js', preview / 'style.css'):
            self.assertFalse(path.exists(), f'{path} is stale preview code')


if __name__ == '__main__':
    unittest.main()
