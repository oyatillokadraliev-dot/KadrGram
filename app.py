import os, re
from flask import Flask, render_template, request, session, redirect, jsonify, url_for, send_from_directory
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "kadrgram_ultra_2026"

# НАСТРОЙКИ БЕЗОПАСНОСТИ ФАЙЛОВ
UPLOAD_FOLDER = 'uploads'
if not os.path.exists(UPLOAD_FOLDER): os.makedirs(UPLOAD_FOLDER)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024 # 10МБ
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'docx', 'txt'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# БАЗА ДАННЫХ
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://Admin:KadrGram01@cluster0.tfe27jw.mongodb.net/?appName=Cluster0")
client = MongoClient(MONGO_URI)
db = client['kadrgram_database']
users_table = db['users']
messages_table = db['messages']
users_table.create_index("login", unique=True)

def update_last_seen():
    my_id = session.get('user_id')
    if my_id:
        users_table.update_one({"_id": ObjectId(my_id)}, {"$set": {"last_seen": datetime.utcnow()}})

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
        ls = u.get('last_seen')
        u['online'] = (now - ls).total_seconds() < 25 if ls else False
    
    target_user = None
    chat_with_id = request.args.get('chat_with')
    if chat_with_id:
        try:
            target_user = users_table.find_one({"_id": ObjectId(chat_with_id)})
            if target_user:
                target_user['id'] = str(target_user['_id'])
                ls = target_user.get('last_seen')
                target_user['last_seen_iso'] = ls.isoformat() + "Z" if ls else None
                target_user['online'] = (now - ls).total_seconds() < 25 if ls else False
        except: pass
            
    return render_template('index.html', user=user, all_users=all_users, target_user=target_user)

@app.route('/upload_file', methods=['POST'])
def upload_file():
    if 'user_id' not in session: return jsonify({"status": "error"}), 401
    
    # Обработка картинок (Base64 через JSON)
    if request.is_json:
        data = request.get_json()
        messages_table.insert_one({
            "sender": str(session['user_id']),
            "receiver": str(data.get('receiver_id')),
            "text": data.get('file_data'),
            "type": "image",
            "filename": data.get('filename'),
            "time": datetime.utcnow().isoformat() + "Z",
            "read": False
        })
        return jsonify({"status": "ok"})
    
    # Обработка файлов (FormData)
    file = request.files.get('file')
    receiver_id = request.form.get('receiver_id')
    if file and allowed_file(file.filename):
        filename = secure_filename(f"{datetime.utcnow().timestamp()}_{file.filename}")
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        messages_table.insert_one({
            "sender": str(session['user_id']),
            "receiver": str(receiver_id),
            "text": f"/uploads/{filename}",
            "type": "file",
            "filename": file.filename,
            "time": datetime.utcnow().isoformat() + "Z",
            "read": False
        })
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "File not allowed"}), 400

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
            m['type'] = m.get('type', 'text')
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
        lt = u.get('last_typing')
        contacts_data.append({
            "id": str(u['_id']),
            "unread": messages_table.count_documents({"sender": str(u['_id']), "receiver": my_id, "read": False}),
            "online": (now - ls).total_seconds() < 25 if ls else False,
            "typing": (now - lt).total_seconds() < 5 if (u.get('typing_to') == my_id and lt) else False,
            "last_seen_iso": ls.isoformat() + "Z" if ls else None
        })
    return jsonify(contacts_data)

@app.route('/typing', methods=['POST'])
def typing():
    my_id = session.get('user_id')
    target_id = request.json.get('target_id')
    if my_id:
        users_table.update_one({"_id": ObjectId(my_id)}, {"$set": {"typing_to": target_id, "last_typing": datetime.utcnow()}})
    return jsonify({"status": "ok"})

@app.route('/send_simple', methods=['POST'])
def send_simple():
    update_last_seen()
    data = request.get_json()
    my_id = session.get('user_id')
    if not data or not my_id: return jsonify({"status": "error"}), 400
    messages_table.insert_one({
        "sender": str(my_id), "receiver": str(data.get('receiver_id')),
        "text": data.get('text'), "time": datetime.utcnow().isoformat() + "Z", "read": False, "type": "text"
    })
    users_table.update_one({"_id": ObjectId(my_id)}, {"$unset": {"typing_to": "", "last_typing": ""}})
    return jsonify({"status": "ok"})

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = request.args.get('error')
    if request.method == 'POST':
        u_login = request.form.get('login')
        u_pwd = request.form.get('pwd')
        user = users_table.find_one({"login": u_login})
        if user and check_password_hash(user["password"], u_pwd):
            session['user_id'] = str(user['_id'])
            return redirect('/')
        return redirect(url_for('login', error="wrong_pass"))
    return render_template('login.html', error=error)

@app.route('/register', methods=['POST'])
def register():
    l, p, n = request.form.get('login', '').strip(), request.form.get('pwd', ''), request.form.get('name', '').strip()
    if users_table.find_one({"login": l}): return redirect(url_for('login', error="login_taken"))
    if not re.match(r"^[A-Za-z0-9]+$", l) or len(p) < 7 or not any(x.isupper() for x in p):
        return redirect(url_for('login', error="invalid_data"))
    hashed = generate_password_hash(p)
    new_user = users_table.insert_one({"login": l, "password": hashed, "name": n, "last_seen": datetime.utcnow()})
    session['user_id'] = str(new_user.inserted_id)
    return redirect('/')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
