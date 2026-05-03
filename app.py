import os
from flask import Flask, render_template, request, session, redirect, jsonify
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime

app = Flask(__name__)
app.secret_key = "kadrgram_ultra_2026"

# ПОДКЛЮЧЕНИЕ К БАЗЕ
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://Admin:KadrGram01@cluster0.tfe27jw.mongodb.net/?appName=Cluster0")
client = MongoClient(MONGO_URI)
db = client['kadrgram_database']
users_table = db['users']
messages_table = db['messages']

@app.route('/')
def home():
    if 'user_id' not in session: return redirect('/login')
    my_id = session['user_id']
    
    try:
        user = users_table.find_one({"_id": ObjectId(my_id)})
    except:
        return redirect('/login')
        
    if not user: return redirect('/login')
    
    all_users = list(users_table.find({"_id": {"$ne": ObjectId(my_id)}}))
    for u in all_users:
        u['id'] = str(u['_id'])
        u['unread'] = messages_table.count_documents({"sender": u['id'], "receiver": my_id, "read": False})

    chat_with_id = request.args.get('chat_with')
    target_user = None
    if chat_with_id:
        try:
            target_user = users_table.find_one({"_id": ObjectId(chat_with_id)})
            if target_user: target_user['id'] = str(target_user['_id'])
        except:
            target_user = None
            
    return render_template('index.html', user=user, all_users=all_users, target_user=target_user)

@app.route('/get_messages')
def get_messages():
    chat_with = request.args.get('chat_with')
    my_id = session.get('user_id')
    
    if not chat_with or not my_id:
        return jsonify({"messages": [], "my_id": my_id})

    try:
        # ПОМЕЧАЕМ КАК ПРОЧИТАННЫЕ: когда я загружаю чат с кем-то, 
        # все сообщения от него ко мне становятся read: True
        messages_table.update_many(
            {"sender": chat_with, "receiver": my_id, "read": False},
            {"$set": {"read": True}}
        )

        msgs = list(messages_table.find({
            "$or": [
                {"sender": my_id, "receiver": chat_with},
                {"sender": chat_with, "receiver": my_id}
            ]
        }))
        
        for m in msgs:
            m['id'] = str(m['_id'])
            del m['_id']
            m['display_time'] = m.get('time', "")[11:16]
            
        return jsonify({"messages": msgs, "my_id": my_id})
    except Exception as e:
        return jsonify({"messages": [], "error": str(e)}), 500

@app.route('/get_contacts')
def get_contacts():
    """Эндпоинт для обновления счетчиков в реальном времени"""
    my_id = session.get('user_id')
    if not my_id: return jsonify([])
    
    all_users = list(users_table.find({"_id": {"$ne": ObjectId(my_id)}}))
    contacts_data = []
    for u in all_users:
        contacts_data.append({
            "id": str(u['_id']),
            "unread": messages_table.count_documents({"sender": str(u['_id']), "receiver": my_id, "read": False})
        })
    return jsonify(contacts_data)

@app.route('/send_simple', methods=['POST'])
def send_simple():
    data = request.get_json()
    my_id = session.get('user_id')
    
    if not data or not my_id:
        return jsonify({"status": "error"}), 400
        
    messages_table.insert_one({
        "sender": str(my_id),
        "receiver": str(data.get('receiver_id')),
        "text": data.get('text'),
        "time": datetime.now().isoformat(),
        "read": False
    })
    return jsonify({"status": "ok"})

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        res = users_table.find_one({"login": request.form.get('login'), "password": request.form.get('pwd')})
        if res:
            session['user_id'] = str(res['_id'])
            return redirect('/')
    return render_template('login.html')

@app.route('/register', methods=['POST'])
def register():
    login_val = request.form.get('login')
    name_val = request.form.get('name')
    if not login_val or not name_val: return redirect('/login')
    
    new_user = users_table.insert_one({
        "login": login_val, 
        "password": request.form.get('pwd'), 
        "name": name_val
    })
    session['user_id'] = str(new_user.inserted_id)
    return redirect('/')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
