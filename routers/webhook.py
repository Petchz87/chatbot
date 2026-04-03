# routers/webhook.py
from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import PlainTextResponse
import asyncio
import time
from collections import deque
import re

import config
from services import messenger_service
from services import rag_service
from services import escalation_service
from services import admin_notify_service

router = APIRouter()

# -----------------------------
# Debounce settings
# -----------------------------
BUFFER_SECONDS = 10
MAX_BUFFER_WAIT = 30
POLL_INTERVAL = 0.25  # keep small; this only controls how often we re-check timers

# -----------------------------
# Per-user state
# -----------------------------
pending_buffers: dict[str, list[str]] = {}
ready_batches: dict[str, deque] = {}
first_message_ts: dict[str, float] = {}
last_message_ts: dict[str, float] = {}

debounce_tasks: dict[str, asyncio.Task] = {}
processing_tasks: dict[str, asyncio.Task] = {}
user_locks: dict[str, asyncio.Lock] = {}


@router.get("/webhook")
def verify_webhook(
    mode: str = Query(..., alias="hub.mode"),
    token: str = Query(..., alias="hub.verify_token"),
    challenge: str = Query(..., alias="hub.challenge"),
):
    if mode == "subscribe" and token == config.VERIFY_TOKEN:
        return PlainTextResponse(content=challenge, status_code=200)

    raise HTTPException(status_code=403, detail="Verification failed")


def get_user_lock(sender_id: str) -> asyncio.Lock:
    if sender_id not in user_locks:
        user_locks[sender_id] = asyncio.Lock()
    return user_locks[sender_id]


def get_ready_queue(sender_id: str) -> deque:
    if sender_id not in ready_batches:
        ready_batches[sender_id] = deque()
    return ready_batches[sender_id]


def ensure_processing_worker(sender_id: str):
    task = processing_tasks.get(sender_id)
    if task is None or task.done():
        processing_tasks[sender_id] = asyncio.create_task(
            process_ready_batches(sender_id))


def is_meaningful_message(text: str) -> bool:
    """
    Ignore emoji-only / punctuation-only / whitespace-only messages.
    """
    if not text or not text.strip():
        return False

    core = re.sub(r"[^a-zA-Z0-9ก-๙]+", "", text.strip())
    return len(core) > 0


def move_pending_to_ready(sender_id: str):
    texts = pending_buffers.get(sender_id, [])
    if not texts:
        return

    batch = texts.copy()
    pending_buffers[sender_id] = []
    first_message_ts.pop(sender_id, None)
    last_message_ts.pop(sender_id, None)

    queue = get_ready_queue(sender_id)

    # Merge into last unsent batch if it exists
    if queue:
        queue[-1].extend(batch)
    else:
        queue.append(batch)

    ensure_processing_worker(sender_id)


async def debounce_until_idle(sender_id: str):
    """
    True idle-based debounce.
    Finalize the batch when:
    - user has been idle for BUFFER_SECONDS, or
    - total waiting time exceeds MAX_BUFFER_WAIT
    """
    try:
        while True:
            texts = pending_buffers.get(sender_id, [])
            if not texts:
                return

            now = time.monotonic()
            started_at = first_message_ts.get(sender_id, now)
            last_at = last_message_ts.get(sender_id, now)

            idle_for = now - last_at
            total_wait = now - started_at

            if idle_for >= BUFFER_SECONDS or total_wait >= MAX_BUFFER_WAIT:
                move_pending_to_ready(sender_id)
                return

            await asyncio.sleep(POLL_INTERVAL)

    except asyncio.CancelledError:
        return
    except Exception as e:
        print(f"Error in debounce_until_idle({sender_id}): {e}")


