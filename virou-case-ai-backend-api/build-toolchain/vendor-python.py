import base64
import json
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
profile = root / "python-profile"
subprocess.run([sys.executable, "-m", "pywrangler", "sync"], cwd=profile, check=True)
modules = {}
for path in sorted((profile / "python_modules").rglob("*")):
    if path.is_symlink():
        raise RuntimeError("Vendor symlinks are not supported")
    if not path.is_file() or path.suffix == ".pyc" or "__pycache__" in path.parts:
        continue
    name = "python_modules/" + path.relative_to(profile / "python_modules").as_posix()
    modules[name] = {
        "content": base64.b64encode(path.read_bytes()).decode(),
        "mime": "application/octet-stream",
        "encoding": "base64",
    }
if "python_modules/workers/__init__.py" not in modules or "python_modules/fastapi/__init__.py" not in modules:
    raise RuntimeError("Python runtime catalog incomplete")
(root / "python-vendor.json").write_text(json.dumps(modules, separators=(",", ":")))
