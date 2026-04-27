from flask import Flask, send_file, request, session
import json

app = Flask(__name__)
app.secret_key = "supersecretkey123"  # change later

bot_running = False

# load users
def load_users():
    with open("users.json", "r") as f:
        return json.load(f)

# save users
def save_users(users):
    with open("users.json", "w") as f:
        json.dump(users, f)

@app.route("/")
def home():
    if "user" not in session:
        return send_file("login.html")
    return send_file("index.html")

@app.route("/signup-page")
def signup_page():
    return send_file("signup.html")

@app.route("/signup", methods=["POST"])
def signup():
    users = load_users()

    data = request.get_json()
    username = data["username"]
    password = data["password"]

    if username in users:
        return "User already exists"

    users[username] = password
    save_users(users)

    return "Account created successfully"

@app.route("/login", methods=["POST"])
def login():
    users = load_users()

    data = request.get_json()
    username = data["username"]
    password = data["password"]

    if username in users and users[username] == password:
        session["user"] = username
        return "success"

    return "fail"

@app.route("/logout")
def logout():
    session.pop("user", None)
    return "Logged out"

@app.route("/start")
def start():
    if "user" not in session:
        return "Unauthorized"

    global bot_running
    bot_running = True
    return "Running"

@app.route("/status")
def status():
    if "user" not in session:
        return "Unauthorized"

    return "Running" if bot_running else "Stopped"

if __name__ == "__main__":
    app.run(debug=True)