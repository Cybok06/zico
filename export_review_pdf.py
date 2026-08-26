from __future__ import annotations

from pathlib import Path
import sys

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = ROOT / "app_review_findings_report.md"
DEFAULT_OUTPUT = ROOT / "app_review_findings_report.pdf"
DEFAULT_TITLE = "Web App Review Findings"


def build_styles():
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="ReportTitle",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=19,
            leading=24,
            textColor=colors.HexColor("#0f172a"),
            spaceAfter=10,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SectionHeading",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=17,
            textColor=colors.HexColor("#0f172a"),
            spaceBefore=10,
            spaceAfter=6,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Body",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=15,
            textColor=colors.HexColor("#1f2937"),
            alignment=TA_LEFT,
            spaceAfter=5,
        )
    )
    styles.add(
        ParagraphStyle(
            name="BulletBody",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=15,
            textColor=colors.HexColor("#1f2937"),
            leftIndent=14,
            firstLineIndent=-10,
            bulletIndent=0,
            spaceAfter=4,
        )
    )
    return styles


def escape_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_markdown(lines: list[str], styles) -> list:
    story = []
    for raw_line in lines:
        stripped = raw_line.strip()

        if not stripped:
            story.append(Spacer(1, 4))
            continue

        if stripped.startswith("# "):
            story.append(Paragraph(escape_text(stripped[2:]), styles["ReportTitle"]))
            continue

        if stripped.startswith("## "):
            story.append(Paragraph(escape_text(stripped[3:]), styles["SectionHeading"]))
            continue

        if stripped.startswith("- "):
            story.append(
                Paragraph(
                    escape_text(stripped[2:]),
                    styles["BulletBody"],
                    bulletText="-",
                )
            )
            continue

        if stripped[:2].isdigit() and stripped[1:3] == ". ":
            story.append(
                Paragraph(
                    escape_text(stripped[3:]),
                    styles["BulletBody"],
                    bulletText=escape_text(stripped[:2]),
                )
            )
            continue

        story.append(Paragraph(escape_text(stripped), styles["Body"]))

    return story


def main() -> None:
    source = DEFAULT_SOURCE
    output = DEFAULT_OUTPUT
    title = DEFAULT_TITLE

    if len(sys.argv) >= 2 and sys.argv[1].strip():
        source = (ROOT / sys.argv[1]).resolve()
    if len(sys.argv) >= 3 and sys.argv[2].strip():
        output = (ROOT / sys.argv[2]).resolve()
    if len(sys.argv) >= 4 and sys.argv[3].strip():
        title = sys.argv[3].strip()

    styles = build_styles()
    lines = source.read_text(encoding="utf-8").splitlines()
    story = parse_markdown(lines, styles)

    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=title,
        author="Codex",
    )
    doc.build(story)
    print(output)


if __name__ == "__main__":
    main()
