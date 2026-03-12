"""
AetherAI — Document Agent (Stage 2)
Creates real PowerPoint, Word, and Excel files.

CREATIVITY UPDATE:
- 6 distinct color themes chosen randomly per generation
- PowerPoint uses varied slide layouts (full-bleed accent, split, minimal, bold header)
- Word documents use theme-matched heading colors and varied section styles
- Excel uses theme-matched header/alt-row colors
"""

import logging
import os
import re
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Optional

from agents import BaseAgent
from utils.qwen_client import QwenClient

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent.parent.parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


def safe_filename(text: str, ext: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text).strip()
    slug = re.sub(r"[\s_-]+", "_", slug)[:50]
    ts = datetime.now().strftime("%H%M%S")
    return f"{slug}_{ts}.{ext}"


# ── Color themes ──────────────────────────────────────────────────────────────
# Each theme: (bg_dark, bg_light, accent, text_dark, text_light, muted, name)
THEMES = [
    {
        "name":       "Ocean Deep",
        "bg_dark":    (0x06, 0x40, 0x52),
        "bg_light":   (0xF0, 0xF7, 0xF8),
        "accent":     (0x02, 0xC3, 0x9A),
        "text_dark":  (0x0D, 0x2B, 0x35),
        "text_light": (0xFF, 0xFF, 0xFF),
        "muted":      (0x6B, 0x9E, 0xAB),
        "alt_row":    (0xE8, 0xF5, 0xF7),
    },
    {
        "name":       "Midnight Purple",
        "bg_dark":    (0x1A, 0x0A, 0x2E),
        "bg_light":   (0xF5, 0xF0, 0xFF),
        "accent":     (0x9B, 0x59, 0xB6),
        "text_dark":  (0x1A, 0x0A, 0x2E),
        "text_light": (0xFF, 0xFF, 0xFF),
        "muted":      (0xA5, 0x8B, 0xC5),
        "alt_row":    (0xEE, 0xE8, 0xF8),
    },
    {
        "name":       "Crimson Executive",
        "bg_dark":    (0x2C, 0x06, 0x06),
        "bg_light":   (0xFF, 0xF5, 0xF5),
        "accent":     (0xE7, 0x2B, 0x2B),
        "text_dark":  (0x1A, 0x05, 0x05),
        "text_light": (0xFF, 0xFF, 0xFF),
        "muted":      (0xC0, 0x7A, 0x7A),
        "alt_row":    (0xFD, 0xED, 0xED),
    },
    {
        "name":       "Forest Green",
        "bg_dark":    (0x0B, 0x2D, 0x0F),
        "bg_light":   (0xF1, 0xFA, 0xF2),
        "accent":     (0x27, 0xAE, 0x60),
        "text_dark":  (0x0B, 0x2D, 0x0F),
        "text_light": (0xFF, 0xFF, 0xFF),
        "muted":      (0x6B, 0xAB, 0x78),
        "alt_row":    (0xE6, 0xF6, 0xE9),
    },
    {
        "name":       "Solar Gold",
        "bg_dark":    (0x2D, 0x1B, 0x00),
        "bg_light":   (0xFF, 0xFB, 0xF0),
        "accent":     (0xF3, 0x9C, 0x12),
        "text_dark":  (0x2D, 0x1B, 0x00),
        "text_light": (0xFF, 0xFF, 0xFF),
        "muted":      (0xC8, 0xA0, 0x55),
        "alt_row":    (0xFE, 0xF5, 0xDC),
    },
    {
        "name":       "Steel Blue",
        "bg_dark":    (0x0D, 0x1B, 0x2A),
        "bg_light":   (0xF0, 0xF4, 0xF8),
        "accent":     (0x1E, 0x90, 0xFF),
        "text_dark":  (0x0D, 0x1B, 0x2A),
        "text_light": (0xFF, 0xFF, 0xFF),
        "muted":      (0x6A, 0x8F, 0xBB),
        "alt_row":    (0xE5, 0xEE, 0xF8),
    },
]


def pick_theme() -> dict:
    return random.choice(THEMES)


