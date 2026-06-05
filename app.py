from flask import Flask, render_template, request, jsonify, session, Response
from telethon.sync import TelegramClient
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest, AddChatUserRequest
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.types import Chat, Channel
import telethon
import os, queue, threading, json, time

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tadder-secret-key-2024")

CONFIG_FILE = "config.json"
SESSION_FILE = "BlackFox"

progress_queue = queue.Queue()
add_running = False

def load_credentials():
    """Load API credentials: config.json first, then env vars."""
    api_id = 0
    api_hash = ""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            api_id = int(cfg.get("api_id", 0))
            api_hash = cfg.get("api_hash", "")
        except Exception:
            pass
    if not api_id:
        api_id = int(os.environ.get("TELEGRAM_API_ID", 0))
    if not api_hash:
        api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    return api_id, api_hash

def get_client():
    api_id, api_hash = load_credentials()
    return TelegramClient(SESSION_FILE, api_id, api_hash)

def parse_invite_link(gp_id):
    link = gp_id.strip()
    for prefix in ["https://t.me/+", "http://t.me/+", "t.me/+",
                   "https://t.me/joinchat/", "http://t.me/joinchat/", "t.me/joinchat/"]:
        if link.startswith(prefix):
            return link[len(prefix):], None
    if link.startswith("https://t.me/") or link.startswith("t.me/"):
        return None, link.split("/")[-1]
    return None, link

@app.route("/")
def index():
    return render_template("index.html")

# ── Settings ──────────────────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def get_settings():
    api_id, api_hash = load_credentials()
    source = "none"
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            if cfg.get("api_id") and cfg.get("api_hash"):
                source = "config"
        except Exception:
            pass
    if source == "none" and api_id and api_hash:
        source = "env"
    return jsonify({
        "api_id": str(api_id) if api_id else "",
        "api_hash": api_hash,
        "source": source
    })

@app.route("/api/settings", methods=["POST"])
def save_settings():
    data = request.get_json()
    api_id = str(data.get("api_id", "")).strip()
    api_hash = str(data.get("api_hash", "")).strip()
    if not api_id or not api_hash:
        return jsonify({"ok": False, "error": "Both API ID and API Hash are required."})
    try:
        int(api_id)
    except ValueError:
        return jsonify({"ok": False, "error": "API ID must be a number."})
    cfg = {"api_id": int(api_id), "api_hash": api_hash}
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f)
    return jsonify({"ok": True})

# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/api/check-auth")
def check_auth():
    api_id, api_hash = load_credentials()
    if not api_id or not api_hash:
        return jsonify({"authenticated": False, "error": "missing_credentials"})
    try:
        client = get_client()
        client.connect()
        if client.is_user_authorized():
            me = client.get_me()
            client.disconnect()
            return jsonify({"authenticated": True, "name": me.first_name, "username": me.username})
        client.disconnect()
        return jsonify({"authenticated": False})
    except Exception as e:
        return jsonify({"authenticated": False, "error": str(e)})

@app.route("/api/send-code", methods=["POST"])
def send_code():
    api_id, api_hash = load_credentials()
    if not api_id or not api_hash:
        return jsonify({"ok": False, "error": "API credentials not configured. Please go to Settings first."})
    data = request.get_json()
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"ok": False, "error": "Phone number is required."})
    try:
        client = get_client()
        client.connect()
        result = client.send_code_request(phone)
        session["phone"] = phone
        session["phone_code_hash"] = result.phone_code_hash
        client.disconnect()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/verify-code", methods=["POST"])
def verify_code():
    data = request.get_json()
    code = data.get("code", "").strip()
    password = data.get("password", "").strip()
    phone = session.get("phone")
    phone_code_hash = session.get("phone_code_hash")
    if not phone or not phone_code_hash:
        return jsonify({"ok": False, "error": "Session expired. Please send code again."})
    try:
        client = get_client()
        client.connect()
        try:
            client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        except telethon.errors.SessionPasswordNeededError:
            if not password:
                client.disconnect()
                return jsonify({"ok": False, "needs_password": True})
            client.sign_in(password=password)
        me = client.get_me()
        client.disconnect()
        return jsonify({"ok": True, "name": me.first_name, "username": me.username})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/logout", methods=["POST"])
