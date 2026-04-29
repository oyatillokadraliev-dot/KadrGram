import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, redirect, session, jsonify
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient

app = Flask(__name__)
app.secret_key = "secret123"

# Инициализируем SocketIO один раз
socketio = SocketIO(app, cors_allowed_origins="*")

# MongoDB
client = MongoClient("mongodb://localhost:27017/")
db = client['chat_db']
users = db['users']
messages = db['messages']

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        
        if users.find_one({"username": username}):
            return "User already exists"
        
        hashed = generate_password_hash(password)
        users.insert_one({
            "username": username,
            "password": hashed
        })
        return redirect("/login")
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        user = users.find_one({"username": username})
        
        if user and check_password_hash(user["password"], password):
            session["username"] = username
            return redirect("/")
        return "Invalid credentials"
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ---------- HISTORY ----------
@app.route("/get_messages/<user1>/<user2>")
def get_messages(user1, user2):
    msgs = list(messages.find({
        "$or": [
            {"sender": user1, "receiver": user2},
            {"sender": user2, "receiver": user1}
        ]
    }, {"_id": 0}))
    return jsonify({"messages": msgs})

# ---------- SOCKET ----------
@socketio.on("join")
def on_join(data):
    username = data["username"]
    room = data["room"]
    join_room(room)
    print(f"{username} joined room: {room}")

@socketio.on("send_message")
def handle_message(data):
    sender = data["sender"]
    receiver = data["receiver"]
    msg_content = data["message"]
    room = data["room"]
    
    msg = {
        "sender": sender,
        "receiver": receiver,
        "message": msg_content
    }
    messages.insert_one(msg.copy()) # Сохраняем в БД
    
    if "_id" in msg: del msg["_id"] # Удаляем ObjectId перед отправкой в JSON
    emit("receive_message", msg, room=room)

# ---------- RUN ----------
if __name__ == "__main__":
    # Запускаем один раз с нужными параметрами
    socketio.run(
        app, 
        host="0.0.0.0", 
        port=5000, 
        debug=True, 
        allow_unsafe_werkzeug=True
    )
