import os
import html
from datetime import datetime

from flask import (
    Flask,
    render_template,
    request,
    session,
    redirect,
    jsonify
)

from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from bson.objectid import ObjectId

# =========================================================
# CONFIG
# =========================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "change_this_secret_key"
)

# Render + SocketIO
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet"
)

# =========================================================
# DATABASE
# =========================================================

MONGO_URI = os.environ.get("MONGO_URI")

if not MONGO_URI:
    raise Exception("MONGO_URI environment variable not found!")

client = MongoClient(MONGO_URI)

db = client["kadrgram_database"]

users_table = db["users"]
messages_table = db["messages"]

# =========================================================
# HELPERS
# =========================================================

def valid_objectid(value):
    try:
        ObjectId(value)
        return True
    except:
        return False


def current_user():
    user_id = session.get("user_id")

    if not user_id:
        return None

    if not valid_objectid(user_id):
        return None

    return users_table.find_one({
        "_id": ObjectId(user_id)
    })


# =========================================================
# ROUTES
# =========================================================

@app.route("/")
def home():

    user = current_user()

    if not user:
        return redirect("/login")

    my_id = str(user["_id"])

    all_users = list(users_table.find({
        "_id": {"$ne": ObjectId(my_id)}
    }))

    for u in all_users:
        u["id"] = str(u["_id"])

        u["online"] = u.get("online", False)

        u["unread"] = messages_table.count_documents({
            "sender": u["id"],
            "receiver": my_id,
            "read": False
        })

    target_user = None

    chat_with = request.args.get("chat_with")

    if chat_with and valid_objectid(chat_with):

        target_user = users_table.find_one({
            "_id": ObjectId(chat_with)
        })

        if target_user:
            target_user["id"] = str(target_user["_id"])

    return render_template(
        "index.html",
        user=user,
        all_users=all_users,
        target_user=target_user
    )


# =========================================================
# MARK READ
# =========================================================

@app.route("/mark_read/<sender_id>")
def mark_read(sender_id):

    user = current_user()

    if not user:
        return jsonify({"status": "error"})

    if not valid_objectid(sender_id):
        return jsonify({"status": "error"})

    my_id = str(user["_id"])

    messages_table.update_many(
        {
            "sender": sender_id,
            "receiver": my_id,
            "read": False
        },
        {
            "$set": {
                "read": True
            }
        }
    )

    socketio.emit(
        "messages_read",
        {
            "reader": my_id
        },
        room=sender_id
    )

    return jsonify({"status": "ok"})


# =========================================================
# SOCKETS
# =========================================================

@socketio.on("connect")
def on_connect():

    user = current_user()

    if not user:
        return False

    user_id = str(user["_id"])

    join_room(user_id)

    users_table.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "online": True,
                "last_seen": datetime.utcnow()
            }
        }
    )

    emit(
        "user_online",
        {
            "user_id": user_id
        },
        broadcast=True
    )


@socketio.on("disconnect")
def on_disconnect():

    user_id = session.get("user_id")

    if not user_id:
        return

    if not valid_objectid(user_id):
        return

    users_table.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "online": False,
                "last_seen": datetime.utcnow()
            }
        }
    )

    emit(
        "user_offline",
        {
            "user_id": user_id
        },
        broadcast=True
    )


@socketio.on("new_message")
def new_message(data):

    user = current_user()

    if not user:
        return

    sender_id = str(user["_id"])

    receiver_id = data.get("receiver_id")
    text = data.get("text", "").strip()
    msg_type = data.get("type", "text")

    # VALIDATION

    if not receiver_id:
        return

    if not valid_objectid(receiver_id):
        return

    if not text:
        return

    if len(text) > 1000:
        return

    # XSS protection
    text = html.escape(text)

    receiver = users_table.find_one({
        "_id": ObjectId(receiver_id)
    })

    if not receiver:
        return

    message = {
        "sender": sender_id,
        "receiver": receiver_id,
        "text": text,
        "type": msg_type,
        "time": datetime.utcnow().isoformat() + "Z",
        "read": False,
        "delivered": True
    }

    result = messages_table.insert_one(message)

    message["id"] = str(result.inserted_id)

    emit(
        "receive_message",
        message,
        room=receiver_id
    )

    emit(
        "receive_message",
        message,
        room=sender_id
    )


@socketio.on("start_typing")
def start_typing(data):

    user = current_user()

    if not user:
        return

    receiver_id = data.get("receiver_id")

    if not receiver_id:
        return

    emit(
        "is_typing",
        {
            "sender_id": str(user["_id"])
        },
        room=receiver_id
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 5000))

    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=False
    )
