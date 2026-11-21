"""OpenSpeaker TTS adapter. API credentials stay on the TempoTouch server."""
import http.client
import json
import re
import ssl
import time
from urllib.parse import urlsplit


API_HOST = "api.ai33.pro"
DEFAULT_VOICE = "edge_vi-VN-HoaiMyNeural"
MAX_TEXT = 4000
MAX_RESPONSE = 65536
TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class SpeechProblem(Exception):
    def __init__(self, reason):
        self.reason = reason


def validate_request(value):
    if type(value) is not dict or value.keys() != {"text"}:
        raise ValueError("invalid speech fields")
    text = value["text"]
    if type(text) is not str or not text.strip() or len(text) > MAX_TEXT:
        raise ValueError("invalid speech text")
    return text.strip()


class OpenSpeakerSpeech:
    def __init__(self, key, voice=DEFAULT_VOICE, *, connection_factory=http.client.HTTPSConnection, sleep=time.sleep):
        self.key = key.strip()
        self.voice = voice.strip() or DEFAULT_VOICE
        self.connection_factory = connection_factory
        self.sleep = sleep
        if not self.key or len(self.key) > 4096 or "\r" in self.key or "\n" in self.key:
            raise ValueError("invalid TTS credential")
        if not re.fullmatch(r"(?:edge|elevenlabs|minimax|clone|kokoro|vbee|fishaudio)_[A-Za-z0-9_-]{1,200}", self.voice):
            raise ValueError("invalid TTS voice")

    def _json(self, method, path, *, body=None, content_type=None, timeout=12):
        headers = {"xi-api-key": self.key, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type or "application/json"
        conn = None
        try:
            conn = self.connection_factory(API_HOST, timeout=timeout, context=ssl.create_default_context())
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            payload = bytearray()
            while len(payload) <= MAX_RESPONSE:
                chunk = response.read(min(8192, MAX_RESPONSE + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > MAX_RESPONSE:
                raise SpeechProblem("invalid_response")
            if response.status == 429:
                raise SpeechProblem("rate_limit")
            if response.status >= 500:
                raise SpeechProblem("unavailable")
            if response.status < 200 or response.status >= 300:
                raise SpeechProblem("rejected")
            try:
                data = json.loads(payload.decode("utf-8")) if payload else {}
            except (UnicodeDecodeError, ValueError, RecursionError):
                raise SpeechProblem("invalid_response")
            if not isinstance(data, dict):
                raise SpeechProblem("invalid_response")
            return data
        except SpeechProblem:
            raise
        except TimeoutError:
            raise SpeechProblem("timeout")
        except (OSError, http.client.HTTPException):
            raise SpeechProblem("transport")
        finally:
            if conn is not None:
                conn.close()

    def _create(self, text):
        boundary = "----TempoTouch" + __import__("uuid").uuid4().hex
        parts = []
        for name, value in (("text", text), ("voice_id", self.voice), ("speed", "1"),
                            ("with_transcript", "false")):
            parts.extend((f"--{boundary}\r\n".encode(),
                          f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                          value.encode("utf-8"), b"\r\n"))
        parts.append(f"--{boundary}--\r\n".encode())
        response = self._json("POST", "/v3/text-to-speech", body=b"".join(parts),
                              content_type=f"multipart/form-data; boundary={boundary}", timeout=20)
        task_id = response.get("task_id")
        if response.get("success") is False or not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise SpeechProblem("invalid_response")
        return task_id

    @staticmethod
    def _audio_url(task):
        metadata = task.get("metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (ValueError, RecursionError):
                metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        value = task.get("output_uri") or metadata.get("audio_url")
        if not isinstance(value, str) or len(value) > 2048:
            return None
        try:
            parsed = urlsplit(value)
        except ValueError:
            return None
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment
                or parsed.port not in (None, 443)
                or not (host == "ai33.pro" or host.endswith(".ai33.pro"))):
            return None
        return value

    def synthesize(self, text):
        task_id = self._create(text)
        deadline = time.monotonic() + 55
        while time.monotonic() < deadline:
            task = self._json("GET", "/v1/task/" + task_id, timeout=12)
            status = task.get("status")
            if status == "done":
                audio_url = self._audio_url(task)
                if audio_url is None:
                    raise SpeechProblem("expired")
                return audio_url
            if status == "error":
                raise SpeechProblem("generation_failed")
            if status != "doing":
                raise SpeechProblem("invalid_response")
            self.sleep(min(1.25, max(0, deadline - time.monotonic())))
        raise SpeechProblem("timeout")
