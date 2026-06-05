from flask import Flask, render_template, request, jsonify, session, Response
from pyrogram import Client as PyroClient
from pyrogram.errors import (
    FloodWait, PeerFlood, UserPrivacyRestricted, UserAlreadyParticipant,
    UserChannelsTooMuch, UserNotMutualContact, SessionPasswordNeeded
)
import os, queue, threading, json, time, uuid, asyncio, random

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tadder-secret-key-2024")

CONFIG_FILE  = "config.json"
SESSION_FILE = "pyro_main"
ACCOUNTS_FILE = "accounts.json"

progress_queue = queue.Queue()
add_running = False

# ── Async helper ──────────────────────────────────────────────────────────────

def _run(coro):
    """Run an async coroutine synchronously. Creates a fresh event loop per call (thread-safe)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

# ── Credentials ────────────────────────────────────────────────────────────────

def load_credentials():
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

def make_client(session_name):
    api_id, api_hash = load_credentials()
    return PyroClient(session_name, api_id=api_id, api_hash=api_hash, workdir=".")

# ── Account helpers ────────────────────────────────────────────────────────────

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

async def resolve_chat(client, identifier):
    """Resolve a chat from username, ID, or invite link."""
    ident = identifier
    if str(ident).lstrip("-").isdigit():
        ident = int(ident)
    try:
        return await client.get_chat(ident)
    except Exception:
        pass
    try:
        result = await client.join_chat(str(identifier))
        return result
    except Exception as e:
        if "already" in str(e).lower() or "participant" in str(e).lower():
            async for dialog in client.get_dialogs():
                if dialog.chat and dialog.chat.invite_link and str(identifier) in dialog.chat.invite_link:
                    return dialog.chat
        raise Exception(f"Could not resolve group '{identifier}': {e}")

def remove_session_files(session_name):
    for ext in [".session", ".session-journal"]:
        f = session_name + ext
        if os.path.exists(f):
            os.remove(f)

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
    return jsonify({"api_id": str(api_id) if api_id else "", "api_hash": api_hash, "source": source})

@app.route("/api/settings", methods=["POST"])
def save_settings():
    data = request.get_json()
    api_id  = str(data.get("api_id", "")).strip()
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

# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/api/check-auth")
def check_auth():
    api_id, api_hash = load_credentials()
    if not api_id or not api_hash:
        return jsonify({"authenticated": False, "error": "missing_credentials"})
    async def _check():
        client = make_client(SESSION_FILE)
        try:
            await client.connect()
            me = await client.get_me()
            await client.disconnect()
            return {"authenticated": True, "name": me.first_name or "", "username": me.username or ""}
        except Exception:
            try: await client.disconnect()
            except Exception: pass
            return {"authenticated": False}
    try:
        return jsonify(_run(_check()))
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
    async def _send():
        client = make_client(SESSION_FILE)
        await client.connect()
        try:
            sent = await client.send_code(phone)
            return sent.phone_code_hash
        finally:
            await client.disconnect()
    try:
        phone_code_hash = _run(_send())
        session["phone"] = phone
        session["phone_code_hash"] = phone_code_hash
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/verify-code", methods=["POST"])
def verify_code():
    data = request.get_json()
    code     = data.get("code", "").strip()
    password = data.get("password", "").strip()
    phone    = session.get("phone")
    phone_code_hash = session.get("phone_code_hash")
    if not phone or not phone_code_hash:
        return jsonify({"ok": False, "error": "Session expired. Please send code again."})
    async def _verify():
        client = make_client(SESSION_FILE)
        await client.connect()
        try:
            try:
                user = await client.sign_in(phone, phone_code_hash, code)
                return user, False
            except SessionPasswordNeeded:
                if not password:
                    return None, True
                user = await client.check_password(password)
                return user, False
        finally:
            await client.disconnect()
    try:
        user, needs_pw = _run(_verify())
        if needs_pw:
            return jsonify({"ok": False, "needs_password": True})
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
        except Exception: pass
        try: await client.disconnect()
        except Exception: pass
    _run(_logout())
    remove_session_files(SESSION_FILE)
    session.clear()
    return jsonify({"ok": True})

# ── Multi-account management ──────────────────────────────────────────────────

async def _account_status(acc):
    client = make_client(acc["session"])
    try:
        await client.connect()
        me = await client.get_me()
        await client.disconnect()
        return {**acc, "logged_in": True, "display_name": me.first_name or "", "username": me.username or ""}
    except Exception:
        try: await client.disconnect()
        except Exception: pass
        return {**acc, "logged_in": False}

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
    acc_id = f"acc_{uuid.uuid4().hex[:8]}"
    new_acc = {"id": acc_id, "name": f"Account {len(extras) + 2}", "session": f"pyro_{acc_id}"}
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
            await client.disconnect()
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
    data = request.get_json()
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"ok": False, "error": "Phone number required."})
    async def _send():
        client = make_client(acc["session"])
        await client.connect()
        try:
            sent = await client.send_code(phone)
            return sent.phone_code_hash
        finally:
            await client.disconnect()
    try:
        phone_code_hash = _run(_send())
        session[f"phone_{acc_id}"] = phone
        session[f"hash_{acc_id}"]  = phone_code_hash
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/accounts/<acc_id>/verify-code", methods=["POST"])
def acc_verify_code(acc_id):
    acc = next((a for a in get_all_accounts() if a["id"] == acc_id), None)
    if not acc:
        return jsonify({"ok": False, "error": "Account not found."})
    data = request.get_json()
    code     = data.get("code", "").strip()
    password = data.get("password", "").strip()
    phone    = session.get(f"phone_{acc_id}")
    phone_code_hash = session.get(f"hash_{acc_id}")
    if not phone or not phone_code_hash:
        return jsonify({"ok": False, "error": "Session expired. Please send code again."})
    async def _verify():
        client = make_client(acc["session"])
        await client.connect()
        try:
            try:
                user = await client.sign_in(phone, phone_code_hash, code)
                return user, False
            except SessionPasswordNeeded:
                if not password:
                    return None, True
                user = await client.check_password(password)
                return user, False
        finally:
            await client.disconnect()
    try:
        user, needs_pw = _run(_verify())
        if needs_pw:
            return jsonify({"ok": False, "needs_password": True})
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
            await client.disconnect()
        except Exception: pass
    _run(_logout())
    remove_session_files(acc["session"])
    if acc_id == "main":
        session.clear()
    return jsonify({"ok": True})

# ── Groups & Members ──────────────────────────────────────────────────────────

@app.route("/api/groups")
def groups():
    async def _groups():
        client = make_client(SESSION_FILE)
        await client.connect()
        try:
            me = await client.get_me()
            if not me:
                return None
            group_list = []
            async for dialog in client.get_dialogs():
                chat = dialog.chat
                if chat.type.value in ("group", "supergroup"):
                    group_list.append({
                        "id": chat.id,
                        "title": chat.title,
                        "username": chat.username,
                        "members": chat.members_count or "?"
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
    data = request.get_json()
    identifier = data.get("identifier")
    async def _extract():
        client = make_client(SESSION_FILE)
        await client.connect()
        try:
            me = await client.get_me()
            my_id = me.id
            chat = await resolve_chat(client, identifier)
            saved = []
            async for member in client.get_chat_members(chat.id):
                user = member.user
                if not user or user.is_bot:
                    continue
                if user.id == my_id:
                    continue
                entry = user.username if user.username else f"id:{user.id}"
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

# ── Add Members ────────────────────────────────────────────────────────────────

async def _account_worker_async(acc, gp_id, users, delay_min, delay_max, shared):
    """Async worker for one Pyrogram account."""
    label = acc.get("name", acc["id"])
    client = make_client(acc["session"])
    await client.connect()
    try:
        me = await client.get_me()
        if not me:
            progress_queue.put({"type": "warn", "message": f"[{label}] Not logged in — skipping."})
            return
        try:
            chat = await resolve_chat(client, gp_id)
        except Exception as e:
            progress_queue.put({"type": "error", "message": f"[{label}] Could not resolve group: {e}"})
            return
        progress_queue.put({"type": "info",
                            "message": f"[{label}] Resolved '{chat.title}'. Adding {len(users)} members."})
        i = 0
        while i < len(users):
            userr = users[i]
            retry = False
            lookup = int(userr[3:]) if userr.startswith("id:") else userr
            try:
                await client.add_chat_members(chat.id, lookup)
                with shared["lock"]:
                    shared["added"]   += 1
                    shared["current"] += 1
                    a, f, c, t = shared["added"], shared["failed"], shared["current"], shared["total"]
                progress_queue.put({"type": "progress", "current": c, "total": t,
                                    "added": a, "failed": f, "user": userr,
                                    "status": "added", "account": label})
                if i < len(users) - 1:
                    await asyncio.sleep(random.uniform(delay_min, delay_max))
            except FloodWait as e:
                wait = e.value
                progress_queue.put({"type": "flood",
                                    "message": f"[{label}] FloodWait {wait}s — waiting then retrying @{userr}..."})
                # Countdown during wait
                remaining = wait
                while remaining > 0:
                    progress_queue.put({"type": "countdown", "seconds": remaining})
                    chunk = min(5, remaining)
                    await asyncio.sleep(chunk)
                    remaining -= chunk
                progress_queue.put({"type": "countdown", "seconds": 0})
                retry = True
            except PeerFlood:
                progress_queue.put({"type": "flood",
                                    "message": f"[{label}] PeerFlood — account restricted by Telegram. Stopping this account, others continue."})
                return
            except UserPrivacyRestricted:
                with shared["lock"]:
                    shared["failed"]  += 1
                    shared["current"] += 1
                    a, f, c, t = shared["added"], shared["failed"], shared["current"], shared["total"]
                progress_queue.put({"type": "progress", "current": c, "total": t,
                                    "added": a, "failed": f, "user": userr,
                                    "status": "privacy", "account": label})
            except UserAlreadyParticipant:
                with shared["lock"]:
                    shared["added"]   += 1
                    shared["current"] += 1
                    a, f, c, t = shared["added"], shared["failed"], shared["current"], shared["total"]
                progress_queue.put({"type": "progress", "current": c, "total": t,
                                    "added": a, "failed": f, "user": userr,
                                    "status": "added", "account": label})
            except UserChannelsTooMuch:
                with shared["lock"]:
                    shared["failed"]  += 1
                    shared["current"] += 1
                    a, f, c, t = shared["added"], shared["failed"], shared["current"], shared["total"]
                progress_queue.put({"type": "progress", "current": c, "total": t,
                                    "added": a, "failed": f, "user": userr,
                                    "status": "too_many", "account": label})
            except UserNotMutualContact:
                with shared["lock"]:
                    shared["failed"]  += 1
                    shared["current"] += 1
                    a, f, c, t = shared["added"], shared["failed"], shared["current"], shared["total"]
                progress_queue.put({"type": "progress", "current": c, "total": t,
                                    "added": a, "failed": f, "user": userr,
                                    "status": "not_contact", "account": label})
            except Exception as ex:
                with shared["lock"]:
                    shared["failed"]  += 1
                    shared["current"] += 1
                    a, f, c, t = shared["added"], shared["failed"], shared["current"], shared["total"]
                progress_queue.put({"type": "progress", "current": c, "total": t,
                                    "added": a, "failed": f, "user": userr,
                                    "status": "error", "detail": str(ex), "account": label})
            if not retry:
                i += 1
    finally:
        await client.disconnect()


def run_account_worker(acc, gp_id, users, delay_min, delay_max, shared):
    """Thread target: runs the async account worker with its own event loop."""
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
    delay_min = max(1, delay)
    delay_max = delay_min + 5
    async def _active_accounts():
        active = []
        for acc in get_all_accounts():
            client = make_client(acc["session"])
            try:
                await client.connect()
                me = await client.get_me()
                await client.disconnect()
                if me:
                    active.append(acc)
            except Exception:
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
        n = len(active)
        per = f"  (~{round(total/n)} per account)" if n > 1 else ""
        progress_queue.put({"type": "info",
                            "message": f"Using {n} account{'s' if n > 1 else ''} | {total} members | delay {delay_min}–{delay_max}s{per}."})
        chunks = [users[i::n] for i in range(n)]
        shared = {"lock": threading.Lock(), "added": 0, "failed": 0, "current": 0, "total": total}
        threads = [
            threading.Thread(target=run_account_worker,
                             args=(acc, gp_id, chunk, delay_min, delay_max, shared),
                             daemon=True)
            for acc, chunk in zip(active, chunks)
        ]
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
    data   = request.get_json()
    gp_id  = data.get("group", "").strip()
    limit  = data.get("limit", 0)
    delay  = data.get("delay", 15)
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
