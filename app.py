from flask import Flask, render_template, request, redirect, session
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient

app = Flask(__name__)
app.secret_key = "secret123"

socketio = SocketIO(app)

# MongoDB
client = MongoClient("mongodb://localhost:27017/")
db = client["kadrgram"]
users = db["users"]
messages = db["messages"]

# ---------- ROUTES ----------

@app.route('/')
def index():
    if "user" not in session:
        return redirect("/login")

    all_users = list(users.find({}, {"_id": 0, "username": 1}))
    return render_template("index.html", users=all_users)

@app.route('/register', methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        if users.find_one({"username": username}):
            return "User already exists"

        hashed = generate_password_hash(password)

        users.insert_one({
            "username": username,
            "password": hashed
        })

        return redirect("/login")

    return render_template("register.html")

@app.route('/login', methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        user = users.find_one({"username": username})

        if user and check_password_hash(user["password"], password):
            session["user"] = username
            return redirect("/")

        return "Invalid login"

    return render_template("login.html")

# ---------- SOCKET ----------

@socketio.on("join")
def on_join(data):
    room = "_".join(sorted([data["user1"], data["user2"]]))
    join_room(room)

@socketio.on("send_message")
def handle_message(data):
    sender = data["sender"]
    receiver = data["receiver"]
    text = data["text"]

    room = "_".join(sorted([sender, receiver]))

    msg = {
        "sender": sender,
        "receiver": receiver,
        "text": text
    }

    messages.insert_one(msg)

    emit("receive_message", msg, room=room)

# ---------- RUN ----------

if __name__ == "__main__":
    socketio.run(app, debug=True)
