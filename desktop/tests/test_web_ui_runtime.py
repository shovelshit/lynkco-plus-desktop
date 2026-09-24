from pathlib import Path
import json
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "desktop" / "web" / "app.js"


class WebUIRuntimeTests(unittest.TestCase):
    def test_browser_requests_use_standard_deadlines_after_native_login(self):
        script = textwrap.dedent('''\
            const fs=require("fs");
            const source=fs.readFileSync(__APP_PATH__,"utf8").replace(
              /  start\\(\\);\\r?\\n\\}\\)\\(\\);/,
              "  global.__desktopTest={api};\\n})();"
            );
            const calls=[];
            global.sessionStorage={getItem:()=>"fixture-token"};
            global.location={hash:"",pathname:"/",search:""};
            global.history={replaceState(){}};
            global.document={getElementById:()=>({hidden:false,addEventListener(){}}),querySelector:()=>({classList:{toggle(){}}}),querySelectorAll:()=>[]};
            global.AbortSignal={timeout:ms=>({deadline:ms})};
            global.fetch=async(path,options)=>{calls.push([path,options.signal?.deadline ?? null]);return {ok:true,json:async()=>({ok:true,data:{}})}};
            eval(source);
            (async()=>{
              for(const path of ["/api/activity","/api/binding/run","/api/status"])
                await global.__desktopTest.api(path, path==="/api/status"?undefined:{});
              const expected=[["/api/activity",45000],["/api/binding/run",130000],["/api/status",45000]];
              if(JSON.stringify(calls)!==JSON.stringify(expected)) throw Error("request deadlines differ: "+JSON.stringify(calls));
            })().catch(error=>{console.error(error);process.exit(1)});
        '''.replace("__APP_PATH__", json.dumps(str(APP))))
        result = subprocess.run(["node", "-e", script], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    @unittest.skip('native Flet window owns unlock; browser has no vault retry view')
    def test_vault_failure_stays_on_retry_view_until_explicit_unlock(self):
        script = textwrap.dedent('''\
            const fs=require("fs");
            const source=fs.readFileSync(__APP_PATH__,"utf8");
            class Element {
              constructor(id){this.id=id;this.hidden=false;this.disabled=false;this.checked=false;this.value="";this.options=[];this.textContent="";this.dataset={};this.listeners={};this.classList={add(){},remove(){},toggle(){},contains(){return false}};}
              addEventListener(type,handler){(this.listeners[type] ||= []).push(handler)}
              replaceChildren(){} append(){} prepend(){} removeAttribute(){} setAttribute(){} closest(){return this} querySelector(){return new Element("child")} querySelectorAll(){return []}
            }
            const nodes=new Map(); const get=id=>nodes.get(id)||(nodes.set(id,new Element(id)),nodes.get(id));
            global.document={hidden:false,activeElement:null,getElementById:get,querySelector:s=>s==="main"?get("main"):null,querySelectorAll:()=>[],createElement:()=>new Element("created"),createTextNode:()=>new Element("text")};
            global.window={};global.location={hash:"",pathname:"/",search:""};global.history={replaceState(){}};
            global.sessionStorage={getItem:()=>"fixture-token",setItem(){}};global.AbortSignal={timeout:()=>({})};global.setInterval=()=>({});
            let unlocked=false,retries=0;
            const state=()=>({hasIdentity:unlocked,vaultUnavailable:!unlocked,connected:true,configured:true,binding:null,candidate:null,capture:{stage:"idle",events:[]},proxy:{running:false},scheduleWindows:{items:[]},runs:{items:[],nextCursor:null}});
            global.fetch=async path=>{
              if(path==="/api/identity/retry"){retries++; unlocked=true;return {ok:true,json:async()=>({ok:true,data:state()})}}
              if(path==="/api/status") return {ok:true,json:async()=>({ok:true,data:state()})};
              if(path==="/api/networks") return {ok:true,json:async()=>({ok:true,data:[]})};
              if(path==="/api/refresh") return {ok:true,json:async()=>({ok:true,data:state()})};
              throw Error("unexpected request "+path);
            };
            eval(source);
            (async()=>{
              for(let i=0;i<8;i++) await new Promise(setImmediate);
              if(get("vault-unavailable").hidden || !get("identity-panel").hidden || retries) throw Error("vault timeout opened onboarding or retried automatically");
              await get("vault-retry").listeners.click[0]({currentTarget:get("vault-retry")});
              for(let i=0;i<8;i++) await new Promise(setImmediate);
              if(!get("vault-unavailable").hidden || get("view-overview").hidden || retries!==1) throw Error("unlock did not restore overview");
            })().catch(error=>{console.error(error);process.exit(1)});
        '''.replace("__APP_PATH__", json.dumps(str(APP))))
        result = subprocess.run(["node", "-e", script], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_startup_navigation_respects_bound_account_and_explicit_overview(self):
        script = textwrap.dedent('''\
            const fs = require("fs");
            const source = fs.readFileSync(__APP_PATH__, "utf8").replace(
              /  start\\(\\);\\r?\\n\\}\\)\\(\\);/,
              "  global.__desktopTest = { navigate };\\n  start();\\n})();"
            );
            const tick = () => new Promise(resolve => setImmediate(resolve));
            class Element {
              constructor(id) { this.id=id; this.hidden=false; this.disabled=false; this.checked=false; this.value=""; this.options=[]; this.textContent=""; this.dataset={}; this.listeners={}; const classes=new Set(); this.classList={add:(...items)=>items.forEach(item=>classes.add(item)),remove:(...items)=>items.forEach(item=>classes.delete(item)),toggle:(item,enabled)=>enabled?classes.add(item):classes.delete(item),contains:item=>classes.has(item)}; }
              addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
              replaceChildren(...items) { this.options=items; } append(item) { this.options.push(item); } prepend() {} removeAttribute() {} setAttribute() {} closest() { return this; } querySelector() { return new Element("child"); } querySelectorAll() { return []; }
            }
            const response = data => ({ok:true,json:async()=>({ok:true,data})});
            const fixture = (bound, status="paused", running=true) => ({hasIdentity:true,connected:true,configured:true,
              binding:bound?{id:"bound-1",label:"已绑定",status,scheduleTime:"08:00",inventory:{days:0},notifications:{}}:null,
              candidate:null,capture:{stage:"idle",events:[]},proxy:{running,paired:false,pairUrl:null,address:"127.0.0.1",port:8080},
              scheduleWindows:{items:[]},runs:{items:[],nextCursor:null}});
            async function scenario(bound, navigateAway, expectedBind, status="paused", running=true) {
              const nodes=new Map(); const get=id=>nodes.get(id)||(nodes.set(id,new Element(id)),nodes.get(id));
              global.document={hidden:false,activeElement:null,getElementById:get,querySelector:selector=>selector==="main"?get("main"):null,querySelectorAll:()=>[],createElement:()=>new Element("created"),createTextNode:()=>new Element("text")};
              global.window={}; global.location={hash:"",pathname:"/",search:""}; global.history={replaceState(){}};
              global.sessionStorage={getItem:()=>"fixture-token",setItem(){}}; global.AbortSignal={timeout:()=>({})}; global.setInterval=()=>({});
              let finishRefresh, refreshStarted, proxyStarts=0;
              const refreshRequested=new Promise(resolve=>{refreshStarted=resolve});
              global.fetch=path=>{
                if(path==="/api/status") return Promise.resolve(response(fixture(bound,status,running)));
                if(path==="/api/networks") return Promise.resolve(response([{address:"127.0.0.1",name:"Wi-Fi"}]));
                if(path==="/api/refresh") {refreshStarted(); return new Promise(resolve=>{finishRefresh=()=>resolve(response({}))});}
                if(path==="/api/capture/start") {proxyStarts++; return Promise.resolve(response({}));}
                throw new Error("unexpected request "+path);
              };
              eval(source);
              await refreshRequested;
              if(navigateAway) {
                global.__desktopTest.navigate("bind");
                global.__desktopTest.navigate("overview");
              }
              finishRefresh();
              for(let i=0;i<6;i++) await tick();
              if(get("view-bind").hidden===expectedBind || get("view-overview").hidden!==expectedBind)
                throw new Error(`wrong startup page for bound=${bound}, status=${status}, navigateAway=${navigateAway}`);
              if(status==="needs_rebind") {
                if(proxyStarts) throw new Error("forced rebind started capture without user action");
                if(get("proxy-start").hidden) throw new Error("forced rebind has no manual capture action");
                get("network").value="127.0.0.1";
                await get("proxy-start").listeners.click[0]({currentTarget:get("proxy-start")});
                if(proxyStarts!==1) throw new Error("manual capture action did not start proxy");
              }
            }
            (async()=>{
              await scenario(true,false,false);
              await scenario(true,true,false);
              await scenario(false,true,false);
              await scenario(false,false,false);
              await scenario(true,false,true,"needs_rebind",false);
              await scenario(true,false,false,"paused",false);
            })().catch(error=>{console.error(error);process.exit(1)});
            '''.replace("__APP_PATH__", json.dumps(str(APP))))
        result = subprocess.run(["node", "-e", script], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_failed_qr_request_shows_retry_and_recovers_without_rebinding(self):
        script = textwrap.dedent('''\
            const fs = require("fs");
            const source = fs.readFileSync(__APP_PATH__, "utf8");
            class Element {
              constructor(id) { this.id=id; this.hidden=false; this.disabled=false; this.checked=false; this.value="08:00-10:00"; this.textContent=""; this.dataset={}; this.listeners={}; this.classList={add(){},remove(){},toggle(){},contains(){return false}}; }
              addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
              querySelector() { return new Element("child"); } querySelectorAll() { return []; }
              replaceChildren() {} append() {} prepend() {} removeAttribute() {} setAttribute() {} closest() { return this; }
            }
            const nodes=new Map(); const get=id=>nodes.get(id) || (nodes.set(id,new Element(id)),nodes.get(id));
            global.document={hidden:false,activeElement:null,getElementById:get,querySelector:s=>s==="main"?get("main"):null,querySelectorAll:()=>[],createElement:()=>new Element("created"),createTextNode:()=>new Element("text")};
            global.window={}; global.location={hash:"",pathname:"/",search:""}; global.history={replaceState(){}};
            global.sessionStorage={getItem:()=>"fixture-token",setItem(){}};
            global.AbortSignal={timeout:()=>({})}; global.setInterval=()=>({});
            let blobNumber=0;
            global.URL={createObjectURL:()=>"blob:qr-"+(++blobNumber),revokeObjectURL(){}};
            let qrCalls=0, finishStale;
            global.fetch=async path=>{
              if(path!=="/api/capture/qr") throw new Error("unexpected request "+path);
              qrCalls++;
              if(qrCalls===1) throw new Error("temporary QR failure");
              if(qrCalls===3) return new Promise(resolve=>{finishStale=()=>resolve({ok:true,blob:async()=>({})})});
              return {ok:true,blob:async()=>({})};
            };
            const boot=source.replace(/  start\\(\\);\\r?\\n\\}\\)\\(\\);/, "  global.__desktopTest={render,setState:next=>{state=next},navigate};\\n})();");
            eval(boot);
            const state={hasIdentity:true,connected:true,configured:true,binding:null,candidate:null,
              capture:{stage:"idle",events:[]},proxy:{running:true,paired:false,pairUrl:"http://example.test/pair",address:"192.168.1.8",port:8080},
              scheduleWindows:{items:[]},runs:{items:[],nextCursor:null}};
            (async()=>{
              global.__desktopTest.setState(state);
              global.__desktopTest.navigate("bind");
              await new Promise(setImmediate);
              if(get("pair-qr-placeholder").textContent.includes("加载中")) throw new Error("QR failure left permanent loading state");
              if(get("pair-qr-retry").hidden) throw new Error("retry action hidden after QR failure");
              get("pair-qr-retry").listeners.click[0]();
              await new Promise(setImmediate);
              if(qrCalls!==2 || get("pair-qr").hidden || !get("pair-qr-retry").hidden) throw new Error("retry did not render the QR");
              state.proxy.pairUrl="http://example.test/stale";
              global.__desktopTest.render();
              if(!get("pair-qr").hidden) throw new Error("old QR remained visible after pair change");
              state.proxy.pairUrl="http://example.test/current";
              global.__desktopTest.render();
              await new Promise(setImmediate);
              if(qrCalls!==4 || get("pair-qr").src!=="blob:qr-2") throw new Error("new pairing QR did not load");
              finishStale();
              await new Promise(setImmediate);
              if(get("pair-qr").src!=="blob:qr-2") throw new Error("late response replaced the current QR");
            })().catch(error=>{console.error(error);process.exit(1)});
            '''.replace("__APP_PATH__", json.dumps(str(APP))))
        result = subprocess.run(["node", "-e", script], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_captured_generation_replaces_capture_session_cache_and_requests_new_token(self):
        script = textwrap.dedent(
            '''\
            const fs = require("fs");
            const source = fs.readFileSync(__APP_PATH__, "utf8");
            class Element {
              constructor(id) { this.id=id; this.hidden=false; this.disabled=false; this.checked=false; this.value="08:00-10:00"; this.textContent=""; this.dataset={}; this.listeners={}; this.classList={add:()=>{},remove:()=>{},toggle:()=>{},contains:()=>false}; }
              addEventListener(t,h){(this.listeners[t] ||= []).push(h)} querySelector(){return new Element("child")} querySelectorAll(){return []}
              replaceChildren(){} append(){} prepend(){} setAttribute(){} removeAttribute(){} closest(){return this}
            }
            const nodes = new Map(); const get = (id) => nodes.get(id) || (nodes.set(id,new Element(id)),nodes.get(id));
            global.document={hidden:false,activeElement:null,getElementById:get,querySelector:(s)=>s==="main"?get("main"):null,querySelectorAll:()=>[],createElement:()=>new Element("created"),createTextNode:()=>new Element("text")};
            const removed=[], stored=new Map([["lynkco-helper.local-token","management-token"]]);
            global.sessionStorage={getItem:(k)=>stored.get(k)||null,setItem:(k,v)=>stored.set(k,String(v)),removeItem:(k)=>{removed.push(k);stored.delete(k)} };
            global.window={}; global.location={hash:"",pathname:"/",search:""}; global.history={replaceState:()=>null}; global.AbortSignal={timeout:()=>({})}; global.setInterval=()=>({});
            let accessCalls=0, rejectAccess=false;
            global.fetch=async (path, options) => {
              if (path === "/api/capture/access-token-once" && rejectAccess) throw new Error("expired fixture");
              if (path === "/api/capture/access-token-once") { accessCalls++; const data=Object.assign({},{accessToken:"mobile-"+accessCalls,expireAt:9999999999999,captureId:accessCalls===1?"A":"B"}); return Object.assign({ok:true},{json:()=>Promise.resolve(Object.assign({ok:true},{data}))}); }
              throw new Error("unexpected request "+path);
            };
            const boot = source.replace(/  start\\(\\);\\r?\\n\\}\\)\\(\\);/, "  global.__desktopTest = { render: () => render(), setState: (next) => { state = next; }, navigate };\\n})();");
            eval(boot);
            const fixture = (id) => Object.assign({},{hasIdentity:true,connected:true,configured:true,binding:null,candidate:null,
              capture:Object.assign({},{stage:"captured",platform:"IOS",capturedAt:Date.now(),captureId:id,events:[],readiness:{login:true,device:true,share:true,vehicle:true}}}}),
              proxy:Object.assign({},{running:true,paired:true,pairUrl:null,address:"127.0.0.1",port:1}),scheduleWindows:Object.assign({},{items:[Object.assign({},{value:"08:00-10:00",remaining:1})]}),runs:Object.assign({},{items:[],nextCursor:null})});
            (async()=>{
              global.__desktopTest.navigate("bind");
              global.__desktopTest.setState(fixture("A")); global.__desktopTest.render(); await new Promise(setImmediate);
              if (get("bind-confirm").hidden) throw new Error("complete capture did not advance to save");
              const expiredAccess = fixture("A"); expiredAccess.capture.expireAt = 1;
              global.__desktopTest.setState(expiredAccess); global.__desktopTest.render();
              const confirmationTime=new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(expiredAccess.capture.capturedAt+1800000));
              if (!get("candidate-expiry").textContent.includes(confirmationTime)) throw new Error("access expiry shortened confirmation window");
              global.__desktopTest.setState(fixture("B")); global.__desktopTest.render(); await new Promise(setImmediate);
              if (accessCalls !== 2) throw new Error("expected two access-token calls, got "+accessCalls);
              for (const key of ["lynkco-helper.capture-access-token","lynkco-helper.capture-expire-at","lynkco-helper.capture-id"])
                if (removed.filter((item)=>item===key).length < 1) throw new Error("did not clear "+key);
              if (!stored.has("lynkco-helper.local-token")) throw new Error("cleared management token");
              eval(boot);
              global.__desktopTest.setState(fixture("B")); global.__desktopTest.render(); await new Promise(setImmediate);
              if (accessCalls !== 2 || !stored.has("lynkco-helper.capture-access-token")) throw new Error("reload did not reuse valid capture cache");
              rejectAccess=true;
              stored.set("lynkco-helper.capture-expire-at",String(Date.now()-1));
              global.__desktopTest.render(); await new Promise(setImmediate);
              if (stored.has("lynkco-helper.capture-access-token")) throw new Error("expired cache survived with the same capture revision");
              rejectAccess=false;
              const incomplete = fixture("C"); incomplete.capture.readiness.share=false;
              global.__desktopTest.setState(incomplete); global.__desktopTest.render();
              if (!get("bind-confirm").hidden) throw new Error("incomplete capture reached save");
              if (get("auto-verify-status").textContent !== "请在领克 App 首页打开一篇文章，分享一次") throw new Error("wrong missing-share prompt");
            })().catch((error)=>{console.error(error);process.exit(1)});
            '''.replace("__APP_PATH__", json.dumps(str(APP))).replace("{{", "{").replace("}}", "}").replace("});", "});")
        )
        result = subprocess.run(["node", "-e", script], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_binding_entry_starts_proxy_once_and_waits_for_phone_configuration(self):
        """Starting the computer proxy only prepares step one; the user advances it."""
        script = textwrap.dedent(
            '''\
            const fs = require("fs");
            const appSource = fs.readFileSync(__APP_PATH__, "utf8");
            const tick = () => new Promise((resolve) => setImmediate(resolve));
            const ticks = async (count = 8) => {{ for (let index = 0; index < count; index += 1) await tick(); }};
            const response = (data) => ({{ ok: true, json: async () => ({{ ok: true, data }}), blob: async () => ({{}}) }});
            const fixture = (running = false, pairUrl = null, paired = false, events = []) => ({{
              hasIdentity: true, connected: true, configured: true, binding: null,
              candidate: null, capture: {{ stage: "idle", events }},
              proxy: {{ running, paired, pairUrl, address: "192.168.1.8", port: 55255 }},
              scheduleWindows: {{ items: [] }}, runs: {{ items: [], nextCursor: null }},
            }});
            class Element {{
              constructor(id) {{ this.id = id; this.disabled = false; this.hidden = false; this.checked = false; this.value = ""; this.options = []; this.dataset = {{}}; this.textContent = ""; this.listeners = {{}}; const classes = new Set(); this.classList = {{ add: (...items) => items.forEach((item) => classes.add(item)), remove: (...items) => items.forEach((item) => classes.delete(item)), toggle: (item, force) => {{ force ? classes.add(item) : classes.delete(item); }}, contains: (item) => classes.has(item) }}; }}
              addEventListener(type, handler) {{ (this.listeners[type] ||= []).push(handler); }}
              setAttribute() {{}} removeAttribute() {{}} replaceChildren(...items) {{ this.options = items; if (!this.value && items[0]?.value != null) this.value = items[0].value; }} append(item) {{ this.options.push(item); }} prepend() {{}} remove() {{}}
              querySelector() {{ return new Element("child"); }} querySelectorAll() {{ return []; }} closest() {{ return this; }} focus() {{}}
            }}
            function boot(networks) {{
              const elements = new Map(); const get = (id) => elements.has(id) ? elements.get(id) : (elements.set(id, new Element(id)), elements.get(id));
              global.document = {{ hidden: false, activeElement: null, getElementById: get, querySelector: (selector) => selector === "main" ? get("main") : null, querySelectorAll: () => [], createElement: () => new Element("created"), createTextNode: () => new Element("text") }};
              global.window = {{}}; global.location = {{ hash: "", pathname: "/", search: "" }}; global.history = {{ replaceState() {{}} }}; global.sessionStorage = {{ getItem: () => "fixture-token", setItem() {{}} }}; global.AbortSignal = {{ timeout: () => ({{}}) }}; global.setInterval = () => ({{}}); global.fetch = async (path) => {{
                calls.push(path);
                if (path === "/api/networks") return response(networks);
                if (path === "/api/capture/start") {{ currentState = fixture(true, "pair-1"); return response({{}}); }}
                if (path === "/api/capture/qr") return response(null);
                if (path === "/api/status") return response(currentState);
                throw new Error(`unexpected request ${{path}}`);
              }};
              const source = appSource.replace(/  start\\(\\);\\r?\\n\\}\\)\\(\\);/, "  global.__desktopTest = {{ navigate, render, setState: (next) => {{ state = next; currentState = next; }} }};\\n}})();");
              eval(source); return {{ get }};
            }}
            const calls = []; let currentState = fixture();
            (async () => {{
              const first = boot([{{ address: "192.168.1.8", name: "Wi-Fi" }}]);
              global.__desktopTest.setState(fixture()); global.__desktopTest.navigate("bind"); global.__desktopTest.navigate("bind"); await ticks(12);
              if (calls.filter((path) => path === "/api/capture/start").length !== 1) throw new Error(`expected one start, got ${{calls.join(",")}}`);
              if (first.get("pairing-step").hidden || !first.get("capture-step").hidden) throw new Error("proxy start skipped phone configuration");
              if (!calls.includes("/api/capture/qr")) throw new Error("step one did not request the pairing QR");
              if (!first.get("proxy-next").disabled) throw new Error("advance enabled before phone pairing");
              global.__desktopTest.setState(fixture(true, "pair-1", true)); global.__desktopTest.render();
              if (first.get("proxy-next").disabled) throw new Error("advance remained disabled after phone pairing");
              first.get("proxy-next").listeners.click[0]();
              if (first.get("capture-step").hidden || !first.get("pairing-step").hidden) throw new Error("explicit advance did not show capture step");

              const resumed = boot([{{ address: "192.168.1.8", name: "Wi-Fi" }}]);
              global.__desktopTest.setState(fixture(true, "pair-2", true)); global.__desktopTest.navigate("bind"); await ticks(8);
              if (resumed.get("pairing-step").hidden) throw new Error("reload treated pairing alone as phone proxy configured");
              global.__desktopTest.setState(fixture(true, "pair-2", true, [{{ outcome: "tunnel" }}]));
              global.__desktopTest.render();
              if (resumed.get("pairing-step").hidden) throw new Error("phone traffic skipped explicit configuration confirmation");
              const captured = fixture(true, "pair-2", true);
              captured.capture.stage = "captured";
              global.__desktopTest.setState(captured); global.__desktopTest.render();
              if (resumed.get("capture-step").hidden) throw new Error("existing capture was lost after reload");

              calls.length = 0; currentState = fixture();
              boot([]); global.__desktopTest.setState(fixture()); global.__desktopTest.navigate("bind"); await ticks(8);
              if (calls.includes("/api/capture/start")) throw new Error("started proxy without a network");
            }})().catch((error) => {{ console.error(error); process.exit(1); }});
            '''.replace("__APP_PATH__", json.dumps(str(APP))).replace("{{", "{").replace("}}", "}")
        )
        result = subprocess.run(["node", "-e", script], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_quit_terminates_startup_and_late_poll_results(self):
        """A terminating local assistant must not restart polling or overwrite its notice."""
        script = textwrap.dedent(
            '''\
            const fs = require("fs");
            const appSource = fs.readFileSync(__APP_PATH__, "utf8");
            const tick = () => new Promise((resolve) => setImmediate(resolve));
            const ticks = async (count = 4) => {{ for (let index = 0; index < count; index += 1) await tick(); }};
            const deferred = () => {{
              let resolve, reject;
              const promise = new Promise((ok, fail) => {{ resolve = ok; reject = fail; }});
              return {{ promise, resolve, reject }};
            }};
            const response = (data) => ({{ json: async () => ({{ ok: true, data }}) }});
            const failure = (error) => ({{ json: async () => ({{ ok: false, error }}) }});
            const fixture = (running = false) => ({{
              hasIdentity: true, connected: true, configured: true, binding: null,
              candidate: null, capture: {{ stage: "idle", events: [] }},
              proxy: {{ running, paired: false, pairUrl: null, address: "127.0.0.1", port: 8080 }},
              scheduleWindows: {{ items: [] }}, runs: {{ items: [], nextCursor: null }},
            }});
            class Element {{
              constructor(id) {{
                this.id = id; this.disabled = false; this.hidden = false; this.checked = false;
                this.value = ""; this.dataset = {{}}; this.textContent = ""; this.listeners = {{}};
                const classes = new Set();
                this.classList = {{
                  add: (...items) => items.forEach((item) => classes.add(item)),
                  remove: (...items) => items.forEach((item) => classes.delete(item)),
                  toggle: (item, force) => {{ force ? classes.add(item) : classes.delete(item); }},
                  contains: (item) => classes.has(item),
                }};
              }}
              addEventListener(type, handler) {{ (this.listeners[type] ||= []).push(handler); }}
              setAttribute() {{}} removeAttribute() {{}} replaceChildren() {{}} append() {{}} prepend() {{}} remove() {{}}
              querySelector() {{ return new Element("child"); }} querySelectorAll() {{ return []; }}
              closest() {{ return this; }} focus() {{}}
            }}
            function boot(fetchImpl, autoStart = true) {{
              const elements = new Map();
              const get = (id) => {{
                if (!elements.has(id)) elements.set(id, new Element(id));
                return elements.get(id);
              }};
              const timers = [], cleared = [];
              global.document = {{
                hidden: false, activeElement: null,
                getElementById: get,
                querySelector: (selector) => selector === "main" ? get("main") : null,
                querySelectorAll: (selector) => selector === "button" ? [get("quit")] : [],
                createElement: () => new Element("created"), createTextNode: () => new Element("text"),
              }};
              global.window = {{}};
              global.location = {{ hash: "", pathname: "/", search: "" }};
              global.history = {{ replaceState() {{}} }};
              global.sessionStorage = {{ getItem: () => "fixture-token", setItem() {{}} }};
              global.AbortSignal = {{ timeout: () => ({{}}) }};
              global.setInterval = (callback) => {{ const timer = {{ callback, active: true }}; timers.push(timer); return timer; }};
              global.clearInterval = (timer) => {{ if (timer) {{ timer.active = false; cleared.push(timer); }} }};
              global.fetch = fetchImpl;
              const source = autoStart ? appSource : appSource.replace(
                /  start\\(\\);\\r?\\n\\}\\)\\(\\);/,
                "  global.__desktopTest = {{ poll, setState: (next) => {{ state = next; }} }};\\n})();",
              );
              eval(source);
              return {{ get, timers, cleared }};
            }}
            const click = (element) => element.listeners.click[0]({{ currentTarget: element }});
            (async () => {{
              const initialStatus = deferred();
              let startupStatusCalls = 0;
              const startup = boot((path) => {{
                if (path === "/api/status") return startupStatusCalls++ === 0 ? initialStatus.promise : Promise.resolve(response(fixture()));
                if (path === "/api/networks") return Promise.resolve(response([]));
                if (path === "/api/refresh") return Promise.resolve(response({{}}));
                if (path === "/api/quit") return Promise.resolve(response({{}}));
                throw new Error(`unexpected startup request ${{path}}`);
              }});
              await ticks();
              click(startup.get("quit"));
              await ticks();
              initialStatus.resolve(response(fixture()));
              await ticks(8);
              if (startup.timers.length !== 0) throw new Error("startup installed a poll timer after quit");
              if (startup.get("notice").textContent !== "助手已退出，可以关闭此页面。") throw new Error("startup overwrote quit notice");

              const running = boot((path) => {{
                if (path === "/api/status") return Promise.resolve(response(fixture()));
                if (path === "/api/networks") return Promise.resolve(response([]));
                if (path === "/api/refresh") return Promise.resolve(response({{}}));
                if (path === "/api/quit") return Promise.resolve(response({{}}));
                throw new Error(`unexpected running request ${{path}}`);
              }});
              await ticks(10);
              if (running.timers.length !== 1 || !running.timers[0].active) throw new Error("startup did not retain its poll timer");
              click(running.get("quit"));
              await ticks();
              if (running.cleared.length !== 1 || running.timers[0].active) throw new Error("quit did not clear the active poll timer");

              const pendingPoll = deferred();
              const latePoll = boot((path) => {{
                if (path === "/api/status") return pendingPoll.promise;
                if (path === "/api/quit") return Promise.resolve(response({{}}));
                throw new Error(`unexpected poll request ${{path}}`);
              }}, false);
              global.__desktopTest.setState(fixture());
              const inFlight = global.__desktopTest.poll();
              await ticks();
              click(latePoll.get("quit"));
              await ticks();
              pendingPoll.resolve(response(fixture()));
              await inFlight;
              await ticks();
              if (latePoll.get("notice").textContent !== "助手已退出，可以关闭此页面。") throw new Error("late poll overwrote quit notice");
              if (!latePoll.get("main").classList.contains("is-disconnected")) throw new Error("late poll cleared disconnected main state");
              if (latePoll.get("disconnected-state").hidden) throw new Error("late poll hid disconnected state");

              const retryable = boot((path) => {{
                if (path === "/api/quit") return Promise.resolve(failure("服务暂时异常"));
                throw new Error(`unexpected retry request ${{path}}`);
              }}, false);
              global.__desktopTest.setState(fixture());
              click(retryable.get("quit"));
              await ticks();
              if (retryable.get("quit").disabled) throw new Error("reachable JSON failure did not restore quit");
              if (retryable.get("quit").classList.contains("is-loading")) throw new Error("reachable JSON failure kept loading state");
              if (retryable.get("notice").textContent !== "服务暂时异常") throw new Error("reachable JSON error was not shown");

              const unreachable = boot((path) => {{
                if (path === "/api/quit") return Promise.reject(new TypeError("network down"));
                throw new Error(`unexpected network request ${{path}}`);
              }}, false);
              global.__desktopTest.setState(fixture());
              click(unreachable.get("quit"));
              await ticks();
              if (!unreachable.get("quit").disabled) throw new Error("unreachable helper left quit enabled");
              if (!unreachable.get("main").classList.contains("is-disconnected")) throw new Error("unreachable helper did not enter disconnected state");
              if (unreachable.get("notice").textContent !== "本机连接已断开，请重新双击打开助手。") throw new Error("unreachable helper did not show recovery message");
            }})().catch((error) => {{ console.error(error); process.exit(1); }});
            '''.replace("__APP_PATH__", json.dumps(str(APP))).replace("{{", "{").replace("}}", "}")
        )
        result = subprocess.run(
            ["node", "-e", script], text=True, capture_output=True, cwd=ROOT
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
