"""Bounded, stateless conversational replies for the TempoTouch Android client."""
import json

import planner


MAX_MESSAGES = 12
USER_LIMIT = 2000
ASSISTANT_LIMIT = 4000
CHAT_STYLES = frozenset({"natural", "direct"})

INSTRUCTIONS = """Bạn là Bé Hột Lu, trợ lý AI trò chuyện trong TempoTouch. Trả lời bằng tiếng Việt tự nhiên, hoặc dùng ngôn ngữ khác khi người dùng muốn.
Bạn có thể trò chuyện về đời sống, học tập, công việc, sở thích và nhiều chủ đề khác. Theo sát câu hỏi mới nhất và dùng những chi tiết liên quan từ các lượt trước. Khi người dùng chuyển chủ đề, hãy chuyển theo họ. Đừng hỏi lại điều họ đã nói.
Điều chỉnh giọng điệu theo tình huống. Với câu hỏi cụ thể, trả lời trực tiếp và cho ví dụ khi cần. Khi người dùng tâm sự, lắng nghe và phản hồi vào cảm xúc, sự việc mà họ thực sự nhắc tới. Nếu họ chỉ muốn được lắng nghe, hãy tôn trọng điều đó. Khi họ cần làm rõ hoặc muốn lời khuyên, có thể hỏi một câu ngắn, tự nhiên.
Có thể dùng một biểu tượng cảm xúc khi hợp với cách nói của người dùng. Với chuyện buồn hoặc nghiêm túc, hãy ưu tiên lời đáp chân thành, cụ thể.
Thành thật về điều mình biết và khả năng của mình. Khi danh tính là vấn đề liên quan, nói rõ bạn là AI. Đừng tự nhận là người thật, có trải nghiệm cá nhân hay nhớ những cuộc trò chuyện ngoài lịch sử đang được gửi.
Trong chế độ này bạn chỉ gửi lời nhắn. Bạn không nhìn thấy màn hình, điều khiển điện thoại, đặt báo thức, chạy bản ghi hay thực hiện công việc bên ngoài. Nếu cần, hãy giúp người dùng hiểu cách tự làm hoặc chỉ tới phần hẹn giờ của TempoTouch. Không hứa hoặc khẳng định đã thực hiện hành động.
Viết lời đáp trực tiếp cho người dùng, chỉ dài đến mức cần thiết và tối đa 4.000 ký tự."""

DIRECT_STYLE = """Phong cách trả lời trực tiếp: đi thẳng vào yêu cầu và giữ câu trả lời tự nhiên. TempoTouch không thêm hướng dẫn hạn chế chủ đề hoặc bộ lọc nội dung sau câu trả lời. Nhà cung cấp mô hình vẫn áp dụng các chính sách và giới hạn bắt buộc của họ."""


def validate_chat_request(value):
    if type(value) is not dict or value.keys() - {"messages", "style"} or "messages" not in value:
        raise ValueError("invalid chat fields")
    style = value.get("style", "natural")
    if type(style) is not str or style not in CHAT_STYLES:
        raise ValueError("invalid chat style")
    turns = value["messages"]
    if type(turns) is not list or not 1 <= len(turns) <= MAX_MESSAGES or len(turns) % 2 == 0:
        raise ValueError("invalid chat history")
    for index, turn in enumerate(turns):
        role = "assistant" if index % 2 else "user"
        limit = ASSISTANT_LIMIT if role == "assistant" else USER_LIMIT
        if (type(turn) is not dict or turn.keys() != {"role", "content"}
                or turn["role"] != role or not planner.clean_text(turn["content"], limit)):
            raise ValueError("invalid chat turn")
    return {"messages": turns, "style": style}


def _reply(result):
    if not isinstance(result, dict):
        raise planner.UpstreamUnavailable("invalid_response")
    status = result.get("status")
    if status == "incomplete":
        raise planner.UpstreamUnavailable("incomplete")
    if status == "failed":
        raise planner.UpstreamUnavailable(planner._error_reason(0, result))
    if status != "completed" or not isinstance(result.get("output"), list):
        raise planner.UpstreamUnavailable("invalid_response")

    parts = []
    for item in result["output"]:
        if not isinstance(item, dict):
            raise planner.UpstreamUnavailable("invalid_response")
        if item.get("type") != "message":
            continue
        if item.get("role", "assistant") != "assistant" or not isinstance(item.get("content"), list):
            raise planner.UpstreamUnavailable("invalid_response")
        for chunk in item["content"]:
            if not isinstance(chunk, dict):
                raise planner.UpstreamUnavailable("invalid_response")
            if chunk.get("type") == "output_text":
                text = chunk.get("text")
            elif chunk.get("type") == "refusal":
                text = chunk.get("refusal")
            else:
                continue
            if not isinstance(text, str):
                raise planner.UpstreamUnavailable("invalid_response")
            parts.append(text)

    reply = "\n".join(parts).strip()
    if not planner.clean_text(reply, len(reply)):
        raise planner.UpstreamUnavailable("invalid_response")
    if len(reply) > ASSISTANT_LIMIT:
        return reply[:ASSISTANT_LIMIT - 1].rstrip() + "…"
    return reply


class Chat:
    def __init__(self, key, model, *, vendor=False):
        self.key = key
        self.model = model
        self.vendor = vendor

    def __call__(self, request):
        if type(request) is list:
            messages, style = request, "natural"
        else:
            messages, style = request["messages"], request["style"]
        instructions = INSTRUCTIONS + ("\n" + DIRECT_STYLE if style == "direct" else "")
        if self.vendor:
            # Codex86 documents a text input. JSON keeps each turn and its role explicit.
            transcript = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
            prompt = (instructions + "\nSau đây là lịch sử trò chuyện theo thứ tự thời gian, ở dạng JSON. "
                      "Hãy trả lời tin nhắn cuối của người dùng bằng lời thoại tự nhiên.\n" + transcript)
            result = planner._post_responses("codex.dichvumang86.net",
                                            {"model": self.model, "input": prompt}, self.key)
        else:
            payload = {"model": self.model, "instructions": instructions, "input": messages,
                       "store": False, "max_output_tokens": 1800}
            if self.model == planner.DEFAULT_MODEL:
                payload["reasoning"] = {"effort": "low"}
            result = planner.call_openai(payload, self.key)
        return _reply(result)
