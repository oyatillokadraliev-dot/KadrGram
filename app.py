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
from werkzeug.utils import secure_filename

load_dotenv()
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet", manage_session=False)

# ================= DB =================
client = MongoClient(os.getenv("MONGO_URI"))
db = client["kadrgram"]
users = db["users"]
messages = db["messages"]

users.create_index("login", unique=True)

# ================= HELPERS =================
def oid(x):
    try:
        return ObjectId(x)
    except:
        return None

def get_user():
    uid = session.get("user_id")
    return users.find_one({"_id": oid(uid)}) if uid else None

def allowed_file(fn):
    return "." in fn and fn.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def serialize_user(u):
    return {
        "id": str(u["_id"]),
        "name": u.get("name"),
        "online": u.get("online", False),
        "last_seen": u.get("last_seen")
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

# ================= HOME =================
@app.route("/")
def home():
    user = get_user()
    if not user:
        return redirect("/login")

    my_id = str(user["_id"])

    all_users = []
    for u in users.find({"_id": {"$ne": user["_id"]}}):
        uid = str(u["_id"])
        unread = messages.count_documents({
            "sender": uid,
            "receiver": my_id,
            "read": False
        })
        u = serialize_user(u)
        u["unread"] = unread
        all_users.append(u)

    target_user = None
    chat_with = request.args.get("chat_with")

    if chat_with:
        tu = users.find_one({"_id": oid(chat_with)})
        if tu:
            target_user = serialize_user(tu)

    return render_template(
        "index.html",
        user=serialize_user(user),
        all_users=all_users,
        target_user=target_user,
        my_id=my_id
    )

# ================= MESSAGES =================
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
    }).sort("created_at", DESCENDING).limit(50))

    msgs.reverse()
    return jsonify([serialize_message(m) for m in msgs])

# ================= UPLOAD IMAGE (FIXED) =================
@app.route("/upload_image", methods=["POST"])
def upload_image():
    user = get_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    f = request.files.get("image")
    if not f or not allowed_file(f.filename):
        return jsonify({"error": "bad file"}), 400

    filename = secure_filename(f"{datetime.utcnow().timestamp()}_{f.filename}")
    path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    f.save(path)

    return jsonify({"url": f"/static/uploads/{filename}"})

# ================= SOCKET =================
@socketio.on("connect")
def connect():
    uid = session.get("user_id")
    if not uid:
        disconnect()
        return
    users.update_one({"_id": oid(uid)}, {"$set": {"online": True}})
    join_room(uid)

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

    msg = {
        "sender": my_id,
        "receiver": to_id,
        "text": text,
        "image": image,
        "read": False,
        "edited": False,
        "deleted": False,
        "created_at": datetime.utcnow()
    }

    # reply FIX
    if reply_to:
        orig = messages.find_one({"_id": oid(reply_to)})
        if orig:
            msg["reply_to"] = reply_to
            msg["reply_text"] = orig.get("text", "📷 фото")[:80]

    res = messages.insert_one(msg)
    msg["_id"] = res.inserted_id

    payload = serialize_message(msg)

    emit("receive_message", payload, room=my_id)
    emit("receive_message", payload, room=to_id)

@socketio.on("delete_message")
def delete_message(data):
    uid = str(get_user()["_id"])
    mid = data.get("msg_id")

    msg = messages.find_one({"_id": oid(mid), "sender": uid})
    if not msg:
        return

    messages.update_one({"_id": oid(mid)}, {"$set": {"deleted": True, "text": ""}})

    emit("message_deleted", {"msg_id": mid}, room=uid)
    emit("message_deleted", {"msg_id": mid}, room=msg["receiver"])

@socketio.on("mark_read")
def mark_read(data):
    user = get_user()
    if not user:
        return

    my_id = str(user["_id"])
    sender_id = data.get("sender_id")

    messages.update_many(
        {"sender": sender_id, "receiver": my_id, "read": False},
        {"$set": {"read": True}}
    )

    emit("messages_read", {"by": my_id}, room=sender_id)

# ================= RUN =================
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