class DocumentAgent(BaseAgent):
    name = "document_agent"
    description = "Creates PowerPoint, Word, and Excel files"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> Optional[str]:
        all_signals = " ".join([
            parameters.get("type", ""),
            parameters.get("topic", ""),
            parameters.get("query", ""),
            parameters.get("title", ""),
            parameters.get("content", ""),
            parameters.get("description", ""),
            parameters.get("format", ""),
            context,
        ]).lower()

        PPTX_KEYS = ["presentation", "powerpoint", "pptx", "slides", "slide deck", "deck", "slideshow"]
        XLSX_KEYS = ["spreadsheet", "excel", "xlsx"]

        if any(k in all_signals for k in PPTX_KEYS):
            doc_type = "presentation"
        elif any(k in all_signals for k in XLSX_KEYS):
            doc_type = "spreadsheet"
        else:
            raw_type = parameters.get("type", "").lower()
            if raw_type in ("presentation", "pptx", "slides"):
                doc_type = "presentation"
            elif raw_type in ("spreadsheet", "excel", "xlsx"):
                doc_type = "spreadsheet"
            else:
                doc_type = "document"

        topic = (parameters.get("topic")
                 or parameters.get("title")
                 or parameters.get("query")
                 or parameters.get("content")
                 or parameters.get("subject")
                 or (context[:100] if context else "Untitled"))

        logger.info(f"[DocumentAgent] type={doc_type}  topic={topic[:60]}")

        if doc_type in ("presentation", "pptx", "slides", "powerpoint"):
            return await self._create_pptx(topic, context, parameters)
        elif doc_type in ("spreadsheet", "excel", "xlsx"):
            return await self._create_xlsx(topic, context, parameters)
        else:
            return await self._create_docx(topic, context, parameters)

    # ── PowerPoint ────────────────────────────────────────────────────────────

    async def _create_pptx(self, topic: str, context: str, params: dict) -> str:
        try:
            from pptx import Presentation
            from pptx.util import Inches, Pt, Emu
            from pptx.dml.color import RGBColor
            from pptx.enum.text import PP_ALIGN
        except ImportError:
            return "[DocumentAgent] python-pptx not installed. Run: pip install python-pptx"

        plan_prompt = f"""Create a professional PowerPoint presentation about: {topic}

Research context:
{context[:2000] if context else 'Use your knowledge.'}

Return ONLY valid JSON — no markdown fences, no extra text:
{{
  "title": "Presentation Title",
  "subtitle": "A compelling subtitle",
  "slides": [
    {{
      "title": "Slide Title",
      "bullets": ["Point 1", "Point 2", "Point 3"],
      "speaker_note": "Brief note"
    }}
  ]
}}

Include 6-8 content slides. Keep bullet points concise (max 15 words). Max 4 bullets per slide.
Make the content rich, specific, and informative — not generic."""

        raw = await self.qwen.chat(
            system_prompt="You are a presentation writer. Return ONLY valid JSON, no markdown fences.",
            user_message=plan_prompt,
            temperature=0.5,
        )
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[DocumentAgent] JSON parse failed, using fallback")
            data = {
                "title": topic, "subtitle": "Generated by AetherAI",
                "slides": [{"title": "Overview", "bullets": ["Content generated by AetherAI"], "speaker_note": ""}]
            }

        T = pick_theme()
        logger.info(f"[DocumentAgent] Using theme: {T['name']}")

        def rgb(tup): return RGBColor(*tup)

        prs = Presentation()
        prs.slide_width  = Inches(13.33)
        prs.slide_height = Inches(7.5)
        blank = prs.slide_layouts[6]

        def rect(slide, x, y, w, h, color, alpha=None):
            s = slide.shapes.add_shape(1, Inches(x), Inches(y), Inches(w), Inches(h))
            s.fill.solid()
            s.fill.fore_color.rgb = color
            s.line.fill.background()
            return s

        def txt(slide, text, x, y, w, h, size, bold=False, color=None,
                align=PP_ALIGN.LEFT, italic=False, wrap=True):
            tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
            tf = tb.text_frame
            tf.word_wrap = wrap
            p  = tf.paragraphs[0]
            p.alignment = align
            r  = p.add_run()
            r.text = str(text)
            r.font.size  = Pt(size)
            r.font.bold  = bold
            r.font.italic = italic
            r.font.color.rgb = color or rgb(T["text_dark"])
            return tb

        def add_bullets(slide, bullets, x, y, w, h, size=19):
            tb  = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
            tf2 = tb.text_frame
            tf2.word_wrap = True
            for j, b in enumerate(bullets):
                para = tf2.paragraphs[0] if j == 0 else tf2.add_paragraph()
                para.space_before = Pt(6)
                para.space_after  = Pt(6)
                run = para.add_run()
                run.text = f"▸  {b}"
                run.font.size = Pt(size)
                run.font.color.rgb = rgb(T["text_dark"])
            return tb

        # ── Title slide ───────────────────────────────────────────────────────
        ts = prs.slides.add_slide(blank)
        rect(ts, 0, 0, 13.33, 7.5, rgb(T["bg_dark"]))
        # Diagonal accent strip
        rect(ts, 0, 5.8, 13.33, 1.7, rgb(T["accent"]))
        rect(ts, 0, 5.6, 6.5, 1.9, rgb(T["bg_dark"]))
        txt(ts, data.get("title", topic), 0.8, 1.6, 11.5, 2.5, 54,
            bold=True, color=rgb(T["text_light"]), align=PP_ALIGN.LEFT)
        txt(ts, data.get("subtitle", ""), 0.8, 4.2, 9.0, 1.0, 22,
            color=rgb(T["muted"]), align=PP_ALIGN.LEFT)
        txt(ts, f"{T['name']} Theme  ·  Generated by AetherAI  ·  {datetime.now().strftime('%B %d, %Y')}",
            0.8, 6.9, 11.5, 0.4, 10, color=rgb(T["bg_dark"]), align=PP_ALIGN.LEFT)

        # ── Content slides — cycle through 3 layout variants ─────────────────
        layouts = ["standard", "accent_left", "bold_header"]

        for i, sd in enumerate(data.get("slides", [])):
            layout = layouts[i % len(layouts)]
            sl = prs.slides.add_slide(blank)

            if layout == "standard":
                # Clean white slide with dark header bar
                rect(sl, 0, 0, 13.33, 7.5, rgb(T["bg_light"]))
                rect(sl, 0, 0, 13.33, 1.2, rgb(T["bg_dark"]))
                rect(sl, 12.5, 0.25, 0.83, 0.7, rgb(T["accent"]))  # slide number badge
                txt(sl, str(i + 1), 12.5, 0.25, 0.83, 0.7, 14,
                    bold=True, color=rgb(T["text_light"]), align=PP_ALIGN.CENTER)
                txt(sl, sd.get("title", ""), 0.4, 0.15, 11.8, 0.9, 28,
                    bold=True, color=rgb(T["text_light"]))
                rect(sl, 0, 1.2, 0.2, 6.3, rgb(T["accent"]))  # left accent bar
                add_bullets(sl, sd.get("bullets", []), 0.55, 1.45, 12.3, 5.7)

            elif layout == "accent_left":
                # Split: colored left panel + white right content area
                rect(sl, 0, 0, 4.2, 7.5, rgb(T["bg_dark"]))
                rect(sl, 4.2, 0, 9.13, 7.5, rgb(T["bg_light"]))
                rect(sl, 4.2, 0, 9.13, 0.08, rgb(T["accent"]))  # top accent line
                # Slide number on left panel
                txt(sl, str(i + 1), 0.3, 0.3, 1.0, 0.8, 36,
                    bold=True, color=rgb(T["accent"]))
                # Title on left panel (vertical centered)
                txt(sl, sd.get("title", ""), 0.3, 1.2, 3.5, 5.0, 24,
                    bold=True, color=rgb(T["text_light"]))
                add_bullets(sl, sd.get("bullets", []), 4.55, 0.8, 8.4, 6.3)

            elif layout == "bold_header":
                # Full-width accent header, light body
                rect(sl, 0, 0, 13.33, 7.5, rgb(T["bg_light"]))
                rect(sl, 0, 0, 13.33, 2.0, rgb(T["accent"]))
                txt(sl, str(i + 1), 12.3, 0.2, 0.8, 0.6, 13,
                    bold=True, color=rgb(T["bg_dark"]), align=PP_ALIGN.CENTER)
                txt(sl, sd.get("title", ""), 0.5, 0.3, 11.6, 1.4, 32,
                    bold=True, color=rgb(T["text_light"]))
                rect(sl, 0.5, 2.15, 1.5, 0.07, rgb(T["bg_dark"]))  # underline
                add_bullets(sl, sd.get("bullets", []), 0.55, 2.35, 12.2, 4.8)

            note = sd.get("speaker_note", "")
            if note:
                sl.notes_slide.notes_text_frame.text = note

        # ── Closing slide ─────────────────────────────────────────────────────
        cs = prs.slides.add_slide(blank)
        rect(cs, 0, 0, 13.33, 7.5, rgb(T["bg_dark"]))
        rect(cs, 0, 3.2, 13.33, 0.1, rgb(T["accent"]))
        rect(cs, 0, 0, 13.33, 3.2, rgb(T["accent"]))
        txt(cs, "Thank You", 0.8, 0.6, 11.5, 2.0, 64,
            bold=True, color=rgb(T["bg_dark"]), align=PP_ALIGN.CENTER)
        txt(cs, data.get("title", topic), 0.8, 3.5, 11.5, 1.0, 20,
            color=rgb(T["muted"]), align=PP_ALIGN.CENTER)
        txt(cs, "Generated by AetherAI", 0.8, 4.6, 11.5, 0.5, 13,
            color=rgb(T["muted"]), align=PP_ALIGN.CENTER)

        fname = safe_filename(data.get("title", topic), "pptx")
        fpath = OUTPUT_DIR / fname
        prs.save(str(fpath))
        n = len(data.get("slides", []))
        return (f"✅ PowerPoint created: output/{fname}\n"
                f"Theme: {T['name']}  |  Slides: {n + 2} (title + {n} content + closing)\n"
                f"Topic: {data.get('title', topic)}\n"
                f"Full path: {fpath}")

    # ── Word Document ─────────────────────────────────────────────────────────

    async def _create_docx(self, topic: str, context: str, params: dict) -> str:
        try:
            from docx import Document
            from docx.shared import Pt, Inches, RGBColor as DR
            from docx.enum.text import WD_ALIGN_PARAGRAPH
        except ImportError:
            return "[DocumentAgent] python-docx not installed. Run: pip install python-docx"

        prompt = f"""Write a professional, detailed document about: {topic}

Context:
{context[:2000] if context else 'Use your knowledge.'}

Return ONLY valid JSON:
{{
  "title": "Document Title",
  "sections": [
    {{"heading": "Section Heading", "paragraphs": ["Paragraph 1...", "Paragraph 2..."]}}
  ],
  "conclusion": "Concluding paragraph."
}}
Include 4-6 sections with 2-3 paragraphs each. Make the content rich and specific."""

        raw = await self.qwen.chat(
            system_prompt="Professional document writer. Return ONLY valid JSON, no markdown fences.",
            user_message=prompt, temperature=0.5)
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"title": topic,
                    "sections": [{"heading": "Content", "paragraphs": [context[:1000] or topic]}],
                    "conclusion": ""}

        T = pick_theme()
        logger.info(f"[DocumentAgent] Using theme: {T['name']}")

        def dr(tup): return DR(*tup)

        doc = Document()
        for sec in doc.sections:
            sec.top_margin = sec.bottom_margin = Inches(1.0)
            sec.left_margin = sec.right_margin = Inches(1.25)

        # Title
        tp = doc.add_paragraph()
        tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        tr = tp.add_run(data.get("title", topic))
        tr.bold = True
        tr.font.size = Pt(26)
        tr.font.color.rgb = dr(T["bg_dark"])

        # Theme & date
        dp = doc.add_paragraph()
        dp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        dr2 = dp.add_run(
            f"{T['name']} Theme  ·  Generated by AetherAI  ·  {datetime.now().strftime('%B %d, %Y')}"
        )
        dr2.font.size = Pt(10)
        dr2.font.color.rgb = dr(T["muted"])
        doc.add_paragraph()

        for sec in data.get("sections", []):
            h = doc.add_heading(sec.get("heading", ""), level=1)
            if h.runs:
                h.runs[0].font.color.rgb = dr(T["bg_dark"])
                h.runs[0].font.size = Pt(14)
            for pt in sec.get("paragraphs", []):
                p = doc.add_paragraph(pt)
                p.paragraph_format.space_after = Pt(8)
                p.paragraph_format.first_line_indent = Inches(0.3)

        if data.get("conclusion"):
            h2 = doc.add_heading("Conclusion", level=1)
            if h2.runs:
                h2.runs[0].font.color.rgb = dr(T["bg_dark"])
                h2.runs[0].font.size = Pt(14)
            doc.add_paragraph(data["conclusion"])

        fname = safe_filename(data.get("title", topic), "docx")
        fpath = OUTPUT_DIR / fname
        doc.save(str(fpath))
        return (f"✅ Word document created: output/{fname}\n"
                f"Theme: {T['name']}  |  Sections: {len(data.get('sections', []))}\n"
                f"Title: {data.get('title', topic)}\n"
                f"Full path: {fpath}")

    # ── Excel Spreadsheet ─────────────────────────────────────────────────────

    async def _create_xlsx(self, topic: str, context: str, params: dict) -> str:
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
            from openpyxl.utils import get_column_letter
        except ImportError:
            return "[DocumentAgent] openpyxl not installed. Run: pip install openpyxl"

        prompt = f"""Create comprehensive spreadsheet data about: {topic}

Context: {context[:1500] if context else 'Use your knowledge.'}

Return ONLY valid JSON:
{{
  "title": "Spreadsheet Title",
  "sheets": [
    {{
      "name": "Sheet Name",
      "headers": ["Col1", "Col2", "Col3"],
      "rows": [["val1","val2","val3"]]
    }}
  ]
}}
Include 1-3 sheets with 10-15 data rows each. Make the data specific and realistic."""

        raw = await self.qwen.chat(
            system_prompt="Data analyst. Return ONLY valid JSON, no markdown fences.",
            user_message=prompt, temperature=0.4)
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"title": topic, "sheets": [{"name": "Data", "headers": ["Item", "Value"], "rows": [["Example", "Data"]]}]}

        T = pick_theme()
        logger.info(f"[DocumentAgent] Using theme: {T['name']}")

        def hex_color(tup):
            return "{:02X}{:02X}{:02X}".format(*tup)

        wb = openpyxl.Workbook()
        wb.remove(wb.active)

        hdr_fill  = PatternFill("solid", fgColor=hex_color(T["bg_dark"]))
        alt_fill  = PatternFill("solid", fgColor=hex_color(T["alt_row"]))
        acc_fill  = PatternFill("solid", fgColor=hex_color(T["accent"]))
        hdr_font  = Font(bold=True, color=hex_color(T["text_light"]), size=11)
        title_font = Font(bold=True, color=hex_color(T["bg_dark"]), size=15)
        thin  = Side(style="thin", color="CCCCCC")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        center = Alignment(horizontal="center", vertical="center")
        left   = Alignment(horizontal="left",   vertical="center")

        for sh in data.get("sheets", []):
            ws = wb.create_sheet(title=sh.get("name", "Sheet")[:31])
            headers = sh.get("headers", [])
            ncols   = max(len(headers), 1)

            # Title row
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
            c = ws.cell(1, 1, data.get("title", topic))
            c.font = title_font
            c.alignment = center
            c.fill = PatternFill("solid", fgColor=hex_color(T["bg_light"]))
            ws.row_dimensions[1].height = 32

            # Accent separator row
            for ci in range(1, ncols + 1):
                ac = ws.cell(2, ci, "")
                ac.fill = acc_fill
            ws.row_dimensions[2].height = 4

            # Header row
            for ci, h in enumerate(headers, 1):
                cell = ws.cell(3, ci, h)
                cell.font = hdr_font
                cell.fill = hdr_fill
                cell.alignment = center
                cell.border = border
            ws.row_dimensions[3].height = 24

            # Data rows
            for ri, row in enumerate(sh.get("rows", []), 4):
                for ci, val in enumerate(row, 1):
                    cell = ws.cell(ri, ci, val)
                    cell.border = border
                    cell.alignment = left
                    if ri % 2 == 0:
                        cell.fill = alt_fill
                ws.row_dimensions[ri].height = 18

            # Auto-width columns
            for ci, _ in enumerate(headers, 1):
                col_letter = get_column_letter(ci)
                max_len = len(str(headers[ci - 1]))
                for row in sh.get("rows", []):
                    if ci <= len(row):
                        max_len = max(max_len, len(str(row[ci - 1])))
                ws.column_dimensions[col_letter].width = min(max_len + 4, 40)

        fname = safe_filename(data.get("title", topic), "xlsx")
        fpath = OUTPUT_DIR / fname
        wb.save(str(fpath))
        sheets_info = ", ".join(s.get("name", "Sheet") for s in data.get("sheets", []))
        return (f"✅ Excel spreadsheet created: output/{fname}\n"
                f"Theme: {T['name']}  |  Sheets: {sheets_info}\n"
                f"Title: {data.get('title', topic)}\n"
                f"Full path: {fpath}")
