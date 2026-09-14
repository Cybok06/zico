# complaints.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from bson.objectid import ObjectId
from datetime import datetime
from werkzeug.utils import secure_filename
import os, time, uuid

from db import db
from activity_log import log_activity
from tenant import resolve_admin_id_for_user_id

complaints_bp = Blueprint("complaints", __name__)
orders_col = db["orders"]
complaints_col = db["complaints"]
users_col = db["users"]

# === Uploads ===
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_IMAGE_MB = 8  # hard cap per file

def _allowed_image(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS

def _filesize_ok(f) -> bool:
    # Some servers won’t populate content_length reliably for in‑memory streams; this guards typical cases.
    try:
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        return size <= MAX_IMAGE_MB * 1024 * 1024
    except Exception:
        # If we can’t measure, allow and rely on server’s MAX_CONTENT_LENGTH if set
        return True

def _save_image(file_storage, prefix: str) -> str:
    """Save image to uploads/ with a unique name; returns web path like /uploads/xxx.jpg"""
    original = secure_filename(file_storage.filename or "")
    ext = original.rsplit(".", 1)[1].lower() if "." in original else "jpg"
    unique_name = f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:10]}.{ext}"
    fullpath = os.path.join(UPLOAD_FOLDER, unique_name)
    file_storage.save(fullpath)
    return f"/uploads/{unique_name}"

def _try_objectid(s: str):
    try:
        return ObjectId(s)
    except Exception:
        return None

def _find_order_for_user(user_id: ObjectId, order_number: str):
    """
    Attempts to find an order for this user by common identifiers:
    - order_no
    - order_id
    - _id (ObjectId string)
    """
    oid = _try_objectid(order_number)
    query = {
        "user_id": ObjectId(user_id),
        "$or": [{"order_no": order_number}, {"order_id": order_number}] + ([{"_id": oid}] if oid else [])
    }
    return orders_col.find_one(query)

@complaints_bp.route("/complaints", methods=["GET", "POST"])
def submit_complaint():
    """
    Form fields (POST):
      - order_number: str (required)
      - screenshot_balance: File (required)  -> proof of data balance
      - screenshot_msisdn:  File (required)  -> proof of phone number (MSISDN)
    """
    user_id = session.get("user_id")
    if not user_id:
        flash("You must be logged in to submit a complaint.", "danger")
        return redirect(url_for("login.login"))

    if request.method == "POST":
        order_number = (request.form.get("order_number") or "").strip()
        message = (request.form.get("message") or "").strip()
        file_balance = request.files.get("screenshot_balance")
        file_msisdn = request.files.get("screenshot_msisdn")

        # --- Normalize empty files to None ---
        if file_balance and not file_balance.filename:
            file_balance = None
        if file_msisdn and not file_msisdn.filename:
            file_msisdn = None

        # --- File validation (only if provided) ---
        for field_name, f in [("data balance", file_balance), ("phone number", file_msisdn)]:
            if not f:
                continue
            if not _allowed_image(f.filename):
                flash(f"Invalid {field_name} image type. Allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))}.", "danger")
                return redirect(url_for("complaints.submit_complaint"))
            if not _filesize_ok(f):
                flash(f"The {field_name} screenshot is too large (>{MAX_IMAGE_MB}MB).", "danger")
                return redirect(url_for("complaints.submit_complaint"))

        # --- Find order for this user (optional) ---
        order = _find_order_for_user(ObjectId(user_id), order_number) if order_number else None
        item = (order.get("items") or [None])[0] if order else None
        service_name = item.get("serviceName") if item else None
        offer = item.get("value") if item else None
        created_at = order.get("created_at") if order else None

        # --- Save images ---
        balance_path = _save_image(file_balance, "balance") if file_balance else ""
        msisdn_path = _save_image(file_msisdn, "msisdn") if file_msisdn else ""

        order_admin_id = order.get("admin_id") if order else None
        complaint_admin_id = resolve_admin_id_for_user_id(users_col, user_id) or order_admin_id
        screenshots = {}
        if balance_path:
            screenshots["data_balance"] = balance_path
        if msisdn_path:
            screenshots["phone_msisdn"] = msisdn_path
        complaint_doc = {
            "user_id": ObjectId(user_id),
            "admin_id": complaint_admin_id,
            "sent_to_main_admin": False,
            "order_ref": {
                # Keep flexible keys to match whatever you store on orders
                "_id": order.get("_id") if order else None,
                "order_no": order.get("order_no") if order else None,
                "order_id": order.get("order_id") if order else None,
            } if order else {},
            "service_name": service_name or "",
            "offer": offer or "",
            "order_date": created_at,
            "order_number_provided": order_number or "",  # exactly what user typed
            "screenshots": screenshots,
            "message": message,
            "description": message,
            "submitted_at": datetime.utcnow(),
            "status": "pending",
        }

        complaints_col.insert_one(complaint_doc)
        try:
            log_activity(
                "complaint_submitted",
                actor_id=session.get("user_id"),
                actor_role=session.get("role"),
                admin_id=complaint_doc.get("admin_id"),
                target_type="complaint",
                target_id=complaint_doc.get("order_number_provided") or complaint_doc.get("order_id"),
                message="Complaint submitted",
                meta={
                    "service": complaint_doc.get("service_name"),
                    "offer": complaint_doc.get("offer"),
                },
            )
        except Exception:
            pass
        flash("✅ Complaint submitted successfully!", "success")
        return redirect(url_for("complaints.submit_complaint"))

    # GET – if you still want to pre-fill anything, you can render a minimal page.
    # (Template will now just show fields for order number + two uploads.)
    return render_template("complaints.html")

@complaints_bp.route("/view_complaints")
def view_complaints():
    user_id = session.get("user_id")
    if not user_id:
        flash("You must be logged in to view complaints.", "danger")
        return redirect(url_for("login.login"))

    status_filter = (request.args.get("status") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()

    query = {"user_id": ObjectId(user_id)}

    if status_filter:
        query["status"] = status_filter

    # Date filtering (submitted_at)
    date_cond = {}
    if start_date:
        try:
            date_cond["$gte"] = datetime.strptime(start_date, "%Y-%m-%d")
        except Exception:
            flash("Invalid start date format (use YYYY-MM-DD).", "warning")
    if end_date:
        try:
            # include the whole end day
            dt = datetime.strptime(end_date, "%Y-%m-%d")
            date_cond["$lte"] = dt
        except Exception:
            flash("Invalid end date format (use YYYY-MM-DD).", "warning")
    if date_cond:
        query["submitted_at"] = date_cond

    complaints = list(complaints_col.find(query).sort("submitted_at", -1))
    for c in complaints:
        try:
            dt = c.get("order_date")
            c["order_date_str"] = dt.strftime("%Y-%m-%d") if isinstance(dt, datetime) else ""
        except Exception:
            c["order_date_str"] = ""
    return render_template(
        "view_complaints.html",
        complaints=complaints,
        status_filter=status_filter,
        start_date=start_date,
        end_date=end_date,
    )