async def process_ready_batches(sender_id: str):
    lock = get_user_lock(sender_id)

    async with lock:
        try:
            queue = get_ready_queue(sender_id)

            while queue:
                batch = queue.popleft()
                merged_text = "\n".join(batch).strip()

                if not merged_text:
                    continue

                # Ignore emoji-only / reaction-only turns completely
                if not is_meaningful_message(merged_text):
                    continue

                print(f"📦 Final merged turn for {sender_id}: {merged_text!r}")

                if escalation_service.should_skip_bot(sender_id):
                    print(
                        f"⏸️ Bot skipped for {sender_id} because mode={escalation_service.get_mode(sender_id)}")
                    continue

                # Fast path: explicit admin request only
                fast_decision = escalation_service.evaluate(
                sender_id=sender_id,
                user_text=merged_text,
                bot_reply="",
            )

                if escalation_service.is_explicit_handoff_request(merged_text):
                    escalation_service.update_history(
                        sender_id=sender_id,
                        user_text=merged_text,
                        bot_reply="",
                    )
                    escalation_service.set_pending_admin(sender_id)

                    messenger_service.send_reply(
                        sender_id,
                        fast_decision["handoff_message"]
                    )

                    admin_notify_service.notify_admin_email(
                        sender_id=sender_id,
                        merged_text=merged_text,
                        response_text="(LLM skipped due to explicit admin request)",
                        decision=fast_decision,
                    )

                    print(f"✅ Handoff message sent to {sender_id}")
                    continue

                response_text = await rag_service.get_rag_response(merged_text, sender_id)

                # Evaluate AFTER model response, but only send one final action
                decision = escalation_service.evaluate(
                    sender_id=sender_id,
                    user_text=merged_text,
                    bot_reply=response_text,
                )

                escalation_service.update_history(
                    sender_id=sender_id,
                    user_text=merged_text,
                    bot_reply=response_text,
                )

                if decision["needs_admin"]:
                    escalation_service.set_pending_admin(sender_id)

                    messenger_service.send_reply(
                        sender_id,
                        decision["handoff_message"]
                    )

                    admin_notify_service.notify_admin_email(
                        sender_id=sender_id,
                        merged_text=merged_text,
                        response_text=response_text,
                        decision=decision,
                    )

                    print(f"✅ Handoff message sent to {sender_id}")
                    continue

                messenger_service.send_reply(sender_id, response_text)
                escalation_service.mark_successful_normal_reply(sender_id)
                print(f"✅ Reply sent to {sender_id}")

            print(f"✅ Processing finished for {sender_id}")

        except Exception as e:
            print(f"Error in process_ready_batches({sender_id}): {e}")
        finally:
            queue = get_ready_queue(sender_id)
            if queue:
                processing_tasks[sender_id] = asyncio.create_task(process_ready_batches(sender_id))


def enqueue_message(sender_id: str, text: str):
    if not is_meaningful_message(text):
        return

    now = time.monotonic()

    if sender_id not in pending_buffers:
        pending_buffers[sender_id] = []

    if not pending_buffers[sender_id]:
        first_message_ts[sender_id] = now

    last_message_ts[sender_id] = now
    pending_buffers[sender_id].append(text)

    old_task = debounce_tasks.get(sender_id)
    if old_task and not old_task.done():
        old_task.cancel()

    debounce_tasks[sender_id] = asyncio.create_task(debounce_until_idle(sender_id))


@router.post("/webhook")
async def handle_webhook(request: Request):
    body = await request.json()
    events = messenger_service.parse_webhook_payload(body)

    for event in events:
        sender_id = event["sender_id"]
        incoming_text = event["text"]
        enqueue_message(sender_id, incoming_text)

    return {
        "status": "success",
        "message": "Event received"
    }


@router.post("/admin/resume-bot/{sender_id}")
async def resume_bot_for_user(sender_id: str):
    escalation_service.resume_bot(sender_id)
    return {
        "status": "success",
        "sender_id": sender_id,
        "mode": escalation_service.get_mode(sender_id)
    }


@router.get("/admin/escalations")
async def list_escalations():
    return {
        "status": "success",
        "items": escalation_service.get_escalation_logs(limit=50)
    }