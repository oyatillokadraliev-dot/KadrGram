import os
from flask import Flask, render_template, request, session, redirect, jsonify
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime

app = Flask(__name__)
app.secret_key = "kadrgram_ultra_2026"
socketio = SocketIO(app, cors_allowed_origins="*")

# БАЗА ДАННЫХ
MONGO_URI = os.environ.get("MONGO_URI", "твой_адрес_монго")
client = MongoClient(MONGO_URI)
db = client['kadrgram_database']
users_table = db['users']
messages_table = db['messages']

@app.route('/')
def home():
    if 'user_id' not in session: return redirect('/login')
    my_id = session['user_id']
    user = users_table.find_one({"_id": ObjectId(my_id)})
    if not user: return redirect('/login')

    all_users = list(users_table.find({"_id": {"$ne": ObjectId(my_id)}}))
    for u in all_users:
        u['id'] = str(u['_id'])
        u['unread'] = messages_table.count_documents({"sender": u['id'], "receiver": my_id, "read": False})

    target_user = None
    chat_with_id = request.args.get('chat_with')
    if chat_with_id:
        target_user = users_table.find_one({"_id": ObjectId(chat_with_id)})
        if target_user: target_user['id'] = str(target_user['_id'])
            
    return render_template('index.html', user=user, all_users=all_users, target_user=target_user)

@app.route('/get_messages')
def get_messages():
    my_id = session.get('user_id')
    with_id = request.args.get('with')
    if not my_id or not with_id: return jsonify([])

    query = {"$or": [{"sender": my_id, "receiver": with_id}, {"sender": with_id, "receiver": my_id}]}
    messages = list(messages_table.find(query).sort("time", 1))
    for m in messages:
        m['id'] = str(m['_id'])
        del m['_id']
    
    messages_table.update_many({"sender": with_id, "receiver": my_id}, {"$set": {"read": True}})
    return jsonify(messages)

# --- SOCKET LOGIC ---

@socketio.on('join')
def on_join():
    user_id = session.get('user_id')
    if user_id:
        join_room(user_id)
        users_table.update_one({"_id": ObjectId(user_id)}, {"$set": {"online": True}})

@socketio.on('new_message')
def handle_message(data):
    sender_id = session.get('user_id')
    receiver_id = data.get('receiver_id')
    if not sender_id or not receiver_id: return

    msg_obj = {
        "sender": str(sender_id),
        "receiver": str(receiver_id),
        "text": data.get('text'),
        "type": data.get('type', 'text'),
        "time": datetime.utcnow().isoformat() + "Z",
        "read": False
    }
    msg_obj['id'] = str(messages_table.insert_one(msg_obj).inserted_id)
    
    emit('receive_message', msg_obj, room=str(receiver_id))
    emit('receive_message', msg_obj, room=str(sender_id))

@socketio.on('start_typing')
def on_typing(data):
    emit('is_typing', {"sender_id": session.get('user_id')}, room=str(data['receiver_id']))

if __name__ == '__main__':
    socketio.run(app, debug=True)
