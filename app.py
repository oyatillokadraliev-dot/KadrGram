import os
import bleach
import eventlet
import logging
from datetime import datetime, timedelta
from bson.objectid import ObjectId
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, session, jsonify, url_for
from flask_socketio import SocketIO, emit, join_room, disconnect
from pymongo import MongoClient
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
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
    except:
        return None

def get_user():
    uid = session.get("user_id")
    if not uid or not users:
        return None
    return users.find_one({"_id": oid(uid)})

# =========================
# AUTH
# =========================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if not users:
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
    if not users:
        return "DB error", 500

    name = bleach.clean(request.form.get("name", "").strip())
    login_val = bleach.clean(request.form.get("login", "").strip())
    pwd = request.form.get("pwd", "")

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

    if uid and users:
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

    all_users = list(users.find({"_id": {"$ne": user["_id"]}}))

    target_user = None
    chat_with = request.args.get("chat_with")

    if chat_with:
        target_user = users.find_one({"_id": oid(chat_with)})

        if target_user:
            messages.update_many(
                {
                    "sender": chat_with,
                    "receiver": my_id,
                    "read": False
                },
                {"$set": {"read": True}}
            )

    return render_template(
        "index.html",
        user=user,
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
    my_id = str(user["_id"])

    msgs = list(messages.find({
        "$or": [
            {"sender": my_id, "receiver": other},
            {"sender": other, "receiver": my_id}
        ]
    }).sort("created_at", -1).limit(50))

    msgs.reverse()

    return jsonify([
        {
            "sender": m["sender"],
            "text": m["text"],
            "time": m["created_at"].isoformat()
        }
        for m in msgs
    ])

# =========================
# SOCKET.IO
# =========================
@socketio.on("connect")
def on_connect():
    uid = session.get("user_id")

    if not uid:
        disconnect()
        return

    if users:
        users.update_one(
            {"_id": oid(uid)},
            {"$set": {"online": True}}
        )

    join_room(uid)


@socketio.on("disconnect")
def on_disconnect():
    uid = session.get("user_id")

    if uid and users:
        users.update_one(
            {"_id": oid(uid)},
            {"$set": {"online": False}}
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

    if not rate_limit(my_id):
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
        "time": msg["created_at"].isoformat()
    }

    emit("receive_message", payload, room=to_id)
    emit("receive_message", payload, room=my_id)

# =========================
# START
# =========================
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
