import traceback
import eventlet
eventlet.monkey_patch()

import os
import re
import bleach
import logging
from collections import Counter
from datetime import datetime

from bson.objectid import ObjectId
from dotenv import load_dotenv
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    session,
    jsonify
)
from flask_socketio import (
    SocketIO,
    emit,
    join_room,
    disconnect
)
from pymongo import MongoClient, ASCENDING, DESCENDING
from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)

import cloudinary
import cloudinary.uploader

# =====================================================
# CONFIG
# =====================================================

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

ALLOWED_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp"
}

# =====================================================
# DATABASE
# =====================================================

MONGO_URI = os.getenv("MONGO_URI")

client = None
users = None
messages = None

try:
    client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=5000
    )

    client.admin.command("ping")

    db = client["kadrgram"]

    users = db["users"]
    messages = db["messages"]

    users.create_index("login", unique=True)

    messages.create_index([
        ("sender", ASCENDING),
        ("receiver", ASCENDING),
        ("created_at", DESCENDING)
    ])

    messages.create_index([
        ("receiver", ASCENDING),
        ("read", ASCENDING)
    ])

    messages.create_index([
        ("created_at", DESCENDING)
    ])

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
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def get_user():
    uid = session.get("user_id")

    if not uid:
        return None

    return users.find_one({
        "_id": oid(uid)
    })


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
        mins = int(diff // 60)
        return f"был(а) {mins} мин назад"

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
        "avatar": u.get("avatar", ""),
        "online": bool(u.get("online", False)),
        "last_seen_str": format_last_seen(
            u.get("last_seen")
        )
    }


def serialize_message(m):
    reactions = m.get("reactions", {})
    reaction_counts = Counter(reactions.values())

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
        "reactions": dict(reaction_counts),
        "time": m["created_at"].isoformat()
    }

# =====================================================
# AUTH
# =====================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":

        login_val = request.form.get("login", "").strip()
        pwd = request.form.get("pwd", "")

        user = users.find_one({
            "login": login_val
        })

        if user and check_password_hash(user["password"], pwd):
            session["user_id"] = str(user["_id"])
            return redirect("/")

        return render_template(
            "login.html",
            error="wrong_pass"
        )

    return render_template("login.html")


@app.route("/register", methods=["POST"])
def register():

    name = bleach.clean(
        request.form.get("name", "").strip()
    )

    login_val = bleach.clean(
        request.form.get("login", "").strip()
    )

    pwd = request.form.get("pwd", "")

    if not name or not login_val or not pwd:
        return redirect("/login")

    if len(pwd) < 8:
        return redirect("/login")

    if not re.match(r'^[A-Za-z0-9]+$', login_val):
        return redirect("/login")

    if users.find_one({"login": login_val}):
        return redirect("/login")

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

    if uid:
        users.update_one(
            {"_id": oid(uid)},
            {
                "$set": {
                    "online": False,
                    "last_seen": datetime.utcnow()
                }
            }
        )

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

    raw_users = list(users.find(
        {
            "_id": {
                "$ne": user["_id"]
            }
        }
    ))

    all_users = []

    for u in raw_users:

        unread = messages.count_documents({
            "sender": str(u["_id"]),
            "receiver": my_id,
            "read": False
        })

        su = serialize_user(u)
        su["unread"] = unread

        all_users.append(su)

    target_user = None

    chat_with = request.args.get("chat_with")

    if chat_with:

        raw_target = users.find_one({
            "_id": oid(chat_with)
        })

        if raw_target:
            target_user = serialize_user(raw_target)

            messages.update_many(
                {
                    "sender": chat_with,
                    "receiver": my_id,
                    "read": False
                },
                {
                    "$set": {
                        "read": True
                    }
                }
            )

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

    return render_template(
        "profile.html",
        user=serialize_user(user)
    )


