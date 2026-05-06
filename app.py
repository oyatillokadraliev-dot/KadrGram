import os, re
from flask import Flask, render_template, request, session, redirect, jsonify, url_for
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "kadrgram_ultra_2026"
# Инициализация сокетов (cors_allowed_origins="*" для доступа с любых устройств)
socketio = SocketIO(app, cors_allowed_origins="*")

# БАЗА ДАННЫХ
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://Admin:KadrGram01@cluster0.tfe27jw.mongodb.net/?appName=Cluster0")
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

# --- ЛОГИКА СОКЕТОВ ---

@socketio.on('join')
def on_join(data):
    # Пользователь заходит в свою персональную "комнату" по ID
    room = session.get('user_id')
    if room:
        join_room(room)
        users_table.update_one({"_id": ObjectId(room)}, {"$set": {"last_seen": datetime.utcnow()}})

@socketio.on('new_message')
def handle_message(data):
    my_id = session.get('user_id')
    receiver_id = data.get('receiver_id')
    text = data.get('text')
    m_type = data.get('type', 'text')
    
    if not my_id or not receiver_id: return

    msg_obj = {
        "sender": str(my_id),
        "receiver": str(receiver_id),
        "text": text,
        "type": m_type,
        "time": datetime.utcnow().isoformat() + "Z",
        "read": False
    }
    # Сохраняем в базу
    messages_table.insert_one(msg_obj)
    msg_obj['id'] = str(msg_obj['_id'])
    del msg_obj['_id']

    # Мгновенно отправляем получателю в его комнату
    emit('receive_message', msg_obj, room=str(receiver_id))
    # И себе (для подтверждения)
    emit('receive_message', msg_obj, room=str(my_id))

@socketio.on('start_typing')
def on_typing(data):
    emit('is_typing', {"sender_id": session.get('user_id')}, room=str(data['receiver_id']))

@app.route('/download_socket_io')
def download_socket():
    import urllib.request
    import os
    url = "https://cloudflare.com"
    try:
        if not os.path.exists('static'):
            os.makedirs('static')
        
        # Добавляем заголовок "браузера", чтобы нас не блокировали
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        )
        
        with urllib.request.urlopen(req) as response:
            with open("static/socket.io.js", "wb") as f:
                f.write(response.read())
            
        return "УСПЕХ! Файл теперь на сервере. Обнови главную страницу."
    except Exception as e:
        return f"Ошибка: {str(e)}"



if __name__ == '__main__':
    # Для локального запуска
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
