import json
import os
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ALLOWED_FLAGS = {"-std=c17", "-std=c11", "-std=c99", "-Wall", "-Wextra", "-lm"}


class CompilerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        clean_path = self.path.split("?", 1)[0]
        if clean_path == "/":
            file_path = os.path.join(BASE_DIR, "index.html")
            content_type = "text/html; charset=utf-8"
        elif clean_path == "/style.css":
            file_path = os.path.join(BASE_DIR, "style.css")
            content_type = "text/css; charset=utf-8"
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
        if self.path != "/run":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            self.send_json({"compile_ok": False, "output": "Invalid JSON request."})
            return

        code = payload.get("code", "")
        stdin_data = payload.get("input", "")
        check_only = bool(payload.get("check_only", False))
        raw_flags = payload.get("flags", [])

        safe_flags = [fl for fl in raw_flags if isinstance(fl, str) and fl in ALLOWED_FLAGS]
        has_lm = "-lm" in safe_flags
        compile_flags = [fl for fl in safe_flags if fl != "-lm"]

        with tempfile.TemporaryDirectory() as tmpdir:
            c_file = os.path.join(tmpdir, "program.c")
            exe_name = "program.exe" if os.name == "nt" else "program"
            exe_file = os.path.join(tmpdir, exe_name)

            with open(c_file, "w", encoding="utf-8") as f:
                f.write(code)

            compile_cmd = ["gcc", c_file, *compile_flags, "-o", exe_file]
            if has_lm:
                compile_cmd.append("-lm")

            try:
                comp = subprocess.run(
                    compile_cmd,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except FileNotFoundError:
                self.send_json({"compile_ok": False, "output": "Error: gcc not found in PATH."})
                return
            except subprocess.TimeoutExpired:
                self.send_json({"compile_ok": False, "output": "Compilation timed out."})
                return

            if comp.returncode != 0:
                msg = (comp.stderr or comp.stdout or "Compilation failed.").replace(c_file, "program.c")
                self.send_json({"compile_ok": False, "output": msg})
                return

            if check_only:
                self.send_json({"compile_ok": True, "output": ""})
                return

            try:
                run_res = subprocess.run(
                    [exe_file],
                    input=stdin_data,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                combined = ""
                if comp.stderr and "-Wall" in safe_flags:
                    combined += comp.stderr.replace(c_file, "program.c") + "\n"
                combined += run_res.stdout
                if run_res.stderr:
                    combined += "\n[stderr]\n" + run_res.stderr
                if run_res.returncode != 0:
                    combined += f"\n[Process exited with code {run_res.returncode}]"
                self.send_json({"compile_ok": True, "output": combined})
            except subprocess.TimeoutExpired:
                self.send_json({"compile_ok": True, "output": "Execution timed out (possible infinite loop)."})

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