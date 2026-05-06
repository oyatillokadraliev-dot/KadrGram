from flask import Flask, render_template, session
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
import uuid

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'

# Socket.IO
socketio = SocketIO(app, cors_allowed_origins="*", manage_session=True)

# MongoDB
client = MongoClient("mongodb://localhost:27017/")
db = client["chat_db"]
messages_table = db["messages"]

@app.route('/')
def index():
    # создаём пользователя
    if 'user_id' not in session:
        session['user_id'] = str(uuid.uuid4())[:8]

    return render_template('index.html', user=session['user_id'])

# подключение к комнате
@socketio.on('join')
def on_join():
    user_room = session.get('user_id')

    if user_room:
        join_room(user_room)
        join_room('global')  # общий чат

    print(f"User {user_room} joined")

# отправка сообщения
@socketio.on('send_message')
def handle_message(data):
    user = session.get('user_id', 'anon')
    message = data.get('message')

    if not message:
        return

    msg_obj = {
        'user': user,
        'message': message
    }

    # сохраняем в Mongo
    result = messages_table.insert_one(msg_obj)

    msg_obj['id'] = str(result.inserted_id)

    # отправляем всем
    emit('receive_message', msg_obj, to='global')

if __name__ == '__main__':
    socketio.run(app, debug=True)
