from flask import Flask, render_template, request, session, redirect, jsonify, flash
from tinydb import TinyDB, Query
from datetime import datetime
import re

app = Flask(__name__)
app.secret_key = "kadrgram_ultra_2026"

db = TinyDB('db.json')
users_table = db.table('users')
messages_table = db.table('messages')
User = Query()

@app.route('/')
def home():
    if 'user_id' not in session: return redirect('/login')
    my_id = str(session['user_id'])
    user = users_table.get(doc_id=int(my_id))
    if not user: return redirect('/login')
    
    all_users = [u for u in users_table.all() if str(u.doc_id) != my_id]
    for u in all_users:
        u['id'] = str(u.doc_id)
        msg_q = Query()
        unread = messages_table.search((msg_q.sender == u['id']) & (msg_q.receiver == my_id) & (msg_q.read == False))
        u['unread'] = len(unread)

    chat_with_id = request.args.get('chat_with')
    target_user = users_table.get(doc_id=int(chat_with_id)) if chat_with_id else None
    return render_template('index.html', user=user, all_users=all_users, target_user=target_user)

@app.route('/get_contacts')
def get_contacts():
    if 'user_id' not in session: return jsonify({"users": []})
    my_id = str(session['user_id'])
    all_users = [u for u in users_table.all() if str(u.doc_id) != my_id]
    for u in all_users:
        u['id'] = str(u.doc_id)
        msg_q = Query()
        unread = messages_table.search((msg_q.sender == u['id']) & (msg_q.receiver == my_id) & (msg_q.read == False))
        u['unread'] = len(unread)
    return jsonify({"users": all_users})

@app.route('/get_messages')
def get_messages():
    chat_with = request.args.get('chat_with')
    my_id = str(session.get('user_id'))
    msg_q = Query()
    msgs = messages_table.search(
        ((msg_q.sender == my_id) & (msg_q.receiver == chat_with)) |
        ((msg_q.sender == chat_with) & (msg_q.receiver == my_id))
    )
    for m in msgs:
        m['id'] = m.doc_id
        m['display_time'] = m['time'][11:16] if 'time' in m else ""
    return jsonify({"messages": msgs, "my_id": my_id})

@app.route('/send_simple', methods=['POST'])
def send_simple():
    data = request.get_json()
    messages_table.insert({
        "sender": str(session.get('user_id')),
        "receiver": str(data['receiver_id']),
        "text": data['text'],
        "time": datetime.now().isoformat(),
        "read": False
    })
    return jsonify({"status": "ok"})

@app.route('/mark_read', methods=['POST'])
def mark_read():
    chat_with = request.json.get('chat_with')
    my_id = str(session.get('user_id'))
    msg_q = Query()
    messages_table.update({'read': True}, (msg_q.sender == chat_with) & (msg_q.receiver == my_id))
    return jsonify({"status": "ok"})

@app.route('/check_all_notifications')
def check_all_notifications():
    if 'user_id' not in session: return jsonify({"total": 0})
    my_id = str(session.get('user_id'))
    msg_q = Query()
    unread_msgs = messages_table.search((msg_q.receiver == my_id) & (msg_q.read == False))
    return jsonify({"total": len(unread_msgs)})

@app.route('/delete_message', methods=['POST'])
def delete_message():
    data = request.get_json()
    msg_id = int(data.get('msg_id'))
    messages_table.remove(doc_ids=[msg_id])
    return jsonify({"status": "ok"})

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        res = users_table.get((User.login == request.form.get('login')) & (User.password == request.form.get('pwd')))
        if res:
            session['user_id'] = str(res.doc_id)
            return redirect('/')
    return render_template('login.html')

@app.route('/register', methods=['POST'])
def register():
    new_id = users_table.insert({"login": request.form.get('login'), "password": request.form.get('pwd'), "name": request.form.get('name')})
    session['user_id'] = str(new_id)
    return redirect('/')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)