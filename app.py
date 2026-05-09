import os, re
from flask import Flask, render_template, request, session, redirect, jsonify
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "kadrgram_ultra_2026"
# Настройка сокетов с поддержкой сессий
socketio = SocketIO(app, cors_allowed_origins="*", manage_session=True)

# БАЗА ДАННЫХ
MONGO_URI = "mongodb+srv://Admin:KadrGram01@cluster0.tfe27jw.mongodb.net/?appName=Cluster0"
client = MongoClient(MONGO_URI)
db = client['kadrgram_database']
users_table = db['users']
messages_table = db['messages']

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u_login = request.form.get('login')
        u_pwd = request.form.get('pwd')
        user = users_table.find_one({"login": u_login})
        if user and check_password_hash(user['password'], u_pwd):
            session['user_id'] = str(user['_id'])
            return redirect('/')
        return render_template('login.html', error='wrong_pass')
    return render_template('login.html')

@app.route('/register', methods=['POST'])
def register():
    name = request.form.get('name'); u_login = request.form.get('login'); u_pwd = request.form.get('pwd')
    if users_table.find_one({"login": u_login}): return render_template('login.html', error='login_taken')
    u_id = users_table.insert_one({"name": name, "login": u_login, "password": generate_password_hash(u_pwd), "online": False}).inserted_id
    session['user_id'] = str(u_id)
    return redirect('/')

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
    chat_id = request.args.get('chat_with')
    if chat_id:
        target_user = users_table.find_one({"_id": ObjectId(chat_id)})
        if target_user: 
            target_user['id'] = str(target_user['_id'])
            messages_table.update_many({"sender": chat_id, "receiver": my_id}, {"$set": {"read": True}})
            
    return render_template('index.html', user=user, all_users=all_users, target_user=target_user)

@app.route('/get_messages')
def get_messages():
    my_id = session.get('user_id'); with_id = request.args.get('with')
    if not my_id or not with_id: return jsonify([])
    query = {"$or": [{"sender": my_id, "receiver": with_id}, {"sender": with_id, "receiver": my_id}]}
    msgs = list(messages_table.find(query).sort("time", 1))
    for m in msgs: m['id'] = str(m['_id']); del m['_id']
    return jsonify(msgs)

@app.route('/logout')
def logout():
    session.clear(); return redirect('/login')

# --- SOCKETS ---
@socketio.on('join')
def on_join():
    uid = session.get('user_id')
    if uid:
        join_room(uid)
        users_table.update_one({"_id": ObjectId(uid)}, {"$set": {"online": True}})
        emit('user_status', {"user_id": uid, "online": True}, broadcast=True)

@socketio.on('new_message')
def handle_message(data):
    sid = str(session.get('user_id'))
    rid = str(data.get('receiver_id'))
    if not sid or not rid or rid == "None": return

    msg = {
        "sender": sid, "receiver": rid, "text": data.get('text'),
        "time": datetime.utcnow().isoformat() + "Z", "read": False
    }
    msg['id'] = str(messages_table.insert_one(msg).inserted_id)
    print(f"MSG: {sid} -> {rid}: {data.get('text')}")
    
    emit('receive_message', msg, room=rid)
    emit('receive_message', msg, room=sid)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
