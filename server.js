const express = require("express");
const fs = require("fs");
const path = require("path");
const { execFile } = require("child_process");

const app = express();
const PORT = 3000;

app.use(express.json());
app.use(express.static(path.join(__dirname, "public")));

app.post("/run", (req, res) => {
  const { code, input } = req.body;

  if (!code || !code.trim()) {
    return res.json({ output: "Error: Code khali hai!" });
  }

  const jobId = `temp_${Date.now()}_${Math.floor(Math.random() * 1000)}`;
  const cFile = path.join(__dirname, `${jobId}.c`);
  const exeFile = path.join(__dirname, process.platform === "win32" ? `${jobId}.exe` : `${jobId}.out`);

  // Temporary .c file likho
  fs.writeFileSync(cFile, code);

  // Step 1: GCC se compile karo
  execFile("gcc", [cFile, "-o", exeFile], { timeout: 5000 }, (compileErr, _, compileStderr) => {
    if (compileErr) {
      cleanup(cFile, exeFile);
      return res.json({
        success: false,
        output: compileStderr || compileErr.message
      });
    }

    // Step 2: Compiled executable ko run karo aur stdin pass karo
    const child = execFile(exeFile, [], { timeout: 5000 }, (runErr, stdout, stderr) => {
      cleanup(cFile, exeFile);

      if (runErr) {
        if (runErr.killed) {
          return res.json({
            success: false,
            output: "Time Limit Exceeded (5 seconds). Kahin infinite loop to nahi hai?"
          });
        }
        return res.json({
          success: false,
          output: stderr || runErr.message
        });
      }

      return res.json({
        success: true,
        output: stdout || stderr || "(Program finished with no output)"
      });
    });

    // User ka custom input (scanf ke liye) stdin mein bhejo
    if (input) {
      child.stdin.write(input);
    }
    child.stdin.end();
  });
});

function cleanup(cFile, exeFile) {
  try { if (fs.existsSync(cFile)) fs.unlinkSync(cFile); } catch (_) {}
  try { if (fs.existsSync(exeFile)) fs.unlinkSync(exeFile); } catch (_) {}
}

app.listen(PORT, () => {
  console.log(`C Compiler Server running at http://localhost:${PORT}`);
});