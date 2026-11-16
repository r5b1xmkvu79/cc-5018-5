"""Demo asset library: catalog, replay sessions, evidence for the control page."""
import threading
import time
from pathlib import Path

from . import wire
from .replay import ReplaySession, compare_declared, verify_capture
from .content import find_ffmpeg

DEMO_DIR = Path(__file__).resolve().parent.parent.parent / "demos"
ASSETS_DIR = DEMO_DIR / "assets"
# demo replay must not share the live broadcast port: SO_REUSEPORT would
# split datagrams between the two receivers and both would undercount.
DEMO_REPLAY_PORT = 5014


def _ffprobe(ffmpeg):
    return str(Path(ffmpeg).parent / "ffprobe")


class DemoLibrary:
    """Owns the demo asset catalog and at most one active replay session."""

    def __init__(self, base_dir, notify=None):
        self.base_dir = Path(base_dir)
        self.assets_dir = self.base_dir / "demos" / "assets"
        self.notify = notify or (lambda event: None)
        self.session = None
        self.last_result = None
        self.injection = {"reorder_packets": 0, "duplicate_packets": 0}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ catalog
    def catalog(self):
        items = []
        if not self.assets_dir.exists():
            return {"assets": items, "expected": {}}
        for path in sorted(self.assets_dir.glob("*.json")):
            try:
                manifest = wire.read_json(path)
            except (OSError, ValueError):
                continue
            items.append({
                "id": manifest["id"],
                "label": manifest["label"],
                "description": manifest["description"],
                "program": manifest["program"]["id"],
                "program_label": manifest["program"]["label"],
                "program_number": manifest["program"]["number"],
                "content_file": manifest["content"]["file"],
                "content_sha256": manifest["content"]["sha256"],
                "frame_count": manifest["content"]["frame_count"],
                "wire_file": manifest["wire"]["file"],
                "wire_sha256": manifest["wire"]["sha256"],
                "wire_packets": manifest["wire"]["packet_records"],
                "duration_ms": manifest["wire"]["duration_ms"],
                "faults": manifest["fault_plan"],
                "summary": manifest["summary"],
                "expected": manifest["expected"],
            })
        return {"assets": items}

    def expected_summary(self, asset_id):
        manifest = self._manifest(asset_id)
        return manifest["expected"]

    def _manifest(self, asset_id):
        path = self.assets_dir / f"{asset_id}.json"
        if not path.exists():
            raise ValueError(f"unknown demo asset {asset_id!r}")
        manifest = wire.read_json(path)
        expected_wire = self.assets_dir / manifest["wire"]["file"]
        actual = wire.sha256_file(expected_wire)
        if actual != manifest["wire"]["sha256"]:
            raise ValueError(
                f"demo asset {asset_id!r} wire hash mismatch "
                f"(manifest {manifest['wire']['sha256']}, file {actual})")
        content_path = self.base_dir / manifest["content"]["file"]
        if not content_path.exists():
            raise ValueError(
                f"demo asset {asset_id!r} content missing: {content_path}")
        if wire.sha256_file(content_path) != manifest["content"]["sha256"]:
            raise ValueError(
                f"demo asset {asset_id!r} content hash mismatch")
        return manifest

    # ------------------------------------------------------------ replay
    def start_replay(self, asset_id, speed=1.0, port=DEMO_REPLAY_PORT,
                     program_id=None):
        self.stop_replay()
        manifest = self._manifest(asset_id)
        if program_id is not None and manifest["program"]["id"] != program_id:
            raise ValueError(
                f"asset {asset_id!r} carries program "
                f"{manifest['program']['id']!r}, not {program_id!r}")
        cap_dir = self.base_dir / "demos" / "runs"
        cap_dir.mkdir(parents=True, exist_ok=True)
        session = ReplaySession(self.base_dir, manifest, port=port,
                                speed=speed, capture_dir=cap_dir)
        with self._lock:
            self.session = session
            self.last_result = None
        session.start()
        threading.Thread(target=self._finish, args=(session,),
                         daemon=True).start()
        self.notify("demo_replay_started")
        return self._state_locked(session)

    def _finish(self, session):
        session.join()
        try:
            capture = session.receiver.snapshot()["stats"]["capture"]
            manifest = session.manifest
            if capture and Path(capture).exists():
                ffmpeg = find_ffmpeg()
                content_path = self.base_dir / manifest["content"][
                    "content_manifest"]
                content_manifest = wire.read_json(content_path)
                verification = verify_capture(
                    Path(capture), content_manifest, ffmpeg, _ffprobe(ffmpeg))
            else:
                verification, checks = {}, []
            with self._lock:
                injection = dict(self.injection)
            checks = compare_declared(
                manifest, session.receiver.snapshot(), verification,
                ffmpeg, content_manifest, injection=injection)
            capture_name = Path(capture).name if capture else None
            result = {
                "asset_id": manifest["id"],
                "finished_at": time.time(),
                "capture": (f"demos/runs/{capture_name}"
                            if capture_name else None),
                "capture_url": (f"/demos/runs/{capture_name}"
                                if capture_name else None),
                "verification": verification,
                "checks": checks,
                "passed": sum(1 for c in checks if c["passed"]),
                "failed": sum(1 for c in checks if not c["passed"]),
            }
            with self._lock:
                self.last_result = result
            self.notify("demo_replay_verified")
        except Exception as exc:
            with self._lock:
                self.last_result = {"asset_id": session.manifest["id"],
                                    "error": str(exc), "checks": []}
            self.notify("demo_replay_failed")

    def stop_replay(self):
        with self._lock:
            session = self.session
            self.session = None
        if session:
            session.stop()
            self.notify("demo_replay_stopped")

    def inject(self, reorder_packets=None, duplicate_packets=None):
        with self._lock:
            if reorder_packets is not None:
                self.injection["reorder_packets"] = max(
                    0, min(64, int(reorder_packets)))
            if duplicate_packets is not None:
                self.injection["duplicate_packets"] = max(
                    0, min(64, int(duplicate_packets)))
            session = self.session
        if session is None:
            return self.injection
        session.driver.reconfigure(**self.injection)
        self.notify("demo_injection")
        return self.injection

    def switch_program(self, asset_id, program_id):
        """Restart the replay with a different program's asset.

        The program is a property of the recorded content, so switching
        means loading another asset; the library refuses ids that do not
        match the requested program.
        """
        return self.start_replay(asset_id, program_id=program_id)

    def injection_effect(self):
        with self._lock:
            session = self.session
        return session.driver.stats["injection_effect"] if session else None

    # ------------------------------------------------------------ state
    def _state_locked(self, session):
        state = session.state()
        state["injection"] = dict(self.injection)
        return state

    def state(self):
        with self._lock:
            session = self.session
            result = self.last_result
            injection = dict(self.injection)
        if session is None:
            return {"active": False, "injection": injection,
                    "last_result": result}
        state = session.state()
        state["active"] = True
        state["injection"] = injection
        state["last_result"] = result
        return state
