from flask import Flask, render_template, request, jsonify, Response
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError, PeerFloodError, UserPrivacyRestrictedError,
    UserAlreadyParticipantError, UserChannelsTooMuchError,
    UserNotMutualContactError, SessionPasswordNeededError
)
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest, AddChatUserRequest
from telethon.tl.types import Channel as TeleChannel, InputPeerUser
import os, queue, threading, json, uuid, asyncio, random, time

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tadder-secret-key-2024")

CONFIG_FILE   = "config.json"
SESSION_FILE  = "tele_main"
ACCOUNTS_FILE = "accounts.json"

progress_queue = queue.Queue()
add_running = False

# Server-side store: avoids cookie/proxy session issues
_pending_codes: dict = {}   # key -> {"hash": str}


# ── Async helper ────────────────────────────────────────────────────────────────

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Credentials ─────────────────────────────────────────────────────────────────

def load_credentials():
    api_id = 0
    api_hash = ""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            api_id  = int(cfg.get("api_id", 0))
            api_hash = cfg.get("api_hash", "")
        except Exception:
            pass
    if not api_id:
        api_id = int(os.environ.get("TELEGRAM_API_ID", 0))
    if not api_hash:
        api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    return api_id, api_hash


def make_client(session_name):
    api_id, api_hash = load_credentials()
    return TelegramClient(session_name, api_id, api_hash)


# ── Account helpers ─────────────────────────────────────────────────────────────

def load_extra_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_extra_accounts(accounts):
    with open(ACCOUNTS_FILE, "w") as f:
        json.dump(accounts, f, indent=2)


def get_all_accounts():
    main = {"id": "main", "name": "Main Account", "session": SESSION_FILE}
    return [main] + load_extra_accounts()


def remove_session_files(session_name):
    for ext in [".session", ".session-journal"]:
        p = session_name + ext
        if os.path.exists(p):
            os.remove(p)


async def resolve_chat(client, identifier):
    """Resolve a chat entity from username, ID, or invite link."""
    ident = str(identifier).strip()
    # Private invite links
    for prefix in ["https://t.me/+", "http://t.me/+", "t.me/+",
                   "https://t.me/joinchat/", "http://t.me/joinchat/", "t.me/joinchat/"]:
        if ident.startswith(prefix):
            invite_hash = ident[len(prefix):]
            try:
                result = await client(ImportChatInviteRequest(invite_hash))
                return result.chats[0]
            except Exception:
                pass
            try:
                info = await client(CheckChatInviteRequest(invite_hash))
                if hasattr(info, "chat"):
                    return info.chat
            except Exception:
                pass
    # Numeric ID
    if ident.lstrip("-").isdigit():
        try:
            return await client.get_entity(int(ident))
        except Exception:
            pass
    # Username / public link
    try:
        return await client.get_entity(ident)
    except Exception as e:
        raise Exception(f"Could not resolve group '{identifier}': {e}")


# ── Settings ────────────────────────────────────────────────────────────────────

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
    return jsonify({"api_id": str(api_id) if api_id else "", "api_hash": api_hash, "source": source})


@app.route("/api/settings", methods=["POST"])
def save_settings():
    data = request.get_json()
    api_id   = str(data.get("api_id", "")).strip()
    api_hash = str(data.get("api_hash", "")).strip()
    if not api_id or not api_hash:
        return jsonify({"ok": False, "error": "Both API ID and API Hash are required."})
    try:
        int(api_id)
    except ValueError:
        return jsonify({"ok": False, "error": "API ID must be a number."})
    with open(CONFIG_FILE, "w") as f:
        json.dump({"api_id": int(api_id), "api_hash": api_hash}, f)
    return jsonify({"ok": True})


# ── Auth ─────────────────────────────────────────────────────────────────────────

@app.route("/api/check-auth")
def check_auth():
    api_id, api_hash = load_credentials()
    if not api_id or not api_hash:
        return jsonify({"authenticated": False, "error": "missing_credentials"})
    async def _check():
        client = make_client(SESSION_FILE)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return {"authenticated": False}
            me = await client.get_me()
            return {"authenticated": True, "name": me.first_name or "", "username": me.username or ""}
        except Exception:
            return {"authenticated": False}
        finally:
            try: await client.disconnect()
            except Exception: pass
    try:
        return jsonify(_run(_check()))
    except Exception as e:
        return jsonify({"authenticated": False, "error": str(e)})


