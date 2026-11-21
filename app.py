"""WSGI application for TempoTouch schedule proposals and conversations."""

import hashlib
import json
import logging
import os
import sqlite3
import time
from http import HTTPStatus
from pathlib import Path

from chat import Chat, validate_chat_request
from control import Controller, validate_request
from licenses import (device_public, load_public_key, verify_device_proof,
                      verify_device_request, verify_token)
from planner import (CODEX86_MODEL_DEFAULT, DEFAULT_MODEL, Planner, UpstreamUnavailable,
                     call_codex86, message, parse_json, validate_input)
from speech import DEFAULT_VOICE, OpenSpeakerSpeech, SpeechProblem, validate_request as validate_speech_request


ISSUES = {
    "invalid_key": "Nhà cung cấp từ chối token API. Kiểm tra token còn hiệu lực và đúng nhà cung cấp trong Environment của Render.",
    "insufficient_quota": "Hạn mức hoặc số dư API đã hết. Kiểm tra số dư tại tài khoản của nhà cung cấp đang dùng.",
    "rate_limit": "Nhà cung cấp đang giới hạn số yêu cầu. Chờ một lúc rồi gửi lại.",
    "model_access": "Tài khoản API chưa dùng được mô hình đã chọn. Kiểm tra tên mô hình và quyền của tài khoản tại nhà cung cấp.",
    "permission": "Nhà cung cấp từ chối quyền truy cập. Kiểm tra quyền của token và nhóm máy chủ trên tài khoản cung cấp API.",
    "invalid_request": "Nhà cung cấp từ chối định dạng yêu cầu. Kiểm tra cấu hình máy chủ và mô hình.",
    "upstream": "Dịch vụ AI của nhà cung cấp tạm thời chưa xử lý được yêu cầu. Hãy thử lại sau.",
    "timeout": "Nhà cung cấp AI trả lời quá chậm. Chờ một lúc rồi thử lại.",
    "transport": "Máy chủ TempoTouch chưa kết nối được với nhà cung cấp AI. Kiểm tra kết nối từ Render.",
    "invalid_response": "Nhà cung cấp AI đã trả lời nhưng nội dung chưa đọc được. Hãy thử lại.",
    "incomplete": "Nhà cung cấp AI chưa trả lời đầy đủ. Thử rút ngắn tin nhắn rồi gửi lại.",
    "storage": "Máy chủ chưa ghi được bộ đếm sử dụng. Chủ máy chủ cần kiểm tra quyền ghi ổ lưu trữ trên Render.",
}


def diagnostic_text(reason):
    code = "TT-" + reason.replace("_", "-").upper()
    logging.getLogger("tempotouch.planner").warning("TempoTouch %s", code)
    return f"{ISSUES[reason]} ({code})"


class Limited(Exception):
    def __init__(self, seconds):
        self.seconds = seconds


