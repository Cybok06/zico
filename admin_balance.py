from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from bson.objectid import ObjectId
from db import db
from datetime import datetime
import uuid
import re
from pymongo import UpdateOne, ASCENDING, DESCENDING
from sms_sender import resolve_system_sender_id, send_sms
from tenant import current_admin_id_from_session, is_admin_role

admin_balance_bp = Blueprint("admin_balance", __name__)

balances_col = db["balances"]
users_col = db["users"]
balance_logs_col = db["balance_logs"]
transactions_col = db["transactions"]
manual_topups_col = db["manual_wallet_topups"]


def _now():
    return datetime.utcnow()


def _is_ajax(req) -> bool:
    return req.headers.get("X-Requested-With", "").lower() == "xmlhttprequest"


def _admin_oid():
    return current_admin_id_from_session(session)


def _is_main_admin() -> bool:
    return (session.get("role") or "").strip().lower() == "main_admin"


def _scoped_admin_oid():
    return None if _is_main_admin() else _admin_oid()


def _normalize_phone(raw: str) -> str | None:
    if not raw:
        return None
    p = raw.strip().replace(" ", "").replace("-", "").replace("+", "")
    if p.startswith("0") and len(p) == 10:
        p = "233" + p[1:]
    if p.startswith("233") and len(p) == 12:
        return p
    return None


def _get_actor():
    actor_id = session.get("user_id")
    actor_name = session.get("username") or session.get("email") or "admin"
    if actor_id:
        try:
            u = users_col.find_one({"_id": ObjectId(actor_id)}, {"username": 1, "first_name": 1, "last_name": 1})
            if u:
                actor_name = (
                    u.get("username")
                    or f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
                    or actor_name
                )
        except Exception:
            pass
    return actor_id, actor_name


def _make_admin_wallet_reference(balance_id: ObjectId) -> str:
    ts = _now().strftime("%Y%m%d%H%M%S")
    return f"ADM-WAL-{balance_id}-{ts}-{uuid.uuid4().hex[:6].upper()}"


def _send_sms(msisdn: str, message: str, recipient_role: str | None = None, recipient_user_id=None, admin_id=None) -> str:
    sender_id = resolve_system_sender_id(admin_id=admin_id, recipient_role=recipient_role, recipient_user_id=recipient_user_id)
    return send_sms(msisdn, message, sender_id=sender_id)


def _fmt_money(x: float) -> str:
    amt = f"{float(x):.0f}" if float(x).is_integer() else f"{float(x):.2f}"
    return f"GHS{amt}"


