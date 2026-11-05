"""WebView DOM automation through explicitly exposed DevTools sockets."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request


class WebViewMixin:
    def _webview_forward(self, socket):
        if not re.fullmatch(r"(?:webview_devtools_remote[\w.-]*|chrome_devtools_remote[\w.-]*)", socket):
            raise ValueError("socket must be an explicitly discovered WebView/Chrome DevTools socket")
        self.ensure_device()
        key = (self.serial, socket)
        cached = self._owned_forwards.get(key)
        if cached:
            return cached
        r = self.adb_result(["forward", "--no-rebind", "tcp:0", "localabstract:" + socket])
        if not r.success or not r.text.strip().isdigit():
            raise RuntimeError(f"Cannot forward WebView socket: {r.as_dict()}")
        port = int(r.text.strip())
        self._owned_forwards[key] = port
        return port

    @staticmethod
    def _devtools_json(port, path):
        # Bypass host proxies; neither a phone page nor a tool argument can choose
        # an external HTTP target. Bound the discovery response before parsing.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            payload = response.read(2*1024*1024+1)
        if len(payload) > 2*1024*1024:
            raise ValueError("DevTools response exceeds 2 MiB")
        return json.loads(payload)

    def webview(self, action="discover", socket="", page_id="", expression="", css="", value="", url="",
                timeout=10, max_nodes=100):
        if not 0 < timeout <= 60 or not 1 <= max_nodes <= 1000:
            raise ValueError("invalid timeout/max_nodes")
        if action == "discover":
            raw = self.require_shell("cat /proc/net/unix", root=True, max_bytes=262144)
            sockets = sorted(set(re.findall(r"@((?:webview|chrome)_devtools_remote[\w.-]*)", raw)))
            return {"success": True, "sockets": sockets,
                    "note": "Only WebViews with debugging enabled expose DOM. Enable WebView.setWebContentsDebuggingEnabled(true) in your debug build."}
        if action == "close":
            key = (self.serial, socket)
            port = self._owned_forwards.get(key)
            if port is None:
                return {"success": True, "changed": False}
            r = self.adb_result(["forward", "--remove", f"tcp:{port}"])
            if r.success:
                self._owned_forwards.pop(key, None)
            return r.as_dict()
        if action not in ("pages", "evaluate", "dom", "click", "fill", "navigate"):
            raise ValueError("unknown WebView action")
        port = self._webview_forward(socket)
        try:
            pages = self._devtools_json(port, "/json/list")
        except Exception:
            self._owned_forwards.pop((self.serial, socket), None)
            self.adb_result(["forward", "--remove", f"tcp:{port}"])
            raise
        if action == "pages":
            return {"success": True, "socket": socket, "pages": [
                {k: page.get(k) for k in ("id", "type", "title", "url")} for page in pages
            ]}
        candidates = [p for p in pages if p.get("type") == "page" and (not page_id or p.get("id") == page_id)]
        if len(candidates) != 1:
            raise ValueError("specify page_id from webview_control(action='pages') when several/no pages match")
        page = candidates[0]
        endpoint = urllib.parse.urlsplit(page["webSocketDebuggerUrl"])
        if not endpoint.path.startswith("/devtools/page/") or endpoint.query or endpoint.fragment:
            raise ValueError("unexpected DevTools websocket endpoint")
        # Rewrite the authority to our own ADB loopback port. Never follow an
        # arbitrary hostname supplied by a device/page response.
        ws_url = f"ws://127.0.0.1:{port}{endpoint.path}"
        if action == "navigate":
            if not url.startswith(("http://", "https://", "about:blank")):
                raise ValueError("navigate supports http/https/about:blank")
            method, params = "Page.navigate", {"url": url}
        else:
            method = "Runtime.evaluate"
            if action == "dom":
                expression = """(() => Array.from(document.querySelectorAll('a,button,input,textarea,select,[role],[contenteditable=true]')).filter(e => e.getClientRects().length).slice(0,LIMIT).map((e,i)=>({index:i,tag:e.tagName,id:e.id,text:(e.innerText||e.getAttribute('aria-label')||'').slice(0,500),name:e.name,type:e.type,value:e.type==='password'?'[redacted]':e.value,selector:e.id?'#'+CSS.escape(e.id):null,rect:(()=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}})()})))()""".replace("LIMIT", str(max_nodes))
            elif action in ("click", "fill"):
                if not css:
                    raise ValueError("click/fill requires css")
                effect = "e.click();" if action == "click" else (
                    "const proto=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
                    "const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set;"
                    f"if(setter)setter.call(e,{json.dumps(value)});else e.value={json.dumps(value)};"
                    "e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));"
                )
                expression = f"(() => {{const n=document.querySelectorAll({json.dumps(css)});if(n.length!==1)throw new Error('CSS must match exactly one element, found '+n.length);const e=n[0];{effect}return {{success:true}};}})()"
            elif not expression:
                raise ValueError("evaluate requires expression")
            params = {"expression": expression, "returnByValue": True, "awaitPromise": True,
                      "userGesture": True, "timeout": int(timeout*1000)}
        try:
            import websocket
        except ImportError:
            return {"success": False, "code": "OPTIONAL_DEPENDENCY_MISSING", "error": "Install android-root-mcp[webview] (websocket-client)"}
        ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True,
                                         http_no_proxy=["127.0.0.1", "localhost"])
        deadline = time.monotonic() + timeout
        try:
            ws.send(json.dumps({"id": 1, "method": method, "params": params}))
            while time.monotonic() < deadline:
                ws.settimeout(max(0.01, deadline-time.monotonic()))
                raw = ws.recv()
                if len(raw) > 4*1024*1024:
                    raise ValueError("DevTools websocket response exceeds 4 MiB")
                message = json.loads(raw)
                if message.get("id") != 1:
                    continue
                result = message.get("result", {})
                error = message.get("error") or result.get("exceptionDetails") or result.get("errorText")
                self.invalidate_ui()
                return {"success": not bool(error), "page_id": page["id"], "result": result, **({"error": error} if error else {})}
            return {"success": False, "error": "DevTools response timed out"}
        finally:
            ws.close()
