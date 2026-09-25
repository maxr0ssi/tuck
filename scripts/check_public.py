#!/usr/bin/env python3
"""Check publishable files, including untracked non-ignored files, without printing secrets.

This is a guardrail, not a guarantee. Review `git diff --cached` before publishing.
Ignored local files are intentionally never opened.
"""
import re
import hashlib
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
# Only these visually reviewed images using fictional data may be published.
# A changed image needs another review and an updated fingerprint.
PUBLIC_SCREENSHOTS = {
    'docs/screenshots/menu-bar.png': '5c6700533dfa7322a1682dbcac616cf89fb5c928e1f19a96886bd4c9c0cb70c9',
    'docs/screenshots/review.jpg': 'f0bcf26b2b266dc14f231fba2259f4dcc381dc1924094513ef9d5d3feaae2e1c',
    'docs/screenshots/filed.jpg': '5e2433b9d2dd7422d0deedbdae3e2e8eb360fe0dc76dc07e657f574ee88cd500',
}
PRIVATE_PARTS = {".local", ".private", ".env", "state", "models", "samples", "private-fixtures", ".venv"}
PRIVATE_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".log", ".jsonl", ".safetensors", ".gguf", ".bin",
                    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".tsv",
                    ".png", ".heic", ".jpeg", ".jpg", ".gif", ".webp", ".mov", ".mp4",
                    ".zip", ".dmg", ".pkg", ".pem", ".key", ".p12"}
PATTERNS = {
    "absolute home path": re.compile(rb"/(?:Users|home)/[A-Za-z0-9][A-Za-z0-9_.-]*/"),
    "private key": re.compile(rb"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "service credential": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{30,})"),
}


def check_file(name, size, read):
    relative = Path(name)
    if name not in PUBLIC_SCREENSHOTS and (
            set(relative.parts) & PRIVATE_PARTS or relative.suffix.lower() in PRIVATE_SUFFIXES
            or relative.name.startswith("config.local.") or relative.name.endswith(".local.json")
            or re.search(r"\.(?:db|sqlite3?|log)[.-]", relative.name)
            or (relative.name.startswith(".env.") and relative.name != ".env.example")):
        return "private file or document"
    if size > 1_000_000:
        return "large file"
    content = read()
    if name in PUBLIC_SCREENSHOTS:
        return None if hashlib.sha256(content).hexdigest() == PUBLIC_SCREENSHOTS[name] else "unreviewed screenshot change"
    if b"\0" in content:
        return "binary file"
    return next((category for category, pattern in PATTERNS.items() if pattern.search(content)), None)


def main():
    listed = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT)
    failures = []
    paths = sorted(set(listed.decode().split("\0")) - {""})
    for name in paths:
        path = ROOT / name
        if path.is_symlink():
            failures.append((name, "symlink"))
        elif path.is_file():
            if category := check_file(name, path.stat().st_size, path.read_bytes):
                failures.append((name, category))
    # The index can still contain private text after the working copy is cleaned.
    staged = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=ROOT)
    for entry in staged.decode().split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, blob, stage = metadata.split()
        if mode not in ("100644", "100755") or stage != "0":
            failures.append((name, "unsupported or unmerged staged entry"))
            continue
        size = int(subprocess.check_output(["git", "cat-file", "-s", blob], cwd=ROOT))
        if category := check_file(name, size, lambda: subprocess.check_output(["git", "cat-file", "blob", blob], cwd=ROOT)):
            failures.append((name, "staged " + category))
    # These paths must remain ignored even before the first commit.
    protected = [".local/config.json", ".private/notes.txt", "config.local.json", ".env",
                 "state/session.json", "recommendations.jsonl", "passport.pdf", "slides.pptx",
                 "capture.png", "export.csv", "credentials.pem", "queue.db-wal", "models/weights.safetensors", "private-fixtures/document.txt"]
    result = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"], input="\n".join(protected) + "\n",
                            text=True, capture_output=True, cwd=ROOT)
    ignored = set(result.stdout.splitlines())
    for name in protected:
        if name not in ignored:
            failures.append((".gitignore", "missing private-path protection"))
    for name, category in failures:
        print(f"FAIL {name}: {category}", file=sys.stderr)
    if failures:
        return 1
    print(f"Privacy guard passed for {len(paths)} publishable files; ignored data was not read.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
