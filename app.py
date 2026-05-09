import os
import re
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, session, redirect, jsonify
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from bson.objectid import ObjectId
from bson.errors import InvalidId
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev_secret")

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet", manage_session=True)

# DB
client = MongoClient(os.getenv("MONGO_URI"))
db = client["kadrgram_database"]

users_table = db["users"]
messages_table = db["messages"]

# -------------------------
# HELPERS
# -------------------------

def safe_object_id(oid):
    try:
        return ObjectId(oid)
    except:
        return None


def valid_login(login):
    return bool(re.fullmatch(r"[A-Za-z0-9_]{3,20}", login))


def valid_password(pwd):
    return (
        len(pwd) >= 8
        and re.search(r"[A-Z]", pwd)
        and re.search(r"[a-z]", pwd)
        and re.search(r"\d", pwd)
    )


# -------------------------
# AUTH
# -------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u_login = request.form.get("login", "").strip()
        u_pwd = request.form.get("pwd", "")

        user = users_table.find_one({"login": u_login})

        if user and check_password_hash(user["password"], u_pwd):
            session["user_id"] = str(user["_id"])
            return redirect("/")

        return render_template("login.html", error="wrong_pass")

    return render_template("login.html")


@app.route("/register", methods=["POST"])
def register():
    name = request.form.get("name", "").strip()
    u_login = request.form.get("login", "").strip()
    u_pwd = request.form.get("pwd", "")

    if not name or not valid_login(u_login) or not valid_password(u_pwd):
        return render_template("login.html", error="invalid_data")

    if users_table.find_one({"login": u_login}):
        return render_template("login.html", error="login_taken")

    user = {
        "name": name,
        "login": u_login,
        "password": generate_password_hash(u_pwd),
        "online": False,
        "last_seen": datetime.utcnow()
    }

    user_id = users_table.insert_one(user).inserted_id
    session["user_id"] = str(user_id)

    return redirect("/")


@app.route("/logout")
def logout():
    uid = session.get("user_id")

    if uid:
        users_table.update_one(
            {"_id": ObjectId(uid)},
            {"$set": {"online": False, "last_seen": datetime.utcnow()}}
        )

    session.clear()
    return redirect("/login")


# -------------------------
# MAIN PAGE
# -------------------------

@app.route("/")
def home():
    uid = session.get("user_id")
    if not uid:
        return redirect("/login")

    my_id = safe_object_id(uid)
    if not my_id:
        return redirect("/login")

    user = users_table.find_one({"_id": my_id})
    if not user:
        return redirect("/login")

    all_users = list(users_table.find({"_id": {"$ne": my_id}}))

    # FIX: N+1 still exists but acceptable MVP level
    for u in all_users:
        u["id"] = str(u["_id"])
        u["unread"] = messages_table.count_documents({
            "sender": u["id"],
            "receiver": uid,
            "read": False
        })

    target_user = None
    chat_id = request.args.get("chat_with")

    if chat_id:
        target_id = safe_object_id(chat_id)

        if target_id:
            target_user = users_table.find_one({"_id": target_id})

            if target_user:
                target_user["id"] = str(target_user["_id"])

                messages_table.update_many(
                    {
                        "sender": chat_id,
                        "receiver": uid,
                        "read": False
                    },
                    {"$set": {"read": True}}
                )

    return render_template(
        "index.html",
        user=user,
        all_users=all_users,
        target_user=target_user
    )


# -------------------------
# MESSAGES API
# -------------------------

@app.route("/get_messages")
def get_messages():
    uid = session.get("user_id")
    other = request.args.get("with")

    if not uid or not other:
        return jsonify([])

    other_id = safe_object_id(other)
    if not other_id:
        return jsonify([])

    query = {
        "$or": [
            {"sender": uid, "receiver": other},
            {"sender": other, "receiver": uid}
        ]
    }

    msgs = list(
        messages_table.find(query)
        .sort("created_at", 1)
        .limit(50)
    )

    for m in msgs:
        m["id"] = str(m["_id"])
        del m["_id"]

    return jsonify(msgs)


# -------------------------
# SOCKET.IO
# -------------------------

@socketio.on("join")
def on_join():
    uid = session.get("user_id")
    if not uid:
        return

    join_room(uid)

    users_table.update_one(
        {"_id": ObjectId(uid)},
        {"$set": {"online": True}}
    )

    emit(
        "user_status",
        {"user_id": uid, "online": True},
        broadcast=True
    )


@socketio.on("disconnect")
def on_disconnect():
    uid = session.get("user_id")

    if uid:
        users_table.update_one(
            {"_id": ObjectId(uid)},
            {"$set": {"online": False, "last_seen": datetime.utcnow()}}
        )

        emit(
            "user_status",
            {"user_id": uid, "online": False},
            broadcast=True
        )


@socketio.on("new_message")
def handle_message(data):
    sid = session.get("user_id")
    rid = data.get("receiver_id")
    text = data.get("text", "").strip()

    if not sid or not rid:
        return

    if not text or len(text) > 2000:
        return

    if sid == rid:
        return

    msg = {
        "sender": sid,
        "receiver": rid,
        "text": text,
        "created_at": datetime.utcnow(),
        "read": False
    }

    msg_id = messages_table.insert_one(msg).inserted_id
    msg["id"] = str(msg_id)

    emit("receive_message", msg, room=rid)
    emit("receive_message", msg, room=sid)


# -------------------------
# RUN
# -------------------------

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
