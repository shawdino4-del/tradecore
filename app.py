from flask import Flask, jsonify, render_template, request, redirect, session, Response
from functools import wraps
from dotenv import load_dotenv
from datetime import datetime, timedelta
import threading
import time
import json
import websocket
import os
import csv
import io
import uuid
import urllib.request
import urllib.parse

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback_secret_key")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "change_this_password")

DEMO_DERIV_TOKEN = os.getenv("DEMO_DERIV_TOKEN") or os.getenv("DERIV_TOKEN")
REAL_DERIV_TOKEN = os.getenv("REAL_DERIV_TOKEN")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
VIP_GROUP_ID = os.getenv("VIP_GROUP_ID", "")
UPDATES_GROUP_ID = os.getenv("UPDATES_GROUP_ID", "")

DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"
SETTINGS_FILE = "settings.json"
SUBSCRIBERS_FILE = "subscribers.json"

MARKET_SYMBOLS = [
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
    "JD10", "JD25", "JD50", "JD75", "JD100",
    "BOOM300", "BOOM500", "BOOM1000",
    "CRASH300", "CRASH500", "CRASH1000"
]

active_account = {"mode": os.getenv("ACTIVE_ACCOUNT", "DEMO").upper()}

logs = []
price_history = {s: [] for s in MARKET_SYMBOLS}
open_contracts = []
trade_history = []

market_data = {
    "symbol": "R_75",
    "price": 0,
    "status": "Disconnected",
    "last_update": "Waiting",
    "best_symbol": "R_75",
    "best_score": 0
}

auto_settings = {
    "auto_mode": False,
    "symbol": "R_75",
    "strategy_mode": "digits",
    "stake": 1,
    "currency": "USD",
    "cooldown_seconds": 60,

    "max_trades": 5,
    "profit_target": 3,
    "daily_loss_limit": 2,

    "profit_protection": True,
    "protect_at_profit": 1.5,
    "max_giveback": 0.75,

    "base_required_score": 90,
    "learning_mode": True,

    "digit_pro_ticks": 1
}

session_data = {
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "profit": 0,
    "peak_profit": 0,
    "profit_protected": False,
    "loss_streak": 0,
    "last_trade_time": 0,
    "session_locked": False,
    "lock_reason": "",
    "last_status": "Waiting",
    "last_signal": "NONE",
    "last_score": 0,
    "last_symbol": "NONE"
}

learning_stats = {
    "digits": {"wins": 0, "losses": 0, "score_adjust": 0},
    "digit_pro": {"wins": 0, "losses": 0, "score_adjust": 0}
}


# =========================
# CORE HELPERS
# =========================

def now_time():
    return time.strftime("%H:%M:%S")


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_date():
    return datetime.now().date()


def log(msg):
    logs.append(now_time() + " | " + str(msg))
    logs[:] = logs[-300:]


def get_active_mode():
    mode = active_account.get("mode", "DEMO").upper()
    return "REAL" if mode == "REAL" else "DEMO"


def get_active_token():
    return REAL_DERIV_TOKEN if get_active_mode() == "REAL" else DEMO_DERIV_TOKEN


def get_trade_stake():
    return round(float(auto_settings.get("stake", 1)), 2)


def save_settings():
    with open(SETTINGS_FILE, "w") as f:
        json.dump({
            "auto_settings": auto_settings,
            "learning_stats": learning_stats,
            "active_account": active_account
        }, f, indent=4)


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                saved = json.load(f)

            auto_settings.update(saved.get("auto_settings", {}))
            active_account.update(saved.get("active_account", {}))

            for k, v in saved.get("learning_stats", {}).items():
                if k in learning_stats:
                    learning_stats[k] = v

        except Exception as e:
            log(f"Settings load error: {e}")

    active_account["mode"] = get_active_mode()

    if auto_settings.get("symbol") not in price_history:
        auto_settings["symbol"] = "R_75"

    if auto_settings.get("strategy_mode") not in ["digits", "digit_pro"]:
        auto_settings["strategy_mode"] = "digits"


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def lock_session(reason):
    auto_settings["auto_mode"] = False
    session_data["session_locked"] = True
    session_data["lock_reason"] = reason
    session_data["last_status"] = reason
    log(f"SESSION LOCKED: {reason}")


def current_required_score():
    mode = auto_settings.get("strategy_mode", "digits")
    base = int(auto_settings.get("base_required_score", 90))
    adj = int(learning_stats.get(mode, {}).get("score_adjust", 0)) if auto_settings.get("learning_mode") else 0
    return max(75, min(100, base + adj))