@app.route("/api/send-code", methods=["POST"])
def send_code():
    api_id, api_hash = load_credentials()
    if not api_id or not api_hash:
        return jsonify({"ok": False, "error": "API credentials not configured. Please go to Settings first."})
    data  = request.get_json()
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"ok": False, "error": "Phone number is required."})
    async def _send():
        client = make_client(SESSION_FILE)
        await client.connect()
        try:
            result = await client.send_code_request(phone)
            return result.phone_code_hash
        finally:
            await client.disconnect()
    try:
        phone_code_hash = _run(_send())
        _pending_codes[phone] = {"hash": phone_code_hash}
        return jsonify({"ok": True, "phone": phone})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/verify-code", methods=["POST"])
def verify_code():
    data     = request.get_json()
    code     = data.get("code", "").strip()
    password = data.get("password", "").strip()
    phone    = data.get("phone", "").strip()
    pending  = _pending_codes.get(phone) if phone else None
    if not phone or not pending:
        return jsonify({"ok": False, "error": "Session expired. Please send the code again."})
    phone_code_hash = pending["hash"]
    async def _verify():
        client = make_client(SESSION_FILE)
        await client.connect()
        try:
            try:
                user = await client.sign_in(phone, code=code, phone_code_hash=phone_code_hash)
                return user, False
            except SessionPasswordNeededError:
                if not password:
                    return None, True
                user = await client.sign_in(password=password)
                return user, False
        finally:
            await client.disconnect()
    try:
        user, needs_pw = _run(_verify())
        if needs_pw:
            return jsonify({"ok": False, "needs_password": True})
        _pending_codes.pop(phone, None)
        return jsonify({"ok": True, "name": user.first_name or "", "username": user.username or ""})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/logout", methods=["POST"])
def logout():
    async def _logout():
        client = make_client(SESSION_FILE)
        try:
            await client.connect()
            await client.log_out()
        except Exception:
            pass
        finally:
            try: await client.disconnect()
            except Exception: pass
    _run(_logout())
    remove_session_files(SESSION_FILE)
    return jsonify({"ok": True})


# ── Multi-account management ────────────────────────────────────────────────────

async def _account_status(acc):
    client = make_client(acc["session"])
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return {**acc, "logged_in": False}
        me = await client.get_me()
        return {**acc, "logged_in": True, "display_name": me.first_name or "", "username": me.username or ""}
    except Exception:
        return {**acc, "logged_in": False}
    finally:
        try: await client.disconnect()
        except Exception: pass


@app.route("/api/accounts")
def list_accounts():
    async def _list():
        return [await _account_status(a) for a in get_all_accounts()]
    try:
        return jsonify({"ok": True, "accounts": _run(_list())})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/accounts/add", methods=["POST"])
def create_account():
    extras = load_extra_accounts()
    acc_id  = f"acc_{uuid.uuid4().hex[:8]}"
    new_acc = {"id": acc_id, "name": f"Account {len(extras) + 2}", "session": f"tele_{acc_id}"}
    extras.append(new_acc)
    save_extra_accounts(extras)
    return jsonify({"ok": True, "account": new_acc})


@app.route("/api/accounts/<acc_id>/remove", methods=["POST"])
def remove_account(acc_id):
    if acc_id == "main":
        return jsonify({"ok": False, "error": "Cannot remove the main account."})
    extras = load_extra_accounts()
    acc = next((a for a in extras if a["id"] == acc_id), None)
    if not acc:
        return jsonify({"ok": False, "error": "Account not found."})
    async def _remove():
        client = make_client(acc["session"])
        try:
            await client.connect()
            await client.log_out()
        except Exception:
            pass
        finally:
            try: await client.disconnect()
            except Exception: pass
    _run(_remove())
    remove_session_files(acc["session"])
    save_extra_accounts([a for a in extras if a["id"] != acc_id])
    return jsonify({"ok": True})


@app.route("/api/accounts/<acc_id>/send-code", methods=["POST"])
def acc_send_code(acc_id):
    acc = next((a for a in get_all_accounts() if a["id"] == acc_id), None)
    if not acc:
        return jsonify({"ok": False, "error": "Account not found."})
    data  = request.get_json()
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"ok": False, "error": "Phone number required."})
    async def _send():
        client = make_client(acc["session"])
        await client.connect()
        try:
            result = await client.send_code_request(phone)
            return result.phone_code_hash
        finally:
            await client.disconnect()
    try:
        phone_code_hash = _run(_send())
        _pending_codes[f"{acc_id}:{phone}"] = {"hash": phone_code_hash}
        return jsonify({"ok": True, "phone": phone})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/accounts/<acc_id>/verify-code", methods=["POST"])
