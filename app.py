import os, re
from flask import Flask, render_template, request, session, redirect, jsonify, url_for
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = "kadrgram_ultra_2026"

# ПОДКЛЮЧЕНИЕ К БАЗЕ
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://Admin:KadrGram01@cluster0.tfe27jw.mongodb.net/?appName=Cluster0")
client = MongoClient(MONGO_URI)
db = client['kadrgram_database']
users_table = db['users']
messages_table = db['messages']

users_table.create_index("login", unique=True)

def update_last_seen():
    """Обновляет время последней активности текущего пользователя"""
    my_id = session.get('user_id')
    if my_id:
        users_table.update_one(
            {"_id": ObjectId(my_id)},
            {"$set": {"last_seen": datetime.utcnow()}}
        )

@app.route('/')
def home():
    if 'user_id' not in session: return redirect('/login')
    my_id = session['user_id']
    try:
        user = users_table.find_one({"_id": ObjectId(my_id)})
        if not user: return redirect('/login')
    except: return redirect('/login')
    
    update_last_seen()
    all_users = list(users_table.find({"_id": {"$ne": ObjectId(my_id)}}))
    now = datetime.utcnow()
    
    for u in all_users:
        u['id'] = str(u['_id'])
        u['unread'] = messages_table.count_documents({"sender": u['id'], "receiver": my_id, "read": False})
        # Проверка онлайн для первой загрузки
        ls = u.get('last_seen')
        u['online'] = (now - ls).total_seconds() < 25 if ls else False

    chat_with_id = request.args.get('chat_with')
    target_user = None
    if chat_with_id:
        try:
            target_user = users_table.find_one({"_id": ObjectId(chat_with_id)})
            if target_user: target_user['id'] = str(target_user['_id'])
        except: target_user = None
            
    return render_template('index.html', user=user, all_users=all_users, target_user=target_user)

@app.route('/get_messages')
def get_messages():
    update_last_seen()
    chat_with = request.args.get('chat_with')
    my_id = session.get('user_id')
    if not chat_with or not my_id: return jsonify({"messages": [], "my_id": my_id})
    try:
        messages_table.update_many({"sender": chat_with, "receiver": my_id, "read": False}, {"$set": {"read": True}})
        msgs = list(messages_table.find({"$or": [{"sender": my_id, "receiver": chat_with}, {"sender": chat_with, "receiver": my_id}]}))
        for m in msgs:
            m['id'] = str(m['_id'])
            del m['_id']
            m['utc_time'] = m.get('time', "")
            m['read'] = m.get('read', False)
        return jsonify({"messages": msgs, "my_id": my_id})
    except Exception as e: return jsonify({"messages": [], "error": str(e)}), 500

@app.route('/get_contacts')
def get_contacts():
    update_last_seen()
    my_id = session.get('user_id')
    if not my_id: return jsonify([])
    
    all_users = list(users_table.find({"_id": {"$ne": ObjectId(my_id)}}))
    contacts_data = []
    now = datetime.utcnow()
    
    for u in all_users:
        ls = u.get('last_seen')
        is_online = (now - ls).total_seconds() < 25 if ls else False
        contacts_data.append({
            "id": str(u['_id']),
            "unread": messages_table.count_documents({"sender": str(u['_id']), "receiver": my_id, "read": False}),
            "online": is_online
        })
    return jsonify(contacts_data)

@app.route('/send_simple', methods=['POST'])
def send_simple():
    update_last_seen()
    data = request.get_json()
    my_id = session.get('user_id')
    if not data or not my_id: return jsonify({"status": "error"}), 400
    messages_table.insert_one({
        "sender": str(my_id),
        "receiver": str(data.get('receiver_id')),
        "text": data.get('text'),
        "time": datetime.utcnow().isoformat() + "Z",
        "read": False
    })
    return jsonify({"status": "ok"})

@app.route('/search')
def search():
    update_last_seen()
    if 'user_id' not in session: return redirect('/login')
    query = request.args.get('query', '').strip()
    results = []
    if query:
        found = users_table.find({"$and": [{"_id": {"$ne": ObjectId(session['user_id'])}}, {"$or": [{"name": {"$regex": query, "$options": "i"}}, {"login": {"$regex": query, "$options": "i"}}]}]})
        for u in found:
            results.append({"id": str(u['_id']), "name": u.get('name'), "login": u.get('login')})
    return render_template('search.html', results=results)

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = request.args.get('error')
    if request.method == 'POST':
        res = users_table.find_one({"login": request.form.get('login'), "password": request.form.get('pwd')})
        if res:
            session['user_id'] = str(res['_id'])
            return redirect('/')
        return redirect(url_for('login', error="wrong_pass"))
    return render_template('login.html', error=error)

@app.route('/register', methods=['POST'])
def register():
    l, p, n = request.form.get('login', '').strip(), request.form.get('pwd', ''), request.form.get('name', '').strip()
    if users_table.find_one({"login": l}): return redirect(url_for('login', error="login_taken"))
    if not re.match(r"^[A-Za-z0-9]+$", l) or len(p) < 7 or not any(x.isupper() for x in p):
        return redirect(url_for('login', error="invalid_data"))
    new_user = users_table.insert_one({"login": l, "password": p, "name": n, "last_seen": datetime.utcnow()})
    session['user_id'] = str(new_user.inserted_id)
    return redirect('/')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