def update_profit_protection():
    current_profit = float(session_data["profit"])

    if current_profit > float(session_data["peak_profit"]):
        session_data["peak_profit"] = round(current_profit, 2)

    if not auto_settings.get("profit_protection", True):
        return

    if current_profit >= float(auto_settings.get("protect_at_profit", 1.5)):
        session_data["profit_protected"] = True

    if session_data["profit_protected"]:
        floor = float(session_data["peak_profit"]) - float(auto_settings.get("max_giveback", 0.75))

        if current_profit <= floor:
            lock_session(f"Profit protection locked. Peak {session_data['peak_profit']} | Current {current_profit}")


# =========================
# LOGIN ROUTES
# =========================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["logged_in"] = True
            return redirect("/")
        error = "Wrong password"
    else:
        error = ""

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>AI Trading Login</title>
        <style>
            body{{background:#020617;color:white;font-family:Arial;display:flex;align-items:center;justify-content:center;height:100vh}}
            .box{{background:#0f172a;padding:30px;border-radius:16px;border:1px solid #334155;width:330px;text-align:center}}
            input{{width:100%;padding:12px;border-radius:8px;border:1px solid #475569;background:#020617;color:white;margin:10px 0}}
            button{{width:100%;padding:12px;border:none;border-radius:8px;background:#38bdf8;color:#020617;font-weight:bold}}
            .err{{color:#ef4444}}
        </style>
    </head>
    <body>
        <form class="box" method="POST">
            <h2>🔐 AI Trading Login</h2>
            <input type="password" name="password" placeholder="Dashboard password">
            <button type="submit">LOGIN</button>
            <p class="err">{error}</p>
        </form>
    </body>
    </html>
    """


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
def home():
    if not session.get("logged_in"):
        return redirect("/login")
    return render_template("index.html")


# =========================
# TELEGRAM SUBSCRIBER MANAGER
# =========================

def load_subscribers():
    if not os.path.exists(SUBSCRIBERS_FILE):
        return []

    try:
        with open(SUBSCRIBERS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_subscribers(subscribers):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(subscribers, f, indent=4)


def parse_date(date_text):
    return datetime.strptime(date_text, "%Y-%m-%d").date()


def subscriber_status(sub):
    try:
        expiry = parse_date(sub.get("expiry_date", "2000-01-01"))
        if expiry < today_date():
            return "EXPIRED"
        return "ACTIVE"
    except Exception:
        return "UNKNOWN"


def normalize_subscriber(sub):
    sub["status"] = subscriber_status(sub)
    return sub


def find_subscriber(subscriber_id):
    subscribers = load_subscribers()

    for sub in subscribers:
        if sub.get("id") == subscriber_id:
            return sub, subscribers

    return None, subscribers


def telegram_api(method, payload):
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "description": "Missing TELEGRAM_BOT_TOKEN in .env"}

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"

    try:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")

        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    except Exception as e:
        return {"ok": False, "description": str(e)}


def create_vip_invite_link(sub):
    if not VIP_GROUP_ID:
        return {"ok": False, "description": "Missing VIP_GROUP_ID in .env"}

    expire_timestamp = int((datetime.now() + timedelta(days=1)).timestamp())

    payload = {
        "chat_id": VIP_GROUP_ID,
        "name": f"VIP invite - {sub.get('name', 'subscriber')}",
        "expire_date": expire_timestamp,
        "member_limit": 1
    }

    return telegram_api("createChatInviteLink", payload)


def remove_user_from_vip(telegram_user_id):
    if not VIP_GROUP_ID:
        return {"ok": False, "description": "Missing VIP_GROUP_ID in .env"}

    if not telegram_user_id:
        return {"ok": False, "description": "Missing Telegram User ID for this subscriber"}

    ban_payload = {
        "chat_id": VIP_GROUP_ID,
        "user_id": telegram_user_id
    }

    ban_result = telegram_api("banChatMember", ban_payload)

    unban_payload = {
        "chat_id": VIP_GROUP_ID,
        "user_id": telegram_user_id,
        "only_if_banned": "true"
    }

    unban_result = telegram_api("unbanChatMember", unban_payload)

    return {
        "ok": ban_result.get("ok", False),
        "ban_result": ban_result,
        "unban_result": unban_result
    }


def send_telegram_message(chat_id, text):
    payload = {
        "chat_id": chat_id,
        "text": text
    }

    return telegram_api("sendMessage", payload)


@app.route("/subscribers")
@login_required
def subscribers():
    subs = [normalize_subscriber(s) for s in load_subscribers()]
    save_subscribers(subs)

    return jsonify({
        "subscribers": subs,
        "telegram_ready": bool(TELEGRAM_BOT_TOKEN and VIP_GROUP_ID),
        "vip_group_id": VIP_GROUP_ID,
        "updates_group_id": UPDATES_GROUP_ID
    })


@app.route("/subscriber_add", methods=["POST"])
@login_required
def subscriber_add():
    data = request.json or {}

    name = str(data.get("name", "")).strip()
    telegram_username = str(data.get("telegram_username", "")).strip()
    telegram_user_id = str(data.get("telegram_user_id", "")).strip()
    plan = str(data.get("plan", "Monthly")).strip()
    days = int(data.get("days", 30))
    notes = str(data.get("notes", "")).strip()

    if not name:
        return jsonify({"ok": False, "error": "Subscriber name is required"})

    start_date = today_date()
    expiry_date = start_date + timedelta(days=days)

    sub = {
        "id": str(uuid.uuid4())[:8],
        "name": name,
        "telegram_username": telegram_username,
        "telegram_user_id": telegram_user_id,
        "plan": plan,
        "start_date": start_date.strftime("%Y-%m-%d"),
        "expiry_date": expiry_date.strftime("%Y-%m-%d"),
        "status": "ACTIVE",
        "invite_link": "",
        "bot_access": True,
        "vip_removed": False,
        "notes": notes,
        "created_at": now_iso(),
        "updated_at": now_iso()
    }

    subscribers = load_subscribers()
    subscribers.append(sub)
    save_subscribers(subscribers)

    log(f"SUBSCRIBER ADDED: {name} | Expires {sub['expiry_date']}")

    return jsonify({"ok": True, "subscriber": sub})


@app.route("/subscriber_extend/<subscriber_id>", methods=["POST"])
@login_required
def subscriber_extend(subscriber_id):
    data = request.json or {}
    days = int(data.get("days", 30))

    sub, subscribers = find_subscriber(subscriber_id)

    if not sub:
        return jsonify({"ok": False, "error": "Subscriber not found"})

    try:
        current_expiry = parse_date(sub.get("expiry_date"))
    except Exception:
        current_expiry = today_date()

    base_date = max(current_expiry, today_date())
    new_expiry = base_date + timedelta(days=days)

    sub["expiry_date"] = new_expiry.strftime("%Y-%m-%d")
    sub["status"] = "ACTIVE"
    sub["bot_access"] = True
    sub["vip_removed"] = False
    sub["updated_at"] = now_iso()

    save_subscribers(subscribers)
    log(f"SUBSCRIBER EXTENDED: {sub['name']} | New expiry {sub['expiry_date']}")

    return jsonify({"ok": True, "subscriber": sub})


@app.route("/subscriber_invite/<subscriber_id>", methods=["POST"])
@login_required
def subscriber_invite(subscriber_id):
    sub, subscribers = find_subscriber(subscriber_id)

    if not sub:
        return jsonify({"ok": False, "error": "Subscriber not found"})

    if subscriber_status(sub) != "ACTIVE":
        return jsonify({"ok": False, "error": "Subscriber is expired. Extend before creating invite."})

    result = create_vip_invite_link(sub)

    if result.get("ok"):
        invite_link = result.get("result", {}).get("invite_link", "")
        sub["invite_link"] = invite_link
        sub["updated_at"] = now_iso()
        save_subscribers(subscribers)

        log(f"VIP INVITE CREATED: {sub['name']}")
        return jsonify({"ok": True, "invite_link": invite_link, "telegram": result})

    return jsonify({
        "ok": False,
        "error": result.get("description", "Telegram invite failed"),
        "telegram": result
    })


@app.route("/subscriber_remove_vip/<subscriber_id>", methods=["POST"])
@login_required
def subscriber_remove_vip(subscriber_id):
    sub, subscribers = find_subscriber(subscriber_id)

    if not sub:
        return jsonify({"ok": False, "error": "Subscriber not found"})

    result = remove_user_from_vip(sub.get("telegram_user_id", ""))

    if result.get("ok"):
        sub["vip_removed"] = True
        sub["bot_access"] = False
        sub["updated_at"] = now_iso()
        save_subscribers(subscribers)

        log(f"VIP ACCESS REMOVED: {sub['name']}")
        return jsonify({"ok": True, "telegram": result})

    return jsonify({
        "ok": False,
        "error": result.get("description", "Telegram removal failed"),
        "telegram": result
    })


@app.route("/subscriber_delete/<subscriber_id>", methods=["POST"])
@login_required
def subscriber_delete(subscriber_id):
    subscribers = load_subscribers()
    new_subscribers = [s for s in subscribers if s.get("id") != subscriber_id]

    if len(new_subscribers) == len(subscribers):
        return jsonify({"ok": False, "error": "Subscriber not found"})

    save_subscribers(new_subscribers)
    log(f"SUBSCRIBER DELETED: {subscriber_id}")

    return jsonify({"ok": True})


@app.route("/subscriber_check_expired", methods=["POST"])
@login_required
def subscriber_check_expired():
    subscribers = load_subscribers()
    removed = []
    errors = []

    for sub in subscribers:
        sub["status"] = subscriber_status(sub)

        if sub["status"] == "EXPIRED":
            sub["bot_access"] = False

            if sub.get("telegram_user_id") and not sub.get("vip_removed", False):
                result = remove_user_from_vip(sub.get("telegram_user_id"))

                if result.get("ok"):
                    sub["vip_removed"] = True
                    removed.append(sub.get("name"))
                    log(f"EXPIRED VIP REMOVED: {sub.get('name')}")
                else:
                    errors.append({
                        "name": sub.get("name"),
                        "error": result.get("description", "Failed")
                    })

            sub["updated_at"] = now_iso()

    save_subscribers(subscribers)

    return jsonify({
        "ok": True,
        "removed": removed,
        "errors": errors,
        "subscribers": subscribers
    })


@app.route("/subscriber_send_update", methods=["POST"])
@login_required
def subscriber_send_update():
    data = request.json or {}
    message = str(data.get("message", "")).strip()

    if not message:
        return jsonify({"ok": False, "error": "Message is required"})

    if not UPDATES_GROUP_ID:
        return jsonify({"ok": False, "error": "Missing UPDATES_GROUP_ID in .env"})

    result = send_telegram_message(UPDATES_GROUP_ID, message)

    if result.get("ok"):
        log("UPDATE MESSAGE SENT TO COMMUNITY GROUP")
        return jsonify({"ok": True, "telegram": result})

    return jsonify({
        "ok": False,
        "error": result.get("description", "Failed to send update"),
        "telegram": result
    })


# =========================
# DERIV STREAM
# =========================

def on_open(ws):
    market_data["status"] = "Connected"

    for symbol in MARKET_SYMBOLS:
        ws.send(json.dumps({"ticks": symbol, "subscribe": 1}))

    log("Multi-market stream connected")


def on_message(ws, message):
    try:
        data = json.loads(message)

        if "tick" in data:
            symbol = data["tick"]["symbol"]
            price = float(data["tick"]["quote"])

            if symbol in price_history:
                price_history[symbol].append(price)
                price_history[symbol] = price_history[symbol][-500:]

            if symbol == auto_settings.get("symbol", "R_75"):
                market_data["symbol"] = symbol
                market_data["price"] = price
                market_data["last_update"] = now_time()

    except Exception as e:
        log(f"Tick error: {e}")


def start_stream():
    while True:
        try:
            ws = websocket.WebSocketApp(
                DERIV_WS_URL,
                on_open=on_open,
                on_message=on_message
            )
            ws.run_forever()
        except Exception as e:
            market_data["status"] = "Disconnected"
            log(f"Stream error: {e}")

        time.sleep(5)


def deriv_request(payload):
    token = get_active_token()
    mode = get_active_mode()

    if not token:
        return {"error": f"Missing {mode}_DERIV_TOKEN"}

    try:
        ws = websocket.create_connection(DERIV_WS_URL)

        ws.send(json.dumps({"authorize": token}))
        auth = json.loads(ws.recv())

        if "error" in auth:
            ws.close()
            return {"error": auth["error"].get("message", auth["error"])}

        ws.send(json.dumps(payload))
        result = json.loads(ws.recv())
        ws.close()

        return result

    except Exception as e:
        return {"error": str(e)}


# =========================
# ACCOUNT / DASHBOARD ROUTES
# =========================

@app.route("/account_info")
@login_required
def account_info():
    token = get_active_token()
    selected_mode = get_active_mode()

    if not token:
        return jsonify({"error": f"Missing {selected_mode}_DERIV_TOKEN", "mode": selected_mode})

    try:
        ws = websocket.create_connection(DERIV_WS_URL)

        ws.send(json.dumps({"authorize": token}))
        auth = json.loads(ws.recv())

        if "error" in auth:
            ws.close()
            return jsonify({"error": auth["error"].get("message", auth["error"]), "mode": selected_mode})

        loginid = auth["authorize"].get("loginid", "")
        actual_mode = "DEMO" if loginid.startswith("VRTC") else "REAL"

        ws.send(json.dumps({"balance": 1}))
        balance_res = json.loads(ws.recv())
        ws.close()

        balance = "---"
        currency = ""

        if "balance" in balance_res:
            balance = balance_res["balance"].get("balance")
            currency = balance_res["balance"].get("currency")

        return jsonify({
            "mode": actual_mode,
            "selected_mode": selected_mode,
            "loginid": loginid,
            "balance": balance,
            "currency": currency
        })

    except Exception as e:
        return jsonify({"error": str(e), "mode": selected_mode})


@app.route("/switch_account", methods=["POST"])
@login_required
def switch_account():
    data = request.json or {}
    mode = str(data.get("mode", "DEMO")).upper()

    if mode not in ["DEMO", "REAL"]:
        return jsonify({"ok": False, "error": "Invalid account mode"})

    if mode == "DEMO" and not DEMO_DERIV_TOKEN:
        return jsonify({"ok": False, "error": "Missing DEMO_DERIV_TOKEN"})

    if mode == "REAL" and not REAL_DERIV_TOKEN:
        return jsonify({"ok": False, "error": "Missing REAL_DERIV_TOKEN"})

    auto_settings["auto_mode"] = False
    active_account["mode"] = mode
    save_settings()

    log(f"ACCOUNT SWITCHED TO {mode}. Auto OFF for safety.")
    return jsonify({"ok": True, "mode": mode})


@app.route("/market")
@login_required
def market():
    return jsonify(market_data)


@app.route("/chart_data")
@login_required
def chart_data():
    prices = price_history.get(auto_settings.get("symbol", "R_75"), [])
    return jsonify({"prices": prices[-100:]})


@app.route("/logs")
@login_required
def get_logs():
    return jsonify({"logs": logs})


@app.route("/set_auto", methods=["POST"])
@login_required
def set_auto():
    data = request.json or {}
    auto_settings.update(data)

    if auto_settings.get("strategy_mode") not in ["digits", "digit_pro"]:
        auto_settings["strategy_mode"] = "digits"

    if auto_settings.get("symbol") not in price_history:
        auto_settings["symbol"] = "R_75"

    market_data["symbol"] = auto_settings["symbol"]
    save_settings()
    log("Settings saved")

    return jsonify({"ok": True})


@app.route("/auto_status")
@login_required
def auto_status():
    trades = session_data["wins"] + session_data["losses"]
    win_rate = round((session_data["wins"] / trades) * 100, 2) if trades else 0

    return jsonify({
        "active_account": get_active_mode(),
        "auto_settings": auto_settings,
        "session": session_data,
        "win_rate": win_rate,
        "required_score": current_required_score(),
        "trade_stake": get_trade_stake()
    })


@app.route("/learning")
@login_required
def learning():
    return jsonify({
        "learning_mode": auto_settings.get("learning_mode", True),
        "required_score": current_required_score(),
        "stats": learning_stats
    })


@app.route("/performance")
@login_required
def performance():
    total = len(trade_history)
    wins = len([t for t in trade_history if float(t.get("profit", 0)) > 0])
    total_profit = round(sum(float(t.get("profit", 0)) for t in trade_history), 2)
    avg_profit = round(total_profit / total, 2) if total else 0
    win_rate = round((wins / total) * 100, 2) if total else 0

    return jsonify({
        "win_rate": win_rate,
        "total_profit": total_profit,
        "avg_profit": avg_profit,
        "trade_history": trade_history[-100:]
    })


# =========================
# BUY HELPERS
# =========================

def buy_contract(symbol, contract_type, stake, duration, duration_unit, barrier=None, strategy="manual", reason="Manual", score=100):
    token = get_active_token()
    mode = get_active_mode()

    if not token:
        return {"success": False, "error": f"Missing {mode}_DERIV_TOKEN"}

    payload = {
        "proposal": 1,
        "amount": round(float(stake), 2),
        "basis": "stake",
        "currency": auto_settings["currency"],
        "symbol": symbol,
        "contract_type": contract_type,
        "duration": int(duration),
        "duration_unit": duration_unit
    }

    if barrier is not None:
        payload["barrier"] = str(barrier)

    try:
        ws = websocket.create_connection(DERIV_WS_URL)

        ws.send(json.dumps({"authorize": token}))
        auth = json.loads(ws.recv())

        if "error" in auth:
            ws.close()
            return {"success": False, "error": auth["error"].get("message", auth["error"])}

        ws.send(json.dumps(payload))
        proposal = json.loads(ws.recv())

        if "error" in proposal:
            ws.close()
            return {
                "success": False,
                "error": proposal["error"].get("message", proposal["error"]),
                "payload": payload
            }

        proposal_id = proposal["proposal"]["id"]
        ask_price = proposal["proposal"]["ask_price"]

        ws.send(json.dumps({"buy": proposal_id, "price": ask_price}))
        buy = json.loads(ws.recv())
        ws.close()

        if "buy" in buy:
            cid = buy["buy"]["contract_id"]

            open_contracts.append({
                "contract_id": cid,
                "symbol": symbol,
                "type": contract_type,
                "barrier": barrier if barrier is not None else "",
                "duration_ticks": f"{duration}{duration_unit}",
                "score": score,
                "strategy": strategy,
                "reason": reason,
                "entry_time": now_time(),
                "account_mode": get_active_mode(),
                "stake": round(float(stake), 2)
            })

            session_data["trades"] += 1
            session_data["last_trade_time"] = time.time()
            session_data["last_status"] = f"EXECUTED: {symbol} {contract_type}"
            session_data["last_symbol"] = symbol
            session_data["last_signal"] = contract_type
            session_data["last_score"] = score

            log(f"BUY {get_active_mode()} | {symbol} | {contract_type} | Stake {stake} | ID {cid}")

            return {"success": True, "contract_id": cid}

        return {"success": False, "error": buy.get("error", buy)}

    except Exception as e:
        return {"success": False, "error": str(e)}


def update_learning(strategy, won):
    if not auto_settings.get("learning_mode", True):
        return

    if strategy not in learning_stats:
        return

    if won:
        learning_stats[strategy]["wins"] += 1
        learning_stats[strategy]["score_adjust"] = max(-5, learning_stats[strategy]["score_adjust"] - 1)
    else:
        learning_stats[strategy]["losses"] += 1
        learning_stats[strategy]["score_adjust"] = min(15, learning_stats[strategy]["score_adjust"] + 2)

    save_settings()


def result_loop():
    while True:
        for c in open_contracts[:]:
            res = deriv_request({
                "proposal_open_contract": 1,
                "contract_id": c["contract_id"]
            })

            if "proposal_open_contract" in res:
                con = res["proposal_open_contract"]

                if int(con.get("is_sold", 0)) == 1:
                    profit = float(con.get("profit", 0))
                    won = profit > 0

                    session_data["profit"] = round(session_data["profit"] + profit, 2)

                    if won:
                        session_data["wins"] += 1
                        session_data["loss_streak"] = 0
                    else:
                        session_data["losses"] += 1
                        session_data["loss_streak"] += 1

                    update_learning(c.get("strategy", "manual"), won)
                    update_profit_protection()

                    trade_history.append({
                        "contract_id": c.get("contract_id"),
                        "symbol": c.get("symbol"),
                        "type": c.get("type"),
                        "barrier": c.get("barrier", ""),
                        "duration_ticks": c.get("duration_ticks", ""),
                        "score": c.get("score"),
                        "strategy": c.get("strategy"),
                        "status": con.get("status", ""),
                        "profit": profit,
                        "entry_time": c.get("entry_time"),
                        "close_time": now_time(),
                        "account_mode": c.get("account_mode", get_active_mode()),
                        "stake": c.get("stake", get_trade_stake())
                    })

                    trade_history[:] = trade_history[-250:]
                    log(("WIN" if won else "LOSS") + f" | {c['symbol']} | Profit {profit}")

                    try:
                        open_contracts.remove(c)
                    except ValueError:
                        pass

        time.sleep(3)


# =========================
# DIGITS + DIGIT PRO
# =========================

def get_last_digit_from_price(price):
    try:
        digits = "".join(ch for ch in str(price) if ch.isdigit())
        if digits:
            return int(digits[-1])
    except Exception:
        pass
    return None


@app.route("/digit_pro_data")
@login_required
def digit_pro_data():
    symbol = auto_settings.get("symbol", "R_75")
    prices = price_history.get(symbol, [])

    last_price = prices[-1] if prices else "---"
    last_digit = get_last_digit_from_price(last_price) if prices else None

    digits = []
    for price in prices[-100:]:
        d = get_last_digit_from_price(price)
        if d is not None:
            digits.append(d)

    counts = {str(i): digits.count(i) for i in range(10)}
    sample_size = len(digits)

    percentages = {
        str(i): round((counts[str(i)] / sample_size) * 100, 1) if sample_size else 0
        for i in range(10)
    }

    if sample_size:
        highest_digit = max(counts, key=counts.get)
        lowest_digit = min(counts, key=counts.get)
        highest_count = counts[highest_digit]
        lowest_count = counts[lowest_digit]
    else:
        highest_digit = "---"
        lowest_digit = "---"
        highest_count = "---"
        lowest_count = "---"

    best_over_digit = 0
    best_over_score = 0

    for barrier in range(10):
        score = sum(counts[str(i)] for i in range(barrier + 1, 10))
        percent = round((score / sample_size) * 100, 1) if sample_size else 0

        if percent > best_over_score:
            best_over_score = percent
            best_over_digit = barrier

    best_under_digit = 9
    best_under_score = 0

    for barrier in range(10):
        score = sum(counts[str(i)] for i in range(0, barrier))
        percent = round((score / sample_size) * 100, 1) if sample_size else 0

        if percent > best_under_score:
            best_under_score = percent
            best_under_digit = barrier

    return jsonify({
        "symbol": symbol,
        "last_price": last_price,
        "last_digit": last_digit,
        "sample_size": sample_size,
        "ticks": int(auto_settings.get("digit_pro_ticks", 1)),
        "counts": counts,
        "percentages": percentages,
        "highest_digit": highest_digit,
        "highest_count": highest_count,
        "lowest_digit": lowest_digit,
        "lowest_count": lowest_count,
        "best_over_digit": best_over_digit,
        "best_over_score": best_over_score,
        "best_under_digit": best_under_digit,
        "best_under_score": best_under_score
    })


@app.route("/set_digit_pro", methods=["POST"])
@login_required
def set_digit_pro():
    data = request.json or {}
    ticks = max(1, min(10, int(data.get("ticks", 1))))

    auto_settings["digit_pro_ticks"] = ticks
    auto_settings["strategy_mode"] = "digit_pro"
    save_settings()

    log(f"DIGIT PRO TICKS SET: {ticks}")
    return jsonify({"ok": True, "digit_pro_ticks": ticks})


@app.route("/digit_over/<int:digit>", methods=["POST"])
@login_required
def digit_over(digit):
    data = request.json or {}
    ticks = max(1, min(10, int(data.get("ticks", auto_settings.get("digit_pro_ticks", 1)))))
    digit = max(0, min(9, digit))

    result = buy_contract(
        symbol=auto_settings["symbol"],
        contract_type="DIGITOVER",
        stake=get_trade_stake(),
        duration=ticks,
        duration_unit="t",
        barrier=digit,
        strategy="digit_pro",
        reason=f"Digit Pro OVER {digit}",
        score=100
    )

    return jsonify(result)


@app.route("/digit_under/<int:digit>", methods=["POST"])
@login_required
def digit_under(digit):
    data = request.json or {}
    ticks = max(1, min(10, int(data.get("ticks", auto_settings.get("digit_pro_ticks", 1)))))
    digit = max(0, min(9, digit))

    result = buy_contract(
        symbol=auto_settings["symbol"],
        contract_type="DIGITUNDER",
        stake=get_trade_stake(),
        duration=ticks,
        duration_unit="t",
        barrier=digit,
        strategy="digit_pro",
        reason=f"Digit Pro UNDER {digit}",
        score=100
    )

    return jsonify(result)


# =========================
# DIGITS AUTO SCANNER
# =========================

def digit_auto_signal(symbol):
    prices = price_history.get(symbol, [])

    if len(prices) < 80:
        return {
            "symbol": symbol,
            "signal": "NO",
            "barrier": "",
            "score": 0,
            "reason": "Waiting for digit data",
            "entry_quality": "WAIT"
        }

    digits = []
    for p in prices[-80:]:
        d = get_last_digit_from_price(p)
        if d is not None:
            digits.append(d)

    if len(digits) < 40:
        return {
            "symbol": symbol,
            "signal": "NO",
            "barrier": "",
            "score": 0,
            "reason": "Not enough digit samples",
            "entry_quality": "WAIT"
        }

    over0 = len([d for d in digits if d > 0]) / len(digits) * 100
    over1 = len([d for d in digits if d > 1]) / len(digits) * 100
    under9 = len([d for d in digits if d < 9]) / len(digits) * 100
    under8 = len([d for d in digits if d < 8]) / len(digits) * 100

    candidates = [
        ("DIGITOVER", 0, over0, "Digit pressure OVER 0"),
        ("DIGITOVER", 1, over1, "Digit pressure OVER 1"),
        ("DIGITUNDER", 9, under9, "Digit pressure UNDER 9"),
        ("DIGITUNDER", 8, under8, "Digit pressure UNDER 8")
    ]

    best = max(candidates, key=lambda x: x[2])
    score = round(best[2], 1)

    return {
        "symbol": symbol,
        "signal": best[0],
        "barrier": best[1],
        "score": score,
        "reason": best[3],
        "entry_quality": "READY" if score >= current_required_score() else "WAIT"
    }


def get_best_digit_signal():
    scans = [digit_auto_signal(s) for s in MARKET_SYMBOLS]
    best = max(scans, key=lambda x: x["score"])
    market_data["best_symbol"] = best["symbol"]
    market_data["best_score"] = best["score"]
    return best, scans


@app.route("/scanner")
@login_required
def scanner():
    best, scans = get_best_digit_signal()

    return jsonify({
        "best": best,
        "signals": scans
    })


@app.route("/auto_on", methods=["POST"])
@login_required
def auto_on():
    if session_data["session_locked"]:
        return jsonify({"ok": False, "error": session_data["lock_reason"]})

    auto_settings["auto_mode"] = True
    save_settings()
    log("DIGIT AUTO ON")
    return jsonify({"ok": True})


@app.route("/auto_off", methods=["POST"])
@login_required
def auto_off():
    auto_settings["auto_mode"] = False
    save_settings()
    log("DIGIT AUTO OFF")
    return jsonify({"ok": True})


def digit_auto_loop():
    while True:
        try:
            if auto_settings.get("auto_mode", False):
                if session_data["session_locked"]:
                    auto_settings["auto_mode"] = False
                    save_settings()
                    time.sleep(3)
                    continue

                if time.time() - session_data["last_trade_time"] < int(auto_settings.get("cooldown_seconds", 60)):
                    time.sleep(3)
                    continue

                if session_data["trades"] >= int(auto_settings.get("max_trades", 5)):
                    lock_session("Max trades reached")
                    time.sleep(3)
                    continue

                if session_data["profit"] >= float(auto_settings.get("profit_target", 3)):
                    lock_session("Profit target reached")
                    time.sleep(3)
                    continue

                if session_data["profit"] <= -abs(float(auto_settings.get("daily_loss_limit", 2))):
                    lock_session("Daily loss limit reached")
                    time.sleep(3)
                    continue

                best, scans = get_best_digit_signal()
                required = current_required_score()

                session_data["last_symbol"] = best["symbol"]
                session_data["last_signal"] = best["signal"]
                session_data["last_score"] = best["score"]

                if best["signal"] != "NO" and best["score"] >= required:
                    ticks = int(auto_settings.get("digit_pro_ticks", 1))

                    result = buy_contract(
                        symbol=best["symbol"],
                        contract_type=best["signal"],
                        stake=get_trade_stake(),
                        duration=ticks,
                        duration_unit="t",
                        barrier=best["barrier"],
                        strategy=auto_settings.get("strategy_mode", "digits"),
                        reason=best["reason"],
                        score=best["score"]
                    )

                    if not result.get("success"):
                        log(f"DIGIT AUTO ERROR: {result.get('error')}")
                else:
                    log(f"DIGIT AUTO WAIT | Best {best['symbol']} score {best['score']} required {required}")

        except Exception as e:
            log(f"DIGIT AUTO ERROR: {e}")

        time.sleep(3)


# =========================
# MANUAL DIGIT ROUTES
# =========================

@app.route("/manual_digit_over", methods=["POST"])
@login_required
def manual_digit_over():
    return jsonify(
        buy_contract(
            symbol=auto_settings["symbol"],
            contract_type="DIGITOVER",
            stake=get_trade_stake(),
            duration=1,
            duration_unit="t",
            barrier=0,
            strategy="digits",
            reason="Manual digit over",
            score=100
        )
    )


@app.route("/manual_digit_under", methods=["POST"])
@login_required
def manual_digit_under():
    return jsonify(
        buy_contract(
            symbol=auto_settings["symbol"],
            contract_type="DIGITUNDER",
            stake=get_trade_stake(),
            duration=1,
            duration_unit="t",
            barrier=9,
            strategy="digits",
            reason="Manual digit under",
            score=100
        )
    )


# =========================
# RESET / DOWNLOADS
# =========================

@app.route("/reset_session", methods=["POST"])
@login_required
def reset_session():
    session_data.update({
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "profit": 0,
        "peak_profit": 0,
        "profit_protected": False,
        "loss_streak": 0,
        "last_trade_time": 0,
        "session_locked": False,
        "lock_reason": "",
        "last_status": "Reset",
        "last_signal": "NONE",
        "last_score": 0,
        "last_symbol": "NONE"
    })

    auto_settings["auto_mode"] = False

    save_settings()
    log("SESSION RESET / AUTO OFF")

    return jsonify({"ok": True})


@app.route("/download_report")
@login_required
def download_report():
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Account", "Stake", "Contract ID", "Symbol", "Strategy",
        "Type", "Barrier", "Duration", "Score", "Status", "Profit", "Entry", "Close"
    ])

    for t in trade_history:
        writer.writerow([
            t.get("account_mode"),
            t.get("stake"),
            t.get("contract_id"),
            t.get("symbol"),
            t.get("strategy"),
            t.get("type"),
            t.get("barrier"),
            t.get("duration_ticks"),
            t.get("score"),
            t.get("status"),
            t.get("profit"),
            t.get("entry_time"),
            t.get("close_time")
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=trade_report.csv"}
    )


@app.route("/download_settings")
@login_required
def download_settings():
    data = {
        "auto_settings": auto_settings,
        "learning_stats": learning_stats,
        "active_account": active_account
    }

    return Response(
        json.dumps(data, indent=4),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment;filename=bot_settings.json"}
    )


@app.route("/download_backup")
@login_required
def download_backup():
    data = {
        "auto_settings": auto_settings,
        "learning_stats": learning_stats,
        "active_account": active_account,
        "session_data": session_data,
        "trade_history": trade_history,
        "subscribers": load_subscribers()
    }

    return Response(
        json.dumps(data, indent=4),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment;filename=bot_backup.json"}
    )


# =========================
# START APP
# =========================

if __name__ == "__main__":
    load_settings()

    threading.Thread(target=start_stream, daemon=True).start()
    threading.Thread(target=result_loop, daemon=True).start()
    threading.Thread(target=digit_auto_loop, daemon=True).start()

    log("AI Pro Trading Dashboard loaded - Digits / Digit Pro + Telegram")

    app.run(host="0.0.0.0", port=5000, debug=False)