# services/escalation_service.py
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
import re


ESCALATION_THRESHOLD = 4

HANDOFF_MESSAGE = "ขออภัยค่ะ ตอนนี้แอดมินกำลังเข้ามาดูแลให้โดยตรงนะคะ"

STRONG_ESCALATION_KEYWORDS = [
    "ขอคุยแอดมิน",
    "ขอคุยกับแอดมิน",
    "ขอคุย admin",
    "ขอคุยกับ admin",
    "ขอคุยคน",
    "ขอคุยกับคน",
    "ขอคุยคนจริง",
    "ขอคุยกับคนจริง",
    "ขอเจ้าหน้าที่",
    "ขอคุยกับเจ้าหน้าที่",
    "ขอพนักงาน",
    "ให้แอดมินตอบ",
    "ให้คนตอบ",
    "ให้เจ้าหน้าที่ตอบ",
    "ตอบไม่รู้เรื่อง",
    "ไม่โอเค",
    "แย่มาก",
    "จะร้องเรียน",
    "ขอคืนเงิน",
    "ยกเลิกออเดอร์",
    "ยกเลิกคำสั่งซื้อ",
]

SOFT_NEGATIVE_KEYWORDS = [
    "ไม่ตรง",
    "งง",
    "ไม่เข้าใจ",
    "ทำไม",
    "ช้า",
    "ตอบวน",
    "ช่วยหน่อย",
    "ตอบผิด",
    "ไม่ถูก",
    "มั่ว",
    "หงุดหงิด",
    "เสียเวลา",
]

NO_DATA_PHRASES = [
    "ยังไม่มีข้อมูล",
    "ไม่มีข้อมูล",
    "ขออภัยค่ะ ขณะนี้แอดมินยังไม่มีข้อมูลส่วนนั้น",
    "ขออภัยครับ ขณะนี้แอดมินยังไม่มีข้อมูลส่วนนั้น",
]


@dataclass
class ConversationState:
    mode: str = "BOT_ACTIVE"
    negative_streak: int = 0
    recent_user_messages: deque = field(default_factory=lambda: deque(maxlen=5))
    recent_bot_replies: deque = field(default_factory=lambda: deque(maxlen=5))
    last_escalated_at: str | None = None


conversation_states: dict[str, ConversationState] = {}
escalation_logs: list[dict] = []


def get_state(sender_id: str) -> ConversationState:
    if sender_id not in conversation_states:
        conversation_states[sender_id] = ConversationState()
    return conversation_states[sender_id]


