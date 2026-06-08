import eventlet
eventlet.monkey_patch()

import traceback
import os
import re
import bleach
import logging
from collections import Counter
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

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    manage_session=False
)

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

# =====================================================
# DATABASE
# =====================================================

MONGO_URI = os.getenv("MONGO_URI")
client = None
users = None
messages = None
mongo_ok = False

try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    db = client["kadrgram"]
    users = db["users"]
    messages = db["messages"]
    users.create_index("login", unique=True)
    messages.create_index([("sender", ASCENDING), ("receiver", ASCENDING), ("_id", DESCENDING)])
    messages.create_index([("receiver", ASCENDING), ("read", ASCENDING)])
    messages.create_index([("_id", DESCENDING)])
    mongo_ok = True
    print("✅ Mongo connected")
except Exception as e:
    print("❌ Mongo error:", e)
    logging.error(e)

# =====================================================
# HELPERS
# =====================================================

last_msg_time = {}


def oid(x):
    try:
        return ObjectId(x)
    except Exception:
        return None


def allowed_file(filename):
    if not filename:
        return False
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_user():
    if not mongo_ok or users is None:
        return None
    uid = session.get("user_id")
    if not uid:
        return None
    try:
        return users.find_one({"_id": oid(uid)})
    except Exception:
        return None


def rate_limit(user_id):
    now = datetime.utcnow()
    last = last_msg_time.get(user_id)
    if last and (now - last).total_seconds() < 0.5:
        return False
    last_msg_time[user_id] = now
    return True


def format_last_seen(last_seen):
    if not last_seen:
        return "не был(а) в сети"
    now = datetime.utcnow()
    diff = (now - last_seen).total_seconds()
    if diff < 60:
        return "был(а) только что"
    if diff < 3600:
        return f"был(а) {int(diff // 60)} мин. назад"
    if diff < 86400:
        return "был(а) сегодня в " + last_seen.strftime("%H:%M")
    if diff < 172800:
        return "был(а) вчера в " + last_seen.strftime("%H:%M")
    return "был(а) " + last_seen.strftime("%d.%m.%Y")


def serialize_user(u):
    last_seen = u.get("last_seen")
    last_seen_iso = (last_seen.isoformat() + "Z") if last_seen else None
    return {
        "id": str(u["_id"]),
        "name": u.get("name", ""),
        "login": u.get("login", ""),
        "avatar": u.get("avatar", ""),
        "online": bool(u.get("online", False)),
        "last_seen_iso": last_seen_iso,
        "last_seen_str": format_last_seen(last_seen),
        "pinned": False,
    }


def serialize_message(m):
    reactions = m.get("reactions", {})
    reaction_counts = Counter(reactions.values())
    return {
        "id": str(m["_id"]),
        "sender": m.get("sender", ""),
        "receiver": m.get("receiver", ""),
        "text": m.get("text", ""),
        "image": m.get("image"),
        "read": m.get("read", False),
        "edited": m.get("edited", False),
        "deleted": m.get("deleted", False),
        "reply_to": m.get("reply_to"),
        "reply_text": m.get("reply_text"),
        "reply_sender": m.get("reply_sender"),
        "reactions": dict(reaction_counts),
        "time": m["created_at"].isoformat() if m.get("created_at") else datetime.utcnow().isoformat()
    }

# =====================================================
# AUTH
# =====================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if not mongo_ok or users is None:
        return "База данных недоступна", 503

    if request.method == "POST":
        login_val = request.form.get("login", "").strip()
        pwd = request.form.get("pwd", "")
        if not login_val or not pwd:
            return render_template("login.html", error="wrong_pass")
        user = users.find_one({"login": login_val})
        if user and check_password_hash(user["password"], pwd):
            session["user_id"] = str(user["_id"])
            return redirect("/")
        return render_template("login.html", error="wrong_pass")
    return render_template("login.html")


@app.route("/register", methods=["POST"])
def register():
    if not mongo_ok or users is None:
        return "База данных недоступна", 503

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
        "avatar": "",
        "pinned_chats": []
    })
    session["user_id"] = str(res.inserted_id)
    return redirect("/")


@app.route("/logout")
def logout():
    uid = session.get("user_id")
    if uid and mongo_ok and users is not None:
        try:
            users.update_one(
                {"_id": oid(uid)},
                {"$set": {"online": False, "last_seen": datetime.utcnow()}}
            )
        except Exception:
            pass
    session.clear()
    return redirect("/login")

# =====================================================
# HOME
# =====================================================