def acc_verify_code(acc_id):
    acc = next((a for a in get_all_accounts() if a["id"] == acc_id), None)
    if not acc:
        return jsonify({"ok": False, "error": "Account not found."})
    data     = request.get_json()
    code     = data.get("code", "").strip()
    password = data.get("password", "").strip()
    phone    = data.get("phone", "").strip()
    pending  = _pending_codes.get(f"{acc_id}:{phone}") if phone else None
    if not phone or not pending:
        return jsonify({"ok": False, "error": "Session expired. Please send code again."})
    phone_code_hash = pending["hash"]
    async def _verify():
        client = make_client(acc["session"])
        await client.connect()
        try:
            try:
                user = await client.sign_in(phone, code=code, phone_code_hash=phone_code_hash)
                return user, False
            except SessionPasswordNeededError:
                if not password:
                    return None, True
                user = await client.sign_in(password=password)
                return user, False
        finally:
            await client.disconnect()
    try:
        user, needs_pw = _run(_verify())
        if needs_pw:
            return jsonify({"ok": False, "needs_password": True})
        _pending_codes.pop(f"{acc_id}:{phone}", None)
        if acc_id != "main":
            extras = load_extra_accounts()
            for a in extras:
                if a["id"] == acc_id:
                    a["name"] = user.first_name or a["name"]
            save_extra_accounts(extras)
        return jsonify({"ok": True, "name": user.first_name or "", "username": user.username or ""})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/accounts/<acc_id>/logout", methods=["POST"])
def acc_logout(acc_id):
    acc = next((a for a in get_all_accounts() if a["id"] == acc_id), None)
    if not acc:
        return jsonify({"ok": False, "error": "Account not found."})
    async def _logout():
        client = make_client(acc["session"])
        try:
            await client.connect()
            await client.log_out()
        except Exception:
            pass
        finally:
            try: await client.disconnect()
            except Exception: pass
    _run(_logout())
    remove_session_files(acc["session"])
    return jsonify({"ok": True})


# ── Groups & Members ─────────────────────────────────────────────────────────────

