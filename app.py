import os
import html
from flask import Flask, render_template, request, session, redirect
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime

# --- CONFIG ---
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change_this_secret")

socketio = SocketIO(
    app,
    cors_allowed_origins=os.environ.get("CORS_ORIGINS", "*")
)

# --- DATABASE ---
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    raise Exception("MONGO_URI not set!")

client = MongoClient(MONGO_URI)
db = client['kadrgram_database']
users_table = db['users']
messages_table = db['messages']

# --- HELPERS ---
def is_valid_objectid(value):
    try:
        ObjectId(value)
        return True
    except:
        return False

# --- ROUTES ---
@app.route('/')
def home():
    if 'user_id' not in session:
        return redirect('/login')

    my_id = session['user_id']

    if not is_valid_objectid(my_id):
        return redirect('/login')

    user = users_table.find_one({"_id": ObjectId(my_id)})
    if not user:
        return redirect('/login')

    all_users = list(users_table.find({"_id": {"$ne": ObjectId(my_id)}}))

    for u in all_users:
        u['id'] = str(u['_id'])
        u['unread'] = messages_table.count_documents({
            "sender": u['id'],
            "receiver": my_id,
            "read": False
        })

    target_user = None
    chat_with_id = request.args.get('chat_with')

    if chat_with_id and is_valid_objectid(chat_with_id):
        target_user = users_table.find_one({"_id": ObjectId(chat_with_id)})
        if target_user:
            target_user['id'] = str(target_user['_id'])

    return render_template(
        'index.html',
        user=user,
        all_users=all_users,
        target_user=target_user
    )

# --- SOCKET EVENTS ---

@socketio.on('connect')
def handle_connect():
    if 'user_id' not in session:
        return False  # disconnect

    room = session['user_id']
    join_room(room)

    users_table.update_one(
        {"_id": ObjectId(room)},
        {"$set": {"last_seen": datetime.utcnow()}}
    )

@socketio.on('new_message')
def handle_message(data):
    if 'user_id' not in session:
        return

    sender_id = session['user_id']
    receiver_id = data.get('receiver_id')
    text = data.get('text', '')
    msg_type = data.get('type', 'text')

    # --- VALIDATION ---
    if not receiver_id or not is_valid_objectid(receiver_id):
        return

    if not text or len(text) > 1000:
        return

    # защита от XSS
    text = html.escape(text)

    # проверка существования получателя
    if not users_table.find_one({"_id": ObjectId(receiver_id)}):
        return

    msg_obj = {
        "sender": sender_id,
        "receiver": receiver_id,
        "text": text,
        "type": msg_type,
        "time": datetime.utcnow().isoformat() + "Z",
        "read": False
    }

    result = messages_table.insert_one(msg_obj)

    msg_obj['id'] = str(result.inserted_id)

    # --- SEND ---
    emit('receive_message', msg_obj, room=receiver_id)
    emit('receive_message', msg_obj, room=sender_id)

@socketio.on('start_typing')
def on_typing(data):
    if 'user_id' not in session:
        return

    receiver_id = data.get('receiver_id')

    if not receiver_id:
        return

    emit(
        'is_typing',
        {"sender_id": session['user_id']},
        room=receiver_id
    )

# --- MARK AS READ ---
@app.route('/mark_read/<user_id>')
def mark_read(user_id):
    if 'user_id' not in session:
        return {"status": "error"}

    my_id = session['user_id']

    messages_table.update_many(
        {
            "sender": user_id,
            "receiver": my_id,
            "read": False
        },
        {"$set": {"read": True}}
    )

    return {"status": "ok"}

# --- RUN ---
if __name__ == '__main__':
    socketio.run(
        app,
        host='0.0.0.0',
        port=5000,
        debug=True
    )
