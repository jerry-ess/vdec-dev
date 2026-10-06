"""The e-mail of a report (v45, AI plan Step 24): a short summary in the body, the full report as a PDF.

The body repeats page 1 of the report - the headline, up to three key messages and the "Since your last report"
lines - taken from the stored document, so the mail can never say something the report does not. Plain inline-styled
HTML (works in every mail program, no images, no scripts, no external links) plus a plain-text version.
"""
from __future__ import annotations

import html as _html
import logging
import os

from ..i18n import LOCALES, bundle, norm_lang
from .html import _R, render_html
from .pdf import PdfError, find_browser, html_to_pdf
from .pdf_native import PdfNativeError, render_pdf

log = logging.getLogger("vdash.mail")
MAX_MESSAGES = 3
TONE = {"good": "#1a7f37", "bad": "#cf222e", "warn": "#9a6700", "warning": "#9a6700", "info": "#57606a"}


def _texts(lg: str) -> dict:
    return {**bundle("en")["ui"], **bundle(lg)["ui"]}


def _start(text: str, n: int = 30) -> str:
    return " ".join((text or "").lower().split())[:n]


def make_pdf(doc: dict, lg: str) -> tuple[bytes, str]:
    """The report as PDF bytes and the engine that made it. ``VDASH_PDF_ENGINE``: ``auto`` (default: the browser's
    print layout when Chrome / Edge / Chromium exists, else - or if printing fails - the built-in ReportLab layout),
    ``browser`` or ``native``. Raises PdfError."""
    engine = os.environ.get("VDASH_PDF_ENGINE", "auto").strip().lower()
    ui, loc = _texts(lg), LOCALES[lg]
    if engine == "browser" or (engine == "auto" and find_browser()):
        try:
            return html_to_pdf(render_html(doc, ui, loc, standalone=True)), "browser"
        except PdfError as exc:
            if engine == "browser":
                raise
            log.warning("browser PDF failed (%s) - using the built-in PDF layout", exc)
    try:
        return render_pdf(doc, ui, loc), "native"
    except PdfNativeError as exc:
        raise PdfError(f"built-in PDF layout failed: {exc}") from None


def mail_parts(doc: dict, name: str | None = None) -> dict:
    """{subject, headline, messages: [(tone, text)], changes: [(tone, text)], title, business, period, language}."""
    lg = norm_lang(doc["meta"]["lang"])
    ui = _texts(lg)
    r = _R(doc, ui)
    s = doc.get("summary") or {}
    st = r.story()
    head = (st or {}).get("headline") or (s.get("headline") or {}).get("text") or ""
    if st and st.get("key_messages"):
        msgs = [(m.get("tone") or "info", m["text"]) for m in st["key_messages"]]
    else:
        msgs = [(m.get("tone") or "info", m["text"]) for m in s.get("messages") or []]
    ch = doc.get("changes") or {}
    items = ch.get("items") or []
    lines = [(items[i].get("tone") or "info", items[i]["text"]) for i in ch.get("page1") or [] if 0 <= i < len(items) and items[i].get("text")]
    if not lines and ch.get("steady") and ch.get("steady_text"):
        lines = [("info", ch["steady_text"])]
    msgs = [(t, x) for t, x in msgs if _start(x) != _start(head)] if head else msgs          # a message that opens like the headline
    said = {x for _, x in msgs}
    lines = [(t, x) for t, x in lines if x not in said]          # a line the key messages already say is not repeated
    cover = doc.get("cover") or {}
    report = (name or "").split(" · ", 1)[-1] if name else cover.get("title") or ""
    business = cover.get("business") or ""
    return {"subject": ui["mail_subject"].replace("{business}", business).replace("{report}", report).strip(" ·"),
            "headline": head, "messages": msgs[:MAX_MESSAGES], "changes": lines, "title": report, "business": business,
            "period": cover.get("period_text") or "", "language": lg, "ui": ui,
            "generated": cover.get("generated_text") or ""}


def build_mail(doc: dict, name: str | None = None, with_pdf: bool = True, pdf_bytes: bytes | None = None,
               unsubscribe_url: str | None = None) -> dict:
    """The mail of one stored report: {subject, html, text, attachments, pdf_error}. The PDF is made here unless given."""
    p = mail_parts(doc, name)
    ui, e = p["ui"], _html.escape
    pdf_error = pdf_engine = None
    attachments: list[dict] = []
    if with_pdf:
        try:
            lg = p["language"]
            if pdf_bytes:
                data, pdf_engine = pdf_bytes, "given"
            else:
                data, pdf_engine = make_pdf(doc, lg)
            m = doc["meta"]["period"]
            attachments.append({"filename": f"{doc['meta']['tenant_id']}_report_{m['start']}_{m['end']}.pdf", "content": data})
        except PdfError as exc:
            pdf_error = str(exc)
            log.warning("report mail without PDF: %s", pdf_error)
    note = ui["mail_attached"] if attachments else ui["mail_no_pdf"]

    def li(items):
        return "".join(f'<li style="margin:0 0 6px;color:{TONE.get(t, TONE["info"])}"><span style="color:#24292f">{e(x)}</span></li>'
                       for t, x in items)
    parts = [f'<div style="font-size:13px;color:#57606a">{e(p["business"])} · {e(p["period"])}</div>',
             f'<h2 style="margin:6px 0 14px;font-size:20px;line-height:1.3;color:#24292f">{e(p["headline"] or p["title"])}</h2>']
    if p["messages"]:
        parts.append(f'<ul style="margin:0 0 18px;padding-left:20px;font-size:15px;line-height:1.5">{li(p["messages"])}</ul>')
    if p["changes"]:
        parts.append(f'<h3 style="margin:0 0 8px;font-size:14px;color:#24292f">{e(ui["chg_title"])}</h3>'
                     f'<ul style="margin:0 0 18px;padding-left:20px;font-size:14px;line-height:1.5">{li(p["changes"])}</ul>')
    parts.append(f'<p style="margin:0 0 18px;font-size:14px;color:#24292f">{e(note)}</p>')
    unsub = (f'<div style="margin-top:6px"><a href="{e(unsubscribe_url)}" style="color:#6e7781">{e(ui["mail_unsub"])}</a></div>'
             if unsubscribe_url else "")
    parts.append(f'<div style="border-top:1px solid #d0d7de;padding-top:10px;font-size:12px;color:#6e7781">'
                 f'{e(ui["mail_footer"].replace("{business}", p["business"]))} {e(p["generated"])}{unsub}</div>')
    html = ('<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#f6f8fa">'
            '<div style="max-width:620px;margin:0 auto;padding:24px 16px;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif">'
            '<div style="background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:24px">' + "".join(parts) +
            '</div></div></body></html>')
    text = "\n".join([f'{p["business"]} · {p["period"]}', "", p["headline"] or p["title"], ""]
                     + [f"- {x}" for _, x in p["messages"]]
                     + (["", ui["chg_title"]] + [f"- {x}" for _, x in p["changes"]] if p["changes"] else [])
                     + ["", note, "", ui["mail_footer"].replace("{business}", p["business"])]
                     + (["", f'{ui["mail_unsub"]}: {unsubscribe_url}'] if unsubscribe_url else []))
    return {"subject": p["subject"], "html": html, "text": text, "attachments": attachments, "pdf_error": pdf_error, "pdf_engine": pdf_engine}
