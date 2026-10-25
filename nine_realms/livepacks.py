"""Live pack intake: safe zip upload, a watched drop folder, and a rescan, all feeding LocalCorpus.ingest_pack.

The final round hands over new event packs at the start. They are dropped into LIVE_PACKS_DIR (a folder or a .zip) or uploaded
through the API; either way they are ingested into the running corpus with no code change and no restart.
"""
from __future__ import annotations

import io
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .corpus import SUPPORTED, LocalCorpus, _slug

ALLOWED_EXTRA = {".json", ".csv", ".md", ".sha256", ".png", ".jpg"}  # pack furniture: kept, never parsed as documents
MAX_FILES, MAX_UNCOMPRESSED = 3000, 300 * 1024 * 1024


class UnsafeArchive(ValueError):
    pass


def safe_extract(data: bytes, dest: Path) -> int:
    """Unpack an uploaded zip. Rejects path traversal, symlinks, absurd sizes and anything that is not a document or pack furniture."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UnsafeArchive("not a valid zip file") from exc
    infos = z.infolist()
    if len(infos) > MAX_FILES:
        raise UnsafeArchive(f"too many files ({len(infos)} > {MAX_FILES})")
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
        raise UnsafeArchive("archive expands to more than 300 MB")
    dest.mkdir(parents=True, exist_ok=True)
    root, written = dest.resolve(), 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        if info.is_dir() or name.startswith("__MACOSX/") or PurePosixPath(name).name.startswith("."):
            continue
        parts = PurePosixPath(name).parts
        if name.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", name):
            raise UnsafeArchive(f"unsafe path in archive: {info.filename!r}")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise UnsafeArchive(f"symbolic link in archive: {info.filename!r}")
        if PurePosixPath(name).suffix.lower() not in SUPPORTED | ALLOWED_EXTRA:
            continue
        target = (root / name).resolve()
        if root not in target.parents:
            raise UnsafeArchive(f"unsafe path in archive: {info.filename!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with z.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
        written += 1
    if not written:
        raise UnsafeArchive("the archive contains no documents")
    return written


def find_pack_root(path: Path) -> Path:
    """The folder that holds manifest.json (or docs/). Zips often wrap the pack in one extra folder."""
    if (path / "manifest.json").exists() or (path / "docs").is_dir():
        return path
    for m in sorted(path.rglob("manifest.json"), key=lambda p: len(p.parts)):
        return m.parent
    kids = [k for k in path.iterdir() if k.is_dir()]
    return kids[0] if len(kids) == 1 else path


class LivePacks:
    def __init__(self, corpus: LocalCorpus, folder: str | Path, interval: float = 5.0) -> None:
        self.corpus, self.folder, self.interval = corpus, Path(folder), interval
        self.folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.errors: dict[str, str] = {}
        self.last_scan = 0.0

    def ingest_dir(self, path: Path, name: str | None = None) -> dict[str, Any]:
        root = find_pack_root(path)
        return self.corpus.ingest_pack(root, _slug(name or path.name))

    def accept_upload(self, data: bytes, name: str = "") -> dict[str, Any]:
        """Save + ingest an uploaded zip. Each upload gets its own folder, so two packs can never overwrite each other."""
        stem = _slug(re.sub(r"\.zip$", "", name or "", flags=re.I)) or "upload"
        dest = self.folder / f"{stem}-{time.strftime('%H%M%S')}"
        with self._lock:
            try:
                safe_extract(data, dest)
                return self.ingest_dir(dest, dest.name)
            except Exception:
                shutil.rmtree(dest, ignore_errors=True)
                raise

    def scan(self) -> list[dict[str, Any]]:
        """Ingest every pack in the folder that is new or has changed. A broken pack is reported and skipped, never fatal."""
        reports: list[dict[str, Any]] = []
        with self._lock:
            self.last_scan = time.time()
            for entry in sorted(self.folder.iterdir()):
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_file() and entry.suffix.lower() == ".zip":
                        target = self.folder / _slug(entry.stem)
                        if target.exists():
                            continue  # already extracted
                        safe_extract(entry.read_bytes(), target)
                        entry = target
                    if not entry.is_dir():
                        continue
                    root, pack_id = find_pack_root(entry), _slug(entry.name)
                    if self.corpus.is_current(root, pack_id):
                        continue
                    reports.append(self.ingest_dir(entry))
                    self.errors.pop(entry.name, None)
                except Exception as exc:
                    self.errors[entry.name] = f"{type(exc).__name__}: {exc}"
        return reports

    def start(self) -> None:
        if self.interval <= 0:
            return

        def loop() -> None:
            while not self._stop.wait(self.interval):
                self.scan()

        threading.Thread(target=loop, daemon=True, name="live-pack-watcher").start()

    def stop(self) -> None:
        self._stop.set()
