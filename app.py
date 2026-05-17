import traceback
import eventlet
eventlet.monkey_patch()

import os
import re
import bleach
import logging
from datetime import datetime
from bson.objectid import ObjectId
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, session, jsonify
from flask_socketio import SocketIO, emit, join_room, disconnect
from pymongo import MongoClient, ASCENDING, DESCENDING
from werkzeug.security import generate_password_hash, check_password_hash
import cloudinary
import cloudinary.uploader

load_dotenv()
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

cloudinary.config(
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key    = os.getenv("CLOUDINARY_API_KEY"),
    api_secret = os.getenv("CLOUDINARY_API_SECRET")
)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    manage_session=False
)

# =========================
# DB
# =========================
MONGO_URI = os.getenv("MONGO_URI")
client = None
db = None
users = None
messages = None

if MONGO_URI:
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        db = client["kadrgram"]
        users = db["users"]
        messages = db["messages"]
        users.create_index("login", unique=True)
        messages.create_index([("sender", ASCENDING), ("receiver", ASCENDING), ("created_at", DESCENDING)])
        messages.create_index([("receiver", ASCENDING), ("read", ASCENDING)])
        print("✅ MONGO CONNECTED SUCCESSFULLY")
    except Exception as e:
        print("❌ MONGO ERROR:", repr(e))
        logging.error(f"DATABASE ERROR: {e}")
        client = None
else:
    print("❌ MONGO_URI is missing")
    client = None

# =========================
# RATE LIMIT
# =========================
last_msg_time = {}

def rate_limit(user_id):
    now = datetime.utcnow()
    last = last_msg_time.get(user_id)
    if last and (now - last).total_seconds() < 0.5:
        return False
    last_msg_time[user_id] = now
    return True

# =========================
# HELPERS
# =========================
def oid(x):
    try:
        return ObjectId(x)
    except Exception:
        return None