def _to_float_safe(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        return float(v)
    except Exception:
        return float(default)


_indexes_ready = False


def _ensure_admin_balance_indexes():
    global _indexes_ready
    if _indexes_ready:
        return
    balances_col.create_index([("user_id", ASCENDING)], background=True)
    balances_col.create_index([("admin_id", ASCENDING)], background=True)
    balances_col.create_index([("updated_at", DESCENDING)], background=True)
    users_col.create_index([("admin_id", ASCENDING)], background=True)
    users_col.create_index([("phone", ASCENDING)], background=True)
    users_col.create_index([("username", ASCENDING)], background=True)
    users_col.create_index([("first_name", ASCENDING)], background=True)
    users_col.create_index([("last_name", ASCENDING)], background=True)
    _indexes_ready = True


def _wants_json(req) -> bool:
    return (req.args.get("format") or "").lower() == "json" or _is_ajax(req)


def _require_admin_session() -> bool:
    role = (session.get("role") or "").strip().lower()
    return is_admin_role(role) and bool(session.get("user_id"))


def _manual_topup_scope_query(status: str = "pending") -> dict:
    q = {"status": status}
    if _is_main_admin():
        q["source"] = {"$ne": "agent_wallet_manual"}
        return q

    admin_oid = _admin_oid()
    if not admin_oid:
        return {"_id": {"$exists": False}}
    q["admin_id"] = admin_oid
    q["source"] = "agent_wallet_manual"
    return q


def _manual_topup_action_query(topup_oid: ObjectId, status: str = "pending") -> dict:
    q = _manual_topup_scope_query(status=status)
    q["_id"] = topup_oid
    return q


def _resolve_wallet_role_filter(raw: str | None, *, is_main_admin: bool) -> str:
    # Business rule:
    # - Main admin manages admin wallets.
    # - Regular admins manage agent/customer wallets; own admin wallet is handled in "My Wallet".
    return "admin" if is_main_admin else "agent"


def _can_adjust_target_role(target_role: str | None) -> bool:
    if is_admin_role(target_role):
        return _is_main_admin()
    return True


def _balance_sort_key(item: dict):
    updated = item.get("updated_at")
    return (updated is not None, updated or datetime.min, item["user"]["_id"])


def _redirect_to_balances():
    q = (request.form.get("q") or request.args.get("q") or "").strip()
    page = request.form.get("page") or request.args.get("page")
    limit = request.form.get("limit") or request.args.get("limit")
    wallet_role = (request.form.get("wallet_role") or request.args.get("wallet_role") or "").strip().lower()
    params = {}
    if q:
        params["q"] = q
    if page:
        params["page"] = page
    if limit:
        params["limit"] = limit
    if wallet_role in {"agent", "admin"}:
        params["wallet_role"] = wallet_role
    return redirect(url_for("admin_balance.view_balances", **params))


def _ensure_owned_balance(balance_id: str):
    try:
        bal = balances_col.find_one({"_id": ObjectId(balance_id)})
    except Exception:
        return None, "Invalid balance id"
    if not bal:
        return None, "Balance not found."
    admin_oid = _scoped_admin_oid()
    if admin_oid and bal.get("admin_id") != admin_oid:
        return None, "Unauthorized balance access."
    return bal, ""


@admin_balance_bp.route("/admin/balances")
def view_balances():
    _ensure_admin_balance_indexes()

    q = (request.args.get("q") or "").strip()
    wallet_role = _resolve_wallet_role_filter(
        request.args.get("wallet_role"),
        is_main_admin=_is_main_admin(),
    )
    try:
        page = max(1, int(request.args.get("page", "1")))
    except Exception:
        page = 1
    try:
        limit = int(request.args.get("limit", "24"))
    except Exception:
        limit = 24
    limit = max(1, min(limit, 100))
    skip = (page - 1) * limit

    if not _require_admin_session():
        return redirect(url_for("login.login"))
    admin_oid = _scoped_admin_oid()

    if wallet_role == "admin":
        role_query = {"role": {"$in": ["admin", "main_admin", "super_admin", "superadmin", "professional_admin"]}}
    else:
        role_query = {"role": {"$in": ["agent", "customer"]}}

    if wallet_role == "admin" and _is_main_admin():
        # Main admin sees other admin wallets only.
        own_id = _admin_oid()
        user_query = {"$and": [role_query, {"_id": {"$ne": own_id}}]} if own_id else dict(role_query)
    elif admin_oid:
        user_query = {"admin_id": admin_oid, **role_query}
    else:
        user_query = dict(role_query)
    if q:
        safe_q = re.escape(q)
        regex = {"$regex": safe_q, "$options": "i"}
        if wallet_role == "admin" and _is_main_admin():
            own_id = _admin_oid()
            filters = [role_query, {"$or": [{"first_name": regex}, {"last_name": regex}, {"phone": regex}, {"username": regex}]}]
            if own_id:
                filters.append({"_id": {"$ne": own_id}})
            user_query = {"$and": filters}
        elif admin_oid:
            user_query = {
                "$and": [
                    {"admin_id": admin_oid},
                    role_query,
                    {"$or": [{"first_name": regex}, {"last_name": regex}, {"phone": regex}, {"username": regex}]},
                ]
            }
        else:
            user_query = {
                "$and": [
                    role_query,
                    {"$or": [{"first_name": regex}, {"last_name": regex}, {"phone": regex}, {"username": regex}]},
                ]
            }

    total_records = users_col.count_documents(user_query)
    total_pages = max(1, (total_records + limit - 1) // limit) if total_records else 0
    if total_pages and page > total_pages:
        page = total_pages
        skip = (page - 1) * limit

    users_raw = list(
        users_col.find(user_query, {"first_name": 1, "last_name": 1, "phone": 1, "username": 1, "role": 1})
        .sort([("_id", DESCENDING)])
        .skip(skip)
        .limit(limit)
    )
    user_ids = [u["_id"] for u in users_raw]
    user_ids_str = [str(uid) for uid in user_ids]
    user_id_set = set(user_ids)

    balances_map = {}
    if user_ids:
        user_balance_match = {"$or": [{"user_id": {"$in": user_ids}}, {"user_id": {"$in": user_ids_str}}]}
        bal_q = user_balance_match
        if wallet_role == "admin" and _is_main_admin():
            bal_q = {
                "$and": [
                    user_balance_match,
                    {
                        "$or": [
                            {"admin_id": {"$in": user_ids}},
                            {"admin_id": {"$in": user_ids_str}},
                            {"admin_id": {"$exists": False}},
                            {"admin_id": None},
                        ]
                    },
                ]
            }
        elif admin_oid:
            bal_q["admin_id"] = admin_oid
        for b in balances_col.find(
            bal_q,
            {"user_id": 1, "amount": 1, "currency": 1, "updated_at": 1, "created_at": 1},
        ):
            raw_uid = b.get("user_id")
            normalized_uid = None
            if isinstance(raw_uid, ObjectId):
                normalized_uid = raw_uid
            elif isinstance(raw_uid, str):
                try:
                    normalized_uid = ObjectId(raw_uid)
                except Exception:
                    normalized_uid = None
            if not normalized_uid or normalized_uid not in user_id_set:
                continue
            if isinstance(raw_uid, str):
                balances_col.update_one({"_id": b["_id"], "user_id": raw_uid}, {"$set": {"user_id": normalized_uid}})
                b["user_id"] = normalized_uid
            existing = balances_map.get(normalized_uid)
            if not existing:
                balances_map[normalized_uid] = b
                continue
            old_key = (existing.get("updated_at") or existing.get("created_at") or datetime.min, existing.get("_id"))
            new_key = (b.get("updated_at") or b.get("created_at") or datetime.min, b.get("_id"))
            if new_key > old_key:
                balances_map[normalized_uid] = b

    missing_ids = [uid for uid in user_ids if uid not in balances_map]
    if missing_ids:
        now = _now()
        missing_writes = []
        for uid in missing_ids:
            balance_admin_id = uid if wallet_role == "admin" and _is_main_admin() else admin_oid
            insert_doc = {
                "user_id": uid,
                "amount": 0.0,
                "currency": "GHS",
                "created_at": now,
            }
            update_filter = {"user_id": uid}
            if balance_admin_id:
                insert_doc["admin_id"] = balance_admin_id
                update_filter["admin_id"] = balance_admin_id
            missing_writes.append(
                UpdateOne(
                    update_filter,
                    {"$setOnInsert": insert_doc},
                    upsert=True,
                )
            )
        balances_col.bulk_write(
            missing_writes,
            ordered=False,
        )
        missing_q = {"user_id": {"$in": missing_ids}}
        if wallet_role == "admin" and _is_main_admin():
            missing_q = {
                "$and": [
                    {"user_id": {"$in": missing_ids}},
                    {
                        "$or": [
                            {"admin_id": {"$in": missing_ids}},
                            {"admin_id": {"$exists": False}},
                            {"admin_id": None},
                        ]
                    },
                ]
            }
        elif admin_oid:
            missing_q["admin_id"] = admin_oid
        for b in balances_col.find(
            missing_q,
            {"user_id": 1, "amount": 1, "currency": 1, "updated_at": 1, "created_at": 1},
        ):
            balances_map[b["user_id"]] = b

    balances = []
    for u in users_raw:
        bal = balances_map.get(u["_id"]) or {}
        target_role = (u.get("role") or "").strip().lower()
        balances.append(
            {
                "_id": str(bal.get("_id", "")),
                "user": {
                    "_id": str(u["_id"]),
                    "first_name": u.get("first_name", ""),
                    "last_name": u.get("last_name", ""),
                    "phone": u.get("phone", ""),
                    "username": u.get("username", ""),
                    "role": target_role,
                },
                "amount": _to_float_safe(bal.get("amount")),
                "currency": bal.get("currency", "GHS"),
                "updated_at": bal.get("updated_at"),
                "updated_at_str": bal.get("updated_at").strftime("%Y-%m-%d %H:%M") if bal.get("updated_at") else "",
                "can_adjust": _can_adjust_target_role(target_role),
            }
        )

    balances.sort(key=_balance_sort_key, reverse=True)

    pending_manual_topups = []
    try:
        pending_query = _manual_topup_scope_query(status="pending")
        pending = list(
            manual_topups_col.find(pending_query)
            .sort([("created_at", DESCENDING)])
            .limit(200)
        )
        if pending:
            owner_ids = []
            for t in pending:
                uid = t.get("wallet_owner_user_id") or t.get("user_id")
                if isinstance(uid, str):
                    try:
                        uid = ObjectId(uid)
                    except Exception:
                        uid = None
                if isinstance(uid, ObjectId):
                    owner_ids.append(uid)
                t["_owner_id"] = uid
                t["_id_str"] = str(t.get("_id"))
                dt = t.get("created_at")
                t["created_at_str"] = dt.strftime("%Y-%m-%d %H:%M") if isinstance(dt, datetime) else ""
                t["amount_display"] = f"{_to_float_safe(t.get('amount')):.2f}"
            owner_map = {}
            if owner_ids:
                owner_map = {
                    u["_id"]: u
                    for u in users_col.find(
                        {"_id": {"$in": owner_ids}},
                        {"first_name": 1, "last_name": 1, "username": 1, "phone": 1, "email": 1},
                    )
                }
            for t in pending:
                u = owner_map.get(t.get("_owner_id")) or {}
                t["user"] = {
                    "first_name": u.get("first_name", ""),
                    "last_name": u.get("last_name", ""),
                    "username": u.get("username", ""),
                    "phone": u.get("phone", ""),
                    "email": u.get("email", ""),
                }
            pending_manual_topups = pending
    except Exception:
        pending_manual_topups = []
    pending_by_owner = {}
    for t in pending_manual_topups:
        owner_key = str(t.get("_owner_id") or "")
        if not owner_key:
            continue
        pending_by_owner.setdefault(owner_key, []).append(t)
    for b in balances:
        owner_pending = pending_by_owner.get(b["user"]["_id"], [])
        b["pending_manual_topups"] = owner_pending
        b["pending_manual_topups_count"] = len(owner_pending)
    start_record = skip + 1 if total_records and balances else 0
    end_record = skip + len(balances) if total_records and balances else 0
    payload = {
        "success": True,
        "balances": balances,
        "pagination": {
            "total_records": total_records,
            "total_users": total_records,
            "total_pages": total_pages,
            "current_page": page,
            "limit": limit,
            "has_next": total_pages > 0 and page < total_pages,
            "has_prev": total_pages > 0 and page > 1,
            "start_record": start_record,
            "end_record": end_record,
        },
        "q": q,
        "wallet_role": wallet_role,
    }
    if _wants_json(request):
        return jsonify(payload)

    return render_template(
        "admin_balance.html",
        balances=balances,
        q=q,
        wallet_role=wallet_role,
        is_main_admin=_is_main_admin(),
        pending_manual_topups=pending_manual_topups,
        page=page,
        limit=limit,
        total_records=total_records,
        total_pages=total_pages,
        has_next=payload["pagination"]["has_next"],
        has_prev=payload["pagination"]["has_prev"],
        start_record=start_record,
        end_record=end_record,
    )


@admin_balance_bp.route("/admin/balances/deposit/<balance_id>", methods=["POST"])
def deposit_balance(balance_id):
    if not _require_admin_session():
        if _is_ajax(request):
            return jsonify(success=False, message="Unauthorized"), 403
        return redirect(url_for("login.login"))

    delta = request.form.get("amount")
    note = (request.form.get("note") or "").strip()
    if not delta:
        msg = "Enter an amount to deposit."
        if _is_ajax(request):
            return jsonify(success=False, message=msg), 400
        flash(msg, "warning")
        return _redirect_to_balances()

    try:
        delta_f = float(delta)
        if delta_f <= 0:
            msg = "Deposit amount must be greater than zero."
            if _is_ajax(request):
                return jsonify(success=False, message=msg), 400
            flash(msg, "warning")
            return _redirect_to_balances()

        bal, err = _ensure_owned_balance(balance_id)
        if not bal:
            if _is_ajax(request):
                return jsonify(success=False, message=err), 404
            flash(err, "danger")
            return _redirect_to_balances()
        target = users_col.find_one({"_id": bal["user_id"]}, {"role": 1}) or {}
        target_role = (target.get("role") or "").strip().lower()
        if not _can_adjust_target_role(target_role):
            msg = "Not authorized to adjust admin wallets."
            if _is_ajax(request):
                return jsonify(success=False, message=msg), 403
            flash(msg, "warning")
            return _redirect_to_balances()

        old_amount = _to_float_safe(bal.get("amount"))
        new_amount = old_amount + delta_f
        currency = bal.get("currency", "GHS")
        balances_col.update_one({"_id": bal["_id"]}, {"$set": {"amount": new_amount, "updated_at": _now()}})

        actor_id, actor_name = _get_actor()
        admin_oid = _admin_oid()
        log_res = balance_logs_col.insert_one(
            {
                "balance_id": bal["_id"],
                "user_id": bal["user_id"],
                "admin_id": admin_oid,
                "action": "deposit",
                "delta": float(delta_f),
                "amount_before": float(old_amount),
                "amount_after": float(new_amount),
                "currency": currency,
                "note": note[:240],
                "actor_id": ObjectId(actor_id) if actor_id else None,
                "actor_name": actor_name,
                "created_at": _now(),
            }
        )

        gateway = (request.form.get("gateway") or "admin_wallet").strip().lower()
        if log_res and not transactions_col.find_one({"balance_log_id": log_res.inserted_id}):
            transactions_col.insert_one(
                {
                    "reference": _make_admin_wallet_reference(bal["_id"]),
                    "user_id": bal["user_id"],
                    "admin_id": admin_oid,
                    "amount": float(delta_f),
                    "currency": currency,
                    "type": "deposit",
                    "status": "success",
                    "source": "admin_wallet",
                    "gateway": gateway or "admin_wallet",
                    "created_at": _now(),
                    "verified_at": _now(),
                    "actor_id": ObjectId(actor_id) if actor_id else None,
                    "actor_name": actor_name,
                    "note": note[:240],
                    "amount_before": float(old_amount),
                    "amount_after": float(new_amount),
                    "target_user_role": target_role,
                    "balance_log_id": log_res.inserted_id,
                    "meta": {
                        "adjustment_action": "deposit",
                        "amount_before": float(old_amount),
                        "amount_after": float(new_amount),
                        "actor_name": actor_name,
                        "target_user_role": target_role,
                    },
                }
            )

        user = users_col.find_one({"_id": bal["user_id"]}, {"phone": 1, "role": 1})
        sms_status = None
        if user:
            msisdn = _normalize_phone(user.get("phone", ""))
            if msisdn:
                message = f"Your account has been credited with {_fmt_money(delta_f)}, balance: {_fmt_money(new_amount)}"
                sms_status = _send_sms(
                    msisdn,
                    message,
                    recipient_role=user.get("role"),
                    recipient_user_id=bal["user_id"],
                    admin_id=admin_oid,
                )
            else:
                sms_status = "invalid_phone"

        ok_msg = "Deposit successful."
        if sms_status == "sent":
            ok_msg += " SMS sent."
        elif sms_status in ("failed", "error"):
            ok_msg += " (SMS delivery failed)"
        elif sms_status == "invalid_phone":
            ok_msg += " (Phone not valid for SMS)"

        if _is_ajax(request):
            return jsonify(success=True, message=ok_msg, new_balance=new_amount)
        flash(ok_msg, "success")
    except Exception:
        if _is_ajax(request):
            return jsonify(success=False, message="Error processing deposit."), 500
        flash("Error processing deposit.", "danger")
    return _redirect_to_balances()


@admin_balance_bp.route("/admin/wallet/manual-topups/<topup_id>/approve", methods=["POST"])
def approve_manual_topup(topup_id):
    if not _require_admin_session():
        flash("Unauthorized.", "danger")
        return _redirect_to_balances()
    try:
        oid = ObjectId(topup_id)
    except Exception:
        flash("Invalid top up id.", "danger")
        return _redirect_to_balances()

    query = _manual_topup_action_query(oid, status="pending")
    doc = manual_topups_col.find_one(query)
    if not doc:
        flash("Top up not found or already handled.", "warning")
        return _redirect_to_balances()

    now = _now()
    res = manual_topups_col.update_one(
        query,
        {"$set": {"status": "approving", "approved_at": now, "approved_by": {"user_id": session.get("user_id"), "name": _get_actor()[1]}}},
    )
    if not res.modified_count:
        flash("Top up is already being processed.", "warning")
        return _redirect_to_balances()

    amount = _to_float_safe(doc.get("amount"))
    if amount <= 0:
        manual_topups_col.update_one({"_id": oid}, {"$set": {"status": "error", "error": "Invalid amount"}})
        flash("Invalid top up amount.", "danger")
        return _redirect_to_balances()

    uid = doc.get("wallet_owner_user_id") or doc.get("user_id")
    if isinstance(uid, str):
        try:
            uid = ObjectId(uid)
        except Exception:
            uid = None
    if not isinstance(uid, ObjectId):
        manual_topups_col.update_one({"_id": oid}, {"$set": {"status": "error", "error": "Invalid wallet owner"}})
        flash("Invalid wallet owner.", "danger")
        return _redirect_to_balances()

    admin_id = doc.get("admin_id") or uid
    if isinstance(admin_id, str):
        try:
            admin_id = ObjectId(admin_id)
        except Exception:
            admin_id = uid

    try:
        current_bal_doc = balances_col.find_one({"user_id": uid}, {"amount": 1})
        old_amount = _to_float_safe((current_bal_doc or {}).get("amount"))
        balances_col.update_one(
            {"user_id": uid},
            {
                "$inc": {"amount": amount},
                "$set": {"updated_at": now, "admin_id": admin_id},
                "$setOnInsert": {"created_at": now, "currency": "GHS"},
            },
            upsert=True,
        )
        new_amount = _to_float_safe(old_amount + amount)
        actor_id, actor_name = _get_actor()
        log_res = balance_logs_col.insert_one(
            {
                "user_id": uid,
                "admin_id": admin_id,
                "action": "deposit",
                "delta": float(amount),
                "amount_before": float(old_amount),
                "amount_after": float(new_amount),
                "currency": "GHS",
                "note": f"Manual top up approved ({doc.get('reference')})",
                "actor_id": ObjectId(actor_id) if actor_id else None,
                "actor_name": actor_name,
                "created_at": now,
            }
        )
        target_user = users_col.find_one({"_id": uid}, {"phone": 1, "role": 1}) or {}
        target_role = (target_user.get("role") or "").strip().lower()
        transactions_col.insert_one(
            {
                "user_id": uid,
                "admin_id": admin_id,
                "amount": amount,
                "reference": doc.get("reference") or "",
                "status": "success",
                "type": "deposit",
                "source": "manual_topup",
                "gateway": "Manual",
                "currency": "GHS",
                "created_at": now,
                "verified_at": now,
                "actor_id": ObjectId(actor_id) if actor_id else None,
                "actor_name": actor_name,
                "note": f"Manual top up approved ({doc.get('reference')})",
                "amount_before": float(old_amount),
                "amount_after": float(new_amount),
                "target_user_role": target_role,
                "balance_log_id": log_res.inserted_id,
                "meta": {
                    "phone": doc.get("phone"),
                    "topup_id": oid,
                    "adjustment_action": "deposit",
                    "amount_before": float(old_amount),
                    "amount_after": float(new_amount),
                    "actor_name": actor_name,
                    "target_user_role": target_role,
                },
            }
        )
        manual_topups_col.update_one(
            {"_id": oid},
            {"$set": {"status": "approved", "credited_amount": amount, "balance_log_id": log_res.inserted_id}},
        )
        user = target_user
        sms_status = None
        msisdn = _normalize_phone(user.get("phone", ""))
        if msisdn:
            message = f"Your manual deposit of {_fmt_money(amount)} has been approved, balance: {_fmt_money(new_amount)}"
            sms_status = _send_sms(
                msisdn,
                message,
                recipient_role=user.get("role"),
                recipient_user_id=uid,
                admin_id=admin_id,
            )
        elif user:
            sms_status = "invalid_phone"
        flash("Manual top up approved and wallet credited.", "success")
        if sms_status == "sent":
            flash("Approval SMS sent.", "success")
        elif sms_status in ("failed", "error"):
            flash("Wallet credited, but SMS delivery failed.", "warning")
    except Exception as e:
        manual_topups_col.update_one({"_id": oid}, {"$set": {"status": "error", "error": str(e)}})
        flash("Failed to approve manual top up.", "danger")

    return _redirect_to_balances()


@admin_balance_bp.route("/admin/wallet/manual-topups/<topup_id>/reject", methods=["POST"])
def reject_manual_topup(topup_id):
    if not _require_admin_session():
        flash("Unauthorized.", "danger")
        return _redirect_to_balances()
    try:
        oid = ObjectId(topup_id)
    except Exception:
        flash("Invalid top up id.", "danger")
        return _redirect_to_balances()

    query = _manual_topup_action_query(oid, status="pending")
    res = manual_topups_col.update_one(
        query,
        {"$set": {"status": "rejected", "rejected_at": _now(), "rejected_by": {"user_id": session.get("user_id"), "name": _get_actor()[1]}}},
    )
    if res.modified_count:
        flash("Manual top up rejected.", "info")
    else:
        flash("Top up not found or already handled.", "warning")
    return _redirect_to_balances()


@admin_balance_bp.route("/admin/balances/withdraw/<balance_id>", methods=["POST"])
def withdraw_balance(balance_id):
    if not _require_admin_session():
        if _is_ajax(request):
            return jsonify(success=False, message="Unauthorized"), 403
        return redirect(url_for("login.login"))

    delta = request.form.get("amount")
    note = (request.form.get("note") or "").strip()
    if not delta:
        msg = "Enter an amount to withdraw."
        if _is_ajax(request):
            return jsonify(success=False, message=msg), 400
        flash(msg, "warning")
        return _redirect_to_balances()

    try:
        delta_f = float(delta)
        if delta_f <= 0:
            msg = "Withdrawal amount must be greater than zero."
            if _is_ajax(request):
                return jsonify(success=False, message=msg), 400
            flash(msg, "warning")
            return _redirect_to_balances()

        bal, err = _ensure_owned_balance(balance_id)
        if not bal:
            if _is_ajax(request):
                return jsonify(success=False, message=err), 404
            flash(err, "danger")
            return _redirect_to_balances()
        target = users_col.find_one({"_id": bal["user_id"]}, {"role": 1}) or {}
        target_role = (target.get("role") or "").strip().lower()
        if not _can_adjust_target_role(target_role):
            msg = "Not authorized to adjust admin wallets."
            if _is_ajax(request):
                return jsonify(success=False, message=msg), 403
            flash(msg, "warning")
            return _redirect_to_balances()

        old_amount = _to_float_safe(bal.get("amount"))
        new_amount = old_amount - delta_f
        if new_amount < 0:
            msg = "Insufficient funds: cannot withdraw more than the current balance."
            if _is_ajax(request):
                return jsonify(success=False, message=msg), 400
            flash(msg, "danger")
            return _redirect_to_balances()

        currency = bal.get("currency", "GHS")
        balances_col.update_one({"_id": bal["_id"]}, {"$set": {"amount": new_amount, "updated_at": _now()}})

        actor_id, actor_name = _get_actor()
        admin_oid = _admin_oid()
        log_res = balance_logs_col.insert_one(
            {
                "balance_id": bal["_id"],
                "user_id": bal["user_id"],
                "admin_id": admin_oid,
                "action": "withdraw",
                "delta": float(-delta_f),
                "amount_before": float(old_amount),
                "amount_after": float(new_amount),
                "currency": currency,
                "note": note[:240],
                "actor_id": ObjectId(actor_id) if actor_id else None,
                "actor_name": actor_name,
                "created_at": _now(),
            }
        )

        gateway = (request.form.get("gateway") or "admin_wallet").strip().lower()
        if log_res and not transactions_col.find_one({"balance_log_id": log_res.inserted_id}):
            transactions_col.insert_one(
                {
                    "reference": _make_admin_wallet_reference(bal["_id"]),
                    "user_id": bal["user_id"],
                    "admin_id": admin_oid,
                    "amount": float(delta_f),
                    "currency": currency,
                    "type": "withdraw",
                    "status": "success",
                    "source": "admin_wallet",
                    "gateway": gateway or "admin_wallet",
                    "created_at": _now(),
                    "verified_at": _now(),
                    "actor_id": ObjectId(actor_id) if actor_id else None,
                    "actor_name": actor_name,
                    "note": note[:240],
                    "amount_before": float(old_amount),
                    "amount_after": float(new_amount),
                    "target_user_role": target_role,
                    "balance_log_id": log_res.inserted_id,
                    "meta": {
                        "adjustment_action": "deduction",
                        "amount_before": float(old_amount),
                        "amount_after": float(new_amount),
                        "actor_name": actor_name,
                        "target_user_role": target_role,
                    },
                }
            )

        user = users_col.find_one({"_id": bal["user_id"]}, {"phone": 1, "role": 1})
        sms_status = None
        if user:
            msisdn = _normalize_phone(user.get("phone", ""))
            if msisdn:
                message = f"Your account has been debited with {_fmt_money(delta_f)}, balance: {_fmt_money(new_amount)}"
                sms_status = _send_sms(
                    msisdn,
                    message,
                    recipient_role=user.get("role"),
                    recipient_user_id=bal["user_id"],
                    admin_id=admin_oid,
                )
            else:
                sms_status = "invalid_phone"

        ok_msg = "Withdrawal successful."
        if sms_status == "sent":
            ok_msg += " SMS sent."
        elif sms_status in ("failed", "error"):
            ok_msg += " (SMS delivery failed)"
        elif sms_status == "invalid_phone":
            ok_msg += " (Phone not valid for SMS)"

        if _is_ajax(request):
            return jsonify(success=True, message=ok_msg, new_balance=new_amount)
        flash(ok_msg, "success")
    except Exception:
        if _is_ajax(request):
            return jsonify(success=False, message="Error processing withdrawal."), 500
        flash("Error processing withdrawal.", "danger")
    return _redirect_to_balances()


@admin_balance_bp.route("/admin/balances/history/<user_id>")
def balance_history(user_id):
    try:
        uid = ObjectId(user_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid user id"}), 400

    if not _require_admin_session():
        return jsonify({"success": False, "error": "Unauthorized"}), 403
    admin_oid = _scoped_admin_oid()

    try:
        logs = []
        history_query = {"user_id": uid, **({"admin_id": admin_oid} if admin_oid else {})}
        if not _is_main_admin():
            history_query["action"] = {"$ne": "purchase_debit"}
        cursor = (
            balance_logs_col.find(
                history_query,
                {
                    "action": 1,
                    "delta": 1,
                    "amount_before": 1,
                    "amount_after": 1,
                    "currency": 1,
                    "note": 1,
                    "actor_name": 1,
                    "created_at": 1,
                },
            )
            .sort("created_at", -1)
            .limit(200)
        )
        for lg in cursor:
            logs.append(
                {
                    "id": str(lg["_id"]),
                    "action": lg.get("action"),
                    "delta": _to_float_safe(lg.get("delta")),
                    "amount_before": _to_float_safe(lg.get("amount_before")),
                    "amount_after": _to_float_safe(lg.get("amount_after")),
                    "currency": lg.get("currency", "GHS"),
                    "note": lg.get("note", ""),
                    "actor_name": lg.get("actor_name", "admin"),
                    "created_at": lg.get("created_at").strftime("%Y-%m-%d %H:%M") if lg.get("created_at") else "",
                }
            )
        return jsonify({"success": True, "logs": logs})
    except Exception:
        return jsonify({"success": False, "error": "Server error loading history"}), 500
