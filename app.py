import traceback  # ИСПРАВЛЕНО: перенесён до всего остального
import eventlet
eventlet.monkey_patch()  # ИСПРАВЛЕНО: сразу после eventlet, до других импортов

import os
import bleach
import logging
from datetime import datetime
from bson.objectid import ObjectId
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, session, jsonify, url_for
from flask_socketio import SocketIO, emit, join_room, disconnect
from pymongo import MongoClient, ASCENDING, DESCENDING
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

# ИСПРАВЛЕНО: async_mode="eventlet" — должен совпадать с monkey_patch
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

        # ИСПРАВЛЕНО: добавлены индексы для производительности
        users.create_index("login", unique=True)
        messages.create_index([("sender", ASCENDING), ("receiver", ASCENDING), ("created_at", DESCENDING)])
        messages.create_index([("receiver", ASCENDING), ("read", ASCENDING)])

        print("✅ MONGO CONNECTED SUCCESSFULLY")
        logging.info("SUCCESS: Connected to MongoDB Atlas!")

    except Exception as e:
        print("❌ MONGO ERROR:", repr(e))
        logging.error(f"DATABASE ERROR: {e}")
        client = None
else:
    print("❌ MONGO_URI is missing in environment variables")
    client = None

# =========================
# SIMPLE RATE LIMIT
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
    except Exception:  # ИСПРАВЛЕНО: голый except заменён на Exception
        return None

def get_user():
    uid = session.get("user_id")
    if not uid or users is None:
        return None
    return users.find_one({"_id": oid(uid)})

def serialize_user(u):
    """Конвертирует ObjectId → str для безопасной передачи в шаблон."""
    return {
        "id": str(u["_id"]),
        "name": u.get("name", ""),
        "login": u.get("login", ""),
        "online": u.get("online", False),
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
    if users is None:  # ИСПРАВЛЕНО: было `if not users` — ложный False при пустой коллекции
        return "DB error", 500

    name = bleach.clean(request.form.get("name", "").strip())
    login_val = bleach.clean(request.form.get("login", "").strip())
    pwd = request.form.get("pwd", "")

    # ИСПРАВЛЕНО: добавлена серверная валидация обязательных полей
    if not name or not login_val or not pwd:
        return render_template("login.html", error="invalid_data")

    if len(pwd) < 8:
        return render_template("login.html", error="invalid_data")

    import re
    if not re.match(r'^[A-Za-z0-9]+$', login_val):
        return render_template("login.html", error="invalid_data")

    if users.find_one({"login": login_val}):
        return render_template("login.html", error="login_taken")

    user = {
        "name": name,
        "login": login_val,
        "password": generate_password_hash(pwd),
        "created_at": datetime.utcnow(),
        "online": False
    }

    res = users.insert_one(user)
    session["user_id"] = str(res.inserted_id)
    return redirect("/")


@app.route("/logout")
def logout():
    uid = session.get("user_id")
    if uid and users is not None:
        users.update_one({"_id": oid(uid)}, {"$set": {"online": False}})
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

    # ИСПРАВЛЕНО: сериализуем пользователей — ObjectId не передаётся в шаблон напрямую
    raw_users = list(users.find({"_id": {"$ne": user["_id"]}}))

    # Считаем непрочитанные для каждого пользователя
    all_users = []
    for u in raw_users:
        uid_str = str(u["_id"])
        unread = messages.count_documents({
            "sender": uid_str,
            "receiver": my_id,
            "read": False
        }) if messages is not None else 0
        su = serialize_user(u)
        su["unread"] = unread
        all_users.append(su)

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

# =========================
# MESSAGES API
# =========================
@app.route("/get_messages")
def get_messages():
    user = get_user()
    if not user:
        return jsonify([])

    other = request.args.get("with")
    if not other:  # ИСПРАВЛЕНО: добавлена проверка параметра
        return jsonify([])

    my_id = str(user["_id"])

    msgs = list(messages.find({
        "$or": [
            {"sender": my_id, "receiver": other},
            {"sender": other, "receiver": my_id}
        ]
    }).sort("created_at", DESCENDING).limit(50))

    msgs.reverse()

    return jsonify([
        {
            "sender": m["sender"],
            "receiver": m["receiver"],   # ИСПРАВЛЕНО: добавлено поле receiver
            "text": m["text"],
            "read": m.get("read", False), # ИСПРАВЛЕНО: добавлено поле read
            "time": m["created_at"].isoformat()
        }
        for m in msgs
    ])

# =========================
# SEARCH  — ИСПРАВЛЕНО: маршрут существовал в шаблоне, но отсутствовал в app.py
# =========================
@app.route("/search")
def search():
    user = get_user()
    if not user:
        return redirect("/login")

    query = request.args.get("query", "").strip()
    results = []

    if query and users is not None:
        my_id = user["_id"]
        import re
        safe_query = re.escape(query)
        raw = list(users.find({
            "_id": {"$ne": my_id},
            "$or": [
                {"name":  {"$regex": safe_query, "$options": "i"}},
                {"login": {"$regex": safe_query, "$options": "i"}}
            ]
        }).limit(20))
        results = [serialize_user(u) for u in raw]

    return render_template("search.html", results=results)

# =========================
# SOCKET.IO
# =========================
@socketio.on("connect")
def on_connect():
    uid = session.get("user_id")

    if not uid:
        disconnect()
        return

    users.update_one(
        {"_id": oid(uid)},
        {"$set": {"online": True}}
    )

    join_room(uid)

    emit(
        "user_online",
        {"user_id": uid},
        broadcast=True
    )


@socketio.on("disconnect")
def on_disconnect():
    uid = session.get("user_id")

    if uid:
        users.update_one(
            {"_id": oid(uid)},
            {"$set": {"online": False}}
        )

        emit(
            "user_offline",
            {"user_id": uid},
            broadcast=True
        )


@socketio.on("new_message")
def new_message(data):
    user = get_user()

    if not user:
        return

    my_id = str(user["_id"])
    to_id = data.get("receiver_id")
    text = bleach.clean(data.get("text", "").strip())

    if not to_id or not text:
        return

    if my_id == to_id:
        return

    if users is None or not users.find_one({"_id": oid(to_id)}):
        return

    if not rate_limit(my_id):
        emit(
            "error",
            {"message": "Слишком быстро"},
            room=my_id
        )
        return

    msg = {
        "sender": my_id,
        "receiver": to_id,
        "text": text,
        "read": False,
        "created_at": datetime.utcnow()
    }

    messages.insert_one(msg)

    payload = {
        "sender": my_id,
        "receiver": to_id,
        "text": text,
        "read": False,
        "time": msg["created_at"].isoformat()
    }

    emit("receive_message", payload, room=to_id)
    emit("receive_message", payload, room=my_id)

# =========================
# ERROR HANDLER — ИСПРАВЛЕНО: перенесён до if __name__
# =========================
@app.errorhandler(Exception)
def handle_error(e):
    print(traceback.format_exc())
    return "SERVER ERROR", 500


# =========================
# START
# =========================
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
