"""VPXConfig HTTP server: the wizard UI plus a small JSON API. Standard library only."""
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__, fs, paths, system
from .generate import generate
from .inputs import dynamic_input_groups
from .ini import IniDocument
from .settings import validate
from .steps import all_fields, build_steps

WEB = paths.resource_dir() / "web"
TEMPLATE = paths.resource_dir() / "VPinballX.ini"       # wizard definition and default base, never modified
# Where VPX keeps its settings on Linux (VPX 10.8+ uses a folder per minor version). The wizard writes here unless told otherwise.
DEFAULT_OUTPUT_PARTS = (".local", "share", "VPinballX", "10.8", "VPinballX.ini")
STATE = paths.state_path()             # wizard answers, so a restart keeps them
API_HEADER = "X-VPX-Config"             # required on writes: forces a CORS preflight from other sites


def default_output():
    return Path.home().joinpath(*DEFAULT_OUTPUT_PARTS)


MAX_BASE_BYTES = 10_000_000
BACKUP_SUFFIX = ".vpconfigbackup"


class App:
    def __init__(self, template=TEMPLATE, output=None, state=STATE):
        self.template_path, self.state_path = Path(template), Path(state)
        self.default_output_path = Path(output) if output else default_output()
        self.output_override = ""      # a path chosen by the user; blank = the default location
        # The project's ini defines the wizard (labels, defaults, options) and is the base when none is chosen.
        self.template_text = self.template_path.read_text(encoding="utf-8", errors="replace")
        self.lock = threading.Lock()
        self.base_text, self.base_path, self.base_backup, self.base_error = self.template_text, "", "", ""
        saved = {}
        try:
            saved = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            pass
        if not isinstance(saved, dict):
            saved = {}
        if saved.get("base_path"):
            try:
                self.base_text, self.base_path = self._read_base(saved["base_path"])
                backup = Path(saved.get("base_backup") or "")
                self.base_backup = str(backup) if backup.name and backup.is_file() else ""
            except ValueError as e:
                self.base_error = f"Saved base file not used: {e}"
        try:
            self.output_override = self._checked_output(saved.get("output_path"))
        except ValueError:
            pass                       # a saved path that is no longer acceptable falls back to the default
        self._rebuild()
        self.state = {"values": self._initial_values()}
        # saved answers win over the suggested starting ones (a cleared answer is saved as "")
        self.state["values"].update({k: v for k, v in (saved.get("values") or {}).items() if k in self.fields})

    def _rebuild(self):
        """Steps and fields: the wizard definition, plus the [Input] lines of a loaded base file that it doesn't define
        (a connected game controller adds device, sensor and mapping lines; their ids are read from the file)."""
        steps = build_steps(IniDocument(self.template_text))
        if self.base_path:
            static_keys = {f["key"] for f in all_fields(steps).values() if f["section"] == "Input"}
            extra = dynamic_input_groups(IniDocument(self.base_text).keys("Input"), static_keys, IniDocument(self.template_text))
            if extra:
                next(s for s in steps if s["id"] == "input")["groups"] += extra
        self.steps, self.fields = steps, all_fields(steps)

    @property
    def output_path(self):
        return Path(self.output_override) if self.output_override else self.default_output_path

    def _checked_output(self, raw):
        """Validate a user chosen output path. Returns it normalised, or "" for the default location."""
        raw = (raw or "").strip().strip("\"'")
        if not raw:
            return ""
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError("Enter the full path, starting with / or ~")
        path = Path(os.path.abspath(path))
        if path.is_dir():
            raise ValueError("That is a folder; add the file name, for example VPinballX.ini")
        if path.name.endswith(BACKUP_SUFFIX):
            raise ValueError("That is a backup file name; choose the ini file to write")
        try:
            if path.exists() and path.resolve() == self.template_path.resolve():
                raise ValueError("That is VPXConfig's own base file, which is never overwritten")
        except OSError:
            pass
        return str(path)

    def set_output(self, raw):
        """Choose where the new ini is written (blank = VPX's default location)."""
        override = self._checked_output(raw)
        with self.lock:
            self.output_override = override
            self._persist()
            return self.public_state()

    def _initial_values(self):
        """Answers the wizard starts with: the `initial` of each Setting that has one."""
        return {fid: f["initial"] for fid, f in self.fields.items() if f["initial"]}

    def _read_base(self, raw):
        """Validate and read a user supplied ini path. Blank means the project's ini."""
        raw = (raw or "").strip().strip("\"'")
        if not raw:
            return self.template_text, ""
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError("Enter the full path, starting with / or ~")
        try:
            if not path.is_file():
                raise ValueError("No file at that path")
            if path.stat().st_size > MAX_BASE_BYTES:
                raise ValueError("That file is too large to be a VPinballX.ini")
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise ValueError(f"Cannot read that file ({e.strerror})")
        if not IniDocument(text).has_section("Player"):
            raise ValueError("That doesn't look like a VPinballX.ini (no [Player] section)")
        return text, str(path)

    @staticmethod
    def _backup(path, consequence="Nothing was loaded"):
        """Copy the chosen ini next to itself as <name>.vpconfigbackup; never overwrite a different backup."""
        path = Path(path)
        target = path.with_name(path.name + BACKUP_SUFFIX)
        try:
            if target.exists():
                if target.read_bytes() == path.read_bytes():
                    return str(target)
                target = path.with_name(f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}{BACKUP_SUFFIX}")
            shutil.copy2(path, target)
        except OSError as e:
            raise ValueError(f"Could not create the backup next to that file ({e.strerror}). {consequence}")
        return str(target)

    def public_state(self):
        name = Path(self.base_path).name if self.base_path else self.template_path.name
        out = self.output_path
        return {**self.state,
                "base": {"path": self.base_path, "name": name, "is_default": not self.base_path,
                         "backup": self.base_backup, "error": self.base_error},
                "output": {"path": str(out), "name": out.name, "override": self.output_override,
                           "is_default": not self.output_override, "exists": out.is_file(), "folder_exists": out.parent.is_dir(),
                           "default_path": str(self.default_output_path)}}

    def _persist(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({**self.state, "base_path": self.base_path, "base_backup": self.base_backup,
                                              "output_path": self.output_override}, indent=2))

    def set_base(self, raw):
        """Use another ini as the base and fill the wizard answers from its values."""
        text, path = self._read_base(raw)
        backup = self._backup(path) if path else ""
        doc = IniDocument(text)
        with self.lock:
            self.base_text, self.base_path, self.base_backup, self.base_error = text, path, backup, ""
            self._rebuild()
            self.state["values"] = ({fid: v for fid, f in self.fields.items() if (v := doc.get(f["section"], f["key"]) or "")}
                                    if path else self._initial_values())
            self._persist()
            return self.public_state()

    def info(self):
        return {"template": self.template_path.name, "output": self.output_path.name, "version": __version__}

    def update_state(self, body):
        with self.lock:
            for k, v in (body.get("values") or {}).items():
                if k in self.fields:
                    self.state["values"][k] = str(v)
            self._persist()
            return self.public_state()

    def preview(self):
        text, todo = generate(self.base_text, self.fields, self.state["values"])
        # Only values about to be written are checked, so an odd value already in the base file doesn't block saving.
        problems = [{"id": c["id"], "step": c["step"], "parent": c["parent"], "group": c["group"], "label": c["label"], "value": c["value"], "message": msg}
                    for c in todo if (msg := validate(self.fields[c["id"]], c["value"]))]
        return {"changes": todo, "text": text, "problems": problems, "base": self.public_state()["base"]}

    def save(self):
        p = self.preview()
        if p["problems"]:
            n = len(p["problems"])
            return {"error": f"{n} value{'s are' if n != 1 else ' is'} not acceptable. Fix {'them' if n != 1 else 'it'} first.",
                    "problems": p["problems"]}
        path = self.output_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            backup = self._backup(path, "Nothing was written") if path.is_file() else ""
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(p["text"], encoding="utf-8")
            if path.is_file():
                shutil.copymode(path, tmp)         # keep the permissions of the file being replaced
            tmp.replace(path)
        except ValueError as e:
            return {"error": str(e)}
        except OSError as e:
            return {"error": f"Could not write {path} ({e.strerror}). Nothing was changed."}
        return {"file": path.name, "path": str(path), "count": len(p["changes"]), "backup": backup}


