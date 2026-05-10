import os
import re
import bleach
import logging
import eventlet

eventlet.monkey_patch()

from datetime import datetime
from bson.objectid import ObjectId
from dotenv import load_dotenv
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    session,
    jsonify,
)
from flask_socketio import SocketIO, emit, join_room
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pymongo import MongoClient
from werkzeug.security import (
    generate_password_hash,
    check_password_hash,
)

# =========================
# ENV
# =========================

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
MONGO_URI = os.getenv("MONGO_URI")

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY missing")

if not MONGO_URI:
    raise RuntimeError("MONGO_URI missing")

# =========================
# APP
# =========================

app = Flask(__name__)
app.secret_key = SECRET_KEY

csrf = CSRFProtect(app)

logging.basicConfig(level=logging.INFO)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200/day", "50/hour"]
)

socketio = SocketIO(
    app,
    cors_allowed_origins=[
        "http://localhost:5000"
    ],
    async_mode="eventlet",
    manage_session=True,
)

# =========================
# DB
# =========================

client = MongoClient(MONGO_URI)
db = client["kadrgram_database"]

users_table = db["users"]
messages_table = db["messages"]

users_table.create_index("login", unique=True)
messages_table.create_index([
    ("sender", 1),
    ("receiver", 1),
])
messages_table.create_index("created_at")

# =========================
# HELPERS
# =========================


def safe_object_id(value):
    try:
        return ObjectId(value)
    except Exception:
        return None



def valid_login(login):
    return bool(re.fullmatch(r"[A-Za-z0-9_]{3,20}", login))



def valid_password(password):
    return (
        len(password) >= 8
        and re.search(r"[A-Z]", password)
        and re.search(r"[a-z]", password)
        and re.search(r"\d", password)
    )



def current_user():
    uid = session.get("user_id")

    if not uid:
        return None

    oid = safe_object_id(uid)

    if not oid:
        return None

    return users_table.find_one({"_id": oid})


# =========================
# AUTH
# =========================

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10/minute")
def login():
    if request.method == "POST":
        login_value = request.form.get("login", "").strip()
        password = request.form.get("password", "")

        try:
            user = users_table.find_one({"login": login_value})

            if user and check_password_hash(user["password"], password):
                session["user_id"] = str(user["_id"])

                users_table.update_one(
                    {"_id": user["_id"]},
                    {
                        "$set": {
                            "online": True,
                            "last_seen": datetime.utcnow(),
                        }
                    },
                )

                return redirect("/")

        except Exception as error:
            logging.error(error)

        return render_template("login.html", error="invalid_credentials")

    return render_template("login.html")


@app.route("/register", methods=["POST"])
@limiter.limit("5/minute")
def register():
    name = request.form.get("name", "").strip()
    login_value = request.form.get("login", "").strip()
    password = request.form.get("password", "")

    if len(name) > 50:
        return render_template("login.html", error="invalid_name")

    if not valid_login(login_value):
        return render_template("login.html", error="invalid_login")

    if not valid_password(password):
        return render_template("login.html", error="weak_password")

    exists = users_table.find_one({"login": login_value})

    if exists:
        return render_template("login.html", error="login_exists")

    user = {
        "name": bleach.clean(name),
        "login": bleach.clean(login_value),
        "password": generate_password_hash(password),
        "online": False,
        "avatar": None,
        "last_seen": datetime.utcnow(),
        "created_at": datetime.utcnow(),
    }

    inserted = users_table.insert_one(user)

    session["user_id"] = str(inserted.inserted_id)

    return redirect("/")


@app.route("/logout")
def logout():
    user = current_user()

    if user:
        users_table.update_one(
            {"_id": user["_id"]},
            {
                "$set": {
                    "online": False,
                    "last_seen": datetime.utcnow(),
                }
            },
        )

    session.clear()

    return redirect("/login")


# =========================
# HOME
# =========================

