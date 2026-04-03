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

BUFFER_SECONDS = 10
MAX_BUFFER_WAIT = 30

pending_buffers: dict[str, list[str]] = {}
ready_batches: dict[str, deque] = {}
first_message_ts: dict[str, float] = {}
last_message_ts: dict[str, float] = {}

debounce_handles: dict[str, asyncio.TimerHandle] = {}
max_wait_handles: dict[str, asyncio.TimerHandle] = {}
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
        processing_tasks[sender_id] = asyncio.create_task(process_ready_batches(sender_id))


def is_meaningful_message(text: str) -> bool:
    if not text or not text.strip():
        return False
    core = re.sub(r"[^a-zA-Z0-9ก-๙]+", "", text.strip())
    return len(core) > 0


def cancel_timer(handle: asyncio.TimerHandle | None):
    if handle and not handle.cancelled():
        handle.cancel()


def clear_sender_timers(sender_id: str):
    cancel_timer(debounce_handles.pop(sender_id, None))
    cancel_timer(max_wait_handles.pop(sender_id, None))


def flush_sender(sender_id: str):
    texts = pending_buffers.get(sender_id, [])
    if not texts:
        return

    batch = texts.copy()
    pending_buffers[sender_id] = []
    first_message_ts.pop(sender_id, None)
    last_message_ts.pop(sender_id, None)
    clear_sender_timers(sender_id)

    queue = get_ready_queue(sender_id)
    queue.append(batch)

    ensure_processing_worker(sender_id)


def schedule_sender_timers(sender_id: str):
    loop = asyncio.get_running_loop()

    cancel_timer(debounce_handles.get(sender_id))
    debounce_handles[sender_id] = loop.call_later(
        BUFFER_SECONDS,
        flush_sender,
        sender_id,
    )

    if sender_id not in max_wait_handles:
        max_wait_handles[sender_id] = loop.call_later(
            MAX_BUFFER_WAIT,
            flush_sender,
            sender_id,
        )


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

                if not is_meaningful_message(merged_text):
                    continue

                print(f"📦 Final merged turn for {sender_id}: {merged_text!r}")

                if escalation_service.should_skip_bot(sender_id):
                    print(f"⏸️ Bot skipped for {sender_id} because mode={escalation_service.get_mode(sender_id)}")
                    continue

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

    schedule_sender_timers(sender_id)


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