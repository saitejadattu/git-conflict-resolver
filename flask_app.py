import logging
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, Start, Stream, VoiceResponse


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "transcripts.db"

app = Flask(
    "flask_app",
    root_path=str(BASE_DIR),
    template_folder="templates",
    instance_path=str(BASE_DIR / "instance"),
)
app.logger.setLevel(logging.INFO)

ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
PUBLIC_WS_URL = os.getenv("PUBLIC_WS_URL", "ws://127.0.0.1:8765").rstrip("/")

PHONE_PATTERN = r"^\+[1-9]\d{7,14}$"


def get_db_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with get_db_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_sid TEXT UNIQUE,
                from_number TEXT,
                to_number TEXT,
                status TEXT DEFAULT 'queued',
                start_time DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_sid TEXT,
                role TEXT,
                text TEXT,
                ts DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(calls)")
        }
        if "status" not in existing_columns:
            connection.execute("ALTER TABLE calls ADD COLUMN status TEXT DEFAULT 'queued'")

        connection.commit()


def twilio_client():
    if not ACCOUNT_SID or not AUTH_TOKEN or not TWILIO_FROM_NUMBER:
        return None
    return Client(ACCOUNT_SID, AUTH_TOKEN)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.post("/start-call")
def start_call():
    payload = request.get_json(silent=True)
    if not payload or not isinstance(payload, dict):
        return jsonify({"message": "Request body must be valid JSON."}), 400

    phone = str(payload.get("phone", "")).strip()
    if not phone:
        return jsonify({"message": "Phone number is required."}), 400

    import re

    if not re.fullmatch(PHONE_PATTERN, phone):
        return jsonify({"message": "Phone number must be in E.164 format like +919876543210."}), 400

    client = twilio_client()
    if client is None:
        with get_db_connection() as connection:
            connection.execute(
                """
                INSERT INTO calls (call_sid, from_number, to_number, status)
                VALUES (?, ?, ?, ?)
                """,
                (f"demo-{phone[-6:]}", TWILIO_FROM_NUMBER or "not-configured", phone, "not_configured"),
            )
            connection.commit()
        return jsonify({"message": "Twilio is not configured. Call was not placed."}), 503

    try:
        call = client.calls.create(
            to=phone,
            from_=TWILIO_FROM_NUMBER,
            url=f"{PUBLIC_BASE_URL}/outbound-call-twiml",
            status_callback=f"{PUBLIC_BASE_URL}/twilio-status",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
        )
    except TwilioRestException as exc:
        app.logger.exception("Twilio call creation failed")
        return jsonify({"message": exc.msg or "Twilio failed to start the call."}), 502
    except Exception:
        app.logger.exception("Unexpected error while creating Twilio call")
        return jsonify({"message": "Unexpected error while starting the call."}), 500

    with get_db_connection() as connection:
        connection.execute(
            """
            INSERT INTO calls (call_sid, from_number, to_number, status)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(call_sid) DO UPDATE SET
                from_number = excluded.from_number,
                to_number = excluded.to_number,
                status = excluded.status
            """,
            (call.sid, TWILIO_FROM_NUMBER, phone, getattr(call, "status", "queued")),
        )
        connection.commit()

    return jsonify({
        "message": "Call started successfully.",
        "call_sid": call.sid,
        "status": getattr(call, "status", "queued"),
    })


@app.post("/twilio-status")
def twilio_status():
    call_sid = request.form.get("CallSid") or request.json.get("CallSid") if request.is_json else None
    call_status = request.form.get("CallStatus") or request.json.get("CallStatus") if request.is_json else None

    if call_sid and call_status:
        with get_db_connection() as connection:
            connection.execute(
                "UPDATE calls SET status = ? WHERE call_sid = ?",
                (call_status, call_sid),
            )
            connection.commit()

    return ("", 204)


@app.post("/save-transcript")
def save_transcript():
    payload = request.get_json(silent=True)
    if not payload or not isinstance(payload, dict):
        return jsonify({"message": "Request body must be valid JSON."}), 400

    call_sid = str(payload.get("call_sid", "")).strip()
    role = str(payload.get("role", "user")).strip() or "user"
    text = str(payload.get("text", "")).strip()

    if not call_sid or not text:
        return jsonify({"message": "Both call_sid and text are required."}), 400

    with get_db_connection() as connection:
        connection.execute(
            "INSERT INTO transcripts (call_sid, role, text) VALUES (?, ?, ?)",
            (call_sid, role, text),
        )
        connection.commit()

    return jsonify({"message": "Transcript saved."})


@app.get("/calls")
def list_calls():
    with get_db_connection() as connection:
        rows = connection.execute(
            """
            SELECT call_sid, from_number, to_number, status, start_time
            FROM calls
            ORDER BY datetime(start_time) DESC, id DESC
            LIMIT 25
            """
        ).fetchall()

    return jsonify([dict(row) for row in rows])


@app.get("/transcripts/<call_sid>")
def transcripts(call_sid):
    with get_db_connection() as connection:
        rows = connection.execute(
            """
            SELECT role, text, ts
            FROM transcripts
            WHERE call_sid = ?
            ORDER BY datetime(ts) ASC, id ASC
            """,
            (call_sid,),
        ).fetchall()

    return jsonify([dict(row) for row in rows])


@app.post("/outbound-call-twiml")
@app.get("/outbound-call-twiml")
def outbound_call_twiml():
    response = VoiceResponse()

    start = Start()
    start.stream(url=PUBLIC_WS_URL)
    response.append(start)
    response.say("Your AI assistant is joining the call.")

    connect = Connect()
    connect.stream(url=PUBLIC_WS_URL)
    response.append(connect)

    return str(response), 200, {"Content-Type": "text/xml"}


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("FLASK_PORT", "5055"))
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)