class RateLimits:
    """A persistent fixed-window quota shared by all workers on this host."""

    def __init__(self, path, per_minute=6, per_day=60):
        self.path = str(path)
        self.per_minute = per_minute
        self.per_day = per_day
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS usage(
                token_id TEXT PRIMARY KEY, expires INTEGER NOT NULL,
                minute INTEGER NOT NULL, minute_count INTEGER NOT NULL,
                day INTEGER NOT NULL, day_count INTEGER NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS usage_expiry ON usage(expires)")
            db.commit()
        finally:
            db.close()


    def take(self, claims, now):
        token_id = hashlib.sha256(claims["id"].encode("ascii")).hexdigest()
        minute, day = now // 60, now // 86400
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM usage WHERE expires <= ?", (now,))
            row = db.execute("SELECT minute, minute_count, day, day_count FROM usage WHERE token_id=?",
                             (token_id,)).fetchone()
            used_minute = row[1] if row and row[0] == minute else 0
            used_day = row[3] if row and row[2] == day else 0
            if used_day >= self.per_day:
                raise Limited((day + 1) * 86400 - now)
            if used_minute >= self.per_minute:
                raise Limited((minute + 1) * 60 - now)
            db.execute("INSERT OR REPLACE INTO usage VALUES (?, ?, ?, ?, ?, ?)",
                       (token_id, claims["exp"], minute, used_minute + 1, day, used_day + 1))
            db.commit()
        finally:
            db.close()


class ActivationConflict(Exception):
    pass


class ActivationStore:
    """Persistent one-time binding of a generic license to one Android key."""

    def __init__(self, path):
        self.path = str(path)
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS activations(
                token_id TEXT PRIMARY KEY, device TEXT NOT NULL,
                expires INTEGER NOT NULL, activated INTEGER NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS activations_expiry ON activations(expires)")
            db.commit()
        finally:
            db.close()

    def bind(self, token_id, device, expires, now):
        # Keep every binding permanently so an expired key cannot be reused on a new install.
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT device FROM activations WHERE token_id=?", (token_id,)).fetchone()
            if row is None:
                db.execute("INSERT INTO activations VALUES (?, ?, ?, ?)",
                           (token_id, device, expires, now))
                db.commit()
                return True
            if row[0] == device:
                db.commit()
                return True
            raise ActivationConflict
        finally:
            db.close()

    def device(self, token_id):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            row = db.execute("SELECT device FROM activations WHERE token_id=?", (token_id,)).fetchone()
            return row[0] if row else None
        finally:
            db.close()

class App:
    def __init__(self, public_key, planner, limits, clock=time.time, *, chat=None, controller=None,
                 activations=None):
        self.public_key = public_key
        self.planner = planner
        self.chat = chat
        self.controller = controller
        self.speech = None
        self.limits = limits
        self.activations = activations
        self.clock = clock

    def __call__(self, env, start_response):
        def response(status, value, extra=()):
            data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            headers = [("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store"),
                       ("Content-Length", str(len(data))), ("X-Content-Type-Options", "nosniff")]
            start_response(str(status) + " " + HTTPStatus(status).phrase, headers + list(extra))
            return [data]

        def error(status, text, extra=()):
            return response(status, {"error": text}, extra)

        path = env.get("PATH_INFO")
        if path not in ("/v1/activate", "/v1/plan", "/v1/chat", "/v1/action", "/v1/speech"):
            return error(404, "Không tìm thấy đường dẫn.")
        is_activation = path == "/v1/activate"
        is_chat = path == "/v1/chat"
        is_action = path == "/v1/action"
        is_speech = path == "/v1/speech"
        now = int(self.clock())

        if is_activation:
            if env.get("REQUEST_METHOD") != "POST":
                return error(405, "Dùng POST cho đường dẫn này.", [("Allow", "POST")])
            if self.activations is None:
                return error(503, "Máy chủ chưa bật lưu kích hoạt.")
            if env.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() != "application/json":
                return error(415, "Nội dung phải là JSON.")
            raw_len = env.get("CONTENT_LENGTH") or ""
            if not raw_len or not raw_len.isascii() or not raw_len.isdecimal():
                return error(411, "Thiếu độ dài nội dung.")
            if len(raw_len) > 5 or int(raw_len) > 8192:
                return error(413, "Yêu cầu quá dài.")
            size = int(raw_len)
            try:
                raw = env["wsgi.input"].read(size)
                if len(raw) != size:
                    raise ValueError("incomplete request")
                data = parse_json(raw.decode("utf-8"))
                if not isinstance(data, dict) or set(data) != {"token", "device"}:
                    raise ValueError("activation fields")
                token = data["token"]
                device = data["device"]
                if not isinstance(token, str) or not isinstance(device, str):
                    raise ValueError("activation types")
                claims = verify_token(token, self.public_key, now)
                if claims["v"] != 1:
                    return error(409, "Key này đã gắn với một thiết bị cụ thể.")
                device_public(device)
                try:
                    verify_device_proof(device, "TT1", token, path,
                                        env.get("HTTP_X_TEMPO_TIME", ""),
                                        env.get("HTTP_X_TEMPO_PROOF", ""), raw, now)
                except ValueError:
                    return error(403, "Máy chủ không xác thực được thiết bị hoặc chữ ký kích hoạt.")
                token_id = hashlib.sha256(claims["id"].encode("ascii")).hexdigest()
                self.activations.bind(token_id, device, claims["exp"], now)
                return response(200, {"activated": True})
            except ActivationConflict:
                return error(409, "Key này đã được kích hoạt trên thiết bị khác. Hãy cấp key mới.")
            except sqlite3.Error:
                return error(503, "Máy chủ chưa ghi được trạng thái kích hoạt.")
            except (OSError, ValueError, RecursionError):
                return error(400, "Key hoặc thông tin thiết bị không hợp lệ.")

        def provider_failure(reason):
            if reason not in ISSUES:
                return error(503, "Dịch vụ AI tạm thời chưa trả được phản hồi.")
            text = diagnostic_text(reason)
            if is_chat:
                return error(502 if reason == "invalid_response" else 503, text)
            if is_action:
                return error(502 if reason == "invalid_response" else 503, text)
            # Android 1.2 planners can present a message-shaped diagnosis directly.
            return response(200, message(text))

        if env.get("REQUEST_METHOD") != "POST":
            return error(405, "Dùng POST cho đường dẫn này.", [("Allow", "POST")])
        auth = env.get("HTTP_AUTHORIZATION", "")
        try:
            if len(auth) > 4200 or not auth.startswith("Bearer "):
                raise ValueError("bearer")
            claims = verify_token(auth[7:], self.public_key, now)
        except ValueError:
            return error(401, "Mã bản quyền không hợp lệ hoặc đã hết hạn.",
                         [("WWW-Authenticate", 'Bearer realm="TempoTouch"')])
        if env.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() != "application/json":
            return error(415, "Nội dung phải là JSON.")
        raw_len = env.get("CONTENT_LENGTH") or ""
        if not raw_len or not raw_len.isascii() or not raw_len.isdecimal():
            return error(411, "Thiếu độ dài nội dung.")
        max_size = 131072 if is_chat or is_action else 16384 if is_speech else 65536
        if len(raw_len) > 6 or int(raw_len) > max_size:
            return error(413, "Yêu cầu quá dài.")
        size = int(raw_len)
        try:
            raw = env["wsgi.input"].read(size)
            if len(raw) != size:
                raise ValueError("incomplete request")
            if claims["v"] == 2:
                try:
                    verify_device_request(claims, auth[7:], path, env.get("HTTP_X_TEMPO_TIME", ""),
                                          env.get("HTTP_X_TEMPO_PROOF", ""), raw, now)
                except ValueError:
                    return error(403, "Thiết bị hoặc thời gian ký yêu cầu không khớp với key.")
            elif self.activations is not None:
                token_id = hashlib.sha256(claims["id"].encode("ascii")).hexdigest()
                device = self.activations.device(token_id)
                if device is None:
                    return error(403, "Key chưa được kích hoạt trên thiết bị này.")
                try:
                    verify_device_proof(device, "TT1", auth[7:], path,
                                        env.get("HTTP_X_TEMPO_TIME", ""),
                                        env.get("HTTP_X_TEMPO_PROOF", ""), raw, now)
                except ValueError:
                    return error(403, "Thiết bị hoặc thời gian ký yêu cầu không khớp với key.")
            else:
                return error(403, "Máy chủ chưa có dữ liệu kích hoạt cho key này.")
            data = parse_json(raw.decode("utf-8"))
            if is_chat:
                request = validate_chat_request(data)
            elif is_action:
                request = validate_request(data)
            elif is_speech:
                request = validate_speech_request(data)
            else:
                request = validate_input(data)
        except (OSError, ValueError, RecursionError):
            return error(400, "Dữ liệu yêu cầu không hợp lệ.")
        try:
            self.limits.take(claims, now)
        except Limited as limit:
            return error(429, "Đã đạt giới hạn sử dụng. Hãy thử lại sau.",
                         [("Retry-After", str(limit.seconds))])
        except sqlite3.Error:
            return provider_failure("storage")
        try:
            if is_chat:
                if self.chat is None:
                    return error(503, "Máy chủ chưa được cấu hình cho trò chuyện.")
                return response(200, {"reply": self.chat(request)})
            if is_action:
                if self.controller is None:
                    return error(503, "Máy chủ chưa được cấu hình điều khiển thiết bị.")
                return response(200, self.controller(request))
            if is_speech:
                if self.speech is None:
                    return error(503, "Máy chủ chưa được cấu hình API giọng đọc.")
                return response(200, {"audio_url": self.speech.synthesize(request)})
            if self.planner is None:
                return error(503, "Máy chủ chưa được cấu hình cho AI.")
            return response(200, self.planner(request))
        except UpstreamUnavailable as problem:
            return provider_failure(problem.reason)
        except SpeechProblem as problem:
            messages = {
                "rate_limit": "API giọng đọc đang giới hạn yêu cầu. Hãy thử lại sau.",
                "timeout": "Tạo giọng đọc mất quá nhiều thời gian. Hãy thử lại.",
                "transport": "Máy chủ TempoTouch chưa kết nối được API giọng đọc.",
                "rejected": "API giọng đọc từ chối yêu cầu. Hãy kiểm tra cấu hình máy chủ.",
                "generation_failed": "API giọng đọc chưa tạo được âm thanh. Hãy thử lại.",
                "expired": "Tệp âm thanh không còn khả dụng. Hãy tạo giọng đọc lại.",
                "invalid_response": "API giọng đọc trả về dữ liệu chưa hợp lệ.",
                "unavailable": "API giọng đọc tạm thời chưa xử lý được yêu cầu.",
            }
            return error(503 if problem.reason not in ("rejected", "invalid_response", "expired") else 502,
                         messages.get(problem.reason, "Chưa thể tạo giọng đọc."))


def ai_configured():
    return bool(os.environ.get("CODEX86_API_KEY", "").strip()
                or os.environ.get("OPENAI_API_KEY", "").strip()
                or os.environ.get("AI33_TTS_API_KEY", "").strip())


def create_app():
    gateway_key = os.environ.get("CODEX86_API_KEY", "").strip()
    if gateway_key:
        model = os.environ.get("CODEX86_MODEL", "").strip() or CODEX86_MODEL_DEFAULT
        planner = Planner(gateway_key, model, transport=call_codex86)
        chat = Chat(gateway_key, model, vendor=True)
        controller = Controller(gateway_key, model, vendor=True)
    else:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if key:
            model = os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL
            planner = Planner(key, model)
            chat = Chat(key, model)
            controller = Controller(key, model)
        else:
            planner = chat = controller = None
    default_pin = Path(__file__).resolve().with_name("tempo_license_public.der")
    pin = load_public_key(os.environ.get("TEMPO_LICENSE_PUBLIC_KEY", str(default_pin)))
    database = os.environ.get("TEMPO_LIMIT_DB", "tempo-usage.sqlite3")
    limits = RateLimits(database)
    activations = ActivationStore(database)
    app = App(pin, planner, limits, chat=chat, controller=controller, activations=activations)
    speech_key = os.environ.get("AI33_TTS_API_KEY", "").strip()
    if speech_key:
        app.speech = OpenSpeakerSpeech(speech_key, os.environ.get("AI33_TTS_VOICE_ID", DEFAULT_VOICE))
    return app