@app.route("/api/groups")
def groups():
    async def _groups():
        client = make_client(SESSION_FILE)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return None
            group_list = []
            async for dialog in client.iter_dialogs():
                if not (dialog.is_group or dialog.is_channel):
                    continue
                entity = dialog.entity
                group_list.append({
                    "id": entity.id,
                    "title": dialog.name,
                    "username": getattr(entity, "username", None),
                    "members": getattr(entity, "participants_count", "?") or "?"
                })
            return group_list
        finally:
            await client.disconnect()
    try:
        result = _run(_groups())
        if result is None:
            return jsonify({"ok": False, "error": "Not authenticated."})
        return jsonify({"ok": True, "groups": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/extract", methods=["POST"])
def extract():
    data       = request.get_json()
    identifier = data.get("identifier")
    async def _extract():
        client = make_client(SESSION_FILE)
        await client.connect()
        try:
            me    = await client.get_me()
            my_id = me.id
            chat  = await resolve_chat(client, identifier)
            saved = []
            async for user in client.iter_participants(chat, aggressive=True):
                if user.bot or user.id == my_id:
                    continue
                if user.username:
                    entry = user.username
                else:
                    # Store access_hash so any account session can add this user later
                    entry = f"id:{user.id}:{user.access_hash}"
                saved.append(entry)
            with open("members.txt", "w") as f:
                for entry in saved:
                    f.write(entry + "\n")
            return len(saved), saved[:50]
        finally:
            await client.disconnect()
    try:
        count, preview = _run(_extract())
        return jsonify({"ok": True, "count": count, "members": preview})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── Add Members ──────────────────────────────────────────────────────────────────

async def _do_add(client, chat, peer, is_channel):
    """Add a user using the correct method for the chat type, with fallback."""
    if is_channel:
        try:
            await client(InviteToChannelRequest(channel=chat, users=[peer]))
            return
        except Exception as e:
            if "CHAT_MEMBER_ADD_FAILED" in str(e) or "CHAT_ADMIN_REQUIRED" in str(e):
                # Fall back to group method in case chat is actually a linked group
                await client(AddChatUserRequest(chat_id=chat.id, user_id=peer, fwd_limit=50))
                return
            raise
    else:
        try:
            await client(AddChatUserRequest(chat_id=chat.id, user_id=peer, fwd_limit=50))
            return
        except Exception as e:
            if "CHAT_MEMBER_ADD_FAILED" in str(e) or "CHAT_INVALID" in str(e):
                # Chat may actually be a supergroup — try channel method
                await client(InviteToChannelRequest(channel=chat, users=[peer]))
                return
            raise


async def _account_worker_async(acc, gp_id, users, delay_min, delay_max, shared):
    label  = acc.get("name", acc["id"])
    client = make_client(acc["session"])
    await client.connect()
    try:
        if not await client.is_user_authorized():
            progress_queue.put({"type": "warn", "message": f"[{label}] Not logged in — skipping."})
            return
        try:
            chat = await resolve_chat(client, gp_id)
        except Exception as e:
            progress_queue.put({"type": "error", "message": f"[{label}] Could not resolve group: {e}"})
            return

        is_channel = isinstance(chat, TeleChannel)
        title      = getattr(chat, "title", str(gp_id))
        kind       = "channel/supergroup" if is_channel else "group"
        progress_queue.put({"type": "info",
                            "message": f"[{label}] Resolved '{title}' ({kind}). Adding {len(users)} members."})
        i = 0
        while i < len(users):
            userr = users[i]
            retry = False
            try:
                # ── Resolve peer ───────────────────────────────────────────
                if userr.startswith("id:"):
                    parts = userr.split(":")
                    if len(parts) == 3:
                        # New format: id:USER_ID:ACCESS_HASH — build directly, no lookup needed
                        peer = InputPeerUser(int(parts[1]), int(parts[2]))
                    else:
                        # Old format: id:USER_ID — try session cache, skip if not found
                        try:
                            peer = await client.get_input_entity(int(parts[1]))
                        except (ValueError, KeyError, TypeError):
                            with shared["lock"]:
                                shared["failed"]  += 1
                                shared["current"] += 1
                                a, f, c, t = shared["added"], shared["failed"], shared["current"], shared["total"]
                            progress_queue.put({"type": "progress", "current": c, "total": t,
                                                "added": a, "failed": f, "user": userr,
                                                "status": "no_hash", "account": label})
                            i += 1
                            continue
                else:
                    try:
                        peer = await client.get_input_entity(userr)
                    except (ValueError, KeyError, TypeError):
                        with shared["lock"]:
                            shared["failed"]  += 1
                            shared["current"] += 1
                            a, f, c, t = shared["added"], shared["failed"], shared["current"], shared["total"]
                        progress_queue.put({"type": "progress", "current": c, "total": t,
                                            "added": a, "failed": f, "user": userr,
                                            "status": "no_hash", "account": label})
                        i += 1
                        continue

                # ── Add with correct method + fallback ─────────────────────
                await _do_add(client, chat, peer, is_channel)

                with shared["lock"]:
                    shared["added"]   += 1
                    shared["current"] += 1
                    a, f, c, t = shared["added"], shared["failed"], shared["current"], shared["total"]
                progress_queue.put({"type": "progress", "current": c, "total": t,
                                    "added": a, "failed": f, "user": userr,
                                    "status": "added", "account": label})
                if i < len(users) - 1:
                    await asyncio.sleep(random.uniform(delay_min, delay_max))

            except FloodWaitError as e:
                wait = e.seconds
                progress_queue.put({"type": "flood",
                                    "message": f"[{label}] FloodWait {wait}s — waiting then retrying @{userr}..."})
                remaining = wait
                while remaining > 0:
                    progress_queue.put({"type": "countdown", "seconds": remaining})
                    chunk = min(5, remaining)
                    await asyncio.sleep(chunk)
                    remaining -= chunk
                progress_queue.put({"type": "countdown", "seconds": 0})
                retry = True
            except PeerFloodError:
                progress_queue.put({"type": "flood",
                                    "message": f"[{label}] PeerFlood — account restricted. Stopping this account."})
                return
            except UserPrivacyRestrictedError:
                with shared["lock"]:
                    shared["failed"]  += 1
                    shared["current"] += 1
                    a, f, c, t = shared["added"], shared["failed"], shared["current"], shared["total"]
                progress_queue.put({"type": "progress", "current": c, "total": t,
                                    "added": a, "failed": f, "user": userr,
                                    "status": "privacy", "account": label})
            except UserAlreadyParticipantError:
                with shared["lock"]:
                    shared["added"]   += 1
                    shared["current"] += 1
                    a, f, c, t = shared["added"], shared["failed"], shared["current"], shared["total"]
                progress_queue.put({"type": "progress", "current": c, "total": t,
                                    "added": a, "failed": f, "user": userr,
                                    "status": "added", "account": label})
            except UserChannelsTooMuchError:
                with shared["lock"]:
                    shared["failed"]  += 1
                    shared["current"] += 1
                    a, f, c, t = shared["added"], shared["failed"], shared["current"], shared["total"]
                progress_queue.put({"type": "progress", "current": c, "total": t,
                                    "added": a, "failed": f, "user": userr,
                                    "status": "too_many", "account": label})
            except UserNotMutualContactError:
                with shared["lock"]:
                    shared["failed"]  += 1
                    shared["current"] += 1
                    a, f, c, t = shared["added"], shared["failed"], shared["current"], shared["total"]
                progress_queue.put({"type": "progress", "current": c, "total": t,
                                    "added": a, "failed": f, "user": userr,
                                    "status": "not_contact", "account": label})
            except Exception as ex:
                err = str(ex)
                status = "add_failed" if "CHAT_MEMBER_ADD_FAILED" in err else "error"
                with shared["lock"]:
                    shared["failed"]  += 1
                    shared["current"] += 1
                    a, f, c, t = shared["added"], shared["failed"], shared["current"], shared["total"]
                progress_queue.put({"type": "progress", "current": c, "total": t,
                                    "added": a, "failed": f, "user": userr,
                                    "status": status, "detail": err, "account": label})
            if not retry:
                i += 1
    finally:
        await client.disconnect()


def run_account_worker(acc, gp_id, users, delay_min, delay_max, shared):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_account_worker_async(acc, gp_id, users, delay_min, delay_max, shared))
    except Exception as e:
        progress_queue.put({"type": "error", "message": f"[{acc.get('name', acc['id'])}] {e}"})
    finally:
        loop.close()


def run_add_worker(gp_id, limit, delay):
    global add_running
    add_running = True
    delay_min   = max(1, delay)
    delay_max   = delay_min + 5
    async def _active_accounts():
        active = []
        for acc in get_all_accounts():
            client = make_client(acc["session"])
            try:
                await client.connect()
                if await client.is_user_authorized():
                    active.append(acc)
            except Exception:
                pass
            finally:
                try: await client.disconnect()
                except Exception: pass
        return active
    try:
        active = _run(_active_accounts())
        if not active:
            progress_queue.put({"type": "error", "message": "No logged-in accounts found. Please log in first."})
            return
        if not os.path.exists("members.txt"):
            progress_queue.put({"type": "error", "message": "No members.txt found. Please extract members first."})
            return
        with open("members.txt", "r") as f:
            all_users = [line.strip() for line in f if line.strip()]
        users = all_users[:limit] if limit and limit > 0 else all_users
        total = len(users)
        n     = len(active)
        per   = f"  (~{round(total/n)} per account)" if n > 1 else ""
        progress_queue.put({"type": "info",
                            "message": f"Using {n} account{'s' if n > 1 else ''} | {total} members | delay {delay_min}–{delay_max}s{per}."})
        chunks = [users[i::n] for i in range(n)]
        shared = {"lock": threading.Lock(), "added": 0, "failed": 0, "current": 0, "total": total}

        ACC_START_MIN, ACC_START_MAX = 15, 20

        def delayed_worker(acc, gp_id, chunk, delay_min, delay_max, shared, start_delay):
            if start_delay > 0:
                progress_queue.put({"type": "info",
                                    "message": f"[{acc.get('name', acc['id'])}] Starting in {start_delay}s..."})
                time.sleep(start_delay)
            run_account_worker(acc, gp_id, chunk, delay_min, delay_max, shared)

        threads = []
        for idx, (acc, chunk) in enumerate(zip(active, chunks)):
            start_delay = round(random.uniform(ACC_START_MIN, ACC_START_MAX) * idx, 1)
            t = threading.Thread(target=delayed_worker,
                                 args=(acc, gp_id, chunk, delay_min, delay_max, shared, start_delay),
                                 daemon=True)
            threads.append(t)

        for t in threads: t.start()
        for t in threads: t.join()
        progress_queue.put({"type": "done", "added": shared["added"],
                            "failed": shared["failed"], "total": total})
    except Exception as e:
        progress_queue.put({"type": "error", "message": str(e)})
    finally:
        add_running = False


@app.route("/api/add", methods=["POST"])
def add_members():
    global add_running
    if add_running:
        return jsonify({"ok": False, "error": "An add operation is already running."})
    data  = request.get_json()
    gp_id = data.get("group", "").strip()
    limit = data.get("limit", 0)
    delay = data.get("delay", 15)
    try: limit = int(limit)
    except (TypeError, ValueError): limit = 0
    try:
        delay = int(delay)
        if delay < 0: delay = 0
    except (TypeError, ValueError): delay = 15
    if not gp_id:
        return jsonify({"ok": False, "error": "Target group is required."})
    with progress_queue.mutex:
        progress_queue.queue.clear()
    threading.Thread(target=run_add_worker, args=(gp_id, limit, delay), daemon=True).start()
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


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