@app.route("/")
def home():
    user = get_user()
    if not user:
        return redirect("/login")

    my_id = str(user["_id"])
    pinned_ids = set(user.get("pinned_chats", []))

    unread_by_sender = {}
    try:
        pipeline = [
            {"$match": {"receiver": my_id, "read": False}},
            {"$group": {"_id": "$sender", "unread": {"$sum": 1}}}
        ]
        for row in messages.aggregate(pipeline):
            unread_by_sender[str(row["_id"])] = int(row.get("unread", 0))
    except Exception as e:
        logging.error(f"Unread aggregate error: {e}")

    raw_users = list(users.find({"_id": {"$ne": user["_id"]}}))
    all_users = []
    for u in raw_users:
        uid_str = str(u["_id"])
        su = serialize_user(u)
        su["unread"] = unread_by_sender.get(uid_str, 0)
        su["pinned"] = uid_str in pinned_ids
        all_users.append(su)

    all_users.sort(key=lambda u: (0 if u["pinned"] else 1))

    target_user = None
    chat_with = request.args.get("chat_with")
    if chat_with and mongo_ok and messages is not None:
        try:
            raw_target = users.find_one({"_id": oid(chat_with)})
            if raw_target:
                target_user = serialize_user(raw_target)
                messages.update_many(
                    {"sender": chat_with, "receiver": my_id, "read": False},
                    {"$set": {"read": True}}
                )
        except Exception as e:
            logging.error(f"Chat open error: {e}")

    return render_template(
        "index.html",
        user=serialize_user(user),
        all_users=all_users,
        target_user=target_user,
        my_id=my_id
    )

# =====================================================
# PROFILE
# =====================================================

@app.route("/profile")
def profile():
    user = get_user()
    if not user:
        return redirect("/login")
    return render_template("profile.html", user=serialize_user(user))


@app.route("/edit_profile")
def edit_profile():
    user = get_user()
    if not user:
        return redirect("/login")
    return render_template("edit_profile.html", user=serialize_user(user))