def get_user():
    uid = session.get("user_id")
    if not uid or users is None:
        return None
    return users.find_one({"_id": oid(uid)})

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def format_last_seen(last_seen):
    if not last_seen:
        return "не был(а) в сети"
    now = datetime.utcnow()
    diff = (now - last_seen).total_seconds()
    if diff < 60:
        return "был(а) только что"
    if diff < 3600:
        mins = int(diff // 60)
        return f"был(а) {mins} мин. назад"
    if diff < 86400:
        return "был(а) сегодня в " + last_seen.strftime("%H:%M")
    if diff < 172800:
        return "был(а) вчера в " + last_seen.strftime("%H:%M")
    return "был(а) " + last_seen.strftime("%d.%m.%Y")

def serialize_user(u):
    return {
        "id": str(u["_id"]),
        "name": u.get("name", ""),
        "login": u.get("login", ""),
        "online": u.get("online", False),
        "last_seen_str": format_last_seen(u.get("last_seen")),
    }

def serialize_message(m):
    return {
        "id": str(m["_id"]),
        "sender": m["sender"],
        "receiver": m["receiver"],
        "text": m.get("text", ""),
        "image": m.get("image"),
        "read": m.get("read", False),
        "edited": m.get("edited", False),
        "deleted": m.get("deleted", False),
        "reply_to": m.get("reply_to"),
        "reply_text": m.get("reply_text"),
        "reply_sender": m.get("reply_sender"),
        "time": m["created_at"].isoformat()
    }

# =========================
# AUTH
# =========================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if users is None:
            return "DB error", 500
        login_val = request.form.get("login", "").strip()
        pwd = request.form.get("pwd", "")
        user = users.find_one({"login": login_val})
        if user and check_password_hash(user["password"], pwd):
            session["user_id"] = str(user["_id"])
            return redirect("/")
        return render_template("login.html", error="wrong_pass")
    return render_template("login.html")


@app.route("/register", methods=["POST"])
def register():
    if users is None:
        return "DB error", 500
    name = bleach.clean(request.form.get("name", "").strip())
    login_val = bleach.clean(request.form.get("login", "").strip())
    pwd = request.form.get("pwd", "")
    if not name or not login_val or not pwd:
        return render_template("login.html", error="invalid_data")
    if len(pwd) < 8:
        return render_template("login.html", error="invalid_data")
    if not re.match(r'^[A-Za-z0-9]+$', login_val):
        return render_template("login.html", error="invalid_data")
    if users.find_one({"login": login_val}):
        return render_template("login.html", error="login_taken")
    res = users.insert_one({
        "name": name,
        "login": login_val,
        "password": generate_password_hash(pwd),
        "created_at": datetime.utcnow(),
        "online": False,
        "last_seen": None,
    })
    session["user_id"] = str(res.inserted_id)
    return redirect("/")


@app.route("/logout")
def logout():
    uid = session.get("user_id")
    if uid and users is not None:
        users.update_one(
            {"_id": oid(uid)},
            {"$set": {"online": False, "last_seen": datetime.utcnow()}}
        )
    session.clear()
    return redirect("/login")

# =========================
# HOME
# =========================
@app.route("/")
def home():
    user = get_user()
    if not user:
        return redirect("/login")
    my_id = str(user["_id"])
    pinned = user.get("pinned_chats", [])
    raw_users = list(users.find({"_id": {"$ne": user["_id"]}}))
    all_users = []
    for u in raw_users:
        uid_str = str(u["_id"])
        unread = messages.count_documents({
            "sender": uid_str, "receiver": my_id, "read": False
        }) if messages is not None else 0
        su = serialize_user(u)
        su["unread"] = unread
        su["pinned"] = uid_str in pinned
        all_users.append(su)
    # Закреплённые вверху
    all_users.sort(key=lambda u: (0 if u["pinned"] else 1))
    target_user = None
    chat_with = request.args.get("chat_with")
    if chat_with:
        raw_target = users.find_one({"_id": oid(chat_with)})
        if raw_target:
            target_user = serialize_user(raw_target)
            messages.update_many(
                {"sender": chat_with, "receiver": my_id, "read": False},
                {"$set": {"read": True}}
            )
    return render_template(
        "index.html",
        user=serialize_user(user),
        all_users=all_users,
        target_user=target_user,
        my_id=my_id
    )

@app.route("/get_messages")
def get_messages():
    user = get_user()
    if not user:
        return jsonify([])
    other = request.args.get("with")
    if not other:
        return jsonify([])
    my_id = str(user["_id"])

    # Пагинация — before_id позволяет грузить сообщения старше указанного
    before_id = request.args.get("before_id")
    query = {
        "$or": [
            {"sender": my_id, "receiver": other},
            {"sender": other, "receiver": my_id}
        ]
    }
    if before_id:
        query["_id"] = {"$lt": oid(before_id)}

    msgs = list(messages.find(query).sort("created_at", DESCENDING).limit(50))
    msgs.reverse()
    return jsonify([serialize_message(m) for m in msgs])

@app.route("/upload_image", methods=["POST"])
def upload_image():
    user = get_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    if "image" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["image"]
    if f.filename == "" or not allowed_file(f.filename):
        return jsonify({"error": "invalid file"}), 400
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > 5 * 1024 * 1024:
        return jsonify({"error": "too large"}), 400
    try:
        result = cloudinary.uploader.upload(
            f,
            folder="kadrgram",
            transformation=[{"quality": "auto", "fetch_format": "auto"}]
        )
        return jsonify({"url": result["secure_url"]})
    except Exception as e:
        logging.error(f"Cloudinary upload error: {e}")
        return jsonify({"error": "upload failed"}), 500

# =========================
# SEARCH
# =========================
@app.route("/search")
def search():
    user = get_user()
    if not user:
        return redirect("/login")
    query = request.args.get("query", "").strip()
    results = []
    if query and users is not None:
        safe_query = re.escape(query)
        raw = list(users.find({
            "_id": {"$ne": user["_id"]},
            "$or": [
                {"name":  {"$regex": safe_query, "$options": "i"}},
                {"login": {"$regex": safe_query, "$options": "i"}}
            ]
        }).limit(20))
        results = [serialize_user(u) for u in raw]
    return render_template("search.html", results=results)

@app.route("/pin_chat", methods=["POST"])
def pin_chat():
    user = get_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json()
    chat_id = data.get("chat_id")
    action = data.get("action")  # "pin" или "unpin"
    if not chat_id:
        return jsonify({"error": "no chat_id"}), 400
    if action == "pin":
        users.update_one(
            {"_id": user["_id"]},
            {"$addToSet": {"pinned_chats": chat_id}}
        )
    else:
        users.update_one(
            {"_id": user["_id"]},
            {"$pull": {"pinned_chats": chat_id}}
        )
    return jsonify({"ok": True})

# =========================
# SOCKET.IO
# =========================
@socketio.on("connect")
def on_connect():
    uid = session.get("user_id")
    if not uid:
        disconnect()
        return
    if users is not None:
        users.update_one({"_id": oid(uid)}, {"$set": {"online": True}})
    join_room(uid)
    emit("user_online", {"user_id": uid}, broadcast=True, include_self=False)


@socketio.on("disconnect")
def on_disconnect():
    uid = session.get("user_id")
    if uid and users is not None:
        now = datetime.utcnow()
        users.update_one({"_id": oid(uid)}, {"$set": {"online": False, "last_seen": now}})
        u = users.find_one({"_id": oid(uid)})
        last_seen_str = format_last_seen(u.get("last_seen")) if u else "был(а) только что"
        emit("user_offline", {"user_id": uid, "last_seen_str": last_seen_str},
             broadcast=True, include_self=False)


@socketio.on("typing")
def on_typing(data):
    uid = session.get("user_id")
    if not uid:
        return
    to_id = data.get("receiver_id")
    if not to_id or to_id == uid:
        return
    emit("typing", {"sender_id": uid, "typing": bool(data.get("typing", False))}, room=to_id)


@socketio.on("new_message")
def new_message(data):
    user = get_user()
    if not user:
        return
    my_id = str(user["_id"])
    to_id = data.get("receiver_id")
    text = bleach.clean(data.get("text", "").strip())
    image = data.get("image")
    reply_to = data.get("reply_to")

    if not to_id or (not text and not image):
        return
    if my_id == to_id:
        return
    if users is None or not users.find_one({"_id": oid(to_id)}):
        return
    if not rate_limit(my_id):
        emit("error", {"message": "Слишком быстро, подождите"}, room=my_id)
        return

    now = datetime.utcnow()
    msg = {
        "sender": my_id,
        "receiver": to_id,
        "text": text,
        "image": image,
        "read": False,
        "edited": False,
        "deleted": False,
        "created_at": now,
    }

    if reply_to:
        orig = messages.find_one({"_id": oid(reply_to)})
        if orig and not orig.get("deleted"):
            orig_sender = users.find_one({"_id": oid(orig["sender"])})
            msg["reply_to"] = reply_to
            msg["reply_text"] = orig.get("text", "📷 Фото")[:80]
            msg["reply_sender"] = orig_sender.get("name", "") if orig_sender else ""

    result = messages.insert_one(msg)
    msg["_id"] = result.inserted_id

    payload = serialize_message(msg)
    emit("receive_message", payload, room=to_id)
    emit("receive_message", payload, room=my_id)
    emit("typing", {"sender_id": my_id, "typing": False}, room=to_id)


@socketio.on("edit_message")
def edit_message(data):
    user = get_user()
    if not user:
        return
    my_id = str(user["_id"])
    msg_id = data.get("msg_id")
    new_text = bleach.clean(data.get("text", "").strip())
    if not msg_id or not new_text:
        return
    msg = messages.find_one({"_id": oid(msg_id), "sender": my_id})
    if not msg or msg.get("deleted"):
        return
    messages.update_one({"_id": oid(msg_id)}, {"$set": {"text": new_text, "edited": True}})
    payload = {"msg_id": msg_id, "text": new_text}
    emit("message_edited", payload, room=my_id)
    emit("message_edited", payload, room=msg["receiver"])


@socketio.on("delete_message")
def delete_message(data):
    user = get_user()
    if not user:
        return
    my_id = str(user["_id"])
    msg_id = data.get("msg_id")
    if not msg_id:
        return
    msg = messages.find_one({"_id": oid(msg_id), "sender": my_id})
    if not msg:
        return
    messages.update_one({"_id": oid(msg_id)}, {"$set": {"deleted": True, "text": ""}})
    payload = {"msg_id": msg_id}
    emit("message_deleted", payload, room=my_id)
    emit("message_deleted", payload, room=msg["receiver"])


@socketio.on("mark_read")
def mark_read(data):
    user = get_user()
    if not user:
        return
    my_id = str(user["_id"])
    sender_id = data.get("sender_id")
    if not sender_id:
        return
    messages.update_many(
        {"sender": sender_id, "receiver": my_id, "read": False},
        {"$set": {"read": True}}
    )
    emit("messages_read", {"by": my_id}, room=sender_id)


@app.errorhandler(Exception)
def handle_error(e):
    print(traceback.format_exc())
    return "SERVER ERROR", 500


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
