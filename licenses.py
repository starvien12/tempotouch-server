"""Offline TempoTouch license verification and issuance (ECDSA P-256 / SHA-256)."""

import base64
import calendar
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


AUDIENCE = "vn.moccham.app"
TERMS = ("day", "week", "month", "year")
ZONE = ZoneInfo("Asia/Ho_Chi_Minh")
MAX_TOKEN_LENGTH = 2048
CLAIMS = {"v", "id", "iat", "exp", "term", "aud"}
_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}", re.ASCII)
_B64 = re.compile(r"[a-zA-Z0-9_-]+", re.ASCII)


def _require_p256_public(key):
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("Khóa công khai phải sử dụng ECDSA P-256.")
    return key


def load_public_key(path):
    """Load a DER-encoded SubjectPublicKeyInfo public key."""
    der = Path(path).read_bytes()
    if len(der) > 1024:
        raise ValueError("Khóa công khai không hợp lệ.")
    key = _require_p256_public(serialization.load_der_public_key(der))
    if key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != der:
        raise ValueError("Khóa công khai phải dùng định dạng DER SubjectPublicKeyInfo.")
    return key


def _encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(segment):
    if not _B64.fullmatch(segment):
        raise ValueError("Mã bản quyền sai định dạng.")
    data = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    if _encode(data) != segment:
        raise ValueError("Mã bản quyền sai định dạng.")
    return data


def _unique_fields(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError("Mã bản quyền có trường trùng lặp.")
        result[k] = v
    return result


def device_public(code):
    if not isinstance(code, str) or len(code) > 180 or not code.startswith("TD1."):
        raise ValueError("Mã thiết bị không hợp lệ.")
    der = _decode(code[4:])
    key = _require_p256_public(serialization.load_der_public_key(der))
    if der != key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
        raise ValueError("Mã thiết bị không hợp lệ.")
    return key


def verify_device_proof(device, protocol, token, path, timestamp, signature, body, now):
    if not re.fullmatch(r"[0-9]{1,12}", timestamp) or abs(now - int(timestamp)) > 300:
        raise ValueError("Thời điểm xác thực thiết bị không hợp lệ.")
    if not signature or len(signature) > 100:
        raise ValueError("Thiếu chữ ký thiết bị.")
    if protocol not in ("TT1", "TT2"):
        raise ValueError("Phiên bản xác thực thiết bị không hợp lệ.")
    proof = "\n".join((protocol, token, "POST", path, timestamp, _encode(hashlib.sha256(body).digest())))
    try:
        device_public(device).verify(_decode(signature), proof.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise ValueError("Chữ ký thiết bị không hợp lệ.") from exc


def verify_device_request(claims, token, path, timestamp, signature, body, now):
    verify_device_proof(claims["device"], "TT2", token, path, timestamp, signature, body, now)


def verify_token(token, public_key, now):
    """Return validated claims for a token valid at UTC epoch second now."""
    _require_p256_public(public_key)
    if type(now) is not int or now < 0:
        raise ValueError("Thời điểm kiểm tra không hợp lệ.")
    if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH:
        raise ValueError("Mã bản quyền sai định dạng.")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] not in ("TT1", "TT2"):
        raise ValueError("Phiên bản mã bản quyền không hợp lệ.")
    payload, signature = _decode(parts[1]), _decode(parts[2])
    try:
        public_key.verify(signature, (parts[0] + "." + parts[1]).encode("ascii"), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise ValueError("Chữ ký mã bản quyền không hợp lệ.") from exc

    try:
        claims = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_fields)
    except (ValueError, RecursionError) as exc:
        raise ValueError("Nội dung mã bản quyền không hợp lệ.") from exc
    bound = parts[0] == "TT2"
    if not isinstance(claims, dict) or set(claims) != (CLAIMS | {"device"} if bound else CLAIMS):
        raise ValueError("Thiếu hoặc thừa trường trong mã bản quyền.")
    if type(claims["v"]) is not int or claims["v"] != (2 if bound else 1) or claims["aud"] != AUDIENCE:
        raise ValueError("Mã bản quyền không dành cho phiên bản ứng dụng này.")
    if not isinstance(claims["id"], str) or not _ID.fullmatch(claims["id"]) or claims["term"] not in TERMS:
        raise ValueError("Mã định danh hoặc kỳ hạn không hợp lệ.")
    issued, expires = claims["iat"], claims["exp"]
    if type(issued) is not int or type(expires) is not int or not (0 <= issued < expires <= 253402300799):
        raise ValueError("Khoảng hiệu lực không hợp lệ.")
    if now < issued:
        raise ValueError("Mã bản quyền chưa có hiệu lực.")
    if now >= expires:
        raise ValueError("Mã bản quyền đã hết hạn.")
    try:
        expected = _expires(issued, claims["term"])
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("Thời điểm cấp không hợp lệ.") from exc
    if expires != expected:
        raise ValueError("Ngày hết hạn không phù hợp với kỳ hạn đã cấp.")
    if bound:
        device_public(claims["device"])
    return claims


def _expires(issued, term):
    if term not in TERMS:
        raise ValueError("Kỳ hạn không hợp lệ.")
    start = datetime.fromtimestamp(issued, ZONE)
    if term in ("week", "day"):
        end = start + timedelta(days=7 if term == "week" else 1)
    else:
        months = 12 if term == "year" else 1
        year, month_index = divmod(start.year * 12 + start.month - 1 + months, 12)
        month = month_index + 1
        day = min(start.day, calendar.monthrange(year, month)[1])
        end = start.replace(year=year, month=month, day=day)
    return int(end.timestamp())


def issue_token(private_key, term, issued, *, device=None):
    """Issue a random, independently identifiable license at UTC epoch second issued."""
    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(private_key.curve, ec.SECP256R1):
        raise ValueError("Khóa ký phải sử dụng ECDSA P-256.")
    if type(issued) is not int or issued < 0:
        raise ValueError("Thời điểm cấp không hợp lệ.")
    claims = {"v": 1, "id": str(uuid.uuid4()), "iat": issued, "exp": _expires(issued, term),
              "term": term, "aud": AUDIENCE}
    if device is not None:
        device_public(device)
        claims.update(v=2, device=device)
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
    body = ("TT2." if device is not None else "TT1.") + _encode(payload)
    signature = private_key.sign(body.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    return body + "." + _encode(signature)
