"""A report as a PDF (v45, AI plan Step 24): the report's own self-contained HTML page, printed by a headless
Chromium-family browser (Chrome, Edge or Chromium) - the same layout as "Print / Save as PDF" in the report.

The browser is found automatically (``VDASH_PDF_BROWSER`` = full path overrides). Where there is none (for example
a small web server), ``html_to_pdf`` raises ``PdfError`` and the caller sends the mail without the PDF and says so.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class PdfError(RuntimeError):
    pass


def find_browser() -> str | None:
    env = os.environ.get("VDASH_PDF_BROWSER")
    if env:
        return env if Path(env).is_file() else None
    cands: list[str] = []
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if base:
            cands += [f"{base}/Google/Chrome/Application/chrome.exe", f"{base}/Microsoft/Edge/Application/msedge.exe",
                      f"{base}/Chromium/Application/chrome.exe"]
    cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    for c in cands:
        if Path(c).is_file():
            return c
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "chrome", "msedge"):
        w = shutil.which(name)
        if w:
            return w
    hits = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome") + glob.glob("/opt/pw-browsers/chromium"))
    return hits[-1] if hits else None


def html_to_pdf(html: str, timeout: float = 90.0, browser: str | None = None) -> bytes:
    exe = browser or find_browser()
    if not exe:
        raise PdfError("no Chrome / Edge / Chromium found for making the PDF (set VDASH_PDF_BROWSER)")
    with tempfile.TemporaryDirectory(prefix="vdash_pdf_") as tmp:
        src, out = Path(tmp) / "report.html", Path(tmp) / "report.pdf"
        src.write_text(html, encoding="utf-8")
        cmd = [exe, "--headless", "--disable-gpu", "--no-pdf-header-footer", "--hide-scrollbars",
               "--run-all-compositor-stages-before-draw", "--virtual-time-budget=8000",
               f"--user-data-dir={Path(tmp) / 'profile'}", f"--print-to-pdf={out}"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:        # a root shell (containers) needs it; a normal user must not
            cmd.append("--no-sandbox")
        cmd.append(src.as_uri())
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise PdfError("making the PDF took too long") from None
        except OSError as exc:
            raise PdfError(f"the browser could not be started ({type(exc).__name__})") from None
        if not out.is_file() or out.stat().st_size < 1000:
            raise PdfError("the browser did not produce a PDF")
        data = out.read_bytes()
    if not data.startswith(b"%PDF"):
        raise PdfError("the browser did not produce a PDF")
    return data