@app.route("/update_profile", methods=["POST"])
def update_profile():

    user = get_user()

    if not user:
        return jsonify({
            "success": False
        }), 401

    data = request.get_json()

    field = data.get("field")
    value = data.get("value", "").strip()

    if not value:
        return jsonify({
            "success": False
        })

    update_data = {}

    if field == "name":
        update_data["name"] = bleach.clean(value)

    elif field == "login":

        if not re.match(r'^[A-Za-z0-9]+$', value):
            return jsonify({
                "success": False
            })

        exists = users.find_one({
            "login": value,
            "_id": {
                "$ne": user["_id"]
            }
        })

        if exists:
            return jsonify({
                "success": False
            })

        update_data["login"] = value

    elif field == "password":

        if len(value) < 8:
            return jsonify({
                "success": False
            })

        update_data["password"] = generate_password_hash(value)

    users.update_one(
        {"_id": user["_id"]},
        {
            "$set": update_data
        }
    )

    return jsonify({
        "success": True
    })

# =====================================================
# AVATAR
# =====================================================

@app.route("/upload_avatar", methods=["POST"])
def upload_avatar():

    user = get_user()

    if not user:
        return jsonify({
            "success": False
        }), 401

    if "avatar" not in request.files:
        return jsonify({
            "success": False
        }), 400

    file = request.files["avatar"]

    if file.filename == "":
        return jsonify({
            "success": False
        }), 400

    if not allowed_file(file.filename):
        return jsonify({
            "success": False
        }), 400

    try:

        result = cloudinary.uploader.upload(
            file,
            folder="kadrgram_avatars"
        )

        avatar_url = result.get("secure_url")

        users.update_one(
            {
                "_id": user["_id"]
            },
            {
                "$set": {
                    "avatar": avatar_url
                }
            }
        )

        return jsonify({
            "success": True,
            "avatar": avatar_url
        })

    except Exception as e:

        logging.error(e)

        return jsonify({
            "success": False
        }), 500

# =====================================================
# MESSAGES
# =====================================================

@app.route("/get_messages")
def get_messages():

    user = get_user()

    if not user:
        return jsonify([])

    other = request.args.get("with")

    if not other:
        return jsonify([])

    my_id = str(user["_id"])

    query = {
        "$or": [
            {
                "sender": my_id,
                "receiver": other
            },
            {
                "sender": other,
                "receiver": my_id
            }
        ]
    }

    msgs = list(
        messages.find(query)
        .sort("created_at", DESCENDING)
        .limit(50)
    )

    msgs.reverse()

    return jsonify([
        serialize_message(m)
        for m in msgs
    ])

# =====================================================
# IMAGE UPLOAD
# =====================================================

@app.route("/upload_image", methods=["POST"])
def upload_image():

    user = get_user()

    if not user:
        return jsonify({
            "error": "unauthorized"
        }), 401

    if "image" not in request.files:
        return jsonify({
            "error": "no file"
        }), 400

    file = request.files["image"]

    if file.filename == "":
        return jsonify({
            "error": "empty file"
        }), 400

    if not allowed_file(file.filename):
        return jsonify({
            "error": "invalid type"
        }), 400

    try:

        result = cloudinary.uploader.upload(
            file,
            folder="kadrgram_messages"
        )

        return jsonify({
            "url": result.get("secure_url")
        })

    except Exception as e:

        logging.error(e)

        return jsonify({
            "error": "upload failed"
        }), 500

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

    if query:

        safe_query = re.escape(query)

        raw = list(users.find({
            "_id": {
                "$ne": user["_id"]
            },
            "$or": [
                {
                    "name": {
                        "$regex": safe_query,
                        "$options": "i"
                    }
                },
                {
                    "login": {
                        "$regex": safe_query,
                        "$options": "i"
                    }
                }
            ]
        }).limit(20))

        results = [
            serialize_user(u)
            for u in raw
        ]

    return render_template(
        "search.html",
        results=results
    )

# =====================================================
# SOCKET.IO
# =====================================================

@socketio.on("connect")
def on_connect():

    uid = session.get("user_id")

    if not uid:
        disconnect()
        return

    users.update_one(
        {
            "_id": oid(uid)
        },
        {
            "$set": {
                "online": True,
                "last_seen": None
            }
        }
    )

    join_room(uid)

    emit(
        "user_online",
        {
            "user_id": uid
        },
        broadcast=True,
        include_self=False
    )


