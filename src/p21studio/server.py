"""Local web GUI: a tiny JSON API (standard library only) plus the static editor page.

Security model: by default the server only listens on loopback and rejects
requests whose Host is not loopback (DNS rebinding) or whose Origin is another
site (CSRF). POSTs must be application/json, which browsers cannot send
cross-site without a CORS preflight we never answer. In LAN mode (--host
0.0.0.0) every request also needs a random token handed out in the start-up URL.
"""

from __future__ import annotations

import base64
import hmac
import json
import re
import secrets
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import __version__, api, config, discovery, fonts, label as labelmod
from .printer import PrinterError, PrinterNotReady
from .transports import BleTransport, MockTransport, RfcommTransport, SerialTransport, TransportError, from_spec

LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
STATIC_FILES = {
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
MAX_BODY = 32 * 1024 * 1024
SERIAL_RE = re.compile(r"^(COM\d{1,3}|/dev/(tty|cu|rfcomm)[\w.\-]*)$", re.I)


def _web_file(name: str) -> bytes | None:
    try:
        return resources.files("p21studio").joinpath("web", name).read_bytes()
    except OSError:
        return None


def _only_data_uris(label) -> dict:
    """The GUI embeds images as data: URIs; refuse other local paths so the API cannot read files.

    Exception: images inside the templates folder (assets of saved templates).
    Checked again after variable substitution, so {{values}} cannot point elsewhere.
    """
    from .templates import is_template_asset

    if not isinstance(label, dict):
        raise labelmod.LabelError("Missing label")
    for el in label.get("elements") or []:
        if not (isinstance(el, dict) and el.get("type") == "image" and el.get("src")):
            continue
        src = str(el["src"])
        if src.startswith("data:"):
            continue
        if "{{" not in src and is_template_asset(src):
            continue
        if "{{" in src and is_template_asset(labelmod.VAR_RE.sub("x", src)):
            continue  # validated again after substitution
        raise labelmod.LabelError("The GUI only supports uploaded images (data: URI)")
    return label


def _checked_labels(lab: dict, values, rows) -> None:
    for l in api.expand(labelmod.normalize(lab), values, rows):
        _only_data_uris(l)


def safe_device(spec: str | None) -> str | None:
    """Device specs accepted over HTTP: real printer transports or the path-less mock.

    file: and mock:<dir> would let a web request write files anywhere, so they are CLI-only.
    """
    if spec in (None, ""):
        return None
    spec = str(spec).strip()
    if spec in ("mock:", "mock", "auto"):
        return spec
    t = from_spec(spec)
    if isinstance(t, (RfcommTransport, BleTransport)):
        return spec
    if isinstance(t, SerialTransport) and SERIAL_RE.match(t.port):
        return spec
    if isinstance(t, MockTransport):
        raise TransportError("The GUI cannot use mock with a save path (use mock:)")
    raise TransportError(f"Device not allowed from the GUI: {spec}")


class Handler(BaseHTTPRequestHandler):
    server_version = f"p21studio/{__version__}"
    default_device: str | None = None
    loopback_only = True
    token: str | None = None
    timeout = 30  # seconds per socket read: a client cannot hold a thread forever

    def log_message(self, fmt, *args):  # quiet
        pass

    # helpers -------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

    def _error(self, e: Exception) -> None:
        if isinstance(e, PrinterNotReady):
            self._json({"ok": False, "error": "not_ready", "message": str(e)}, 409)
        elif isinstance(e, (TransportError, PrinterError)):
            self._json({"ok": False, "error": "connection", "message": str(e)}, 502)
        elif isinstance(e, (labelmod.LabelError, ValueError, KeyError, TypeError)):
            self._json({"ok": False, "error": "label", "message": str(e)}, 400)
        else:
            traceback.print_exc()
            self._json({"ok": False, "error": "internal", "message": f"{type(e).__name__}: {e}"}, 500)

    def _hostname(self) -> str:
        host = self.headers.get("Host") or ""
        return host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]

    def _token_ok(self, query: dict) -> bool:
        if not self.token:
            return True
        cookie = self.headers.get("Cookie") or ""
        m = re.search(r"(?:^|;\s*)p21token=([\w\-]+)", cookie)
        given = query.get("token") or (m.group(1) if m else "")
        return hmac.compare_digest(given.encode("utf-8", "replace"), self.token.encode())

    def _allowed(self, query: dict, api_call: bool) -> str | None:
        """Return an error message if the request must be refused."""
        if self.loopback_only and self._hostname() not in LOOPBACK:
            return "forbidden host"  # DNS rebinding
        if not self._token_ok(query):
            return "This server only works with the token URL (open the URL shown at startup)"
        if api_call:
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{self.headers.get('Host')}":
                return "cross-origin request refused"
            site = self.headers.get("Sec-Fetch-Site")
            if site and site not in ("same-origin", "none"):
                return "cross-site request refused"
        return None

    def _body(self) -> dict:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ValueError("Content-Type must be application/json")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Invalid Content-Length")
        if n < 0:
            raise ValueError("Invalid Content-Length")
        if n > MAX_BODY:
            raise ValueError("Request too large")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("Send a JSON object")
        return data

    def _device(self, value) -> str | None:
        return safe_device(value) if value else (self.default_device or None)

    # routes --------------------------------------------------------------
    def do_GET(self) -> None:
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        refusal = self._allowed(q, url.path.startswith("/api/"))
        if refusal:
            return self._send(403, refusal.encode(), "text/plain; charset=utf-8")
        try:
            if url.path in ("/", "/index.html"):
                headers = {}
                if self.token and q.get("token"):
                    headers["Set-Cookie"] = f"p21token={self.token}; HttpOnly; SameSite=Strict; Path=/"
                return self._send(200, _web_file("index.html") or b"missing", "text/html; charset=utf-8", headers)
            if url.path.startswith("/static/"):
                name = url.path[len("/static/") :]
                data = _web_file(name) if name in STATIC_FILES else None
                if data is None:
                    return self._send(404, b"not found", "text/plain")
                return self._send(200, data, STATIC_FILES[name])
            if url.path == "/api/meta":
                return self._json({
                    "version": __version__,
                    "media_presets": labelmod.MEDIA_PRESETS,
                    "device": self.default_device or config.default_device(),
                    "fonts_preferred": fonts.available_preferred(),
                    "example": labelmod.EXAMPLE,
                })
            if url.path == "/api/templates":
                from . import templates

                items = []
                for t in templates.list_templates():
                    if "error" not in t:
                        try:
                            t["png"] = "data:image/png;base64," + base64.b64encode(api.render_label(templates.load_template(t["name"])).png_bytes(1)).decode()
                        except Exception:
                            t["png"] = None
                    t.pop("path", None)
                    items.append(t)
                return self._json({"ok": True, "templates": items})
            if url.path == "/api/fonts":
                return self._json({"families": fonts.families(refresh=q.get("refresh") == "1")})
            if url.path == "/api/info":
                return self._json({"ok": True, **api.printer_info(self._device(q.get("device")))})
            if url.path == "/api/scan":
                timeout = min(max(float(q.get("timeout", 5)), 1), 20)
                return self._json(discovery.discover(ble=q.get("ble", "1") == "1", ble_timeout=timeout))
            return self._send(404, b"not found", "text/plain")
        except Exception as e:
            self._error(e)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        refusal = self._allowed({k: v[0] for k, v in parse_qs(url.query).items()}, True)
        if refusal:
            return self._send(403, refusal.encode(), "text/plain; charset=utf-8")
        try:
            body = self._body()
            if url.path == "/api/render":
                lab = _only_data_uris(body.get("label"))
                _checked_labels(lab, body.get("values"), None)
                r = api.render_label(lab, body.get("values"))
                png = base64.b64encode(r.png_bytes(1)).decode()
                return self._json({
                    "ok": True,
                    "png": "data:image/png;base64," + png,
                    "width": r.size[0],
                    "height": r.size[1],
                    "warnings": r.warnings,
                    "errors": r.errors,
                    "boxes": r.boxes,
                    "variables": labelmod.find_variables(labelmod.normalize(lab)),
                })
            if url.path == "/api/print":
                lab = _only_data_uris(body.get("label"))
                rows = body.get("rows")
                if rows is not None and (not isinstance(rows, list) or len(rows) > 1000):
                    raise ValueError("rows must be an array of at most 1000 rows")
                _checked_labels(lab, body.get("values"), rows)
                res = api.print_labels(lab, self._device(body.get("device")), body.get("copies"), body.get("density"), body.get("values"), rows)
                return self._json(res)
            if url.path.startswith("/api/templates/"):
                from . import templates

                action = url.path.rsplit("/", 1)[1]
                name = str(body.get("name") or "")
                if action == "load":
                    return self._json({"ok": True, "label": {k: v for k, v in templates.load_template(name).items() if not k.startswith("_")}})
                if action == "save":
                    lab = _only_data_uris(body.get("label"))
                    templates.save_template(name, lab, overwrite=bool(body.get("overwrite")))
                    return self._json({"ok": True, "name": name})
                if action == "delete":
                    templates.delete_template(name)
                    return self._json({"ok": True, "name": name})
                if action == "exists":
                    return self._json({"ok": True, "exists": templates.exists(name)})
                return self._send(404, b"not found", "text/plain")
            if url.path == "/api/device":
                dev = safe_device(body.get("device"))
                config.set_value("device", dev)
                Handler.default_device = None
                return self._json({"ok": True, "device": dev})
            if url.path == "/api/settings":
                beep, auto_off = body.get("beep"), body.get("auto_off")
                if beep is not None and not isinstance(beep, bool):
                    raise ValueError("beep must be true / false / null")
                if auto_off is not None and auto_off not in (0, 15, 30, 60):
                    raise ValueError("auto_off must be 0 / 15 / 30 / 60 / null")
                cfg = api.apply_settings(self._device(body.get("device")), beep, auto_off)
                return self._json({"ok": True, "config": cfg})
            return self._send(404, b"not found", "text/plain")
        except Exception as e:
            self._error(e)


def serve(host: str = "127.0.0.1", port: int = 8721, open_browser: bool = True, device: str | None = None) -> None:
    Handler.default_device = device
    Handler.loopback_only = host in LOOPBACK
    Handler.token = None if Handler.loopback_only else secrets.token_urlsafe(16)
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"Cannot use port {port} ({e}). Pick another with --port.", file=sys.stderr)
        raise SystemExit(1)
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{shown}:{port}/" + (f"?token={Handler.token}" if Handler.token else "")
    print(f"P21 Studio GUI: {url}  (Ctrl+C to quit)", flush=True)
    if Handler.token:
        print("Note: other devices on the network can reach this. Only people with the token URL above (replace the IP with this PC's address) can use it.", flush=True)
    # warm up font loading so the first preview is quick
    threading.Thread(target=lambda: api.render_label(labelmod.EXAMPLE), daemon=True).start()
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
