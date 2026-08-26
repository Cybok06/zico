import os
import requests
from typing import Any, Dict, List, Optional
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


class ExoSupplierAPIError(Exception):
    """Custom exception for ExoSupplier API errors."""
    pass


class ExoSupplierRuntime:
    BASE_URL = "https://exosupplier.com/api/v2"
    DEFAULT_API_KEY = "fedac68a3f8f8fd040f12c1f15e61380"

    def __init__(self, api_key: Optional[str] = None, timeout: int = 30):
        self.api_key = api_key or os.getenv("EXOSUPPLIER_API_KEY") or self.DEFAULT_API_KEY
        self.timeout = timeout

        if not self.api_key:
            raise ValueError(
                "ExoSupplier API key is missing. "
                "Set EXOSUPPLIER_API_KEY in your environment or pass api_key directly."
            )

    def _post(self, payload: Dict[str, Any]) -> Any:
        """Send POST request to ExoSupplier API."""
        data = {
            "key": self.api_key,
            **payload
        }

        try:
            response = requests.post(
                self.BASE_URL,
                data=data,
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            raise ExoSupplierAPIError(f"Network/API request failed: {e}") from e
        except ValueError as e:
            raise ExoSupplierAPIError("Invalid JSON response from ExoSupplier API.") from e

    def get_services(self) -> List[Dict[str, Any]]:
        """Fetch all available services."""
        result = self._post({
            "action": "services"
        })

        if isinstance(result, dict) and result.get("error"):
            raise ExoSupplierAPIError(result["error"])

        if not isinstance(result, list):
            raise ExoSupplierAPIError(f"Unexpected response format: {result}")

        return result

    def get_balance(self) -> Dict[str, Any]:
        """Fetch account balance."""
        result = self._post({
            "action": "balance"
        })

        if isinstance(result, dict) and result.get("error"):
            raise ExoSupplierAPIError(result["error"])

        return result

    def add_order(
        self,
        service: int,
        link: str,
        quantity: int,
        runs: Optional[int] = None,
        interval: Optional[int] = None
    ) -> Dict[str, Any]:
        """Create a new order."""
        payload = {
            "action": "add",
            "service": service,
            "link": link,
            "quantity": quantity
        }

        if runs is not None:
            payload["runs"] = runs
        if interval is not None:
            payload["interval"] = interval

        result = self._post(payload)

        if isinstance(result, dict) and result.get("error"):
            raise ExoSupplierAPIError(result["error"])

        return result

    def get_order_status(self, order_id: int) -> Dict[str, Any]:
        """Check single order status."""
        result = self._post({
            "action": "status",
            "order": order_id
        })

        if isinstance(result, dict) and result.get("error"):
            raise ExoSupplierAPIError(result["error"])

        return result


def _yes_no(value: Any) -> str:
    return "Yes" if bool(value) else "No"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def save_services_pdf(services: List[Dict[str, Any]], output_path: Optional[str] = None) -> str:
    """Save ExoSupplier services to a polished PDF beside this file by default."""
    if output_path is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        output_path = os.path.join(base_dir, "exosupplier_services.pdf")

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#0f172a"),
        spaceAfter=8,
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#475569"),
        spaceAfter=14,
    )
    cell_style = ParagraphStyle(
        "TableCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.2,
        leading=9,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#111827"),
    )
    header_style = ParagraphStyle(
        "TableHeader",
        parent=cell_style,
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=9,
        textColor=colors.white,
    )

    rows = [[
        Paragraph("Service ID", header_style),
        Paragraph("Name", header_style),
        Paragraph("Category", header_style),
        Paragraph("Type", header_style),
        Paragraph("Rate", header_style),
        Paragraph("Min", header_style),
        Paragraph("Max", header_style),
        Paragraph("Dripfeed", header_style),
        Paragraph("Refill", header_style),
        Paragraph("Cancel", header_style),
    ]]

    for service in services:
        rows.append([
            Paragraph(_text(service.get("service")), cell_style),
            Paragraph(_text(service.get("name")), cell_style),
            Paragraph(_text(service.get("category")), cell_style),
            Paragraph(_text(service.get("type")), cell_style),
            Paragraph(_text(service.get("rate")), cell_style),
            Paragraph(_text(service.get("min")), cell_style),
            Paragraph(_text(service.get("max")), cell_style),
            Paragraph(_yes_no(service.get("dripfeed")), cell_style),
            Paragraph(_yes_no(service.get("refill")), cell_style),
            Paragraph(_yes_no(service.get("cancel")), cell_style),
        ])

    doc = SimpleDocTemplate(
        output_path,
        pagesize=landscape(letter),
        leftMargin=0.35 * inch,
        rightMargin=0.35 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.45 * inch,
        title="ExoSupplier Services",
    )

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elements = [
        Paragraph("ExoSupplier Services", title_style),
        Paragraph(f"Generated {generated_at} | Total services: {len(services)}", subtitle_style),
        Spacer(1, 0.05 * inch),
    ]

    table = Table(
        rows,
        repeatRows=1,
        colWidths=[
            0.65 * inch,
            2.25 * inch,
            1.45 * inch,
            1.05 * inch,
            0.55 * inch,
            0.55 * inch,
            0.75 * inch,
            0.6 * inch,
            0.5 * inch,
            0.55 * inch,
        ],
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(table)

    def draw_footer(canvas, document):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(document.leftMargin, 0.22 * inch, "ExoSupplier service catalogue")
        canvas.drawRightString(landscape(letter)[0] - document.rightMargin, 0.22 * inch, f"Page {document.page}")
        canvas.restoreState()

    doc.build(elements, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return output_path


if __name__ == "__main__":
    try:
        client = ExoSupplierRuntime()
        services = client.get_services()
        pdf_path = save_services_pdf(services)

        print(f"Total services found: {len(services)}")
        print(f"PDF saved to: {pdf_path}")
        print("First 10 services:\n")

        for service in services[:10]:
            print(service)

    except ExoSupplierAPIError as e:
        print("API Error:", e)
    except Exception as e:
        print("General Error:", e)
