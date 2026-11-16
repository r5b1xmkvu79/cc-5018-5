"""HTTP control plane: REST API, SSE events, fMP4 video stream, raw samples."""
import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .core import BroadcastKernel
from .demo.library import DEMO_REPLAY_PORT, DemoLibrary


class ControlServer:
    def __init__(self, base_dir, host="127.0.0.1", port=8080):
        self.base_dir = Path(base_dir)
        self.kernel = BroadcastKernel(self.base_dir)
        self.host = host
        self.port = port
        self._subscribers = []
        self._subs_lock = threading.Lock()
        self.kernel.add_listener(self._broadcast)
        self.demos = DemoLibrary(self.base_dir, notify=self._broadcast)
        self.httpd = None

    def _broadcast(self, event):
        with self._subs_lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    def serve(self):
        kernel = self.kernel
        base_dir = self.base_dir
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, obj, status=200):
                body = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/":
                    return self._static("index.html", "text/html; charset=utf-8")
                if path == "/api/state":
                    return self._json(kernel.snapshot())
                if path == "/api/demo/state":
                    return self._json(server._demo_state())
                if path == "/api/demo/catalog":
                    return self._json(server.demos.catalog())
                if path == "/api/events":
                    return self._sse()
                if path == "/video":
                    return self._video()
                if path == "/api/demo/video":
                    return self._demo_video()
                if path == "/api/captures":
                    return self._json(server._capture_list())
                if path == "/api/demo/assets":
                    return self._json(server.demos.catalog())
                if path.startswith("/api/demo/assets/"):
                    return self._download(path[len("/api/demo/assets/"):])
                if path.startswith("/captures/"):
                    return self._download(path[len("/captures/"):])
                if path.startswith("/demos/"):
                    return self._download_demo(path[len("/demos/"):])
                self._json({"error": "not found"}, 404)

            def do_POST(self):
                path = urlparse(self.path).path
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    return self._json({"error": "bad json"}, 400)
                try:
                    result = server._dispatch(path, payload)
                except (RuntimeError, ValueError) as exc:
                    return self._json({"error": str(exc)}, 409)
                self._json(result or {"ok": True})

            def _static(self, name, ctype):
                f = Path(__file__).resolve().parent / "web" / name
                if not f.exists():
                    return self._json({"error": "missing ui"}, 500)
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _sse(self):
                q = queue.Queue(maxsize=100)
                with server._subs_lock:
                    server._subscribers.append(q)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    while True:
                        event = q.get(timeout=15)
                        self.wfile.write(
                            f"event: kernel\ndata: {json.dumps({'event': event})}\n\n"
                            .encode())
                        self.wfile.flush()
                except (OSError, queue.Empty):
                    pass
                finally:
                    with server._subs_lock:
                        if q in server._subscribers:
                            server._subscribers.remove(q)

            def _video(self):
                if not kernel.receiver:
                    return self._json({"error": "receiver not running"}, 409)
                rx = kernel.receiver
                proc = rx.add_stream()
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    while True:
                        chunk = proc.stdout.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    rx.remove_stream(proc)

            def _demo_video(self):
                session = server.demos.session
                if not session:
                    return self._json({"error": "demo replay not running"}, 409)
                rx = session.receiver
                proc = rx.add_stream()
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    while True:
                        chunk = proc.stdout.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    rx.remove_stream(proc)

            def _download(self, name):
                f = (base_dir / "captures" / Path(name).name).resolve()
                if not str(f).startswith(str(base_dir.resolve())) or not f.exists():
                    return self._json({"error": "not found"}, 404)
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{f.name}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _download_demo(self, name):
                root = (base_dir / "demos").resolve()
                f = (root / Path(name)).resolve()
                if not f.is_relative_to(root) or not f.is_file():
                    return self._json({"error": "not found"}, 404)
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{f.name}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.serve_forever()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
        self.demos.stop_replay()
        self.kernel.stop_pipeline()

    def _dispatch(self, path, payload):
        k = self.kernel
        if path == "/api/pipeline/start":
            k.start_pipeline()
        elif path == "/api/pipeline/stop":
            k.stop_pipeline()
        elif path == "/api/mode":
            k.restart_with_mode(payload["mode"])
        elif path == "/api/channel":
            k.switch_channel(payload["channel"])
        elif path == "/api/inject":
            k.set_injection(loss_rate=payload.get("loss_rate"),
                            duplicate_rate=payload.get("duplicate_rate"),
                            reorder_window=payload.get("reorder_window"))
        elif path == "/api/burst":
            k.trigger_burst(int(payload.get("length", 10)))
        elif path == "/api/block":
            k.block(float(payload.get("seconds", 3)))
        elif path == "/api/scenario":
            k.run_scenario(payload["name"])
        elif path == "/api/demo/replay":
            session = self.demos.start_replay(
                payload["asset"], speed=payload.get("speed", 1.0),
                port=DEMO_REPLAY_PORT,
                program_id=payload.get("program"))
            return {"ok": True, "demo": session,
                    "catalog": self.demos.catalog()}
        elif path == "/api/demo/stop":
            self.demos.stop_replay()
            return {"ok": True, "demo": self.demos.state()}
        elif path == "/api/demo/inject":
            self.demos.inject(
                reorder_packets=payload.get("reorder_packets"),
                duplicate_packets=payload.get("duplicate_packets"))
            return {"ok": True, "demo": self.demos.state()}
        elif path == "/api/demo/program":
            self.demos.switch_program(payload["asset"], payload["program"])
            return {"ok": True, "demo": self.demos.state()}
        else:
            raise ValueError(f"unknown endpoint {path}")
        return k.snapshot()

    def _demo_state(self):
        state = self.demos.state()
        state["receiver_endpoint"] = (
            f"rtp://127.0.0.1:{DEMO_REPLAY_PORT} "
            "(UDP, the same Receiver that handles live broadcast)")
        return state

    def _capture_list(self):
        d = self.base_dir / "captures"
        if not d.exists():
            return {"files": []}
        return {"files": sorted(
            ({"name": f.name, "size": f.stat().st_size,
              "mtime": f.stat().st_mtime} for f in d.iterdir()),
            key=lambda x: x["mtime"], reverse=True)}