@app.route("/")
def home():
    user = current_user()

    if not user:
        return redirect("/login")

    uid = str(user["_id"])

    all_users = list(
        users_table.find({
            "_id": {
                "$ne": user["_id"]
            }
        })
    )

    for item in all_users:
        item["id"] = str(item["_id"])

        item["unread"] = messages_table.count_documents({
            "sender": item["id"],
            "receiver": uid,
            "read": False,
        })

    target_user = None

    chat_with = request.args.get("chat_with")

    if chat_with:
        target = users_table.find_one({
            "_id": safe_object_id(chat_with)
        })

        if target:
            target["id"] = str(target["_id"])
            target_user = target

            messages_table.update_many(
                {
                    "sender": chat_with,
                    "receiver": uid,
                    "read": False,
                },
                {
                    "$set": {
                        "read": True
                    }
                },
            )

    return render_template(
        "index.html",
        user=user,
        all_users=all_users,
        target_user=target_user,
    )


# =========================
# SEARCH
# =========================

@app.route("/search")
def search():
    user = current_user()

    if not user:
        return redirect("/login")

    q = request.args.get("q", "").strip()

    users = []

    if q:
        users = list(
            users_table.find({
                "login": {
                    "$regex": q,
                    "$options": "i",
                }
            }).limit(20)
        )

    return render_template(
        "search.html",
        users=users,
        q=q,
    )


# =========================
# GET MESSAGES
# =========================

@app.route("/get_messages")
def get_messages():
    user = current_user()

    if not user:
        return jsonify([])

    other = request.args.get("with")

    if not other:
        return jsonify([])

    skip = int(request.args.get("skip", 0))

    query = {
        "$or": [
            {
                "sender": str(user["_id"]),
                "receiver": other,
            },
            {
                "sender": other,
                "receiver": str(user["_id"]),
            },
        ]
    }

    messages = list(
        messages_table.find(query)
        .sort("created_at", -1)
        .skip(skip)
        .limit(50)
    )

    messages.reverse()

    result = []

    for message in messages:
        result.append({
            "id": str(message["_id"]),
            "sender": message["sender"],
            "receiver": message["receiver"],
            "text": message["text"],
            "read": message.get("read", False),
            "created_at": message["created_at"].isoformat(),
        })

    return jsonify(result)


# =========================
# SOCKET.IO
# =========================

@socketio.on("join")
def on_join():
    user = current_user()

    if not user:
        return

    uid = str(user["_id"])

    join_room(uid)

    users_table.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "online": True
            }
        },
    )

    emit(
        "user_status",
        {
            "user_id": uid,
            "online": True,
        },
        broadcast=True,
    )


@socketio.on("disconnect")
def on_disconnect():
    user = current_user()

    if not user:
        return

    uid = str(user["_id"])

    users_table.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "online": False,
                "last_seen": datetime.utcnow(),
            }
        },
    )

    emit(
        "user_status",
        {
            "user_id": uid,
            "online": False,
        },
        broadcast=True,
    )


@socketio.on("typing")
def typing(data):
    user = current_user()

    if not user:
        return

    receiver_id = data.get("receiver_id")

    if not receiver_id:
        return

    emit(
        "typing",
        {
            "sender": str(user["_id"])
        },
        room=receiver_id,
    )


@socketio.on("new_message")
def new_message(data):
    user = current_user()

    if not user:
        return

    sender_id = str(user["_id"])

    receiver_id = data.get("receiver_id")
    text = bleach.clean(data.get("text", "").strip())

    if not receiver_id:
        return

    if sender_id == receiver_id:
        return

    if not text:
        return

    if len(text) > 2000:
        return

    receiver = users_table.find_one({
        "_id": safe_object_id(receiver_id)
    })

    if not receiver:
        return

    message = {
        "sender": sender_id,
        "receiver": receiver_id,
        "text": text,
        "read": False,
        "created_at": datetime.utcnow(),
    }

    inserted = messages_table.insert_one(message)

    payload = {
        "id": str(inserted.inserted_id),
        "sender": sender_id,
        "receiver": receiver_id,
        "text": text,
        "read": False,
        "created_at": message["created_at"].isoformat(),
    }

    emit("receive_message", payload, room=receiver_id)
    emit("receive_message", payload, room=sender_id)


# =========================
# RUN
# =========================

if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=False,
    )
