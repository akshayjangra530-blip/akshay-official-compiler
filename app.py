import codecs
import ctypes
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNS_ROOT = os.path.join(BASE_DIR, "_runs")
os.makedirs(RUNS_ROOT, exist_ok=True)

IS_WINDOWS = os.name == "nt"
EXE_NAME = "program.exe" if IS_WINDOWS else "program"

# Make MinGW discoverable automatically on Windows if installed in the standard path
if IS_WINDOWS and shutil.which("g++") is None and os.path.isdir(r"C:\mingw64\bin"):
    os.environ["PATH"] = r"C:\mingw64\bin;" + os.environ.get("PATH", "")

COMPILE_TIMEOUT_SEC = 15
BATCH_TIMEOUT_SEC = 5
INTERACTIVE_TIMEOUT_SEC = 120

ALLOWED_FLAGS = {
    "-std=c23",
    "-std=c17",
    "-std=c11",
    "-std=c99",
    "-std=c++23",
    "-std=c++20",
    "-std=c++17",
    "-std=c++14",
    "-std=c++11",
    "-Wall",
    "-Wextra",
    "-Werror",
    "-Wpedantic",
    "-O0",
    "-O2",
    "-O3",
    "-g",
    "-lm",
}

SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
_UNBUF_LOCK = threading.Lock()
_UNBUF_OBJ_PATH = os.path.join(tempfile.gettempdir(), "akshay-compiler-unbuf-v2.o")


