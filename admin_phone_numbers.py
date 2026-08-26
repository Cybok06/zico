from datetime import datetime
from io import BytesIO
import re

import pandas as pd
from flask import Blueprint, redirect, render_template, request, send_file, session, url_for
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from db import db
from tenant import current_admin_id_from_session

admin_phone_numbers_bp = Blueprint("admin_phone_numbers", __name__)

orders_col = db["orders"]
blocked_phone_numbers_col = db["blocked_phone_numbers"]

PHONE_TEXT_RE = r"^\s*(?:\+?233|0)?[0-9\s().-]{8,16}\s*$"


def _require_admin():
    return session.get("role") in {"admin", "main_admin"}


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", str(phone or ""))
    if not digits:
        return ""
    if digits.startswith("233") and len(digits) == 12:
        return f"0{digits[3:]}"
    if len(digits) == 9:
        return f"0{digits}"
    return digits


def _is_valid_phone_display(value: str) -> bool:
    normalized = _normalize_phone(value)
    return bool(re.fullmatch(r"0\d{9}", normalized))


def _pagination_params():
    page_raw = request.args.get("page", 1)
    try:
        page = max(int(page_raw), 1)
    except Exception:
        page = 1
    per_page = 50
    return page, per_page


def _is_main_admin() -> bool:
    return (session.get("role") or "").strip().lower() == "main_admin"


def _order_scope_match() -> dict | None:
    if _is_main_admin():
        return None
    admin_oid = current_admin_id_from_session(session)
    if not admin_oid:
        return {"_id": {"$exists": False}}
    return {"admin_id": {"$in": [admin_oid, str(admin_oid)]}}


def _phone_match(q: str) -> dict:
    phone_shape_match = {"items.phone": {"$type": "string", "$regex": PHONE_TEXT_RE}}
    if q:
        return {
            "$and": [
                phone_shape_match,
                {"items.phone": {"$regex": re.escape(q), "$options": "i"}},
            ]
        }
    return phone_shape_match


def _scoped_phone_keys(q: str = "") -> list[str]:
    pipeline = []
    scope_match = _order_scope_match()
    if scope_match:
        pipeline.append({"$match": scope_match})
    pipeline.extend([
        {"$unwind": "$items"},
        {"$match": _phone_match(q)},
        {"$group": {"_id": "$items.phone"}},
    ])
    keys = set()
    for row in orders_col.aggregate(pipeline):
        phone = row.get("_id")
        if _is_valid_phone_display(phone):
            keys.add(_normalize_phone(phone))
    return sorted(keys)


