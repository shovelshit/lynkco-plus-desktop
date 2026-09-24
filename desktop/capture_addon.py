"""mitmproxy subprocess entry: no flow logging or retained capture files."""

import asyncio
import json
import os
import time
from pathlib import Path
from urllib.request import Request, urlopen

from desktop.capture import AUTH_HOSTS, AUTH_PATHS, VEHICLE_HOST, VEHICLE_PATH, is_share_request, parse_session, parse_share_capture, parse_vehicle_capture, capture_summary, request_summary


class CaptureAddon:
    def state(self):
        try:
            return json.loads(Path(os.environ['LYNKCO_PROXY_STATE']).read_text())
        except (OSError, ValueError, KeyError):
            return {}

    def client_connected(self, client):
        peer = self.state().get('peerIp')
        if not peer or client.peername[0] != peer:
            client.error = 'Phone is not paired'

    async def requestheaders(self, flow):
        flow.metadata['captureStartedAt'] = int(time.time() * 1000)
        flow.metadata['proxyEpoch'] = self.state().get('proxyEpoch')
        await self.report(flow, 'pending')

    async def http_connect(self, flow):
        await self.report(flow, 'tunnel')

    async def error(self, flow):
        await self.report(flow, 'network_error')

    async def report(self, flow, outcome, session=None, share=None, vehicle=None, summary=None):
        state = self.state()
        if not state.get('captureEnabled') or flow.client_conn.peername[0] != state.get('peerIp'):
            return
        try:
            summary = summary or request_summary(flow.request.pretty_url, flow.request.method,
                                                 flow.response.status_code if flow.response else None, outcome)
            summary['id'] = flow.id
            summary['method'] = flow.request.method
            await asyncio.to_thread(self.notify, session, share, vehicle, flow.client_conn.peername[0], summary,
                                    flow.metadata.get('captureStartedAt'), flow.metadata.get('proxyEpoch'))
        except Exception:
            pass

    async def response(self, flow):
        state = self.state()
        if not state.get('captureEnabled') or flow.client_conn.peername[0] != state.get('peerIp'):
            return
        if flow.request.host == 'app-services.lynkco.com.cn' and flow.request.path.split('?', 1)[0] == '/auth/user/info':
            self.profile_contract(flow)
        is_auth = flow.request.host in AUTH_HOSTS and flow.request.path.split('?', 1)[0] in AUTH_PATHS
        is_share = is_share_request(flow.request.pretty_url)
        is_vehicle = (flow.request.method == 'GET' and flow.request.host == VEHICLE_HOST and
                      flow.request.path.split('?', 1)[0] == VEHICLE_PATH)
        if not is_auth and not is_share and not is_vehicle:
            await self.report(flow, 'completed')
            return
        if not flow.response:
            return
        try:
            payload = flow.response.json() if len(flow.response.raw_content or b'') <= 65536 else None
        except (ValueError, UnicodeError):
            payload = None
        try:
            if is_auth:
                session = parse_session(flow.request.pretty_url, flow.request.headers, flow.response.status_code,
                                        payload, os.environ.get('LYNKCO_PHONE_PLATFORM'))
                summary = capture_summary(flow.request.pretty_url, flow.request.headers, flow.response.status_code,
                                          payload, os.environ.get('LYNKCO_PHONE_PLATFORM'))
                await self.report(flow, summary['outcome'], session=session, summary=summary)
            elif is_share:
                share = parse_share_capture(flow.request.pretty_url, flow.request.headers,
                                            flow.response.status_code, payload)
                await self.report(flow, 'share_captured' if share else 'share_incomplete', share=share,
                                  summary=request_summary(flow.request.pretty_url, flow.request.method,
                                                          flow.response.status_code,
                                                          'share_captured' if share else 'share_incomplete'))
            else:
                vehicle = parse_vehicle_capture(flow.request.pretty_url, flow.response.status_code, payload)
                await self.report(flow, 'vehicle_captured' if vehicle else 'vehicle_incomplete', vehicle=vehicle,
                                  summary=request_summary(flow.request.pretty_url, flow.request.method,
                                                          flow.response.status_code,
                                                          'vehicle_captured' if vehicle else 'vehicle_incomplete'))
        except Exception:
            pass

    def notify(self, session, share, vehicle, peer, summary, request_started_at, proxy_epoch):
        data = json.dumps({'session': session, 'share': share, 'vehicle': vehicle, 'peer': peer, 'summary': summary,
                           'requestStartedAt': request_started_at, 'proxyEpoch': proxy_epoch}).encode()
        request = Request(os.environ['LYNKCO_CALLBACK_URL'], data=data,
                          headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + os.environ['LYNKCO_CALLBACK_TOKEN']})
        with urlopen(request, timeout=3) as response:
            response.read(1024)

    def profile_contract(self, flow):
        """Keep protocol structure only, never personal values or credentials."""
        try:
            if not flow.response or len(flow.response.raw_content or b'') > 65536:
                return
            payload = flow.response.json()
            if not isinstance(payload, dict):
                return
            data = payload.get('data')
            headers = flow.request.headers
            record = {'httpStatus': flow.response.status_code, 'success': payload.get('code') == 'success',
                      'dataFields': [k for k in data if isinstance(k, str) and k.isidentifier() and len(k) < 64][:80] if isinstance(data, dict) else [],
                      'hasAppCode': headers.get('authorization', '').startswith('APPCODE '),
                      'hasSignature': bool(headers.get('x-ca-signature')),
                      'hasTokenHeader': bool(headers.get('token')),
                      'tokenHasBearerPrefix': headers.get('token', '').startswith('bearer'),
                      'hasAuthorizationBearer': headers.get('authorization', '').lower().startswith('bearer ')}
            destination = Path(os.environ['LYNKCO_PROXY_STATE']).with_name('profile-contract.json')
            destination.write_text(json.dumps(record))
            destination.chmod(0o600)
        except Exception:
            pass


addons = [CaptureAddon()]
