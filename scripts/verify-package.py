"""Inspect and extract our small VSIX into the isolated extension-test directory."""
import hashlib
import json
from pathlib import Path
import zipfile

repository = Path(__file__).resolve().parents[1]
version = json.loads((repository / "package.json").read_text(encoding="utf-8"))["version"]
archive = repository / ("artifacts/codex-session-notifier-" + version + ".vsix")
root = (repository / (".vscode-test/packaged-" + version)).resolve()
root.relative_to((repository / ".vscode-test").resolve())
root.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(archive) as bundle:
    files = bundle.infolist()
    names = bundle.namelist()
    assert len(files) < 100 and sum(item.file_size for item in files) < 1048576
    for item in files:
        (root / item.filename).resolve().relative_to(root)
        assert ((item.external_attr >> 16) & 0o170000) != 0o120000
    for required in ("resources/probe.py", "resources/state.py", "out/src/extension.js",
                     "node_modules/@perk-net/perk-pushplus-sdk/dist/index.js"):
        assert "extension/" + required in names, required
    assert not any("/.beads" in name or "/.codex/" in name or "/test/" in name or
                   name.endswith(".sqlite") for name in names)
    for script in ("resources/probe.py", "resources/state.py"):
        assert bundle.read("extension/" + script) == (repository / script).read_bytes()
    bundle.extractall(root)
print(json.dumps({"files": len(files), "uncompressedBytes": sum(item.file_size for item in files),
                  "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "runtimeFilesVerified": True}))