def normalize_text(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def normalize_for_intent_match(text: str) -> str:
    """
    More aggressive normalization for Thai intent matching.
    Removes spaces and common filler words that should not change intent.
    """
    t = normalize_text(text)
    t = t.replace("ค่ะ", "").replace("ครับ", "").replace("คับ", "")
    t = t.replace("หน่อย", "").replace("ที", "")
    t = t.replace("กับ", "")  # important: ขอคุยกับแอดมิน -> ขอคุยแอดมิน
    t = t.replace(" ", "")
    return t


def contains_any(text: str, keywords: list[str]) -> bool:
    t = normalize_text(text)
    return any(normalize_text(k) in t for k in keywords)


def text_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()


def is_meaningful_text(text: str) -> bool:
    t = normalize_text(text)
    if not t:
        return False
    core = re.sub(r"[^a-zA-Z0-9ก-๙]+", "", t)
    return len(core) > 0


def is_explicit_handoff_request(text: str) -> bool:
    """
    Flexible detection for admin/human handoff intent.
    Handles variants like:
    - ขอคุยแอดมิน
    - ขอคุยกับแอดมิน
    - ขอคุยกับ admin
    - ขอคุยคนจริง
    - ให้แอดมินตอบ
    """
    if not is_meaningful_text(text):
        return False

    raw = normalize_text(text)
    compact = normalize_for_intent_match(text)

    # 1) exact/substring keyword list
    if contains_any(raw, STRONG_ESCALATION_KEYWORDS):
        return True

    # 2) flexible compact pattern matching
    flexible_patterns = [
        "ขอคุยแอดมิน",
        "ขอคุยadmin",
        "ขอคุยคน",
        "ขอคุยคนจริง",
        "ขอคุยเจ้าหน้าที่",
        "ให้แอดมินตอบ",
        "ให้คนตอบ",
        "ให้เจ้าหน้าที่ตอบ",
    ]
    if any(p in compact for p in flexible_patterns):
        return True

    # 3) token-style intent check
    has_talk_request = ("ขอคุย" in raw) or ("คุย" in raw and "ขอ" in raw)
    wants_human = any(word in raw for word in ["แอดมิน", "admin", "คนจริง", "เจ้าหน้าที่", "พนักงาน"])
    if has_talk_request and wants_human:
        return True

    return False


def looks_like_repeated_user_question(state: ConversationState, current_user_text: str) -> bool:
    current = normalize_text(current_user_text)
    if not current or not is_meaningful_text(current):
        return False

    similar_count = 0
    for old_text in state.recent_user_messages:
        if text_similarity(old_text, current) >= 0.93:
            similar_count += 1

    return similar_count >= 1


def looks_like_bot_is_repeating(state: ConversationState, current_bot_reply: str) -> bool:
    current = normalize_text(current_bot_reply)
    if not current:
        return False

    for old_reply in state.recent_bot_replies:
        if text_similarity(old_reply, current) >= 0.95:
            return True
    return False


def is_no_data_reply(bot_reply: str) -> bool:
    return contains_any(bot_reply, NO_DATA_PHRASES)


def evaluate(sender_id: str, user_text: str, bot_reply: str) -> dict:
    state = get_state(sender_id)

    score = 0
    reasons = []

    meaningful_user_text = is_meaningful_text(user_text)

    explicit_handoff = meaningful_user_text and is_explicit_handoff_request(user_text)
    soft_negative = meaningful_user_text and contains_any(user_text, SOFT_NEGATIVE_KEYWORDS)
    repeated_question = meaningful_user_text and looks_like_repeated_user_question(state, user_text)
    repeated_bot_reply = bool(bot_reply) and looks_like_bot_is_repeating(state, bot_reply)
    no_data = bool(bot_reply) and is_no_data_reply(bot_reply)

    if explicit_handoff:
        score += 5
        reasons.append("explicit_handoff_request")

    if soft_negative:
        score += 1
        reasons.append("negative_or_frustrated_language")

    if repeated_question:
        score += 1
        reasons.append("repeated_user_question")

    if repeated_bot_reply:
        score += 1
        reasons.append("bot_repeated_itself")

    if no_data:
        score += 1
        reasons.append("bot_has_no_data")

    if explicit_handoff or soft_negative:
        state.negative_streak += 1
    else:
        state.negative_streak = max(0, state.negative_streak - 1)

    if state.negative_streak >= 3:
        score += 1
        reasons.append("negative_streak")

    needs_admin = explicit_handoff or (score >= ESCALATION_THRESHOLD)

    log_item = {
        "timestamp": datetime.utcnow().isoformat(),
        "sender_id": sender_id,
        "score": score,
        "needs_admin": needs_admin,
        "reasons": reasons,
        "user_text": user_text,
        "bot_reply": bot_reply,
        "mode_before": state.mode,
    }
    escalation_logs.append(log_item)

    return {
        "needs_admin": needs_admin,
        "score": score,
        "reasons": reasons,
        "handoff_message": HANDOFF_MESSAGE,
    }


def update_history(sender_id: str, user_text: str, bot_reply: str):
    state = get_state(sender_id)
    state.recent_user_messages.append(user_text or "")
    state.recent_bot_replies.append(bot_reply or "")


def mark_successful_normal_reply(sender_id: str):
    state = get_state(sender_id)
    state.negative_streak = max(0, state.negative_streak - 2)


def set_pending_admin(sender_id: str):
    state = get_state(sender_id)
    state.mode = "PENDING_ADMIN"
    state.last_escalated_at = datetime.utcnow().isoformat()


def set_admin_active(sender_id: str):
    state = get_state(sender_id)
    state.mode = "ADMIN_ACTIVE"


def resume_bot(sender_id: str):
    state = get_state(sender_id)
    state.mode = "BOT_ACTIVE"
    state.negative_streak = 0
    state.recent_user_messages.clear()
    state.recent_bot_replies.clear()


def get_mode(sender_id: str) -> str:
    return get_state(sender_id).mode


def should_skip_bot(sender_id: str) -> bool:
    return get_mode(sender_id) in {"PENDING_ADMIN", "ADMIN_ACTIVE"}


def get_escalation_logs(limit: int = 20) -> list[dict]:
    return escalation_logs[-limit:]