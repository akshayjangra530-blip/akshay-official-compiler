import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNS_ROOT = os.path.join(BASE_DIR, "_runs")
os.makedirs(RUNS_ROOT, exist_ok=True)

ALLOWED_FLAGS = {
    "-std=c17",
    "-std=c11",
    "-std=c99",
    "-std=c++20",
    "-std=c++17",
    "-std=c++14",
    "-std=c++11",
    "-Wall",
    "-Wextra",
    "-lm",
}

SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


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


class LiveProgramSession:
    def __init__(self, exe_path, tmp_dir):
        self.exe_path = exe_path
        self.tmp_dir = tmp_dir
        self.chunks = []
        self.lock = threading.Lock()
        self.done = False
        self.exit_code = None

        self.proc = subprocess.Popen(
            [exe_path],
            cwd=tmp_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        threading.Thread(target=self._read_stream, daemon=True).start()

    def _read_stream(self):
        try:
            while True:
                b = self.proc.stdout.read(1)
                if not b:
                    break
                with self.lock:
                    self.chunks.append(b.decode("utf-8", errors="replace"))
        finally:
            try:
                self.proc.wait()
            except Exception:
                pass
            with self.lock:
                self.exit_code = self.proc.returncode
                self.done = True
            time.sleep(0.05)
            remove_path_safe(self.tmp_dir)

    def write_line(self, line):
        if self.done:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.write((line + "\n").encode("utf-8"))
                self.proc.stdin.flush()
        except Exception:
            pass

    def poll_output(self):
        with self.lock:
            out = "".join(self.chunks)
            self.chunks.clear()
            return out, self.done, self.exit_code

    def stop(self):
        try:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(timeout=2)
        except Exception:
            pass
        remove_path_safe(self.tmp_dir)


def inject_unbuffered_io(code, is_cpp=False):
    if is_cpp:
        header = (
            "#include <cstdio>\n"
            "__attribute__((constructor)) static void _ak_live_stdio(void) {\n"
            "    std::setvbuf(stdout, nullptr, _IONBF, 0);\n"
            "    std::setvbuf(stderr, nullptr, _IONBF, 0);\n"
            "}\n"
        )
    else:
        header = (
            "#include <stdio.h>\n"
            "__attribute__((constructor)) static void _ak_live_stdio(void) {\n"
            "    setvbuf(stdout, NULL, _IONBF, 0);\n"
            "    setvbuf(stderr, NULL, _IONBF, 0);\n"
            "}\n"
        )
    return header + code


def adjust_compiler_line_numbers(msg, src_file_path, display_name, offset=5):
    adjusted = []
    for line in msg.splitlines():
        if src_file_path in line:
            parts = line.split(src_file_path + ":", 1)
            if len(parts) == 2:
                rest = parts[1]
                digits = ""
                for ch in rest:
                    if ch.isdigit():
                        digits += ch
                    else:
                        break
                if digits:
                    new_ln = max(1, int(digits) - offset)
                    rest = str(new_ln) + rest[len(digits) :]
                line = f"{display_name}:" + rest
        adjusted.append(line.replace(src_file_path, display_name))
    return "\n".join(adjusted)


class CompilerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        clean_path = self.path.split("?", 1)[0]
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
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return

        if not os.path.exists(file_path):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"File not found")
            return

        with open(file_path, "rb") as f:
            data = f.read()

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
                code = payload.get("code", "")
                lang = str(payload.get("lang", "c")).lower()
                is_cpp = lang in ("cpp", "c++")
                compiler_bin = "g++" if is_cpp else "gcc"
                display_name = "program.cpp" if is_cpp else "program.c"

                raw_flags = payload.get("flags", [])
                safe_flags = [fl for fl in raw_flags if isinstance(fl, str) and fl in ALLOWED_FLAGS]
                has_lm = "-lm" in safe_flags
                compile_flags = [fl for fl in safe_flags if fl != "-lm"]

                sid = str(uuid.uuid4())
                tmpdir = os.path.join(RUNS_ROOT, sid)
                os.makedirs(tmpdir, exist_ok=True)
                src_file = os.path.join(tmpdir, display_name)
                exe_name = "program.exe" if os.name == "nt" else "program"
                exe_file = os.path.join(tmpdir, exe_name)

                with open(src_file, "w", encoding="utf-8") as f:
                    f.write(inject_unbuffered_io(code, is_cpp=is_cpp))

                compile_cmd = [compiler_bin, src_file, *compile_flags, "-o", exe_file]
                if has_lm:
                    compile_cmd.append("-lm")

                try:
                    comp = subprocess.run(
                        compile_cmd,
                        cwd=tmpdir,
                        capture_output=True,
                        text=True,
                        timeout=12,
                    )
                except FileNotFoundError:
                    remove_path_safe(tmpdir)
                    self.send_json(
                        {
                            "compile_ok": False,
                            "output": f"Error: {compiler_bin} not found in PATH. MinGW/GCC (with g++) install karo.",
                        }
                    )
                    return
                except subprocess.TimeoutExpired:
                    remove_path_safe(tmpdir)
                    self.send_json({"compile_ok": False, "output": "Compilation timed out."})
                    return

                remove_path_safe(src_file)

                if comp.returncode != 0:
                    raw_err = comp.stderr or comp.stdout or "Compilation failed."
                    msg = adjust_compiler_line_numbers(raw_err, src_file, display_name, offset=5)
                    remove_path_safe(tmpdir)
                    self.send_json({"compile_ok": False, "output": msg})
                    return

                session = LiveProgramSession(exe_file, tmpdir)
                with SESSIONS_LOCK:
                    SESSIONS[sid] = session

                time.sleep(0.08)
                out, done, exit_code = session.poll_output()
                if done:
                    with SESSIONS_LOCK:
                        SESSIONS.pop(sid, None)

                self.send_json(
                    {
                        "compile_ok": True,
                        "session_id": sid,
                        "output": out,
                        "done": done,
                        "exit_code": exit_code,
                    }
                )
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
                time.sleep(0.08)
                out, done, exit_code = session.poll_output()
                if done:
                    with SESSIONS_LOCK:
                        SESSIONS.pop(sid, None)
                self.send_json({"output": out, "done": done, "exit_code": exit_code})
                return

            if self.path == "/poll":
                sid = payload.get("session_id", "")
                with SESSIONS_LOCK:
                    session = SESSIONS.get(sid)
                if not session:
                    self.send_json({"output": "", "done": True, "exit_code": 0})
                    return
                out, done, exit_code = session.poll_output()
                if done:
                    with SESSIONS_LOCK:
                        SESSIONS.pop(sid, None)
                self.send_json({"output": out, "done": done, "exit_code": exit_code})
                return

            if self.path == "/stop":
                sid = payload.get("session_id", "")
                with SESSIONS_LOCK:
                    session = SESSIONS.pop(sid, None)
                if session:
                    session.stop()
                self.send_json({"stopped": True})
                return

            # Legacy /run fallback
            code = payload.get("code", "")
            stdin_data = payload.get("input", "")
            lang = str(payload.get("lang", "c")).lower()
            is_cpp = lang in ("cpp", "c++")
            compiler_bin = "g++" if is_cpp else "gcc"
            display_name = "program.cpp" if is_cpp else "program.c"
            check_only = bool(payload.get("check_only", False))
            raw_flags = payload.get("flags", [])

            safe_flags = [fl for fl in raw_flags if isinstance(fl, str) and fl in ALLOWED_FLAGS]
            has_lm = "-lm" in safe_flags
            compile_flags = [fl for fl in safe_flags if fl != "-lm"]

            tmpdir = os.path.join(RUNS_ROOT, str(uuid.uuid4()))
            os.makedirs(tmpdir, exist_ok=True)
            try:
                src_file = os.path.join(tmpdir, display_name)
                exe_name = "program.exe" if os.name == "nt" else "program"
                exe_file = os.path.join(tmpdir, exe_name)

                with open(src_file, "w", encoding="utf-8") as f:
                    f.write(code)

                compile_cmd = [compiler_bin, src_file, *compile_flags, "-o", exe_file]
                if has_lm:
                    compile_cmd.append("-lm")

                try:
                    comp = subprocess.run(
                        compile_cmd,
                        cwd=tmpdir,
                        capture_output=True,
                        text=True,
                        timeout=12,
                    )
                except FileNotFoundError:
                    self.send_json({"compile_ok": False, "output": f"Error: {compiler_bin} not found in PATH."})
                    return
                except subprocess.TimeoutExpired:
                    self.send_json({"compile_ok": False, "output": "Compilation timed out."})
                    return

                if comp.returncode != 0:
                    msg = (comp.stderr or comp.stdout or "Compilation failed.").replace(src_file, display_name)
                    self.send_json({"compile_ok": False, "output": msg})
                    return

                if check_only:
                    self.send_json({"compile_ok": True, "output": ""})
                    return

                try:
                    run_res = subprocess.run(
                        [exe_file],
                        cwd=tmpdir,
                        input=stdin_data,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    combined = ""
                    if comp.stderr and "-Wall" in safe_flags:
                        combined += comp.stderr.replace(src_file, display_name) + "\n"
                    combined += run_res.stdout
                    if run_res.stderr:
                        combined += "\n[stderr]\n" + run_res.stderr
                    if run_res.returncode != 0:
                        combined += f"\n[Process exited with code {run_res.returncode}]"
                    self.send_json({"compile_ok": True, "output": combined})
                except subprocess.TimeoutExpired:
                    self.send_json({"compile_ok": True, "output": "Execution timed out (possible infinite loop)."})
            finally:
                remove_path_safe(tmpdir)

        except Exception as e:
            self.send_json({"compile_ok": False, "output": f"Server Error: {e}", "done": True})

    def send_json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    server = ThreadingHTTPServer(("0.0.0.0", port), CompilerHandler)
    print(f"Server running on port {port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()