def make_handler(app, allowed_hosts, shutdown=None):   # shutdown: called to stop the server (the endpoint is off without it)
    class Handler(BaseHTTPRequestHandler):
        server_version = "VPXConfig"

        def log_message(self, fmt, *args):
            print(f"{self.address_string()} {fmt % args}")

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, write):
            if allowed_hosts and self.headers.get("Host") not in allowed_hosts:
                self._send(403, {"error": "unexpected Host header"})
                return False
            if write and not self.headers.get(API_HEADER):
                self._send(403, {"error": f"missing {API_HEADER} header"})
                return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if not self._guard(False):
                return
            url = urlsplit(self.path)
            path, query = url.path, parse_qs(url.query)
            if path == "/api/fs":
                try:
                    return self._send(200, fs.list_dir(query.get("path", [""])[0],
                                                       show_hidden=query.get("hidden", ["1"])[0] == "1",
                                                       show_all=query.get("all", ["0"])[0] == "1",
                                                       nearest=query.get("nearest", ["0"])[0] == "1"))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if path == "/api/steps":
                return self._send(200, {"steps": app.steps, **app.info()})
            if path == "/api/state":
                return self._send(200, app.public_state())
            if path.startswith("/api/system/"):
                fn = system.COMMANDS.get(path.rsplit("/", 1)[1])
                return self._send(200, fn()) if fn else self._send(404, {"error": "unknown command"})
            if path == "/api/preview":
                return self._send(200, app.preview())
            self._static(path)

        def do_PUT(self):
            if not self._guard(True):
                return
            if self.path == "/api/state":
                return self._send(200, app.update_state(self._body()))
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._guard(True):
                return
            if self.path == "/api/shutdown" and shutdown:
                self._send(200, {"ok": True})        # answer first, then stop, so the page can say so
                self.wfile.flush()
                shutdown()
                return
            if self.path == "/api/save":
                result = app.save()
                return self._send(400 if "error" in result else 200, result)
            if self.path == "/api/output":
                try:
                    return self._send(200, app.set_output(self._body().get("path")))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if self.path == "/api/base":
                try:
                    return self._send(200, app.set_base(self._body().get("path")))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def _static(self, path):
            target = (WEB / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
            if WEB.resolve() not in target.parents or not target.is_file():
                return self._send(404, {"error": "not found"})
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), ctype)

    return Handler


