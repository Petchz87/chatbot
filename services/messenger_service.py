# services/messenger_service.py
import requests
import config

def parse_webhook_payload(body):
    """
    Parse Facebook webhook payload and return ALL text events.
    Return format:
        [
            {"sender_id": "...", "text": "..."},
            ...
        ]
    """
    events = []

    try:
        if body.get("object") == "page":
            for entry in body.get("entry", []):
                for message_event in entry.get("messaging", []):
                    if not message_event.get("message"):
                        continue

                    sender = message_event.get("sender", {})
                    message = message_event.get("message", {})

                    sender_id = sender.get("id")
                    text = message.get("text")

                    # Only keep plain text messages
                    if sender_id and text and text.strip():
                        events.append(
                            {
                                "sender_id": str(sender_id),
                                "text": text.strip(),
                            }
                        )

    except Exception as e:
        print(f"Error parsing webhook payload: {e}")

    return events


def send_reply(sender_id: str, text: str):
    """
    Send reply back to Facebook Messenger
    """
    if not config.PAGE_ACCESS_TOKEN:
        print("❌ Error: PAGE_ACCESS_TOKEN is missing in config!")
        return

    url = f"https://graph.facebook.com/v19.0/me/messages?access_token={config.PAGE_ACCESS_TOKEN}"

    payload = {
        "recipient": {"id": sender_id},
        "message": {"text": text}
    }

    try:
        print(f"🚀 Sending reply to {sender_id}...")
        response = requests.post(url, json=payload, timeout=20)

        if response.status_code == 200:
            print("✅ Reply SENT successfully!")
        else:
            print(f"❌ Facebook Error: {response.status_code}")
            print(response.text)

    except requests.Timeout:
        print("❌ Network Error: Facebook API timeout")
    except Exception as e:
        print(f"❌ Network Error: {e}")