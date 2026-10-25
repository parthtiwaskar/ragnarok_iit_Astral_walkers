"""Ingestion pipeline: discover -> sniff -> extract -> damage checks -> classify -> index (DATA_PIPELINE.md §2).

Every file goes through `safe_ingest` (try/except + per-file timeout). An exception becomes a quarantine
record, never a crash. Incremental: unchanged files (same path + content hash) are skipped.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import statistics
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.ingest import damage
from app.ingest.chunker import chunk_text
from app.ingest.header import doc_meta_from_fields, is_background_event, parse_header
from app.ingest.injection import scan as scan_injections
from app.ingest.manifest import ManifestEntry, find_manifests, load_manifest
from app.ingest.parsers import Extracted, ExtractError
from app.ingest.parsers.docx import extract_docx
from app.ingest.parsers.pdf import extract_pdf
from app.ingest.parsers.txt import extract_txt
from app.ingest.reference import looks_like_reference, parse_reference
from app.ingest.relations import find_relations, id_pattern
from app.ingest.sniff import sniff
from app.ingest.trade_table import looks_like_trade_table, parse_trade_table
from app.obs.events import emit
from app.search.dense import Embedder, VectorIndex
from app.store.repo import DocRecord, Repo

SKIP_NAMES = {"truth.json", ".ds_store", "thumbs.db"}


@dataclass
class Parsed:
    """Result of the (timeout-guarded) extraction phase for one file."""

    path: Path
    rel: str
    pack: str | None
    content_hash: str
    size: int
    kind: str  # doc | trade_table | reference | quarantined
    fmt: str | None
    extracted: Extracted | None = None
    error: str | None = None


@dataclass
class IngestReport:
    index_version: str = ""
    started_at: str = ""
    finished_at: str = ""
    totals: dict[str, int] = field(default_factory=dict)
    by_format: dict[str, int] = field(default_factory=dict)
    quarantine: list[dict[str, Any]] = field(default_factory=list)
    partial: list[dict[str, Any]] = field(default_factory=list)
    repaired: list[dict[str, Any]] = field(default_factory=list)
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    off_manifest: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    flagged_rows: list[dict[str, Any]] = field(default_factory=list)
    trade_table_flags: list[dict[str, Any]] = field(default_factory=list)
    injections: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    skipped_unchanged: int = 0
    manifest_present: bool = True
    embedder: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _extract(data: bytes, kind: str) -> Extracted:
    if kind == "pdf":
        return extract_pdf(data)
    if kind == "docx":
        return extract_docx(data)
    return extract_txt(data)


class Ingestor:
    def __init__(
        self,
        repo: Repo,
        vectors: VectorIndex,
        embedder: Embedder,
        file_timeout_s: float = 20,
        runs_dir: Path | None = None,
    ) -> None:
        self.repo = repo
        self.vectors = vectors
        self.embedder = embedder
        self.file_timeout_s = file_timeout_s
        self.runs_dir = runs_dir
        self._pool = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="ingest")

    # ------------------------------------------------------------------ public
    def ingest(self, roots: list[tuple[Path, str | None]]) -> IngestReport:
        """`roots` = [(folder, pack_name_or_None)]. Never raises."""
        rep = IngestReport(started_at=_now(), embedder=self.embedder.name)
        seq = self.repo.next_ingest_seq()
        emit(
            "ingest_update",
            actor="ingest",
            payload={"stage": "start", "roots": [str(r) for r, _ in roots], "ingest_seq": seq},
        )
        try:
            self._run(roots, seq, rep)
        except Exception as exc:  # last-resort guard: ingestion never crashes the process
            rep.notes.append(f"pipeline error: {type(exc).__name__}: {exc}")
            emit(
                "ingest_issue",
                actor="ingest",
                payload={"file": "*", "status": "PIPELINE_ERROR", "reason": f"{type(exc).__name__}: {exc}"},
            )
        rep.index_version = self.repo.index_version()
        self.repo.set_meta("index_version", rep.index_version)
        rep.finished_at = _now()
        rep.totals.update(self._totals())
        self._write_report(rep)
        emit(
            "ingest_update",
            actor="ingest",
            payload={"stage": "done", "index_version": rep.index_version, "totals": rep.totals},
        )
        return rep

    # ------------------------------------------------------------------ phases
    def _run(self, roots: list[tuple[Path, str | None]], seq: int, rep: IngestReport) -> None:
        manifest: dict[str, ManifestEntry] = {}
        by_file: dict[str, ManifestEntry] = {}
        any_manifest_file = False
        for root, pack in roots:
            if not root.exists():
                rep.notes.append(f"folder missing: {root}")
                emit(
                    "ingest_issue",
                    actor="ingest",
                    payload={"file": str(root), "status": "MISSING", "reason": "folder missing"},
                )
                continue
            self._unzip_archives(root)
            for mpath in find_manifests(root):
                any_manifest_file = True
                for e in load_manifest(mpath, pack):
                    manifest[e.doc_id] = e
                    if e.file:
                        by_file[str((mpath.parent / e.file).resolve()).lower()] = e
        if manifest:
            self.repo.upsert_manifest([e.__dict__ for e in manifest.values()])
        rep.manifest_present = any_manifest_file or self.repo.has_manifest()
        self.repo.set_meta("manifest_present", "1" if rep.manifest_present else "0")
        if not rep.manifest_present:
            rep.notes.append("NO_MANIFEST: citations cannot be verified against a manifest")

        parsed: list[Parsed] = []
        for root, pack in roots:
            if not root.exists():
                continue
            for p in sorted(root.rglob("*")):
                if (
                    not p.is_file()
                    or p.name.lower() in SKIP_NAMES
                    or p.stem.lower() in ("manifest", "manifest_delta")
                ):
                    continue
                if p.suffix.lower() == ".zip" or any(
                    part.startswith(".") for part in p.relative_to(root).parts
                ):
                    continue
                item = self._safe_parse(p, root, pack, rep)
                if item is not None:
                    parsed.append(item)

        # references first (tier cross-check needs outlets), then trade tables, then docs
        for item in parsed:
            if item.kind == "reference":
                self._apply_reference(item, seq)
        for item in parsed:
            if item.kind == "trade_table":
                self._apply_trade(item, seq, rep)
        docs = [i for i in parsed if i.kind in ("doc", "quarantined")]
        lengths = [len(d.body) for d in self.repo.docs("status IN ('OK','REPAIRED')") if d.body]
        lengths += [len(i.extracted.text) for i in docs if i.extracted and i.extracted.text]
        median_len = statistics.median(lengths) if lengths else None
        new_doc_ids: list[str] = []
        for item in docs:
            did = self._apply_doc(item, seq, manifest, by_file, median_len, rep)
            if did:
                new_doc_ids.append(did)
        self._reconcile_manifest(manifest, rep, seq)
        self._relations()
        self._index(new_doc_ids, rep)

    def _unzip_archives(self, root: Path) -> None:
        for z, err in unzip_all(root):
            self.repo.add_issue(str(z), None, "QUARANTINED", f"BAD_ZIP: {err}")

    def _safe_parse(self, p: Path, root: Path, pack: str | None, rep: IngestReport) -> Parsed | None:
        rel = str(p.relative_to(root)).replace("\\", "/")
        key = f"{pack or 'runemark'}/{rel}"
        try:
            data = p.read_bytes()
        except OSError as exc:
            self._quarantine_file(key, None, f"UNREADABLE: {exc}", rep, pack, "", 0, 0)
            return None
        h = _sha(data)
        prev = self.repo.get_file(key)
        if prev is not None and prev["content_hash"] == h:
            rep.skipped_unchanged += 1
            return None
        kind = sniff(data)
        item = Parsed(path=p, rel=key, pack=pack, content_hash=h, size=len(data), kind="doc", fmt=None)
        if kind == "empty":
            item.kind, item.error, item.fmt = (
                "quarantined",
                "EMPTY: 0 bytes",
                p.suffix.lower().lstrip(".") or None,
            )
            return item
        if kind in ("unsupported", "zip"):
            item.kind, item.error = "quarantined", f"UNSUPPORTED: not PDF/TXT/DOCX by magic bytes ({kind})"
            item.fmt = p.suffix.lower().lstrip(".") or None
            return item
        item.fmt = kind
        fut = self._pool.submit(_extract, data, kind)
        try:
            item.extracted = fut.result(timeout=self.file_timeout_s)
        except cf.TimeoutError:
            item.kind, item.error = "quarantined", f"TIMEOUT: extraction exceeded {self.file_timeout_s}s"
            return item
        except ExtractError as exc:
            item.kind, item.error = "quarantined", f"{exc.code}: {exc.detail}"[:300]
            return item
        except Exception as exc:
            item.kind, item.error = "quarantined", f"EXTRACT_FAILED: {type(exc).__name__}: {exc}"[:300]
            return item
        text = item.extracted.text
        if kind == "txt" and looks_like_trade_table(text):
            item.kind = "trade_table"
            # keep column spacing for whitespace-delimited tables
            item.extracted = extract_txt(data, keep_spacing=True)
        elif looks_like_reference(text, p.relative_to(root)) and not parse_header(text).fields.get(
            "pub_date"
        ):
            item.kind = "reference"
        return item

    # ------------------------------------------------------------------ apply
    def _apply_reference(self, item: Parsed, seq: int) -> None:
        assert item.extracted is not None
        sheet = parse_reference(item.extracted.text, item.path)
        self.repo.upsert_reference(sheet.sheet, sheet.rows)
        self.repo.upsert_file(
            item.rel,
            item.content_hash,
            "reference",
            item.fmt,
            "OK",
            None,
            sheet.sheet,
            item.pack,
            item.size,
            seq,
        )
        emit(
            "ingest_update",
            actor="ingest",
            payload={
                "file": item.rel,
                "kind": "reference",
                "sheet": sheet.sheet,
                "rows": len(sheet.rows),
                "status": "OK",
            },
        )

    def _apply_trade(self, item: Parsed, seq: int, rep: IngestReport) -> None:
        assert item.extracted is not None
        t = parse_trade_table(item.extracted.text, fallback_id=item.path.stem.upper())
        rows = [r.__dict__ for r in t.rows]
        flagged = [r for r in t.rows if r.quality != "OK"]
        status = "OK" if not flagged and not t.flags else "PARTIAL"
        self.repo.upsert_trade_table(
            dict(
                table_id=t.table_id,
                exporter=t.exporter,
                importer=t.importer,
                sector=t.sector,
                unit=t.unit,
                period_start=t.period_start,
                period_end=t.period_end,
                event_id=t.event_id,
                is_background=t.is_background,
                columns=t.columns,
                header=t.header,
                file_path=item.rel,
                status=status,
                flags=t.flags,
                pack=item.pack,
                ingest_seq=seq,
            ),
            rows,
        )
        self.repo.upsert_file(
            item.rel,
            item.content_hash,
            "trade_table",
            item.fmt,
            status,
            ";".join(t.flags) or None,
            t.table_id,
            item.pack,
            item.size,
            seq,
        )
        for r in flagged:
            ref = f"{t.table_id}#r{(r.row or 0):02d}"
            rep.flagged_rows.append({"ref": ref, "flag": r.quality, "raw": r.raw_line, "reason": r.reason})
            emit(
                "ingest_issue",
                actor="ingest",
                payload={
                    "file": item.rel,
                    "ref": ref,
                    "status": r.quality,
                    "reason": r.reason,
                    "raw": r.raw_line,
                },
            )
        if t.flags:
            rep.trade_table_flags.append({"table_id": t.table_id, "flags": t.flags})
        emit(
            "ingest_update",
            actor="ingest",
            payload={
                "file": item.rel,
                "kind": "trade_table",
                "table_id": t.table_id,
                "rows": len(t.rows),
                "flagged": len(flagged),
                "status": status,
            },
        )

    def _quarantine_file(
        self,
        key: str,
        ref: str | None,
        reason: str,
        rep: IngestReport,
        pack: str | None,
        content_hash: str,
        size: int,
        seq: int,
        fmt: str | None = None,
    ) -> None:
        self.repo.upsert_file(key, content_hash, "doc", fmt, "QUARANTINED", reason, ref, pack, size, seq)
        self.repo.add_issue(key, ref, "QUARANTINED", reason)
        rep.quarantine.append({"file": key, "doc_id": ref, "reason": reason})
        emit(
            "ingest_issue",
            actor="ingest",
            payload={"file": key, "ref": ref, "status": "QUARANTINED", "reason": reason},
        )

    def _apply_doc(
        self,
        item: Parsed,
        seq: int,
        manifest: dict[str, ManifestEntry],
        by_file: dict[str, ManifestEntry],
        median_len: float | None,
        rep: IngestReport,
    ) -> str | None:
        fmt = item.fmt or "unknown"
        rep.by_format[fmt] = rep.by_format.get(fmt, 0) + 1
        m = by_file.get(str(item.path.resolve()).lower())
        text = item.extracted.text if item.extracted else ""
        header = parse_header(text) if text else None
        meta = doc_meta_from_fields(header.fields) if header and header.found else None
        doc_id = (m.doc_id if m else None) or (meta.doc_id if meta else None)
        flags: list[str] = []
        if m is None and meta and meta.doc_id and meta.doc_id in manifest:
            m = manifest[meta.doc_id]
        if m is not None and meta and meta.doc_id and meta.doc_id != m.doc_id:
            flags.append("METADATA_CONFLICT")
            rep.conflicts.append(
                {"file": item.rel, "field": "doc_id", "manifest": m.doc_id, "header": meta.doc_id}
            )

        # hard failures first
        if item.kind == "quarantined" or item.extracted is None:
            reason = item.error or "UNREADABLE"
            self._quarantine_file(
                item.rel, doc_id, reason, rep, item.pack, item.content_hash, item.size, seq, fmt
            )
            if doc_id:
                self._write_stub(doc_id, m, "QUARANTINED", reason, fmt, item, seq)
            return None
        if damage.is_empty(text):
            reason = "EMPTY: no extractable text"
            self._quarantine_file(
                item.rel, doc_id, reason, rep, item.pack, item.content_hash, item.size, seq, fmt
            )
            if doc_id:
                self._write_stub(doc_id, m, "QUARANTINED", reason, fmt, item, seq)
            return None
        if damage.is_bad_encoding(text):
            reason = "BAD_ENCODING: unrecoverable characters"
            self._quarantine_file(
                item.rel, doc_id, reason, rep, item.pack, item.content_hash, item.size, seq, fmt
            )
            if doc_id:
                self._write_stub(doc_id, m, "QUARANTINED", reason, fmt, item, seq)
            return None

        status, reason = "OK", None
        assert header is not None
        if not header.found or meta is None:
            if m is not None and m.pub_date and m.tier and m.event_id:
                status, reason = "PARTIAL", "NO_HEADER: metadata taken from manifest"
                flags.append("NO_HEADER")
                meta = doc_meta_from_fields({})
            else:
                key_id = doc_id or f"FILE:{item.rel}"
                self._quarantine_file(
                    item.rel,
                    key_id,
                    "NO_HEADER: no metadata header and no manifest metadata",
                    rep,
                    item.pack,
                    item.content_hash,
                    item.size,
                    seq,
                    fmt,
                )
                if m is None and doc_id is None:
                    rep.off_manifest.append(item.rel)
                return None
        body = header.body if header.found else text
        if not doc_id:
            doc_id = f"FILE:{item.rel}"
        # merge: manifest wins for the ID, header wins for the rest
        pub_date = (meta.pub_date.isoformat() if meta.pub_date else None) or (m.pub_date if m else None)
        if m and meta.pub_date and m.pub_date and meta.pub_date.isoformat() != m.pub_date:
            flags.append("METADATA_CONFLICT")
            rep.conflicts.append(
                {
                    "doc_id": doc_id,
                    "field": "pub_date",
                    "manifest": m.pub_date,
                    "header": meta.pub_date.isoformat(),
                }
            )
        event_id = meta.event_id or (
            ("background" if is_background_event(m.event_id) else m.event_id) if m else None
        )
        if (
            m
            and meta.event_id
            and m.event_id
            and meta.event_id.lower()
            != ("background" if is_background_event(m.event_id) else m.event_id).lower()
        ):
            flags.append("METADATA_CONFLICT")
            rep.conflicts.append(
                {"doc_id": doc_id, "field": "event_id", "manifest": m.event_id, "header": meta.event_id}
            )
        tier = meta.tier or (m.tier if m else None)
        outlet = meta.outlet or (m.outlet if m else None)
        flags.extend(meta.flags)
        # tier cross-check with the outlets reference sheet
        if outlet:
            ref = self.repo.reference("outlets", outlet)
            if ref:
                try:
                    ref_tier = int(str(ref[0].get("tier", "")).strip())
                except ValueError:
                    ref_tier = None
                if ref_tier and tier != ref_tier:
                    flags.append("TIER_MISMATCH")
                    rep.conflicts.append(
                        {"doc_id": doc_id, "field": "tier", "header": tier, "reference": ref_tier}
                    )
                    tier = ref_tier
        # unknown countries
        known_countries = {str(r.get("name", "")).lower() for r in self.repo.reference("countries")}
        if known_countries and any(c.lower() not in known_countries for c in meta.countries):
            flags.append("UNKNOWN_COUNTRY")
        if pub_date is None:
            reason = "NO_DATE: publication date missing or not YYYY-MM-DD"
            self._quarantine_file(
                item.rel, doc_id, reason, rep, item.pack, item.content_hash, item.size, seq, fmt
            )
            self._write_stub(doc_id, m, "QUARANTINED", reason, fmt, item, seq)
            return None
        if item.extracted.repaired and status == "OK":
            status, reason = "REPAIRED", "encoding repaired: " + "; ".join(item.extracted.notes)[:200]
            rep.repaired.append({"doc_id": doc_id, "file": item.rel, "notes": item.extracted.notes})
        if damage.is_truncated(body, item.extracted.truncated, median_len, m.word_count if m else None):
            status, reason = "PARTIAL", "TRUNCATED: document appears cut off"
            flags.append("TRUNCATED")
        norm_hash = _sha(" ".join(body.lower().split()).encode())
        # same doc id delivered twice by different files
        existing = self.repo.get_doc(doc_id)
        if existing is not None and existing.file_path and existing.file_path != item.rel and existing.usable:
            other = self.repo.get_file(existing.file_path)
            if other is not None and other["status"] != "QUARANTINED":
                self.repo.upsert_file(
                    item.rel,
                    item.content_hash,
                    "doc",
                    fmt,
                    "DUPLICATE",
                    f"doc id {doc_id} already delivered by {existing.file_path}",
                    doc_id,
                    item.pack,
                    item.size,
                    seq,
                )
                rep.duplicates.append({"doc_id": doc_id, "file": item.rel, "canonical": doc_id})
                emit(
                    "ingest_issue",
                    actor="ingest",
                    payload={
                        "file": item.rel,
                        "ref": doc_id,
                        "status": "DUPLICATE",
                        "reason": "same doc id delivered twice",
                    },
                )
                return None
        canonical_id = None
        dup = self.repo.doc_by_hash(norm_hash, doc_id)
        if dup is not None:
            # the canonical doc is the earliest valid one (ties: lowest id)
            if ((dup.pub_date or ""), dup.doc_id) <= (pub_date, doc_id):
                status, reason, canonical_id = "DUPLICATE", f"same body as {dup.doc_id}", dup.doc_id
                rep.duplicates.append({"doc_id": doc_id, "file": item.rel, "canonical": dup.doc_id})
            else:
                dup.status, dup.status_reason, dup.canonical_id = (
                    "DUPLICATE",
                    f"same body as {doc_id}",
                    doc_id,
                )
                self.repo.upsert_doc(dup)
                self.repo.replace_chunks(dup.doc_id, [], [])
                self.vectors.remove_prefix(f"{dup.doc_id}#")
                rep.duplicates.append({"doc_id": dup.doc_id, "file": dup.file_path, "canonical": doc_id})
                # anything that pointed at the old canonical now points at the new one
                for other in self.repo.docs("canonical_id=?", (dup.doc_id,)):
                    other.canonical_id, other.status_reason = doc_id, f"same body as {doc_id}"
                    self.repo.upsert_doc(other)
                for entry in rep.duplicates:
                    if entry["canonical"] == dup.doc_id:
                        entry["canonical"] = doc_id
        off_manifest = m is None and rep.manifest_present
        if off_manifest:
            flags.append("OFF_MANIFEST")
            rep.off_manifest.append(doc_id)
        rec = DocRecord(
            doc_id=doc_id,
            title=meta.title or (m.title if m else None),
            outlet=outlet,
            tier=tier,
            pub_date=pub_date,
            source_type=meta.source_type or None,
            event_id=event_id,
            is_background=meta.is_background or is_background_event(event_id),
            countries=meta.countries,
            sectors=meta.sectors,
            format=fmt,
            status=status,
            status_reason=reason,
            flags=sorted(set(flags)),
            header=header.fields if header.found else {},
            body=body,
            content_hash=norm_hash,
            file_path=item.rel,
            pack=item.pack,
            off_manifest=off_manifest,
            canonical_id=canonical_id,
            ingest_seq=seq,
        )
        self.repo.upsert_doc(rec)
        self.repo.upsert_file(
            item.rel, item.content_hash, "doc", fmt, status, reason, doc_id, item.pack, item.size, seq
        )
        if status == "PARTIAL":
            rep.partial.append({"doc_id": doc_id, "file": item.rel, "reason": reason})
        payload = {
            "file": item.rel,
            "kind": "doc",
            "doc_id": doc_id,
            "format": fmt,
            "status": status,
            "reason": reason,
            "flags": rec.flags,
            "pack": item.pack,
        }
        if status in ("PARTIAL", "DUPLICATE", "REPAIRED") or off_manifest:
            self.repo.add_issue(
                item.rel, doc_id, status if not off_manifest else "OFF_MANIFEST", reason or ""
            )
            emit("ingest_issue", actor="ingest", payload=payload)
        else:
            emit("ingest_update", actor="ingest", payload=payload)
        return doc_id

    def _write_stub(
        self, doc_id: str, m: ManifestEntry | None, status: str, reason: str, fmt: str, item: Parsed, seq: int
    ) -> None:
        """A quarantined doc still gets a row, so citations to it resolve to a clear QUARANTINED status."""
        existing = self.repo.get_doc(doc_id)
        if existing is not None and existing.usable:
            return
        self.repo.upsert_doc(
            DocRecord(
                doc_id=doc_id,
                title=m.title if m else None,
                outlet=m.outlet if m else None,
                tier=m.tier if m else None,
                pub_date=m.pub_date if m else None,
                event_id=m.event_id if m else None,
                format=fmt,
                status=status,
                status_reason=reason,
                file_path=item.rel,
                pack=item.pack,
                off_manifest=m is None,
                ingest_seq=seq,
            )
        )
        self.vectors.remove_prefix(f"{doc_id}#")

    def _reconcile_manifest(self, manifest: dict[str, ManifestEntry], rep: IngestReport, seq: int) -> None:
        for doc_id, e in manifest.items():
            if self.repo.get_doc(doc_id) is None:
                self.repo.upsert_doc(
                    DocRecord(
                        doc_id=doc_id,
                        title=e.title,
                        outlet=e.outlet,
                        tier=e.tier,
                        pub_date=e.pub_date,
                        event_id=e.event_id,
                        format=e.format,
                        status="MISSING",
                        status_reason="listed in manifest, no file",
                        file_path=e.file,
                        pack=e.pack,
                        ingest_seq=seq,
                    )
                )
                rep.missing.append(doc_id)
                self.repo.add_issue(e.file or doc_id, doc_id, "MISSING", "listed in manifest, no file")
                emit(
                    "ingest_issue",
                    actor="ingest",
                    payload={
                        "file": e.file,
                        "ref": doc_id,
                        "status": "MISSING",
                        "reason": "listed in manifest, no file",
                    },
                )

    def _relations(self) -> None:
        docs = self.repo.docs("status NOT IN ('QUARANTINED','MISSING')")
        known = {d.doc_id: {"outlet": d.outlet, "pub_date": d.pub_date, "title": d.title} for d in docs}
        rx = id_pattern(list(known))
        for d in docs:
            self.repo.clear_relations_from(d.doc_id)
            if d.status == "DUPLICATE" and d.canonical_id:
                self.repo.add_relation(d.doc_id, d.canonical_id, "duplicate_of", d.pub_date)
            for rel in find_relations(
                d.doc_id,
                d.source_type,
                d.title,
                d.body,
                d.pub_date or "",
                d.tier,
                d.event_id,
                d.outlet,
                known,
                rx,
            ):
                self.repo.add_relation(rel.src_doc, rel.dst, rel.kind, rel.pub_date)

    def _index(self, doc_ids: list[str], rep: IngestReport) -> None:
        ids: list[str] = []
        texts: list[str] = []
        for doc_id in doc_ids:
            d = self.repo.get_doc(doc_id)
            if d is None:
                continue
            self.vectors.remove_prefix(f"{doc_id}#")
            if d.status in ("QUARANTINED", "MISSING", "DUPLICATE"):
                self.repo.replace_chunks(doc_id, [], [])
                continue
            hits = scan_injections(d.body)
            self.repo.set_injections(doc_id, [(h.start, h.span) for h in hits])
            if hits:
                self.repo.set_doc_flags(doc_id, [*d.flags, "INJECTION_DETECTED"])
                for h in hits:
                    rep.injections.append({"doc_id": doc_id, "span": h.span})
                emit(
                    "trap_detected",
                    actor="ingest",
                    payload={"type": "INJECTION", "doc_id": doc_id, "spans": [h.span for h in hits]},
                )
            chunks = chunk_text(d.body)
            rows = [(f"{doc_id}#c{c.seq}", c.seq, c.text, c.start, c.end) for c in chunks]
            fts = [f"{d.title or ''}\n{c.text}" for c in chunks]
            self.repo.replace_chunks(doc_id, rows, fts)
            ids.extend(r[0] for r in rows)
            texts.extend(fts)
        if self.vectors.stale:
            # embedder changed: re-embed everything
            all_rows = self.repo.all_chunks()
            ids = [r["chunk_id"] for r in all_rows]
            texts = [f"{r['title'] or ''}\n{r['text']}" for r in all_rows]
            self.vectors.ids, self.vectors.matrix = [], self.vectors.matrix[:0]
            self.vectors.stale = False
        if ids:
            try:
                vecs = self.embedder.embed_passages(texts)
                self.vectors.upsert(ids, vecs)
                self.vectors.save()
            except Exception as exc:
                rep.notes.append(f"embedding failed: {exc}; dense search degraded to BM25 only for new docs")
                emit(
                    "ingest_issue",
                    actor="ingest",
                    payload={"file": "*", "status": "EMBEDDING_FAILED", "reason": str(exc)[:200]},
                )

    # ------------------------------------------------------------------ report
    def _totals(self) -> dict[str, int]:
        docs = self.repo.docs()
        files = self.repo.files()
        tables = self.repo.trade_tables()
        n_rows = sum(len(self.repo.trade_rows(t.table_id)) for t in tables)
        flagged = sum(1 for t in tables for r in self.repo.trade_rows(t.table_id) if r.quality != "OK")
        return {
            "files": len(files),
            "docs_ok": sum(1 for d in docs if d.status == "OK" and not d.off_manifest),
            "repaired": sum(1 for d in docs if d.status == "REPAIRED"),
            "partial": sum(1 for d in docs if d.status == "PARTIAL"),
            "duplicates": sum(1 for f in files if f["status"] == "DUPLICATE")
            + sum(1 for d in docs if d.status == "DUPLICATE"),
            "quarantined": sum(1 for f in files if f["status"] == "QUARANTINED"),
            "off_manifest": sum(1 for d in docs if d.off_manifest),
            "missing": sum(1 for d in docs if d.status == "MISSING"),
            "trade_tables": len(tables),
            "trade_rows": n_rows,
            "trade_rows_flagged": flagged,
            "chunks": self.repo.counts()["chunks"],
            "vectors": len(self.vectors),
        }

    def _write_report(self, rep: IngestReport) -> None:
        if self.runs_dir is None:
            return
        out = self.runs_dir / "_ingest"
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        data = json.dumps(rep.to_dict(), indent=2, ensure_ascii=False, default=str)
        (out / f"report_{stamp}.json").write_text(data, encoding="utf-8")
        (out / "latest.json").write_text(data, encoding="utf-8")


def unzip_all(root: Path) -> list[tuple[Path, str]]:
    """Unzip every archive under `root` into a sibling folder (once). Returns the archives that failed.

    A zip whose single top-level folder repeats the archive name is flattened (pack.zip -> pack/...).
    """
    failed: list[tuple[Path, str]] = []
    for z in sorted(root.rglob("*.zip")):
        target = z.with_suffix("")
        if target.exists():
            continue
        try:
            with zipfile.ZipFile(z) as zf:
                names = zf.namelist()
                for member in names:
                    dest = (target / member).resolve()
                    if not str(dest).startswith(str(target.resolve())):
                        raise ValueError(f"unsafe path in zip: {member}")
                tops = {n.split("/")[0] for n in names if n.strip("/")}
                if tops == {target.name}:
                    zf.extractall(target.parent)
                else:
                    zf.extractall(target)
            emit("ingest_update", actor="ingest", payload={"stage": "unzipped", "file": str(z)})
        except Exception as exc:
            failed.append((z, str(exc)))
            emit(
                "ingest_issue",
                actor="ingest",
                payload={"file": str(z), "status": "QUARANTINED", "reason": f"BAD_ZIP: {exc}"},
            )
    return failed


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