@app.route("/update_profile", methods=["POST"])
def update_profile():
    user = get_user()
    if not user:
        return jsonify({"success": False, "message": "Не авторизован"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Нет данных"}), 400

    field = data.get("field")
    value = data.get("value", "")
    if isinstance(value, str):
        value = value.strip()

    if not value:
        return jsonify({"success": False, "message": "Поле пустое"})

    update_data = {}

    if field == "name":
        if len(value) < 2 or len(value) > 30:
            return jsonify({"success": False, "message": "Имя от 2 до 30 символов"})
        update_data["name"] = bleach.clean(value)

    elif field == "login":
        if not re.match(r'^[A-Za-z0-9]+$', value):
            return jsonify({"success": False, "message": "Только буквы и цифры"})
        if len(value) < 3 or len(value) > 24:
            return jsonify({"success": False, "message": "Логин от 3 до 24 символов"})
        if users.find_one({"login": value, "_id": {"$ne": user["_id"]}}):
            return jsonify({"success": False, "message": "Логин занят"})
        update_data["login"] = value

    elif field == "password":
        if len(value) < 8:
            return jsonify({"success": False, "message": "Пароль минимум 8 символов"})
        update_data["password"] = generate_password_hash(value)

    else:
        return jsonify({"success": False, "message": "Неверное поле"})

    users.update_one({"_id": user["_id"]}, {"$set": update_data})
    return jsonify({"success": True})


@app.route("/verify_password", methods=["POST"])
def verify_password():
    user = get_user()
    if not user:
        return jsonify({"valid": False}), 401
    data = request.get_json()
    if not data:
        return jsonify({"valid": False}), 400
    pwd = data.get("password", "")
    valid = check_password_hash(user["password"], pwd)
    return jsonify({"valid": valid})

# =====================================================
# AVATAR
# =====================================================

@app.route("/upload_avatar", methods=["POST"])
def upload_avatar():
    if "user_id" not in session:
        return jsonify({"success": False, "message": "Не авторизован"}), 401
    if "image" not in request.files:
        return jsonify({"success": False, "message": "Нет файла"}), 400

    f = request.files["image"]
    if not f or not f.filename or not allowed_file(f.filename):
        return jsonify({"success": False, "message": "Недопустимый файл"}), 400

    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > 5 * 1024 * 1024:
        return jsonify({"success": False, "message": "Файл больше 5МБ"}), 400

    try:
        result = cloudinary.uploader.upload(
            f,
            folder="kadrgram_avatars",
            transformation=[{"width": 200, "height": 200, "crop": "fill", "gravity": "face"}]
        )
        url = result["secure_url"]
        users.update_one({"_id": oid(session["user_id"])}, {"$set": {"avatar": url}})
        return jsonify({"success": True, "url": url})
    except Exception as e:
        logging.error(f"Avatar upload error: {e}")
        return jsonify({"success": False, "message": "Ошибка загрузки"}), 500


@app.route("/delete_avatar", methods=["POST"])
def delete_avatar():
    if "user_id" not in session:
        return jsonify({"success": False, "message": "Не авторизован"}), 401
    try:
        users.update_one({"_id": oid(session["user_id"])}, {"$set": {"avatar": ""}})
        return jsonify({"success": True})
    except Exception as e:
        logging.error(e)
        return jsonify({"success": False, "message": "Ошибка"}), 500

# =====================================================
# MESSAGES
# =====================================================

@app.route("/get_messages")
def get_messages():
    user = get_user()
    if not user or not mongo_ok or messages is None:
        return jsonify([])

    other = request.args.get("with")
    if not other:
        return jsonify([])

    my_id = str(user["_id"])
    before_id = request.args.get("before_id")

    query = {
        "$or": [
            {"sender": my_id, "receiver": other},
            {"sender": other, "receiver": my_id}
        ]
    }
    if before_id:
        b_oid = oid(before_id)
        if b_oid:
            query["_id"] = {"$lt": b_oid}

    try:
        msgs = list(messages.find(query).sort("_id", DESCENDING).limit(50))
        msgs.reverse()
        return jsonify([serialize_message(m) for m in msgs])
    except Exception as e:
        logging.error(e)
        return jsonify([])

# =====================================================
# IMAGE UPLOAD
# =====================================================

@app.route("/upload_image", methods=["POST"])
def upload_image():
    user = get_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    if "image" not in request.files:
        return jsonify({"error": "no file"}), 400

    f = request.files["image"]
    if not f or not f.filename or not allowed_file(f.filename):
        return jsonify({"error": "invalid file"}), 400

    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > 10 * 1024 * 1024:
        return jsonify({"error": "too large"}), 400

    try:
        result = cloudinary.uploader.upload(f, folder="kadrgram_messages")
        return jsonify({"url": result.get("secure_url")})
    except Exception as e:
        logging.error(e)
        return jsonify({"error": "upload failed"}), 500

# =====================================================
# SEARCH
# =====================================================

@app.route("/search")
def search():
    user = get_user()
    if not user:
        return redirect("/login")

    query = request.args.get("query", "").strip()
    results = []

    if query and mongo_ok and users is not None:
        try:
            safe_query = re.escape(query)
            raw = list(users.find({
                "_id": {"$ne": user["_id"]},
                "$or": [
                    {"name":  {"$regex": safe_query, "$options": "i"}},
                    {"login": {"$regex": safe_query, "$options": "i"}}
                ]
            }).limit(20))
            results = [serialize_user(u) for u in raw]
        except Exception as e:
            logging.error(e)

    return render_template("search.html", results=results)

# =====================================================
# PIN CHAT
# =====================================================

@app.route("/pin_chat", methods=["POST"])
def pin_chat():
    user = get_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "no data"}), 400

    chat_id = data.get("chat_id")
    action = data.get("action")

    if not chat_id:
        return jsonify({"error": "no chat_id"}), 400

    try:
        if action == "pin":
            users.update_one({"_id": user["_id"]}, {"$addToSet": {"pinned_chats": chat_id}})
        else:
            users.update_one({"_id": user["_id"]}, {"$pull": {"pinned_chats": chat_id}})
        return jsonify({"ok": True})
    except Exception as e:
        logging.error(e)
        return jsonify({"error": "db error"}), 500

# =====================================================
# FAVICON
# =====================================================

@app.route("/favicon.ico")
def favicon():
    return "", 204

# =====================================================
# SOCKET.IO
# =====================================================

@socketio.on("connect")
def on_connect():
    uid = session.get("user_id")
    if not uid or not mongo_ok or users is None:
        disconnect()
        return
    try:
        users.update_one({"_id": oid(uid)}, {"$set": {"online": True, "last_seen": None}})
        join_room(uid)
        emit("user_online", {"user_id": uid}, broadcast=True, include_self=False)
    except Exception as e:
        logging.error(e)


@socketio.on("disconnect")
def on_disconnect():
    uid = session.get("user_id")
    if not uid or not mongo_ok or users is None:
        return
    try:
        now = datetime.utcnow()
        users.update_one({"_id": oid(uid)}, {"$set": {"online": False, "last_seen": now}})
        last_seen_iso = now.isoformat() + "Z"
        last_seen_str = format_last_seen(now)
        emit("user_offline", {
            "user_id": uid,
            "last_seen_str": last_seen_str,
            "last_seen_iso": last_seen_iso
        }, broadcast=True, include_self=False)
    except Exception as e:
        logging.error(e)


@socketio.on("typing")
def on_typing(data):
    uid = session.get("user_id")
    if not uid:
        return
    receiver_id = data.get("receiver_id")
    if not receiver_id or receiver_id == uid:
        return
    emit("typing", {"sender_id": uid, "typing": bool(data.get("typing"))}, room=receiver_id)


