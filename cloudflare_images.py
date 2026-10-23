from __future__ import annotations

import os
import traceback
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple

import requests
from werkzeug.utils import secure_filename

from db import db

images_col = db["images"]

# ===== Cloudflare Images (hardcoded as requested) =====
CF_ACCOUNT_ID = "63e6f91eec9591f77699c4b434ab44c6"
CF_IMAGES_TOKEN = "Brz0BEfl_GqEUjEghS2UEmLZhK39EUmMbZgu_hIo"
CF_HASH = "h9fmMoa1o2c2P55TcWJGOg"
DEFAULT_VARIANT = "public"  # ensure this variant exists in Cloudflare Images

DEFAULT_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def _allowed_file(filename: str, allowed_ext: Optional[Iterable[str]] = None) -> bool:
    allowed = set(allowed_ext or DEFAULT_ALLOWED_EXTENSIONS)
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed


def _safe_filesize(file_storage) -> Optional[int]:
    try:
        file_storage.stream.seek(0, os.SEEK_END)
        size = file_storage.stream.tell()
        file_storage.stream.seek(0)
        return int(size)
    except Exception:
        return None


def upload_image_to_cloudflare(
    file_storage,
    *,
    owner_id: Optional[str] = None,
    module: str = "generic",
    variant: Optional[str] = None,
    content_length: Optional[int] = None,
    allowed_ext: Optional[Iterable[str]] = None,
) -> Tuple[bool, Dict[str, Any], int]:
    """
    Uploads a file to Cloudflare Images and returns (ok, payload, http_status).
    payload contains image_url, image_id, variant on success; error on failure.
    """
    try:
        if not file_storage or not getattr(file_storage, "filename", ""):
            return False, {"error": "No selected file"}, 400

        if not _allowed_file(file_storage.filename, allowed_ext):
            return False, {"error": "File type not allowed"}, 400

        direct_url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/images/v2/direct_upload"
        headers = {"Authorization": f"Bearer {CF_IMAGES_TOKEN}"}

        res = requests.post(direct_url, headers=headers, data={}, timeout=20)
        try:
            j = res.json()
        except Exception:
            return False, {"error": "Cloudflare direct_upload returned non-JSON"}, 502

        if not j.get("success"):
            return False, {"error": "Cloudflare direct_upload failed", "details": j}, 400

        upload_url = j["result"]["uploadURL"]
        image_id = j["result"]["id"]

        up = requests.post(
            upload_url,
            files={
                "file": (
                    secure_filename(file_storage.filename),
                    file_storage.stream,
                    file_storage.mimetype or "application/octet-stream",
                )
            },
            timeout=60,
        )
        try:
            uj = up.json()
        except Exception:
            return False, {"error": "Cloudflare upload returned non-JSON"}, 502

        if not uj.get("success"):
            return False, {"error": "Cloudflare upload failed", "details": uj}, 400

        v = (variant or DEFAULT_VARIANT).strip() or DEFAULT_VARIANT
        image_url = f"https://imagedelivery.net/{CF_HASH}/{image_id}/{v}"

        size_bytes = content_length if content_length is not None else _safe_filesize(file_storage)

        images_col.insert_one(
            {
                "provider": "cloudflare_images",
                "image_id": image_id,
                "variant": v,
                "url": image_url,
                "original_filename": secure_filename(file_storage.filename),
                "mimetype": file_storage.mimetype,
                "size_bytes": size_bytes,
                "created_at": datetime.utcnow(),
                "meta": {"module": module, "owner_id": owner_id},
            }
        )

        return True, {"image_url": image_url, "image_id": image_id, "variant": v}, 200

    except Exception as e:
        traceback.print_exc()
        return False, {"error": str(e)}, 500
