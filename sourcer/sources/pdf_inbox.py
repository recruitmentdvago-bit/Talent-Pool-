"""Folder intake: drop profile PDFs or CVs in storage/pdf_inbox and they get
parsed, stored, deduplicated and scored on the next run.

This is the workhorse route. It accepts LinkedIn "Save to PDF" exports, CVs
emailed to you, and anything else a candidate sends.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .. import events
from ..config import cfg
from ..pdfstore import parse_confidence, parse_profile_pdf
from .base import BaseSource, FetchContext, FetchResult

MIN_CONFIDENCE = 0.4


class PdfInboxSource(BaseSource):
    name = "pdf_inbox"

    def __init__(self, inbox: str | Path | None = None):
        self.inbox = Path(inbox) if inbox else cfg().path_of("storage.pdf_inbox")

    def available(self) -> tuple[bool, str]:
        self.inbox.mkdir(parents=True, exist_ok=True)
        n = len(self._files())
        return bool(n), f"{n} PDF(s) waiting in {self.inbox}"

    def _files(self) -> list[Path]:
        return sorted(p for p in self.inbox.glob("*.pdf") if not p.name.startswith("."))

    def fetch(self, ctx: FetchContext) -> FetchResult:
        res = FetchResult()
        done = self.inbox / "processed"
        quarantine = self.inbox / "needs_review"
        def quarantine_file(path, reason: str) -> None:
            """Move a file we cannot use out of the inbox.

            Without this a corrupt PDF stays in the inbox and fails again on
            every run, filling the daily log forever.
            """
            if ctx.dry_run:
                return
            quarantine.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(path), str(quarantine / path.name))
                (quarantine / (path.name + ".reason.txt")).write_text(reason + "\n")
            except OSError as move_err:
                res.errors.append(f"{path.name}: could not quarantine: {move_err}")

        for path in self._files()[: ctx.max_results]:
            try:
                events.emit("extract", f"Reading the PDF {path.name}", candidate=path.stem)
                cand = parse_profile_pdf(path, source=self.name)
                conf, issues = parse_confidence(cand)
                cand.raw["parse_confidence"] = conf
                cand.raw["parse_issues"] = issues
                events.emit("parse", f"Structured the profile: {cand.current_title() or 'no title found'}"
                            f"{' at ' + cand.current_company() if cand.current_company() else ''} - "
                            f"{len(cand.experiences)} roles, {len(cand.skills())} skills, confidence {conf}",
                            candidate=cand.full_name or path.stem,
                            level="warn" if issues else "info", detail={"issues": issues})
                if conf < MIN_CONFIDENCE:
                    reason = f"low parse confidence {conf}: {', '.join(issues)}"
                    res.errors.append(f"{path.name}: {reason}")
                    events.emit("error", f"Could not read {path.name} well enough ({reason}) - moved to needs_review",
                                candidate=cand.full_name or path.stem, level="error")
                    quarantine_file(path, reason)
                    continue
                if issues:
                    res.notes.append(f"{path.name}: confidence {conf} - {', '.join(issues)}")
                if not ctx.dry_run:
                    done.mkdir(parents=True, exist_ok=True)
                    kept = done / path.name
                    shutil.move(str(path), str(kept))
                    cand.profile_pdf_path = str(kept)
                res.candidates.append(cand)
            except Exception as e:
                reason = f"{type(e).__name__}: {e}"
                res.errors.append(f"{path.name}: {reason}")
                events.emit("error", f"Failed to read {path.name}: {reason}", candidate=path.stem, level="error")
                quarantine_file(path, reason)
        return res