@socketio.on("new_message")
def new_message(data):
    user = get_user()
    if not user or not mongo_ok or messages is None:
        return

    my_id = str(user["_id"])
    to_id = data.get("receiver_id")
    text = bleach.clean(data.get("text", "").strip())
    image = data.get("image")
    reply_to = data.get("reply_to")

    if not to_id or (not text and not image):
        return
    if len(text) > 4000:
        return
    if not rate_limit(my_id):
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
        "reactions": {}
    }

    if reply_to:
        try:
            orig = messages.find_one({"_id": oid(reply_to)})
            if orig and not orig.get("deleted"):
                orig_sender = users.find_one({"_id": oid(orig["sender"])})
                msg["reply_to"] = reply_to
                msg["reply_text"] = orig.get("text", "📷 Фото")[:80]
                msg["reply_sender"] = orig_sender.get("name", "") if orig_sender else ""
        except Exception:
            pass

    try:
        result = messages.insert_one(msg)
        msg["_id"] = result.inserted_id
        payload = serialize_message(msg)
        emit("receive_message", payload, room=my_id)
        emit("receive_message", payload, room=to_id)
        emit("typing", {"sender_id": my_id, "typing": False}, room=to_id)
    except Exception as e:
        logging.error(e)


@socketio.on("edit_message")
def edit_message(data):
    user = get_user()
    if not user or not mongo_ok or messages is None:
        return
    my_id = str(user["_id"])
    msg_id = data.get("msg_id")
    new_text = bleach.clean(data.get("text", "").strip())
    if not msg_id or not new_text:
        return
    try:
        msg = messages.find_one({"_id": oid(msg_id), "sender": my_id})
        if not msg or msg.get("deleted"):
            return
        messages.update_one({"_id": oid(msg_id)}, {"$set": {"text": new_text, "edited": True}})
        payload = {"msg_id": msg_id, "text": new_text}
        emit("message_edited", payload, room=my_id)
        emit("message_edited", payload, room=msg["receiver"])
    except Exception as e:
        logging.error(e)


@socketio.on("delete_message")
def delete_message(data):
    user = get_user()
    if not user or not mongo_ok or messages is None:
        return
    my_id = str(user["_id"])
    msg_id = data.get("msg_id")
    if not msg_id:
        return
    try:
        msg = messages.find_one({"_id": oid(msg_id), "sender": my_id})
        if not msg:
            return
        messages.update_one({"_id": oid(msg_id)}, {"$set": {"deleted": True, "text": ""}})
        emit("message_deleted", {"msg_id": msg_id}, room=my_id)
        emit("message_deleted", {"msg_id": msg_id}, room=msg["receiver"])
    except Exception as e:
        logging.error(e)


@socketio.on("mark_read")
def mark_read(data):
    user = get_user()
    if not user or not mongo_ok or messages is None:
        return
    my_id = str(user["_id"])
    sender_id = data.get("sender_id")
    if not sender_id:
        return
    try:
        messages.update_many(
            {"sender": sender_id, "receiver": my_id, "read": False},
            {"$set": {"read": True}}
        )
        emit("messages_read", {"by": my_id}, room=sender_id)
    except Exception as e:
        logging.error(e)


@socketio.on("toggle_reaction")
def toggle_reaction(data):
    user_id = session.get("user_id")
    if not user_id or not mongo_ok or messages is None:
        return

    msg_id = data.get("message_id")
    emoji = data.get("emoji")
    allowed = ["👍", "❤️", "🔥", "😂", "👏", "😮", "😢"]
    if emoji not in allowed:
        return

    try:
        message = messages.find_one({"_id": oid(msg_id)})
        if not message:
            return

        reactions = message.get("reactions", {})
        if reactions.get(user_id) == emoji:
            del reactions[user_id]
        else:
            reactions[user_id] = emoji

        messages.update_one({"_id": oid(msg_id)}, {"$set": {"reactions": reactions}})
        reaction_counts = Counter(reactions.values())

        payload = {"message_id": msg_id, "reaction_counts": dict(reaction_counts)}

        sender_id = message.get("sender")
        receiver_id = message.get("receiver")
        if sender_id:
            emit("update_reactions", payload, room=str(sender_id))
        if receiver_id and receiver_id != sender_id:
            emit("update_reactions", payload, room=str(receiver_id))
    except Exception as e:
        logging.error(e)

# =====================================================
# ERROR HANDLER
# =====================================================

@app.errorhandler(404)
def not_found(e):
    return redirect("/")


@app.errorhandler(Exception)
def handle_error(e):
    logging.error(traceback.format_exc())
    return "SERVER ERROR", 500


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, use_reloader=False)
