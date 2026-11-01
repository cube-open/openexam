"""成绩单 PDF 生成（reportlab）。"""
import html
import io
import re
from xml.sax.saxutils import escape as xml_escape


def _paragraph_text(value) -> str:
    """Escape arbitrary user content before passing it to ReportLab's XML parser."""
    return xml_escape(str(value or "")).replace("\n", "<br/>")


def _filename_part(value) -> str:
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(value or "")).strip(" ._")
    return value[:80] or "成绩单"


def generate_result_pdf(exam, student, result, rows, total, notes=None,
                        brand_name=None, brand_color="#2563eb") -> bytes:
    """Generate a safe, readable A4 score report with correct group-question numbering."""
    from flask import current_app, has_app_context
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import (HRFlowable, KeepTogether, Paragraph,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    if brand_name is None:
        brand_name = current_app.config.get("BRAND_NAME", "在线考试系统") if has_app_context() else "在线考试系统"
    if has_app_context():
        brand_color = current_app.config.get("BRAND_COLOR", brand_color)
    try:
        accent = colors.HexColor(brand_color)
    except (ValueError, TypeError):
        accent = colors.HexColor("#2563eb")

    palette = {
        "ink": colors.HexColor("#172033"),
        "muted": colors.HexColor("#64748b"),
        "line": colors.HexColor("#dbe2ea"),
        "soft": colors.HexColor("#f1f5f9"),
        "green": colors.HexColor("#087443"),
        "red": colors.HexColor("#b42318"),
    }
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, topMargin=17 * mm, bottomMargin=17 * mm,
        leftMargin=18 * mm, rightMargin=18 * mm,
        title="%s - 成绩单" % str(exam["title"]),
        author=str(brand_name),
    )
    def style(name, **kwargs):
        base = dict(fontName="STSong-Light", textColor=palette["ink"],
                    wordWrap="CJK", splitLongWords=1)
        base.update(kwargs)
        return ParagraphStyle(name, **base)

    brand_style = style("Brand", fontSize=9, leading=13, alignment=TA_CENTER,
                        textColor=palette["muted"])
    title_style = style("ReportTitle", fontSize=19, leading=26, alignment=TA_CENTER,
                        spaceAfter=3)
    section_style = style("Section", fontSize=13, leading=18, textColor=accent,
                          spaceBefore=5, spaceAfter=5)
    question_style = style("Question", fontSize=10.5, leading=16, spaceBefore=6,
                           spaceAfter=3)
    body_style = style("Body", fontSize=9.5, leading=15, textColor=palette["muted"])
    body_indent = style("BodyIndent", fontSize=9.5, leading=15,
                        textColor=palette["muted"], leftIndent=8)
    story = [
        Paragraph(_paragraph_text(brand_name or "在线考试系统"), brand_style),
        Spacer(1, 3 * mm),
        Paragraph(_paragraph_text(exam["title"]) + " · 成绩单", title_style),
        HRFlowable(width="100%", thickness=1.3, color=accent,
                   spaceBefore=3 * mm, spaceAfter=5 * mm),
    ]
    from ..utils import fmt_ts
    mode = "超时自动交卷" if result["auto_submitted"] else "正常交卷"
    summary = [
        ["姓名", str(student["name"]), "学号", str(student["student_no"])],
        ["试卷", str(exam["title"]), "交卷时间", fmt_ts(result["submitted_at"])],
        ["得分", "%s / %s" % (result["score"], total), "切屏警告", "%s 次" % result["violations"]],
        ["交卷方式", mode, "", ""],
    ]
    table = Table(summary, colWidths=[22*mm, 59*mm, 25*mm, 62*mm], repeatRows=0)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (0, -1), palette["soft"]),
        ("BACKGROUND", (2, 0), (2, -1), palette["soft"]),
        ("TEXTCOLOR", (0, 0), (0, -1), accent),
        ("TEXTCOLOR", (2, 0), (2, -1), accent),
        ("GRID", (0, 0), (-1, -1), .45, palette["line"]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story += [table, Spacer(1, 6*mm), Paragraph("答题详情", section_style)]

    letters = "ABCDEFGH"
    try:
        show_answer = bool(exam["show_answer"])
    except (KeyError, IndexError):
        show_answer = True
    try:
        show_explanation = bool(exam["show_explanation"])
    except (KeyError, IndexError):
        show_explanation = True
    for i, row in enumerate(rows, 1):
        pieces = [Paragraph("%d. 【%s】%s" % (
            i, _paragraph_text(row.get("type_label", "")),
            _paragraph_text(row.get("question", ""))), question_style)]
        if row.get("type") in ("choice", "multi", "judge"):
            for j, option in enumerate(row.get("options") or []):
                pieces.append(Paragraph("%s. %s" % (
                    letters[j] if j < len(letters) else str(j + 1),
                    _paragraph_text(option)), body_indent))
        given = row.get("given") or "（未作答）"
        answer_line = "你的答案：%s" % _paragraph_text(given)
        if show_answer:
            answer_line += "　正确答案：%s" % _paragraph_text(row.get("answer_display", ""))
        pieces.append(Paragraph(answer_line, body_indent))
        points = row.get("awarded", row.get("score", 0) if row.get("correct") else 0)
        state = "正确" if row.get("correct") else "错误"
        color = palette["green"] if row.get("correct") else palette["red"]
        pieces.append(Paragraph("%s（%s/%s 分）" % (
            state, _paragraph_text(points), _paragraph_text(row.get("score", 0))),
            style("State%d" % i, fontSize=9.5, leading=14, leftIndent=8,
                  textColor=color, spaceAfter=2)))
        note = (notes or {}).get(str(row.get("id")))
        if note:
            pieces.append(Paragraph("学生笔记：%s" % _paragraph_text(note), body_style))
        explanation = row.get("explanation")
        if show_explanation and explanation:
            pieces.append(Paragraph("解析：%s" % _paragraph_text(explanation), body_style))
        story.append(KeepTogether(pieces))

    doc.build(story)
    return buf.getvalue()


def safe_pdf_filename(*parts) -> str:
    return "成绩单_%s.pdf" % "_".join(_filename_part(p) for p in parts)