def create_server(app, host, port):
    """The HTTP server for `app`, ready to serve_forever(). POST /api/shutdown stops it."""
    loopback = host in ("127.0.0.1", "localhost", "::1")
    allowed = set() if loopback else None                    # filled in once the port is known (port 0 picks a free one)
    httpd = ThreadingHTTPServer((host, port), make_handler(app, allowed, shutdown=lambda: stop(httpd)))
    if loopback:
        allowed.update({f"localhost:{httpd.server_port}", f"127.0.0.1:{httpd.server_port}", f"[::1]:{httpd.server_port}"})
    return httpd


def stop(httpd):
    """Stop serve_forever() from a request thread: shutdown() waits for the serving loop, so it runs on its own thread."""
    threading.Thread(target=httpd.shutdown, daemon=True).start()


def run_with_chrome(httpd, host, port):
    """Serve on a background thread, open google-chrome on it, and once either Chrome or the server stops, tear down
    the other too -- so Quit in the page actually closes the browser window, not just the server underneath it.
    Opt-in (see serve()'s open_chrome param): a plain `serve_forever()` with nothing watching for a browser is what
    tools/smoke_test_exe.sh (headless CI, no display) and the README's "run it, then open the page yourself" workflow
    both need to keep working, so this path is never taken unless something specifically asks for it (VPinOS's
    vpinos-menu.sh does, via run.py's --open-chrome; that project's vpinos-config.py uses the same technique for its
    own always-needs-a-window config tool)."""
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    # --app=URL (not --kiosk): a plain window with a title bar and close button, not a fullscreen one -- a config
    # tool needs a visible, obvious way to close it. --start-maximized fills the screen without suppressing that.
    chrome = subprocess.Popen([
        "google-chrome",
        f"--app=http://{host}:{port}",
        "--start-maximized",
        "--no-first-run",
        "--disable-session-crashed-bubble",
        "--noerrdialogs",
    ])

    try:
        while chrome.poll() is None and server_thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass

    if chrome.poll() is None:
        # The server stopped first (the page's own Quit button) -- close the browser window too, instead of
        # leaving an orphaned "stopped" tab open.
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome.kill()

    if server_thread.is_alive():
        # Chrome exited first (closed via its own window controls) -- stop the server the same way
        # POST /api/shutdown does.
        stop(httpd)
        server_thread.join(timeout=5)


def serve(host="127.0.0.1", port=1111, open_chrome=False):
    app = App()
    httpd = create_server(app, host, port)
    print(f"VPXConfig {__version__}: http://localhost:{port}   (base: {app.template_path.name}, writes to: {app.output_path}, "
          f"answers kept in: {app.state_path})")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: listening on a non-loopback address; anyone on the network can browse this machine's folders and change the output file.")

    if open_chrome:
        try:
            run_with_chrome(httpd, host, httpd.server_port)
        except FileNotFoundError:
            print("ERROR: --open-chrome was given but 'google-chrome' isn't on PATH.", file=sys.stderr)
        finally:
            httpd.server_close()
            print("VPXConfig stopped.")
        return

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
        print("VPXConfig stopped.")
