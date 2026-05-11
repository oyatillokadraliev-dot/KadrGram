import os
import re
import bleach
import logging
import eventlet

# Это должно быть в самом верху
eventlet.monkey_patch()

from datetime import datetime
from bson.objectid import ObjectId
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, 
    session, jsonify, url_for
)
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from werkzeug.security import generate_password_hash, check_password_hash

# =========================
# CONFIG
# =========================
load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY")
MONGO_URI = os.getenv("MONGO_URI")

# Жесткая проверка ключей (то, на чем падал сервер)
if not SECRET_KEY or not MONGO_URI:
    print("!!! КРИТИЧЕСКАЯ ОШИБКА: Проверь Environment Variables на Render !!!")
    # Не будем вызывать raise, чтобы сервер не падал моментально, 
    # но работать он не будет без ключей.

app = Flask(__name__)
app.secret_key = SECRET_KEY

# Упрощенная настройка сокетов
socketio = SocketIO(
    app,
    cors_allowed_origins="*", 
    async_mode="eventlet"
)

# =========================
# DB
# =========================
client = MongoClient(MONGO_URI)
db = client["kadrgram_database"]
users_table = db["users"]
messages_table = db["messages"]

# =========================
# HELPERS
# =========================
def safe_object_id(value):
    try: return ObjectId(value)
    except: return None

def current_user():
    uid = session.get("user_id")
    if not uid: return None
    return users_table.find_one({"_id": safe_object_id(uid)})

# =========================
# ROUTES
# =========================

@app.route("/")
def home():
    user = current_user()
    if not user: return redirect(url_for("login"))
    
    my_id = str(user["_id"])
    all_users_cursor = users_table.find({"_id": {"$ne": user["_id"]}})
    
    # Считаем непрочитанные
    pipeline = [
        {"$match": {"receiver": my_id, "read": False}},
        {"$group": {"_id": "$sender", "count": {"$sum": 1}}}
    ]
    unread_map = {item["_id"]: item["count"] for item in messages_table.aggregate(pipeline)}

    users_list = []
    for u in all_users_cursor:
        u_id = str(u["_id"])
        u["id"] = u_id
        u["unread"] = unread_map.get(u_id, 0)
        users_list.append(u)

    target_user = None
    chat_with = request.args.get("chat_with")
    if chat_with:
        t_oid = safe_object_id(chat_with)
        if t_oid:
            target = users_table.find_one({"_id": t_oid})
            if target:
                target["id"] = str(target["_id"])
                target_user = target
                messages_table.update_many(
                    {"sender": chat_with, "receiver": my_id, "read": False},
                    {"$set": {"read": True}}
                )

    return render_template("index.html", user=user, all_users=users_list, target_user=target_user, my_id=my_id)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        login_val = request.form.get("login", "").strip()
        pwd = request.form.get("pwd", "") 
        user = users_table.find_one({"login": login_val})
        if user and check_password_hash(user["password"], pwd):
            session["user_id"] = str(user["_id"])
            return redirect(url_for("home"))
        return render_template("login.html", error="wrong_pass")
    return render_template("login.html")

@app.route("/register", methods=["POST"])
def register():
    name = bleach.clean(request.form.get("name", "").strip())
    login_val = bleach.clean(request.form.get("login", "").strip())
    pwd = request.form.get("pwd", "")
    if users_table.find_one({"login": login_val}):
        return render_template("login.html", error="login_taken")
    
    user_obj = {
        "name": name, "login": login_val, 
        "password": generate_password_hash(pwd),
        "created_at": datetime.utcnow()
    }
    result = users_table.insert_one(user_obj)
    session["user_id"] = str(result.inserted_id)
    return redirect(url_for("home"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/get_messages")
def get_messages():
    user = current_user()
    if not user: return jsonify([])
    other_id = request.args.get("with")
    my_id = str(user["_id"])
    msgs = list(messages_table.find({
        "$or": [{"sender": my_id, "receiver": other_id}, {"sender": other_id, "receiver": my_id}]
    }).sort("created_at", -1).limit(50))
    msgs.reverse()
    return jsonify([{"sender": m["sender"], "text": m["text"], "time": m["created_at"].isoformat()} for m in msgs])

# =========================
# SOCKETS
# =========================

@socketio.on("join")
def handle_join():
    uid = session.get("user_id")
    if uid:
        join_room(uid)
        emit("user_status", {"user_id": uid, "online": True}, broadcast=True)

@socketio.on("new_message")
def handle_msg(data):
    user = current_user()
    if not user: return
    my_id = str(user["_id"])
    to_id = data.get("receiver_id")
    text = bleach.clean(data.get("text", "").strip())
    if not to_id or not text or my_id == to_id: return

    msg_obj = {"sender": my_id, "receiver": to_id, "text": text, "read": False, "created_at": datetime.utcnow()}
    messages_table.insert_one(msg_obj)
    payload = {"sender": my_id, "receiver": to_id, "text": text, "time": msg_obj["created_at"].isoformat()}
    emit("receive_message", payload, room=to_id)
    emit("receive_message", payload, room=my_id)

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