def remove_path_safe(path):
    if not path or not os.path.exists(path):
        return
    try:
        if os.path.isfile(path) or os.path.islink(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def cleanup_stale_runs():
    """Remove any leftover temporary run folders from earlier sessions."""
    try:
        for entry in os.listdir(RUNS_ROOT):
            remove_path_safe(os.path.join(RUNS_ROOT, entry))
    except Exception:
        pass


def get_peak_memory_bytes(proc):
    """Return peak memory in bytes (PeakWorkingSetSize on Windows)."""
    if not proc:
        return 0
    if IS_WINDOWS:
        try:
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            handle = getattr(proc, "_handle", None)
            if not handle:
                return 0
            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(pmc)
            ok = ctypes.windll.kernel32.K32GetProcessMemoryInfo(
                int(handle), ctypes.byref(pmc), pmc.cb
            )
            return int(pmc.PeakWorkingSetSize) if ok else 0
        except Exception:
            return 0
    return 0


def kill_process_tree(proc):
    if not proc or proc.poll() is not None:
        return
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def ensure_unbuf_object():
    """Compile a small C constructor object that unbuffers stdout/stderr without altering user line numbers."""
    with _UNBUF_LOCK:
        if os.path.exists(_UNBUF_OBJ_PATH):
            return _UNBUF_OBJ_PATH
        gcc = shutil.which("gcc")
        if not gcc:
            return None
        tmp_c = os.path.join(tempfile.gettempdir(), "akshay-compiler-unbuf-v2.c")
        try:
            with open(tmp_c, "w", encoding="utf-8") as f:
                f.write(
                    "#include <stdio.h>\n"
                    "__attribute__((constructor))\n"
                    "static void _ak_live_stdio_init(void) {\n"
                    "    setvbuf(stdout, NULL, _IONBF, 0);\n"
                    "    setvbuf(stderr, NULL, _IONBF, 0);\n"
                    "}\n"
                )
            res = subprocess.run(
                [gcc, "-c", tmp_c, "-o", _UNBUF_OBJ_PATH],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            if res.returncode == 0 and os.path.exists(_UNBUF_OBJ_PATH):
                return _UNBUF_OBJ_PATH
        except Exception:
            pass
        return None


def normalize_compiler_paths(msg, src_file_path, display_name):
    normalized = (msg or "").replace("\r\n", "\n")
    for candidate in {src_file_path, src_file_path.replace("\\", "/")}:
        normalized = normalized.replace(candidate, display_name)
    return normalized


class LiveProgramSession:
    def __init__(self, exe_path, tmp_dir):
        self.exe_path = exe_path
        self.tmp_dir = tmp_dir
        self.chunks = []
        self.cond = threading.Condition()
        self.done = False
        self.exit_code = None
        self.timed_out = False
        self.duration_ms = 0
        self.memory_bytes = 0
        self.stdin_closed = False
        self.started_at = time.monotonic()

        self.proc = subprocess.Popen(
            [exe_path],
            cwd=tmp_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        self.timeout_timer = threading.Timer(INTERACTIVE_TIMEOUT_SEC, self._on_timeout)
        self.timeout_timer.daemon = True
        self.timeout_timer.start()
        threading.Thread(target=self._read_stream, daemon=True).start()

    def _on_timeout(self):
        with self.cond:
            if self.done:
                return
            self.timed_out = True
            self.chunks.append(f"\n[Execution timed out after {INTERACTIVE_TIMEOUT_SEC}s]\n")
            self.cond.notify_all()
        kill_process_tree(self.proc)

    def _read_stream(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        fd = self.proc.stdout.fileno()
        try:
            while True:
                b = os.read(fd, 4096)
                if not b:
                    break
                text = decoder.decode(b, final=False).replace("\r\n", "\n")
                if text:
                    with self.cond:
                        self.chunks.append(text)
                        self.cond.notify_all()
        except OSError:
            pass
        finally:
            tail = decoder.decode(b"", final=True).replace("\r\n", "\n")
            if tail:
                with self.cond:
                    self.chunks.append(tail)
            if self.timeout_timer:
                self.timeout_timer.cancel()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                kill_process_tree(self.proc)
                try:
                    self.proc.wait(timeout=2)
                except Exception:
                    pass
            duration_ms = int((time.monotonic() - self.started_at) * 1000)
            memory_bytes = get_peak_memory_bytes(self.proc)
            with self.cond:
                self.exit_code = self.proc.returncode if self.proc.returncode is not None else 0
                self.duration_ms = duration_ms
                self.memory_bytes = memory_bytes
                self.done = True
                self.cond.notify_all()
            time.sleep(0.08)
            remove_path_safe(self.tmp_dir)

    def write_line(self, line):
        if self.done or self.stdin_closed:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                if "\x04" in line:
                    before, _ = line.split("\x04", 1)
                    if before:
                        self.proc.stdin.write(before.encode("utf-8"))
                        self.proc.stdin.flush()
                    self.close_stdin()
                else:
                    self.proc.stdin.write((line + "\n").encode("utf-8"))
                    self.proc.stdin.flush()
        except Exception:
            self.stdin_closed = True

    def close_stdin(self):
        if self.stdin_closed:
            return
        self.stdin_closed = True
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass

    def poll_output(self, wait_ms=0):
        with self.cond:
            if wait_ms > 0 and not self.chunks and not self.done:
                self.cond.wait(timeout=wait_ms / 1000.0)
            out = "".join(self.chunks)
            self.chunks.clear()
            return out, self.done, self.exit_code, self.duration_ms, self.memory_bytes, self.timed_out

    def stop(self):
        if self.timeout_timer:
            self.timeout_timer.cancel()
        self.close_stdin()
        kill_process_tree(self.proc)
        try:
            self.proc.wait(timeout=2)
        except Exception:
            pass
        remove_path_safe(self.tmp_dir)


def sanitize_compile_request(payload):
    code = payload.get("code", "")
    if not isinstance(code, str):
        code = ""
    lang = str(payload.get("lang", "c")).lower()
    is_cpp = lang in ("cpp", "c++")
    compiler_bin = "g++" if is_cpp else "gcc"
    display_name = "program.cpp" if is_cpp else "program.c"

    raw_flags = payload.get("flags", [])
    if not isinstance(raw_flags, list):
        raw_flags = []
    safe_flags = []
    for fl in raw_flags:
        if isinstance(fl, str) and fl in ALLOWED_FLAGS and fl not in safe_flags:
            safe_flags.append(fl)
    has_lm = "-lm" in safe_flags
    compile_flags = [fl for fl in safe_flags if fl != "-lm"]

    return {
        "code": code,
        "is_cpp": is_cpp,
        "compiler_bin": compiler_bin,
        "display_name": display_name,
        "safe_flags": safe_flags,
        "compile_flags": compile_flags,
        "has_lm": has_lm,
    }


def compile_source_code(cfg, with_unbuf=False):
    tmpdir = os.path.join(RUNS_ROOT, str(uuid.uuid4()))
    os.makedirs(tmpdir, exist_ok=True)
    src_file = os.path.join(tmpdir, cfg["display_name"])
    exe_file = os.path.join(tmpdir, EXE_NAME)

    with open(src_file, "w", encoding="utf-8") as f:
        f.write(cfg["code"])

    compile_cmd = [cfg["compiler_bin"], src_file, *cfg["compile_flags"]]
    if with_unbuf:
        unbuf_obj = ensure_unbuf_object()
        if unbuf_obj:
            compile_cmd.append(unbuf_obj)
    compile_cmd.extend(["-o", exe_file])
    if cfg["has_lm"]:
        compile_cmd.append("-lm")

    start = time.monotonic()
    try:
        comp = subprocess.run(
            compile_cmd,
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        remove_path_safe(tmpdir)
        return {
            "ok": False,
            "compile_ms": int((time.monotonic() - start) * 1000),
            "output": f"Error: {cfg['compiler_bin']} not found in PATH. MinGW/GCC (with g++) install karo.",
        }
    except subprocess.TimeoutExpired:
        remove_path_safe(tmpdir)
        return {
            "ok": False,
            "compile_ms": int((time.monotonic() - start) * 1000),
            "output": "Compilation timed out.",
        }

    compile_ms = int((time.monotonic() - start) * 1000)
    warnings = normalize_compiler_paths(comp.stderr or "", src_file, cfg["display_name"])

    if comp.returncode != 0:
        raw_err = comp.stderr or comp.stdout or "Compilation failed."
        msg = normalize_compiler_paths(raw_err, src_file, cfg["display_name"])
        remove_path_safe(tmpdir)
        return {"ok": False, "compile_ms": compile_ms, "output": msg}

    remove_path_safe(src_file)
    return {
        "ok": True,
        "compile_ms": compile_ms,
        "warnings": warnings,
        "tmpdir": tmpdir,
        "exe_file": exe_file,
    }


class CompilerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        clean_path = self.path.split("?", 1)[0]

        if clean_path == "/api/info":
            gpp = shutil.which("g++")
            gcc = shutil.which("gcc")
            compiler_ver = "Not detected"
            bin_path = gpp or gcc
            if bin_path:
                try:
                    res = subprocess.run(
                        [bin_path, "--version"],
                        capture_output=True,
                        text=True,
                        timeout=3,
                    )
                    lines = (res.stdout or "").splitlines()
                    compiler_ver = lines[0] if lines else "Available"
                except Exception:
                    compiler_ver = "Available"
            self.send_json(
                {
                    "ok": True,
                    "compiler": compiler_ver,
                    "platform": f"{platform.system()} ({platform.machine()})",
                    "python": platform.python_version(),
                }
            )
            return

        if clean_path == "/":
            file_path = os.path.join(BASE_DIR, "index.html")
            content_type = "text/html; charset=utf-8"
        elif clean_path == "/style.css":
            file_path = os.path.join(BASE_DIR, "style.css")
            content_type = "text/css; charset=utf-8"
        elif clean_path == "/google37389b62083ed2a1.html":
            file_path = os.path.join(BASE_DIR, "google37389b62083ed2a1.html")
            content_type = "text/html; charset=utf-8"
        elif clean_path == "/robots.txt":
            body = (
                "User-agent: *\n"
                "Allow: /\n\n"
                "Sitemap: https://akshay-official-compiler.onrender.com/sitemap.xml\n"
            ).encode("utf-8")
            self.send_bytes(200, "text/plain; charset=utf-8", body)
            return
        elif clean_path == "/sitemap.xml":
            body = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                "  <url>\n"
                "    <loc>https://akshay-official-compiler.onrender.com/</loc>\n"
                "    <changefreq>weekly</changefreq>\n"
                "    <priority>1.0</priority>\n"
                "  </url>\n"
                "</urlset>\n"
            ).encode("utf-8")
            self.send_bytes(200, "application/xml; charset=utf-8", body)
            return
        else:
            self.send_bytes(404, "text/plain; charset=utf-8", b"Not Found")
            return

        if not os.path.exists(file_path):
            self.send_bytes(404, "text/plain; charset=utf-8", b"File not found")
            return

        with open(file_path, "rb") as f:
            data = f.read()
        self.send_bytes(200, content_type, data)

    def do_POST(self):
        if self.path not in ("/run", "/start", "/input", "/poll", "/stop"):
            self.send_json({"compile_ok": False, "output": "Not found.", "done": True})
            return

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw_body.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            self.send_json({"compile_ok": False, "output": "Invalid JSON request.", "done": True})
            return

        try:
            if self.path == "/start":
                cfg = sanitize_compile_request(payload)
                comp = compile_source_code(cfg, with_unbuf=True)
                if not comp["ok"]:
                    self.send_json({
                        "compile_ok": False,
                        "output": comp["output"],
                        "compile_ms": comp["compile_ms"],
                    })
                    return

                sid = str(uuid.uuid4())
                session = LiveProgramSession(comp["exe_file"], comp["tmpdir"])
                with SESSIONS_LOCK:
                    SESSIONS[sid] = session

                time.sleep(0.06)
                out, done, exit_code, duration_ms, memory_bytes, timed_out = session.poll_output()
                if done:
                    with SESSIONS_LOCK:
                        SESSIONS.pop(sid, None)

                self.send_json({
                    "compile_ok": True,
                    "session_id": sid,
                    "output": out,
                    "warnings": comp["warnings"],
                    "compile_ms": comp["compile_ms"],
                    "done": done,
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                    "memory_bytes": memory_bytes,
                    "timed_out": timed_out,
                })
                return

            if self.path == "/input":
                sid = payload.get("session_id", "")
                line = str(payload.get("input", ""))
                with SESSIONS_LOCK:
                    session = SESSIONS.get(sid)
                if not session:
                    self.send_json({"output": "", "done": True, "exit_code": 0})
                    return
                session.write_line(line)
                out, done, exit_code, duration_ms, memory_bytes, timed_out = session.poll_output(wait_ms=100)
                if done:
                    with SESSIONS_LOCK:
                        SESSIONS.pop(sid, None)
                self.send_json({
                    "output": out,
                    "done": done,
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                    "memory_bytes": memory_bytes,
                    "timed_out": timed_out,
                })
                return

            if self.path == "/poll":
                sid = payload.get("session_id", "")
                with SESSIONS_LOCK:
                    session = SESSIONS.get(sid)
                if not session:
                    self.send_json({"output": "", "done": True, "exit_code": 0})
                    return
                out, done, exit_code, duration_ms, memory_bytes, timed_out = session.poll_output(wait_ms=120)
                if done:
                    with SESSIONS_LOCK:
                        SESSIONS.pop(sid, None)
                self.send_json({
                    "output": out,
                    "done": done,
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                    "memory_bytes": memory_bytes,
                    "timed_out": timed_out,
                })
                return

            if self.path == "/stop":
                sid = payload.get("session_id", "")
                with SESSIONS_LOCK:
                    session = SESSIONS.pop(sid, None)
                if session:
                    session.stop()
                self.send_json({"stopped": True})
                return

            # Batch / Check Syntax (/run)
            cfg = sanitize_compile_request(payload)
            stdin_data = payload.get("input", "")
            if not isinstance(stdin_data, str):
                stdin_data = ""
            check_only = bool(payload.get("check_only", False))

            comp = compile_source_code(cfg, with_unbuf=False)
            if not comp["ok"]:
                self.send_json({
                    "compile_ok": False,
                    "output": comp["output"],
                    "compile_ms": comp["compile_ms"],
                })
                return

            tmpdir = comp["tmpdir"]
            try:
                if check_only:
                    self.send_json({
                        "compile_ok": True,
                        "output": comp["warnings"].strip(),
                        "compile_ms": comp["compile_ms"],
                    })
                    return

                start = time.monotonic()
                proc = None
                try:
                    proc = subprocess.Popen(
                        [comp["exe_file"]],
                        cwd=tmpdir,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    try:
                        out_b, err_b = proc.communicate(
                            input=stdin_data.encode("utf-8"),
                            timeout=BATCH_TIMEOUT_SEC,
                        )
                        duration_ms = int((time.monotonic() - start) * 1000)
                        memory_bytes = get_peak_memory_bytes(proc)
                        stdout_text = out_b.decode("utf-8", errors="replace").replace("\r\n", "\n")
                        stderr_text = err_b.decode("utf-8", errors="replace").replace("\r\n", "\n")

                        combined = ""
                        if comp["warnings"] and "-Wall" in cfg["safe_flags"]:
                            combined += comp["warnings"] + "\n"
                        combined += stdout_text
                        if stderr_text:
                            combined += ("\n" if combined else "") + "[stderr]\n" + stderr_text
                        if proc.returncode != 0:
                            combined += f"\n[Process exited with code {proc.returncode}]"

                        self.send_json({
                            "compile_ok": True,
                            "output": combined,
                            "stdout": stdout_text,
                            "stderr": stderr_text,
                            "warnings": comp["warnings"],
                            "exit_code": proc.returncode,
                            "compile_ms": comp["compile_ms"],
                            "duration_ms": duration_ms,
                            "memory_bytes": memory_bytes,
                            "timed_out": False,
                        })
                    except subprocess.TimeoutExpired:
                        kill_process_tree(proc)
                        proc.communicate()
                        duration_ms = int((time.monotonic() - start) * 1000)
                        self.send_json({
                            "compile_ok": True,
                            "output": f"Execution timed out after {BATCH_TIMEOUT_SEC}s (Time Limit Exceeded — possible infinite loop).",
                            "stdout": "",
                            "stderr": "",
                            "exit_code": -1,
                            "compile_ms": comp["compile_ms"],
                            "duration_ms": duration_ms,
                            "memory_bytes": get_peak_memory_bytes(proc),
                            "timed_out": True,
                        })
                finally:
                    if proc is not None:
                        try:
                            proc.wait(timeout=1)
                        except Exception:
                            pass
            finally:
                remove_path_safe(tmpdir)

        except Exception as e:
            self.send_json({"compile_ok": False, "output": f"Server Error: {e}", "done": True})

    def send_bytes(self, status_code, content_type, body):
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def send_json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_bytes(200, "application/json; charset=utf-8", body)

    def log_message(self, format, *args):
        pass


class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def pick_port(default_port=8000):
    env_port = os.environ.get("PORT")
    if env_port:
        return int(env_port)
    for candidate in range(default_port, default_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", candidate)) != 0:
                return candidate
    return default_port


if __name__ == "__main__":
    cleanup_stale_runs()
    ensure_unbuf_object()
    bind_host = "127.0.0.1" if IS_WINDOWS and not os.environ.get("PORT") else "0.0.0.0"
    port = pick_port(8000)
    server = LocalHTTPServer((bind_host, port), CompilerHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"Akshay Official Compiler server running at {url}", flush=True)
    if "--open" in sys.argv[1:]:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with SESSIONS_LOCK:
            active_ids = list(SESSIONS.keys())
        for sid in active_ids:
            with SESSIONS_LOCK:
                sess = SESSIONS.pop(sid, None)
            if sess:
                sess.stop()
        server.server_close()