def logout():
    try:
        client = get_client()
        client.connect()
        client.log_out()
        client.disconnect()
    except Exception:
        pass
    if os.path.exists(f"{SESSION_FILE}.session"):
        os.remove(f"{SESSION_FILE}.session")
    session.clear()
    return jsonify({"ok": True})

# ── Groups & Members ──────────────────────────────────────────────────────────

@app.route("/api/groups")
def groups():
    try:
        client = get_client()
        client.connect()
        if not client.is_user_authorized():
            client.disconnect()
            return jsonify({"ok": False, "error": "Not authenticated."})
        dialogs = client.get_dialogs()
        group_list = []
        for dialog in dialogs:
            if dialog.is_group:
                try:
                    group_list.append({
                        "id": dialog.entity.id,
                        "title": dialog.title,
                        "username": getattr(dialog.entity, "username", None),
                        "members": getattr(dialog.entity, "participants_count", "?")
                    })
                except Exception:
                    continue
        client.disconnect()
        return jsonify({"ok": True, "groups": group_list})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/extract", methods=["POST"])
def extract():
    data = request.get_json()
    identifier = data.get("identifier")
    try:
        client = get_client()
        client.connect()
        if not client.is_user_authorized():
            client.disconnect()
            return jsonify({"ok": False, "error": "Not authenticated."})
        invite_hash, username = parse_invite_link(str(identifier))
        if invite_hash:
            try:
                invite_info = client(CheckChatInviteRequest(invite_hash))
                entity = invite_info.chat
            except Exception:
                result = client(ImportChatInviteRequest(invite_hash))
                entity = result.chats[0]
        elif str(identifier).lstrip("-").isdigit():
            entity = client.get_entity(int(identifier))
        else:
            entity = client.get_entity(username or identifier)
        users = client.get_participants(entity, limit=5000)
        me = client.get_me()
        my_username = me.username
        saved = []
        with open("members.txt", "w") as f:
            for user in users:
                if user.username and "bot" not in user.username.lower():
                    if user.username != my_username:
                        f.write(user.username + "\n")
                        saved.append(user.username)
        client.disconnect()
        return jsonify({"ok": True, "count": len(saved), "members": saved[:50]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

def resolve_group(client, gp_id):
    """Resolve a group from username, public link, or private invite link.
    Returns the full entity (Chat or Channel) so callers can pick the right API."""
    invite_hash, username = parse_invite_link(gp_id)
    if invite_hash:
        # Already a member? CheckChatInviteRequest returns ChatInviteAlready with .chat
        try:
            info = client(CheckChatInviteRequest(invite_hash))
            chat = getattr(info, "chat", None)
            if chat:
                return chat
        except Exception:
            pass
        # Not a member yet — join first
        try:
            result = client(ImportChatInviteRequest(invite_hash))
            return result.chats[0]
        except telethon.errors.UserAlreadyParticipantError:
            # Already joined but CheckChatInvite didn't return .chat — scan dialogs
            # Re-fetch dialogs to find the group by matching
            dialogs = client.get_dialogs()
            # Return the most recently active group as a fallback
            for d in dialogs:
                if d.is_group:
                    return d.entity
        raise ValueError("Could not resolve the invite link. Make sure you are an admin or member of the target group.")
    else:
        return client.get_entity(username or gp_id)

def add_user_to_group(client, entity, peer_user):
    """Add a single user to either a regular Chat or a supergroup/Channel."""
    if isinstance(entity, Chat):
        # Regular group → AddChatUserRequest
        client(AddChatUserRequest(chat_id=entity.id, user_id=peer_user, fwd_limit=10))
    else:
        # Supergroup or broadcast channel → InviteToChannelRequest
        input_channel = client.get_input_entity(entity)
        client(InviteToChannelRequest(channel=input_channel, users=[peer_user]))

def run_add_worker(gp_id, limit):
    global add_running
    add_running = True
    try:
        client = get_client()
        client.connect()

        try:
            entity = resolve_group(client, gp_id)
        except Exception as e:
            progress_queue.put({"type": "error", "message": f"Could not resolve target group: {e}"})
            client.disconnect()
            add_running = False
            return

        group_type = "channel" if isinstance(entity, Channel) else "chat"
        progress_queue.put({"type": "info", "message": f"Resolved as {'supergroup/channel' if group_type == 'channel' else 'regular group'}: {getattr(entity, 'title', str(entity.id))}"})

        if not os.path.exists("members.txt"):
            progress_queue.put({"type": "error", "message": "No members.txt found. Please extract members first."})
            client.disconnect()
            add_running = False
            return

        with open("members.txt", "r") as f:
            all_users = [line.strip() for line in f if line.strip()]

        users = all_users[:limit] if limit and limit > 0 else all_users
        total = len(users)
        added = 0
        failed = 0

        for i, userr in enumerate(users):
            try:
                peer_user = client.get_input_entity(userr)
                add_user_to_group(client, entity, peer_user)
                added += 1
                progress_queue.put({"type": "progress", "current": i + 1, "total": total,
                                    "added": added, "failed": failed, "user": userr, "status": "added"})
            except telethon.errors.UserPrivacyRestrictedError:
                failed += 1
                progress_queue.put({"type": "progress", "current": i + 1, "total": total,
                                    "added": added, "failed": failed, "user": userr, "status": "privacy"})
            except telethon.errors.PeerFloodError:
                progress_queue.put({"type": "flood", "message": "Flood wait — pausing 5 seconds..."})
                time.sleep(5)
                failed += 1
            except telethon.errors.FloodWaitError as e:
                wait = e.seconds
                progress_queue.put({"type": "flood", "message": f"FloodWait — pausing {wait} seconds..."})
                time.sleep(wait)
            except telethon.errors.UserChannelsTooMuchError:
                failed += 1
                progress_queue.put({"type": "progress", "current": i + 1, "total": total,
                                    "added": added, "failed": failed, "user": userr, "status": "too_many"})
            except telethon.errors.UserNotMutualContactError:
                failed += 1
                progress_queue.put({"type": "progress", "current": i + 1, "total": total,
                                    "added": added, "failed": failed, "user": userr, "status": "not_contact"})
            except telethon.errors.UserAlreadyParticipantError:
                # Already in the group — count as added
                added += 1
                progress_queue.put({"type": "progress", "current": i + 1, "total": total,
                                    "added": added, "failed": failed, "user": userr, "status": "added"})
            except Exception as ex:
                failed += 1
                progress_queue.put({"type": "progress", "current": i + 1, "total": total,
                                    "added": added, "failed": failed, "user": userr,
                                    "status": "error", "detail": str(ex)})

        client.disconnect()
        progress_queue.put({"type": "done", "added": added, "failed": failed, "total": total})
    except Exception as e:
        progress_queue.put({"type": "error", "message": str(e)})
    finally:
        add_running = False

@app.route("/api/add", methods=["POST"])
def add_members():
    global add_running
    if add_running:
        return jsonify({"ok": False, "error": "An add operation is already running."})
    data = request.get_json()
    gp_id = data.get("group", "").strip()
    limit = data.get("limit", 0)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 0
    if not gp_id:
        return jsonify({"ok": False, "error": "Target group is required."})
    with progress_queue.mutex:
        progress_queue.queue.clear()
    t = threading.Thread(target=run_add_worker, args=(gp_id, limit), daemon=True)
    t.start()
    return jsonify({"ok": True})

@app.route("/api/progress")
def progress():
    def generate():
        while True:
            try:
                item = progress_queue.get(timeout=30)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