def _fetch_phone_rows(q: str, skip: int | None = None, limit: int | None = None):
    base_match = _phone_match(q)

    total_pipeline = []
    scope_match = _order_scope_match()
    if scope_match:
        total_pipeline.append({"$match": scope_match})
    total_pipeline.extend([
        {"$unwind": "$items"},
        {"$match": base_match},
        {"$group": {"_id": "$items.phone"}},
        {"$count": "total"},
    ])
    total_agg = list(orders_col.aggregate(total_pipeline))
    total = int(total_agg[0]["total"]) if total_agg else 0

    # Count-only mode: avoid building a data pipeline with invalid/zero limits.
    if limit is not None and int(limit) <= 0:
        return total, []

    pipeline = []
    if scope_match:
        pipeline.append({"$match": scope_match})
    pipeline.extend([
        {"$unwind": "$items"},
        {"$match": base_match},
        {
            "$group": {
                "_id": "$items.phone",
                "order_ids": {"$addToSet": "$order_id"},
                "last_order_at": {"$max": "$created_at"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "phone": "$_id",
                "orders_count": {"$size": "$order_ids"},
                "last_order_at": 1,
            }
        },
        {"$sort": {"orders_count": 1, "phone": 1, "last_order_at": -1}},
    ])
    if skip is not None:
        pipeline.append({"$skip": skip})
    if limit is not None and int(limit) > 0:
        pipeline.append({"$limit": limit})

    rows = [r for r in orders_col.aggregate(pipeline) if _is_valid_phone_display(r.get("phone"))]

    row_keys = [_normalize_phone(r.get("phone")) for r in rows if r.get("phone")]
    active_blocks = list(
        blocked_phone_numbers_col.find(
            {"is_active": True, "normalized_phone": {"$in": row_keys}},
            {"normalized_phone": 1, "reason": 1, "_id": 0},
        )
    )
    blocked_map = {d.get("normalized_phone"): d for d in active_blocks if d.get("normalized_phone")}

    for row in rows:
        key = _normalize_phone(row.get("phone"))
        row["normalized_phone"] = key
        row["is_blocked"] = key in blocked_map
        row["block_reason"] = (blocked_map.get(key) or {}).get("reason", "")

    return total, rows


def _fetch_blocked_rows(q: str, phone_keys: list[str] | None = None):
    query = {"is_active": True}
    if phone_keys is not None:
        if not phone_keys:
            return []
        query["normalized_phone"] = {"$in": phone_keys}
    if q:
        query["$or"] = [
            {"phone": {"$regex": re.escape(q), "$options": "i"}},
            {"normalized_phone": {"$regex": re.escape(_normalize_phone(q)), "$options": "i"}},
            {"reason": {"$regex": re.escape(q), "$options": "i"}},
        ]

    blocked_rows = list(
        blocked_phone_numbers_col.find(
            query,
            {
                "phone": 1,
                "normalized_phone": 1,
                "reason": 1,
                "created_at": 1,
                "updated_at": 1,
            },
        ).sort("updated_at", -1)
    )

    for row in blocked_rows:
        key = row.get("normalized_phone") or _normalize_phone(row.get("phone"))
        row["phone_display"] = row.get("phone") or key
        row["normalized_phone"] = key

    return blocked_rows


@admin_phone_numbers_bp.route("/admin/phone-numbers")
def phone_numbers_page():
    if not _require_admin():
        return redirect(url_for("login.login"))

    q = (request.args.get("q") or "").strip()
    page, per_page = _pagination_params()

    total, _ = _fetch_phone_rows(q=q, skip=0, limit=0)
    total_pages = max((total + per_page - 1) // per_page, 1)
    if page > total_pages:
        page = total_pages
    skip = (page - 1) * per_page

    _, rows = _fetch_phone_rows(q=q, skip=skip, limit=per_page)
    scoped_phone_keys = None if _is_main_admin() else _scoped_phone_keys()
    blocked_rows = _fetch_blocked_rows(q=q, phone_keys=scoped_phone_keys)

    blocked_count_query = {"is_active": True}
    if scoped_phone_keys is not None:
        blocked_count_query["normalized_phone"] = {"$in": scoped_phone_keys}
    total_blocked = blocked_phone_numbers_col.count_documents(blocked_count_query)

    return render_template(
        "admin_phone_numbers.html",
        rows=rows,
        q=q,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        total_blocked=total_blocked,
        blocked_rows=blocked_rows,
    )


@admin_phone_numbers_bp.route("/admin/phone-numbers/export/excel")
def export_phone_numbers_excel():
    if not _require_admin():
        return redirect(url_for("login.login"))

    q = (request.args.get("q") or "").strip()
    _, rows = _fetch_phone_rows(q=q)
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    data = []
    for row in rows:
        data.append(
            {
                "Phone Number": row.get("phone", ""),
                "Orders Placed": int(row.get("orders_count") or 0),
                "Status": "Blocked" if row.get("is_blocked") else "Active",
                "Block Reason": row.get("block_reason") or "",
                "Last Order At": (
                    row.get("last_order_at").strftime("%Y-%m-%d %H:%M")
                    if row.get("last_order_at")
                    else ""
                ),
                "Generated At": generated_at,
            }
        )

    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Phone Numbers")
    output.seek(0)

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return send_file(
        output,
        as_attachment=True,
        download_name=f"phone_numbers_{stamp}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@admin_phone_numbers_bp.route("/admin/phone-numbers/export/pdf")
def export_phone_numbers_pdf():
    if not _require_admin():
        return redirect(url_for("login.login"))

    q = (request.args.get("q") or "").strip()
    total, rows = _fetch_phone_rows(q=q)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=24,
        leftMargin=24,
        topMargin=24,
        bottomMargin=24,
    )

    styles = getSampleStyleSheet()
    title = Paragraph("Phone Numbers Report", styles["Title"])
    subtitle = Paragraph(
        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')} | Total: {total}",
        styles["Normal"],
    )

    table_data = [["#", "Phone Number", "Orders", "Status", "Reason", "Last Order"]]
    for idx, row in enumerate(rows, start=1):
        table_data.append(
            [
                str(idx),
                str(row.get("phone") or ""),
                str(int(row.get("orders_count") or 0)),
                "Blocked" if row.get("is_blocked") else "Active",
                str(row.get("block_reason") or ""),
                row.get("last_order_at").strftime("%Y-%m-%d %H:%M")
                if row.get("last_order_at")
                else "-",
            ]
        )

    tbl = Table(table_data, repeatRows=1, colWidths=[36, 140, 70, 80, 260, 110])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (2, 0), (3, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )

    doc.build([title, Spacer(1, 8), subtitle, Spacer(1, 12), tbl])
    buffer.seek(0)

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"phone_numbers_{stamp}.pdf",
        mimetype="application/pdf",
    )


@admin_phone_numbers_bp.route("/admin/phone-numbers/block", methods=["POST"])
def block_phone_number():
    if not _require_admin():
        return redirect(url_for("login.login"))

    phone = (request.form.get("phone") or "").strip()
    q = (request.form.get("q") or "").strip()
    page = (request.form.get("page") or "1").strip()
    reason = (request.form.get("reason") or "").strip()

    key = _normalize_phone(phone)
    if key:
        now = datetime.utcnow()
        blocked_phone_numbers_col.update_one(
            {"normalized_phone": key},
            {
                "$set": {
                    "phone": phone,
                    "normalized_phone": key,
                    "reason": reason,
                    "is_active": True,
                    "updated_at": now,
                    "blocked_by": session.get("admin_id") or session.get("user_id"),
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    return redirect(url_for("admin_phone_numbers.phone_numbers_page", q=q, page=page))


@admin_phone_numbers_bp.route("/admin/phone-numbers/unblock", methods=["POST"])
def unblock_phone_number():
    if not _require_admin():
        return redirect(url_for("login.login"))

    phone = (request.form.get("phone") or "").strip()
    q = (request.form.get("q") or "").strip()
    page = (request.form.get("page") or "1").strip()

    key = _normalize_phone(phone)
    if key:
        blocked_phone_numbers_col.update_one(
            {"normalized_phone": key, "is_active": True},
            {
                "$set": {
                    "is_active": False,
                    "updated_at": datetime.utcnow(),
                    "unblocked_by": session.get("admin_id") or session.get("user_id"),
                }
            },
        )

    return redirect(url_for("admin_phone_numbers.phone_numbers_page", q=q, page=page))
