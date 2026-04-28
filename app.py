MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://Admin:KadrGram01@cluster0.tfe27jw.mongodb.net/?appName=Cluster0")

from flask import Flask, render_template, request, session, redirect, jsonify, flash
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime
import os

app = Flask(__name__)
app.secret_key = "kadrgram_ultra_2026"

# ПОДКЛЮЧЕНИЕ К БАЗЕ
# Когда будешь на Render, добавишь MONGO_URI в настройки. 
# Пока сайта нет, программа будет просто ждать ссылку.
MONGO_URI = os.environ.get("MONGO_URI", "ЗДЕСЬ_БУДЕТ_ТВОЯ_ССЫЛКА_ИЗ_MONGODB")
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
    
    # Получаем всех пользователей кроме себя
    all_users = list(users_table.find({"_id": {"$ne": ObjectId(my_id)}}))
    for u in all_users:
        u['id'] = str(u['_id'])
        unread_count = messages_table.count_documents({
            "sender": u['id'], 
            "receiver": my_id, 
            "read": False
        })
        u['unread'] = unread_count

    chat_with_id = request.args.get('chat_with')
    target_user = users_table.find_one({"_id": ObjectId(chat_with_id)}) if chat_with_id else None
    if target_user: target_user['id'] = str(target_user['_id'])
    
    return render_template('index.html', user=user, all_users=all_users, target_user=target_user)

@app.route('/get_messages')
def get_messages():
    chat_with = request.args.get('chat_with')
    my_id = session.get('user_id')
    msgs = list(messages_table.find({
        "$or": [
            {"sender": my_id, "receiver": chat_with},
            {"sender": chat_with, "receiver": my_id}
        ]
    }))
    for m in msgs:
        m['id'] = str(m['_id'])
        m['display_time'] = m['time'][11:16] if 'time' in m else ""
    return jsonify({"messages": msgs, "my_id": my_id})

@app.route('/send_simple', methods=['POST'])
def send_simple():
    data = request.get_json()
    messages_table.insert_one({
        "sender": str(session.get('user_id')),
        "receiver": str(data['receiver_id']),
        "text": data['text'],
        "time": datetime.now().isoformat(),
        "read": False
    })
    return jsonify({"status": "ok"})

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        login_val = request.form.get('login')
        pwd_val = request.form.get('pwd')
        res = users_table.find_one({"login": login_val, "password": pwd_val})
        if res:
            session['user_id'] = str(res['_id'])
            return redirect('/')
    return render_template('login.html')

@app.route('/register', methods=['POST'])
def register():
    login_val = request.form.get('login')
    pwd_val = request.form.get('pwd')
    name_val = request.form.get('name')
    
    new_user = users_table.insert_one({
        "login": login_val, 
        "password": pwd_val, 
        "name": name_val
    })
    session['user_id'] = str(new_user.inserted_id)
    return redirect('/')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

# Удаление сообщения (пример)
@app.route('/delete_message', methods=['POST'])
def delete_message():
    data = request.get_json()
    msg_id = data.get('msg_id')
    messages_table.delete_one({"_id": ObjectId(msg_id)})
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    # Настройка порта для Render
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)