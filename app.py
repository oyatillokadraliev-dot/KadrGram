import os, re
from flask import Flask, render_template, request, session, redirect, jsonify
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "kadrgram_ultra_2026"
socketio = SocketIO(app, cors_allowed_origins="*")

# БАЗА ДАННЫХ
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://Admin:KadrGram01@cluster0.tfe27jw.mongodb.net/?appName=Cluster0")
client = MongoClient(MONGO_URI)
db = client['kadrgram_database']
users_table = db['users']
messages_table = db['messages']

# --- АВТОРИЗАЦИЯ ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        login_val = request.form.get('login')
        pwd = request.form.get('pwd')
        
        user = users_table.find_one({"login": login_val})
        if user and check_password_hash(user['password'], pwd):
            session['user_id'] = str(user['_id'])
            return redirect('/')
        return render_template('login.html', error='wrong_pass')
    return render_template('login.html')

@app.route('/register', methods=['POST'])
def register():
    name = request.form.get('name')
    login_val = request.form.get('login')
    pwd = request.form.get('pwd')

    # Базовая проверка ника
    if not re.match(r"^[A-Za-z0-9]+$", login_val):
        return render_template('login.html', error='invalid_data')

    if users_table.find_one({"login": login_val}):
        return render_template('login.html', error='login_taken')

    new_user = {
        "name": name,
        "login": login_val,
        "password": generate_password_hash(pwd),
        "online": False,
        "last_seen": datetime.utcnow()
    }
    user_id = users_table.insert_one(new_user).inserted_id
    session['user_id'] = str(user_id)
    return redirect('/')

@app.route('/logout')
def logout():
    user_id = session.get('user_id')
    if user_id:
        users_table.update_one({"_id": ObjectId(user_id)}, {"$set": {"online": False}})
    session.clear()
    return redirect('/login')

# --- ГЛАВНАЯ И ЧАТ ---

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
        if target_user: 
            target_user['id'] = str(target_user['_id'])
            # Сбрасываем счетчик при входе в чат
            messages_table.update_many({"sender": chat_with_id, "receiver": my_id}, {"$set": {"read": True}})
            
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
    return jsonify(messages)

# --- SOCKET LOGIC ---

@socketio.on('join')
def on_join():
    uid = session.get('user_id')
    if uid:
        join_room(uid)
        users_table.update_one({"_id": ObjectId(uid)}, {"$set": {"online": True}})
        emit('user_status', {"user_id": uid, "online": True}, broadcast=True)

@socketio.on('disconnect')
def on_disconnect():
    uid = session.get('user_id')
    if uid:
        users_table.update_one({"_id": ObjectId(uid)}, {"$set": {"online": False, "last_seen": datetime.utcnow()}})
        emit('user_status', {"user_id": uid, "online": False}, broadcast=True)

@socketio.on('new_message')
def handle_message(data):
    sid = session.get('user_id')
    rid = data.get('receiver_id')
    if not sid or not rid: return

    msg_obj = {
        "sender": str(sid),
        "receiver": str(rid),
        "text": data.get('text'),
        "type": data.get('type', 'text'),
        "time": datetime.utcnow().isoformat() + "Z",
        "read": False
    }
    msg_obj['id'] = str(messages_table.insert_one(msg_obj).inserted_id)
    
    emit('receive_message', msg_obj, room=str(rid))
    emit('receive_message', msg_obj, room=str(sid))

@socketio.on('start_typing')
def on_typing(data):
    emit('is_typing', {"sender_id": session.get('user_id')}, room=str(data['receiver_id']))

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