@socketio.on("disconnect")
def on_disconnect():

    uid = session.get("user_id")

    if not uid:
        return

    now = datetime.utcnow()

    users.update_one(
        {
            "_id": oid(uid)
        },
        {
            "$set": {
                "online": False,
                "last_seen": now
            }
        }
    )

    emit(
        "user_offline",
        {
            "user_id": uid,
            "last_seen_str": format_last_seen(now)
        },
        broadcast=True,
        include_self=False
    )


@socketio.on("heartbeat")
def heartbeat():

    uid = session.get("user_id")

    if not uid:
        return

    users.update_one(
        {
            "_id": oid(uid)
        },
        {
            "$set": {
                "online": True
            }
        }
    )


@socketio.on("typing")
def typing(data):

    uid = session.get("user_id")

    if not uid:
        return

    receiver_id = data.get("receiver_id")

    emit(
        "typing",
        {
            "sender_id": uid,
            "typing": bool(data.get("typing"))
        },
        room=receiver_id
    )


@socketio.on("new_message")
def new_message(data):

    user = get_user()

    if not user:
        return

    my_id = str(user["_id"])

    to_id = data.get("receiver_id")

    text = bleach.clean(
        data.get("text", "").strip()
    )

    image = data.get("image")

    if len(text) > 4000:
        return

    if not to_id:
        return

    if not text and not image:
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

    result = messages.insert_one(msg)

    msg["_id"] = result.inserted_id

    payload = serialize_message(msg)

    emit("receive_message", payload, room=my_id)
    emit("receive_message", payload, room=to_id)


@socketio.on("edit_message")
def edit_message(data):

    user = get_user()

    if not user:
        return

    my_id = str(user["_id"])

    msg_id = data.get("msg_id")

    new_text = bleach.clean(
        data.get("text", "").strip()
    )

    msg = messages.find_one({
        "_id": oid(msg_id),
        "sender": my_id
    })

    if not msg:
        return

    messages.update_one(
        {
            "_id": oid(msg_id)
        },
        {
            "$set": {
                "text": new_text,
                "edited": True
            }
        }
    )

    payload = {
        "msg_id": msg_id,
        "text": new_text
    }

    emit("message_edited", payload, room=my_id)
    emit("message_edited", payload, room=msg["receiver"])


@socketio.on("delete_message")
def delete_message(data):

    user = get_user()

    if not user:
        return

    my_id = str(user["_id"])

    msg_id = data.get("msg_id")

    msg = messages.find_one({
        "_id": oid(msg_id),
        "sender": my_id
    })

    if not msg:
        return

    messages.update_one(
        {
            "_id": oid(msg_id)
        },
        {
            "$set": {
                "deleted": True,
                "text": ""
            }
        }
    )

    payload = {
        "msg_id": msg_id
    }

    emit("message_deleted", payload, room=my_id)
    emit("message_deleted", payload, room=msg["receiver"])


@socketio.on("toggle_reaction")
def toggle_reaction(data):

    user_id = session.get("user_id")

    if not user_id:
        return

    msg_id = data.get("message_id")
    emoji = data.get("emoji")

    allowed = [
        "👍",
        "❤️",
        "🔥",
        "😂",
        "👏",
        "😮",
        "😢"
    ]

    if emoji not in allowed:
        return

    message = messages.find_one({
        "_id": oid(msg_id)
    })

    if not message:
        return

    reactions = message.get("reactions", {})

    if reactions.get(user_id) == emoji:
        del reactions[user_id]
    else:
        reactions[user_id] = emoji

    messages.update_one(
        {
            "_id": oid(msg_id)
        },
        {
            "$set": {
                "reactions": reactions
            }
        }
    )

    reaction_counts = Counter(reactions.values())

    socketio.emit(
        "update_reactions",
        {
            "message_id": msg_id,
            "reaction_counts": dict(reaction_counts)
        }
    )

# =====================================================
# ERRORS
# =====================================================

@app.errorhandler(Exception)
def handle_error(e):
    print(traceback.format_exc())
    return "SERVER ERROR", 500

# =====================================================
# START
# =====================================================

if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=True,
        use_reloader=False
    )
