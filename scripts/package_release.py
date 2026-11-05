"""Build a source ZIP from an explicit allowlist; exclude all runtime/private state."""

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import __version__

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {"README.md", "CHANGELOG.md", "install.ps1", "requirements.txt", "pyproject.toml",
              "ruff.toml", "MANIFEST.in", ".gitignore"}
SOURCE_DIRS = {"src", "tests", "scripts", "docs", "examples", "plugin"}
EXTENSIONS = {".py", ".md", ".json"}


def package(output):
    files = [ROOT / name for name in ROOT_FILES]
    for name in SOURCE_DIRS:
        files.extend(p for p in (ROOT / name).rglob("*") if p.is_file() and p.suffix in EXTENSIONS
                     and "__pycache__" not in p.parts and not p.is_symlink())
    required = {ROOT / "docs/VALIDATION.md", ROOT / "docs/tool-schema.json"}
    if any(not path.is_file() for path in required | set(files)):
        raise RuntimeError("Required source/docs files are missing; generate schema and validation report first")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.comment = f"Android Root MCP {__version__}; source release; excludes runtime state".encode()
        for path in sorted(set(files)):
            archive.write(path, "android-root-mcp/" + path.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(output) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"ZIP CRC check failed: {bad}")
        names = archive.namelist()
        excluded = {".venv", ".cache", ".artifacts", ".test-state", "state", "__pycache__", ".git"}
        if any(set(Path(name).parts) & excluded for name in names):
            raise RuntimeError("Runtime state unexpectedly included in ZIP")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(digest + "  " + output.name + "\n", encoding="ascii")
    print(json.dumps({"path": str(output), "files": len(names), "bytes": output.stat().st_size, "sha256": digest}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT.parent / f"android-root-mcp-suite-v{__version__}.zip")
    package(parser.parse_args().output)
