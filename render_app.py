"""Public HTTPS routes for a TempoTouch Render web service. Render terminates TLS."""
import json
import os
import re
from pathlib import Path
from wsgiref.util import FileWrapper

from app import ai_configured, create_app

RELEASES = Path(__file__).resolve().parent / "releases"
APK_PATH = re.compile(r"/releases/(TempoTouch-[1-9][0-9]{0,9}\.apk)")
service = create_app()
planner = service if ai_configured() else None


def reply(env, start, status, text, extra=(), content_type="text/plain; charset=utf-8"):
    body = text.encode("utf-8")
    headers = [("Content-Type", content_type), ("Content-Length", str(len(body))),
               ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")]
    start(status, headers + list(extra))
    return [] if env["REQUEST_METHOD"] == "HEAD" else [body]


def application(env, start):
    path = env.get("PATH_INFO", "")
    if path == "/v1/activate":
        return service(env, start)
    if path in ("/v1/plan", "/v1/chat", "/v1/action", "/v1/speech"):
        if planner is None:
            text = "Máy chủ chưa được cấu hình cho AI."
            if path == "/v1/chat":
                return reply(env, start, "503 Service Unavailable", json.dumps({"error": text}, ensure_ascii=False),
                             content_type="application/json; charset=utf-8")
            if path == "/v1/action":
                return reply(env, start, "503 Service Unavailable", json.dumps({"error": text}, ensure_ascii=False),
                             content_type="application/json; charset=utf-8")
            if path == "/v1/speech":
                text = "Máy chủ chưa được cấu hình API giọng đọc."
                return reply(env, start, "503 Service Unavailable", json.dumps({"error": text}, ensure_ascii=False),
                             content_type="application/json; charset=utf-8")
            return reply(env, start, "503 Service Unavailable", text)
        return planner(env, start)

    apk = APK_PATH.fullmatch(path)
    if path not in ("/", "/releases/latest.json") and not apk:
        return reply(env, start, "404 Not Found", "Không tìm thấy.")
    if env["REQUEST_METHOD"] not in ("GET", "HEAD"):
        return reply(env, start, "405 Method Not Allowed", "Dùng GET hoặc HEAD.", [("Allow", "GET, HEAD")])
    if path == "/":
        return reply(env, start, "200 OK", "Máy chủ TempoTouch đang chạy (máy chủ 1.3). Xem phiên bản Android tại /releases/latest.json")

    name = apk.group(1) if apk else "latest.json"
    try:
        stream = (RELEASES / name).open("rb")
    except FileNotFoundError:
        return reply(env, start, "404 Not Found", "Chưa có phiên bản này.")
    headers = [("Content-Length", str(os.fstat(stream.fileno()).st_size)), ("X-Content-Type-Options", "nosniff")]
    if apk:
        headers.extend([("Content-Type", "application/vnd.android.package-archive"),
                        ("Content-Disposition", f'attachment; filename="{name}"'),
                        ("Cache-Control", "public, max-age=31536000, immutable")])
    else:
        headers.extend([("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store")])
    start("200 OK", headers)
    if env["REQUEST_METHOD"] == "HEAD":
        stream.close()
        return []
    return env.get("wsgi.file_wrapper", FileWrapper)(stream)
