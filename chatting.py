import os
import sys
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4, UUID
import threading
import webbrowser

from flask import (
    Flask, request, session, redirect, url_for, send_from_directory,
    jsonify, render_template_string, abort, make_response
)
from flask_socketio import SocketIO, join_room, emit
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

import secrets
# Optional PIL for image processing
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# Import Sticker Manager
try:
    from sticker_feature import StickerManager
    STICKER_FEATURE_AVAILABLE = True
except ImportError:
    STICKER_FEATURE_AVAILABLE = False
    print("[WARN] Sticker feature module not found. Sticker functionality will be limited.")

# System tray imports
try:
    import pystray
    from pystray import MenuItem as item
    PYSTRAY_AVAILABLE = True
except ImportError:
    PYSTRAY_AVAILABLE = False

# --------------------------
# App Config
# --------------------------
# IMPORTANT for portable/frozen build:
# - normal python run  : base = directory of chatting.py
# - packaged .exe run  : base = directory of executable
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
AVATAR_DIR = os.path.join(UPLOAD_DIR, "avatars")
DB_PATH = os.path.join(BASE_DIR, "chat.db")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(AVATAR_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET", "super-secret-key-change-me")
socketio = SocketIO(app, cors_allowed_origins="*")
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # 30 MB

ALLOWED_EXT = set([
    # images
    "png", "jpg", "jpeg", "gif", "webp",
    # docs
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt",
    # media
    "mp3", "wav", "mp4", "mov", "avi", "mkv", "webm", "ogg", "m4a",
    # archives
    "zip", "rar", "7z", "tar", "gz",
    # scripts
    "bat", "sh", "py", "js", "html", "css"
])

# --------------------------
# DB Helpers & Migration
# --------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_column(cur, table, column, col_def):
    cols = cur.execute(f"PRAGMA table_info({table})").fetchall()
    names = [c["name"] for c in cols]
    if column not in names:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")

def migrate():
    conn = db()
    c = conn.cursor()
    # users
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT,
        avatar_path TEXT,
        bio TEXT,
        created_at TEXT NOT NULL
    );
    """)
    # groups
    c.execute("""
    CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        owner_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(owner_id) REFERENCES users(id)
    );
    """)
    # Add avatar_path to groups table
    ensure_column(c, "groups", "avatar_path", "TEXT")
    # group_members
    c.execute("""
    CREATE TABLE IF NOT EXISTS group_members (
        group_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        role TEXT DEFAULT 'member',
        PRIMARY KEY (group_id, user_id),
        FOREIGN KEY(group_id) REFERENCES groups(id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)
    # messages
    c.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_type TEXT NOT NULL, -- 'direct' or 'group'
        sender_id INTEGER NOT NULL,
        receiver_id INTEGER,     -- direct peer
        group_id INTEGER,        -- group target
        content TEXT,
        content_type TEXT DEFAULT 'text', -- text|image|file|audio
        file_path TEXT,          -- path to uploaded file
        reply_to INTEGER,        -- message_id of the replied message
        forwarded_from INTEGER,  -- message_id of the original forwarded message
        edited INTEGER DEFAULT 0,
        deleted INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT,
        FOREIGN KEY(sender_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(receiver_id) REFERENCES users(id) ON DELETE SET NULL,
        FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE,
        FOREIGN KEY(reply_to) REFERENCES messages(id) ON DELETE SET NULL,
        FOREIGN KEY(forwarded_from) REFERENCES messages(id) ON DELETE SET NULL
    );
    """)
    # Indexes for faster message retrieval
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_direct ON messages(sender_id, receiver_id, id);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(group_id, id);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_contacts_added_at ON user_contacts(added_at);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pinned_position ON pinned_conversations(position);")
    
    # New tables for enhanced features
    # 1. Disappearing Messages Settings
    c.execute("""
    CREATE TABLE IF NOT EXISTS disappearing_messages (
        chat_type TEXT NOT NULL,
        ref_id INTEGER NOT NULL,
        duration_seconds INTEGER DEFAULT 0,
        enabled_by INTEGER NOT NULL,
        enabled_at TEXT NOT NULL,
        PRIMARY KEY (chat_type, ref_id),
        FOREIGN KEY(enabled_by) REFERENCES users(id)
    );
    """)
    
    # 2. Polls
    c.execute("""
    CREATE TABLE IF NOT EXISTS polls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER NOT NULL,
        question TEXT NOT NULL,
        options TEXT NOT NULL,
        multiple_choice INTEGER DEFAULT 0,
        created_by INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
        FOREIGN KEY(created_by) REFERENCES users(id)
    );
    """)
    
    # 3. Poll Votes
    c.execute("""
    CREATE TABLE IF NOT EXISTS poll_votes (
        poll_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        option_index INTEGER NOT NULL,
        voted_at TEXT NOT NULL,
        PRIMARY KEY (poll_id, user_id, option_index),
        FOREIGN KEY(poll_id) REFERENCES polls(id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)
    
    # 4. Scheduled Messages
    c.execute("""
    CREATE TABLE IF NOT EXISTS scheduled_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        chat_type TEXT NOT NULL,
        receiver_id INTEGER,
        group_id INTEGER,
        content TEXT,
        content_type TEXT DEFAULT 'text',
        file_path TEXT,
        scheduled_at TEXT NOT NULL,
        sent INTEGER DEFAULT 0,
        sent_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)
    
    # 5. Quick Reply Templates
    c.execute("""
    CREATE TABLE IF NOT EXISTS quick_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        shortcut TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)
    
    # 6. Blocked Contacts
    c.execute("""
    CREATE TABLE IF NOT EXISTS blocked_contacts (
        user_id INTEGER NOT NULL,
        blocked_user_id INTEGER NOT NULL,
        blocked_at TEXT NOT NULL,
        PRIMARY KEY (user_id, blocked_user_id),
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(blocked_user_id) REFERENCES users(id)
    );
    """)
    
    # 7. Chat Statistics
    c.execute("""
    CREATE TABLE IF NOT EXISTS chat_statistics (
        user_id INTEGER NOT NULL,
        chat_type TEXT NOT NULL,
        ref_id INTEGER NOT NULL,
        total_messages_sent INTEGER DEFAULT 0,
        total_messages_received INTEGER DEFAULT 0,
        total_files_shared INTEGER DEFAULT 0,
        last_activity TEXT,
        PRIMARY KEY (user_id, chat_type, ref_id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)
    
    # 8. Message Translations Cache
    c.execute("""
    CREATE TABLE IF NOT EXISTS message_translations (
        message_id INTEGER NOT NULL,
        target_lang TEXT NOT NULL,
        translated_content TEXT NOT NULL,
        translated_at TEXT NOT NULL,
        PRIMARY KEY (message_id, target_lang),
        FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
    );
    """)

    # message_status (read receipts)
    c.execute("""
    CREATE TABLE IF NOT EXISTS message_status (
        message_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        delivered_at TEXT,
        read_at TEXT,
        PRIMARY KEY (message_id, user_id),
        FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)
    # reactions
    c.execute("""
    CREATE TABLE IF NOT EXISTS reactions (
        message_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        emoji TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (message_id, user_id, emoji),
        FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)
    # stars (favorite)
    c.execute("""
    CREATE TABLE IF NOT EXISTS stars (
        message_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (message_id, user_id),
        FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)
    # pinned conversations
    c.execute("""
    CREATE TABLE IF NOT EXISTS pinned_conversations (
        user_id INTEGER NOT NULL,
        chat_type TEXT NOT NULL,
        ref_id INTEGER NOT NULL,
        position INTEGER DEFAULT 0,
        pinned_at TEXT NOT NULL,
        PRIMARY KEY (user_id, chat_type, ref_id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)
    # pinned messages (for group message pinning)
    c.execute("""
    CREATE TABLE IF NOT EXISTS pinned_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER NOT NULL,
        pinned_by INTEGER NOT NULL,
        group_id INTEGER NOT NULL,
        pinned_at TEXT NOT NULL,
        FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
        FOREIGN KEY(pinned_by) REFERENCES users(id),
        FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE
    );
    """)
    # mentions
    c.execute("""
    CREATE TABLE IF NOT EXISTS mentions (
        message_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY (message_id, user_id),
        FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)
    # user contacts (friends/following)
    c.execute("""
    CREATE TABLE IF NOT EXISTS user_contacts (
        user_id INTEGER NOT NULL,
        contact_id INTEGER NOT NULL,
        added_at TEXT NOT NULL,
        status TEXT DEFAULT 'pending', -- pending, accepted, rejected
        PRIMARY KEY (user_id, contact_id),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(contact_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)
    # ensure avatar_path exists
    ensure_column(c, "users", "avatar_path", "TEXT")
    # Add bio column
    ensure_column(c, "users", "bio", "TEXT")
    # Add columns for password reset
    ensure_column(c, "users", "reset_token", "TEXT")
    ensure_column(c, "users", "reset_expires", "TEXT")
    # Add last seen column
    ensure_column(c, "users", "last_seen", "TEXT")

    conn.commit()
    conn.close()

migrate()

call_status = {} # user_id -> True if in call

# --------------------------
# HTTP to HTTPS Redirect
# --------------------------
@app.before_request
def redirect_http_to_https():
    # Redirect only if SSL is enabled and the request is not secure.
    if os.path.exists(os.path.join(BASE_DIR, 'cert.pem')) and not request.is_secure:
        url = request.url.replace('http://', 'https://', 1)
        return redirect(url, code=301)

@app.before_request
def update_user_last_seen():
    # Update last_seen on every authenticated request (except login/register since doesn't have session yet)
    if session.get('user_id') and request.endpoint not in ['login', 'register', 'favicon', 'uploads']:
        update_last_seen(session['user_id'])

# --------------------------
# User Deletion
# --------------------------
def delete_user(user_id):
    conn = db()
    try:
        cur = conn.cursor()

        # Handle owned groups: transfer ownership or delete if sole member
        owned_groups = cur.execute("SELECT id FROM groups WHERE owner_id=?", (user_id,)).fetchall()
        for g in owned_groups:
            gid = g["id"]
            members = cur.execute("SELECT user_id FROM group_members WHERE group_id=? ORDER BY user_id", (gid,)).fetchall()
            if len(members) > 1:
                # transfer to first non-owner
                new_owner = next((m["user_id"] for m in members if m["user_id"] != user_id), None)
                if new_owner:
                    cur.execute("UPDATE groups SET owner_id=? WHERE id=?", (new_owner, gid))
            else:
                # delete group completely
                cur.execute("DELETE FROM messages WHERE group_id=?", (gid,))
                cur.execute("DELETE FROM group_members WHERE group_id=?", (gid,))
                cur.execute("DELETE FROM groups WHERE id=?", (gid,))

        # Remove user from all group memberships
        cur.execute("DELETE FROM group_members WHERE user_id=?", (user_id,))

        # Delete related message data (mentions, reactions, status, stars)
        related_tables = ["mentions", "reactions", "message_status", "stars"]
        for table in related_tables:
            cur.execute(f"DELETE FROM {table} WHERE message_id IN (SELECT id FROM messages WHERE sender_id=?)", (user_id,))
            cur.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))

        # Delete messages sent by the user
        cur.execute("DELETE FROM messages WHERE sender_id=?", (user_id,))

        # Delete other directly related user data
        cur.execute("DELETE FROM pinned_conversations WHERE user_id=?", (user_id,))
        cur.execute("DELETE FROM user_contacts WHERE user_id=? OR contact_id=?", (user_id, user_id))

        # Finally, delete the user
        cur.execute("DELETE FROM users WHERE id=?", (user_id,))

        conn.commit()
    finally:
        conn.close()

# --------------------------
# Utilities & Auth
# --------------------------
def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def update_last_seen(user_id):
    conn = db()
    conn.execute("UPDATE users SET last_seen=? WHERE id=?", (now_str(), user_id))
    conn.commit()
    conn.close()

def current_user():
    if "user_id" in session:
        conn = db()
        row = conn.execute("SELECT id, username, password_hash, display_name, avatar_path, bio, created_at FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        conn.close()
        return row
    return None

def login_required():
    return bool(current_user())

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT

def get_display_name(user_id):
    conn = db()
    row = conn.execute("SELECT COALESCE(display_name, username) AS display_name FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return row["display_name"] if row else "Unknown User"

# --------------------------
# Routes: Auth & Static
# --------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    user = current_user()
    if not user:
        return render_template_string(TPL_LOGIN)
    return render_template_string(TPL_CHAT, user=dict(user))

@app.route("/register", methods=["POST"])
def register():
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    if not username or not password:
        return render_template_string(TPL_LOGIN, error="Username & password wajib diisi.")
    try:
        conn = db()
        conn.execute(
            "INSERT INTO users (username, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
            (username, generate_password_hash(password), username, now_str())
        )
        conn.commit()
        conn.close()
        return render_template_string(TPL_LOGIN, success="Registrasi berhasil. Silakan login.")
    except sqlite3.IntegrityError:
        return render_template_string(TPL_LOGIN, error="Username sudah digunakan.")

@app.route("/login", methods=["POST"])
def login():
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if row and check_password_hash(row["password_hash"], password):
        session["user_id"] = row["id"]
        session["username"] = row["username"]
        return redirect(url_for("index"))
    return render_template_string(TPL_LOGIN, error="Login gagal. Cek username/password.")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/uploads/<path:fname>")
def uploads(fname):
    if ".." in fname or fname.startswith("/"):
        abort(404)
    resp = send_from_directory(UPLOAD_DIR, fname, as_attachment=False)
    # Add cache headers for performance
    resp.cache_control.max_age = 86400  # 24 hours
    resp.cache_control.public = True
    # Add last modified header if file exists
    import os
    file_path = os.path.join(UPLOAD_DIR, fname)
    if os.path.isfile(file_path):
        import datetime
        from werkzeug.wsgi import FileWrapper
        resp.last_modified = datetime.datetime.fromtimestamp(os.path.getmtime(file_path), tz=datetime.UTC)
    return resp

@app.route('/favicon.ico')
def favicon():
    # Return a simple empty response to avoid error
    from flask import make_response
    response = make_response()
    response.headers['Content-Type'] = 'image/x-icon'
    return response

@app.route('/info')
def info():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except:
        local_ip = "127.0.0.1"
    return f"""
    <h1>Chat App Network Info</h1>
    <p>Access from this device: <a href="http://127.0.0.1:8080">http://127.0.0.1:8080</a></p>
    <p>Access from other devices (same network): <a href="http://{local_ip}:8080">http://{local_ip}:8080</a></p>
    <p>If not working, check firewall settings or use http://0.0.0.0:8080</p>
    """

# --------------------------
# APIs: Profile & Avatars
# --------------------------
@app.route("/api/me")
def api_me():
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    u = current_user()
    return jsonify({"id": u["id"], "username": u["username"], "display_name": u["display_name"], "avatar_path": u["avatar_path"], "bio": u["bio"], "created_at": u["created_at"]})

@app.route("/api/profile/avatar", methods=["POST"])
def api_avatar():
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    if "file" not in request.files:
        return jsonify({"error":"No file"}), 400
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error":"Empty filename"}), 400
    ext = f.filename.rsplit(".",1)[-1].lower()
    if ext not in ["png","jpg","jpeg","gif","webp"]:
        return jsonify({"error":"Hanya gambar"}), 400
    fname = secure_filename(f"{uuid4().hex}.{ext}")
    f.save(os.path.join(AVATAR_DIR, fname))
    rel = f"avatars/{fname}"
    conn = db()
    conn.execute("UPDATE users SET avatar_path=? WHERE id=?", (rel, current_user()["id"]))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "avatar_path": rel, "url": url_for("uploads", fname=rel)})

@app.route("/api/profile/update", methods=["POST"])
def api_update_profile():
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    u = current_user()
    print('Starting profile update for user', u["id"])

    # Handle avatar file if present
    avatar_path = None
    if 'avatar' in request.files:
        avatar_file = request.files['avatar']
        print('avatar in request.files, filename:', repr(avatar_file.filename))
        if avatar_file.filename != '':
            print('avatar filename not empty')
            ext = avatar_file.filename.rsplit(".",1)[-1].lower()
            print('extension:', ext)
            if ext in ["png","jpg","jpeg","gif","webp"]:
                fname = secure_filename(f"{uuid4().hex}.{ext}")
                print('generated fname:', fname)
                save_path = os.path.join(AVATAR_DIR, fname)
                print('saving to:', save_path)
                avatar_file.save(save_path)
                if os.path.exists(save_path):
                    print('file saved successfully')

                    # Process image to crop to square and resize
                    if PIL_AVAILABLE:
                        try:
                            with Image.open(save_path) as img:
                                # Convert to RGB if necessary
                                if img.mode != 'RGB':
                                    img = img.convert('RGB')
                                # Crop to square center
                                width, height = img.size
                                size = min(width, height)
                                left = (width - size) // 2
                                top = (height - size) // 2
                                right = left + size
                                bottom = top + size
                                img_cropped = img.crop((left, top, right, bottom))
                                # Resize to 200x200 for high quality
                                img_resized = img_cropped.resize((200, 200), Image.Resampling.LANCZOS)
                                # Save back as JPEG for consistency
                                new_fname = fname.rsplit('.', 1)[0] + '.jpg'
                                new_save_path = os.path.join(AVATAR_DIR, new_fname)
                                img_resized.save(new_save_path, 'JPEG', quality=95)
                                # Remove original
                                if save_path != new_save_path:
                                    os.remove(save_path)
                                fname = new_fname
                                print('Image cropped and resized to 200x200')
                        except Exception as e:
                            print('Failed to process image:', e)
                            # Continue with original if processing fails
                    else:
                        print('PIL not available, skipping image processing')

                    avatar_path = f"avatars/{fname}"
                    print('avatar_path set:', avatar_path)
                else:
                    print('file failed to save')
                    return jsonify({"error": "Failed to save avatar"}), 500
            else:
                return jsonify({"error": "Invalid image type"}), 400
        else:
            print('avatar filename empty')
    else:
        print('no avatar in request.files')

    # Get other fields from form data
    new_username = (request.form.get("username") or "").strip()
    new_display_name = (request.form.get("display_name") or "").strip()
    new_bio = (request.form.get("bio") or "").strip()
    old_password = request.form.get("old_password") or ""
    new_password = request.form.get("new_password") or ""
    confirm_new = request.form.get("confirm_new_password") or ""

    conn = db()
    cursor = conn.cursor()
    updates = []
    params = []

    # Handle avatar update
    if avatar_path:
        updates.append("avatar_path=?")
        params.append(avatar_path)

    if new_username and new_username != u["username"]:
        # check unique
        existing = cursor.execute("SELECT 1 FROM users WHERE username=? AND id!=?", (new_username, u["id"])).fetchone()
        if existing:
            conn.close(); return jsonify({"error": "Username sudah digunakan"}), 400
        updates.append("username=?")
        params.append(new_username)

    if new_display_name != (u["display_name"] or ""):
        updates.append("display_name=?")
        params.append(new_display_name)
    
    if new_bio != (u["bio"] or ""):
        updates.append("bio=?")
        params.append(new_bio)

    if new_password:
        if not check_password_hash(u["password_hash"], old_password):
            conn.close(); return jsonify({"error": "Password lama salah"}), 400
        if new_password != confirm_new:
            conn.close(); return jsonify({"error": "Konfirmasi password baru tidak cocok"}), 400
        if len(new_password) < 6:
            conn.close(); return jsonify({"error": "Password minimal 6 karakter"}), 400
        updates.append("password_hash=?")
        params.append(generate_password_hash(new_password))

    if updates:
        set_clause = ", ".join(updates)
        params.append(u["id"])
        cursor.execute(f"UPDATE users SET {set_clause} WHERE id=?", params)
        conn.commit()
        # update session if username changed
        if new_username:
            session["username"] = new_username
        conn.close()
        return jsonify({"ok": True, "message": "Profile diperbarui"})
    else:
        conn.close()
    return jsonify({"ok": False, "message": "Tidak ada perubahan"})

@app.route("/api/profile/delete", methods=["POST"])
def api_delete_profile():
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    u = current_user()
    password = request.form.get("password") or ""
    if not check_password_hash(u["password_hash"], password):
        return jsonify({"error": "Password salah"}), 400
    try:
        delete_user(u["id"])
        session.clear()
        return jsonify({"ok": True, "message": "Akun berhasil dihapus"})
    except Exception as e:
        return jsonify({"error": f"Gagal menghapus akun: {str(e)}"}), 500

@app.route("/api/forgot_password/request_token", methods=["POST"])
def request_password_reset_token():
    username = (request.json.get("username") or "").strip()
    if not username:
        return jsonify({"error": "Username wajib diisi"}), 400

    conn = db()
    user = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if not user:
        conn.close()
        return jsonify({"error": "User tidak ditemukan"}), 404

    token = secrets.token_urlsafe(32)
    from datetime import timedelta
    expires = datetime.now(datetime.timezone.utc) + timedelta(minutes=15)
    expires_str = expires.strftime("%Y-%m-%d %H:%M:%S")

    conn.execute("UPDATE users SET reset_token=?, reset_expires=? WHERE id=?", (token, expires_str, user["id"]))
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "token": token, "expires": expires_str})

@app.route("/api/forgot_password/reset", methods=["POST"])
def reset_password_with_token():
    token = (request.json.get("token") or "").strip()
    new_password = (request.json.get("new_password") or "").strip()

    if not token or not new_password:
        return jsonify({"error": "Token dan password baru wajib diisi"}), 400

    conn = db()
    user = conn.execute("SELECT id, reset_expires FROM users WHERE reset_token=?", (token,)).fetchone()
    if not user or datetime.utcnow() > datetime.strptime(user["reset_expires"], "%Y-%m-%d %H:%M:%S"):
        conn.close()
        return jsonify({"error": "Token tidak valid atau sudah kedaluwarsa"}), 400

    conn.execute("UPDATE users SET password_hash=?, reset_token=NULL, reset_expires=NULL WHERE id=?", (generate_password_hash(new_password), user["id"]))
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "message": "Password berhasil direset. Silakan login."})
# --------------------------
# APIs: Users / Groups / Conversations / Pins
# --------------------------
@app.route("/api/users")
def api_users():
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    me = current_user()
    conn = db()
    rows = conn.execute("""
        SELECT id, username, COALESCE(display_name, username) AS display_name, avatar_path, bio FROM users WHERE id != ?
        ORDER BY display_name
    """, (me["id"],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/search_users")
def api_search_users():
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    me = current_user()
    q = (request.args.get("username") or "").strip()
    if not q:
        return jsonify([])
    limit = min(int(request.args.get("limit", 10)), 20)
    conn = db()
    rows = conn.execute("""
        SELECT id, username, COALESCE(display_name, username) AS display_name, avatar_path, bio
        FROM users
        WHERE (LOWER(username) LIKE LOWER(?) OR LOWER(COALESCE(display_name, username)) LIKE LOWER(?)) AND id != ?
        ORDER BY username
        LIMIT ?
    """, (f"{q}%", f"{q}%", me["id"], limit)).fetchall()
    # Check contact status
    my_contacts = conn.execute("SELECT contact_id, status FROM user_contacts WHERE user_id=?", (me["id"],)).fetchall()
    contact_status = {r["contact_id"]: r["status"] for r in my_contacts}
    # Check incoming requests
    incoming_requests = conn.execute("""
        SELECT user_id FROM user_contacts WHERE contact_id=? AND status='pending'
    """, (me["id"],)).fetchall()
    incoming_ids = {r["user_id"] for r in incoming_requests}
    results = []
    for r in rows:
        status = contact_status.get(r["id"])
        is_incoming_request = r["id"] in incoming_ids
        results.append({
            "id": r["id"],
            "username": r["username"],
            "display_name": r["display_name"],
            "avatar_path": r["avatar_path"],
            "bio": r["bio"],
            "contact_status": status,  # 'pending', 'accepted', 'rejected', or None
            "has_incoming_request": is_incoming_request
        })
    conn.close()
    return jsonify(results)

@app.route("/api/pending_requests")
def api_pending_requests():
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    me = current_user()
    conn = db()
    rows = conn.execute("""
        SELECT uc.user_id, u.username, COALESCE(u.display_name, u.username) AS display_name, u.avatar_path, uc.added_at
        FROM user_contacts uc
        JOIN users u ON uc.user_id = u.id
        WHERE uc.contact_id=? AND uc.status='pending'
        ORDER BY uc.added_at DESC
    """, (me["id"],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/contact_request", methods=["POST"])
def api_contact_request_action():
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    me = current_user()
    data = request.json or {}
    request_user_id = int(data.get("request_user_id"))  # the user who sent the request
    action = data.get("action")  # 'accept' or 'reject'
    if not request_user_id or action not in ["accept", "reject"]:
        return jsonify({"error": "Invalid data"}), 400

    conn = db()
    # Check if there is a pending request from request_user_id to me
    req = conn.execute("""
        SELECT 1 FROM user_contacts WHERE user_id=? AND contact_id=? AND status='pending'
    """, (request_user_id, me["id"])).fetchone()
    if not req:
        conn.close()
        return jsonify({"error": "No such pending request"}), 404

    if action == "accept":
        # Update existing request to accepted
        conn.execute("UPDATE user_contacts SET status='accepted' WHERE user_id=? AND contact_id=?", (request_user_id, me["id"]))
        # Insert mutual contact if not exists
        existing_reverse = conn.execute("SELECT 1 FROM user_contacts WHERE user_id=? AND contact_id=?", (me["id"], request_user_id)).fetchone()
        if not existing_reverse:
            conn.execute("INSERT INTO user_contacts (user_id, contact_id, status, added_at) VALUES (?, ?, 'accepted', ?)",
                         (me["id"], request_user_id, now_str()))
        conn.commit()
        # Notify the requester
        socketio.emit("contact_accepted", {"by": me["id"]}, room=f"user_{request_user_id}")
    elif action == "reject":
        # Update to rejected
        conn.execute("UPDATE user_contacts SET status='rejected' WHERE user_id=? AND contact_id=?", (request_user_id, me["id"]))
        conn.commit()
        # Optionally notify
        socketio.emit("contact_rejected", {"by": me["id"]}, room=f"user_{request_user_id}")

    conn.close()
    return jsonify({"ok": True, "action": action})

@app.route("/api/remove_contact", methods=["POST"])
def api_remove_contact():
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    me = current_user()
    data = request.json or {}
    target_id = int(data.get("user_id"))
    if not target_id or target_id == me["id"]:
        return jsonify({"error": "invalid user"}), 400

    conn = db()
    cur = conn.cursor()

    # Delete message_status for their messages
    cur.execute("""
        DELETE FROM message_status
        WHERE message_id IN (
            SELECT id FROM messages
            WHERE chat_type='direct' AND
            ((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?))
        )
    """, (me["id"], target_id, target_id, me["id"]))

    # Delete stars for their messages
    cur.execute("""
        DELETE FROM stars
        WHERE message_id IN (
            SELECT id FROM messages WHERE chat_type='direct' AND
            ((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?))
        )
    """, (me["id"], target_id, target_id, me["id"]))

    # Delete reactions for their messages
    cur.execute("""
        DELETE FROM reactions
        WHERE message_id IN (
            SELECT id FROM messages WHERE chat_type='direct' AND
            ((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?))
        )
    """, (me["id"], target_id, target_id, me["id"]))

    # Delete mentions for their messages
    cur.execute("""
        DELETE FROM mentions
        WHERE message_id IN (
            SELECT id FROM messages WHERE chat_type='direct' AND
            ((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?))
        )
    """, (me["id"], target_id, target_id, me["id"]))

    # Delete messages
    cur.execute("""
        DELETE FROM messages
        WHERE chat_type='direct' AND
        ((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?))
    """, (me["id"], target_id, target_id, me["id"]))

    # Delete pinned conversations
    cur.execute("DELETE FROM pinned_conversations WHERE chat_type='direct' AND ref_id=? AND user_id=?", (me["id"], me["id"]))
    cur.execute("DELETE FROM pinned_conversations WHERE chat_type='direct' AND ref_id=? AND user_id=?", (target_id, me["id"]))

    # Delete contacts
    cur.execute("DELETE FROM user_contacts WHERE (user_id=? AND contact_id=?)", (me["id"], target_id))
    cur.execute("DELETE FROM user_contacts WHERE (user_id=? AND contact_id=?)", (target_id, me["id"]))

    conn.commit()
    conn.close()

    # Notify the other user if online
    socketio.emit('contact_removed', {'removed_by': me["id"], 'target': target_id}, room=f"user_{target_id}")

    return jsonify({"ok": True})

@app.route("/api/add_contact", methods=["POST"])
def api_add_contact():
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    me = current_user()
    data = request.json or {}
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"error": "Username wajib diisi"}), 400

    conn = db()
    cur = conn.cursor()
    # Find user by username
    target = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if not target:
        conn.close()
        return jsonify({"error": "User tidak ditemukan"}), 404

    target_id = target["id"]
    if target_id == me["id"]:
        conn.close()
        return jsonify({"error": "Tidak bisa menambah diri sendiri"}), 400

    # Check if already has a contact entry with any status
    existing = conn.execute("SELECT status FROM user_contacts WHERE user_id=? AND contact_id=?", (me["id"], target_id)).fetchone()
    if existing:
        if existing["status"] == "pending":
            conn.close()
            return jsonify({"error": "Request sedang menunggu"}), 400
        elif existing["status"] == "accepted":
            conn.close()
            return jsonify({"error": "Sudah di kontak"}), 400
        else:  # rejected, allow resend
            cur.execute("UPDATE user_contacts SET status='pending', added_at=? WHERE user_id=? AND contact_id=?",
                       (now_str(), me["id"], target_id))
    else:
        # Add pending request
        cur.execute("INSERT INTO user_contacts (user_id, contact_id, added_at, status) VALUES (?,?,?, 'pending')",
                   (me["id"], target_id, now_str()))

    conn.commit()
    conn.close()

    # Notify via socket if user is online
    socketio.emit("contact_request", {"from": me["id"], "from_username": me["username"], "from_display_name": me["display_name"], "to": target_id}, room=f"user_{target_id}")

    return jsonify({"ok": True, "contact_id": target_id, "status": "pending"})

@app.route("/api/groups", methods=["GET", "POST", "PUT"])
def api_groups():
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    me = current_user()
    conn = db()
    if request.method == "POST":
        name = (request.json.get("name") or "").strip()
        member_ids = request.json.get("members") or []
        if not name:
            conn.close(); return jsonify({"error":"Nama grup wajib"}), 400
        cur = conn.cursor()
        cur.execute("INSERT INTO groups (name, owner_id, created_at) VALUES (?,?,?)", (name, me["id"], now_str()))
        gid = cur.lastrowid
        cur.execute("INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES (?,?, 'owner')", (gid, me["id"]))
        for uid in member_ids:
            if uid != me["id"]:
                cur.execute("INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES (?,?, 'member')", (gid, uid))
        conn.commit(); conn.close()
        socketio.emit("group_created", {"group_id": gid, "name": name}, room=f"user_{me['id']}")
        for uid in member_ids:
            socketio.emit("group_created", {"group_id": gid, "name": name}, room=f"user_{uid}")
        return jsonify({"group_id": gid, "name": name})
    elif request.method == "PUT":
        # admin operations: rename, add/remove members (owner only)
        gid = int(request.json.get("group_id"))
        grp = conn.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
        is_owner = conn.execute("SELECT role FROM group_members WHERE group_id=? AND user_id=?", (gid, me["id"])).fetchone()
        if not grp or not is_owner or is_owner["role"] != "owner":
            conn.close(); return jsonify({"error":"Hanya owner grup"}), 403
        action = request.json.get("action")
        if action == "rename":
            new_name = (request.json.get("name") or "").strip()
            if not new_name:
                conn.close(); return jsonify({"error":"Nama tidak boleh kosong"}), 400
            conn.execute("UPDATE groups SET name=? WHERE id=?", (new_name, gid))
            conn.commit(); conn.close()
            socketio.emit("group_updated", {"group_id": gid, "name": new_name}, room=f"group_{gid}")
            return jsonify({"ok": True})
        elif action == "add_members":
            ids = request.json.get("user_ids") or []
            cur = conn.cursor()
            for uid in ids:
                cur.execute("INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES (?,?, 'member')", (gid, uid))
            conn.commit(); conn.close()
            socketio.emit("group_members_updated", {"group_id": gid}, room=f"group_{gid}")
            return jsonify({"ok": True})
        elif action == "remove_members":
            ids = request.json.get("user_ids") or []
            cur = conn.cursor()
            for uid in ids:
                if uid == me["id"]:  # owner tidak dihapus sendiri
                    continue
                cur.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?", (gid, uid))
            conn.commit(); conn.close()
            socketio.emit("group_members_updated", {"group_id": gid}, room=f"group_{gid}")
            return jsonify({"ok": True})
        elif action == "delete_group":
            members = conn.execute("SELECT user_id FROM group_members WHERE group_id=? AND user_id!=? ORDER BY user_id", (gid, me["id"],)).fetchall()
            if len(members) > 1:
                new_owner = members[0]["user_id"]
                conn.execute("UPDATE groups SET owner_id=? WHERE id=?", (new_owner, gid))
                conn.execute("UPDATE group_members SET role='owner' WHERE group_id=? AND user_id=?", (gid, new_owner))
                conn.execute("UPDATE group_members SET role='member' WHERE group_id=? AND user_id=?", (gid, me["id"]))
                conn.commit()
                # emit owner changed
                for m in members + [{"user_id": me["id"]}]:
                    socketio.emit("group_updated", {"group_id": gid, "name": grp["name"], "owner_id": new_owner}, room=f"user_{m['user_id']}")
                conn.close()
                return jsonify({"ok": True})
            else:
                # delete group
                cur = conn.cursor()
                cur.execute("DELETE FROM mentions WHERE message_id IN (SELECT id FROM messages WHERE group_id=?)", (gid,))
                cur.execute("DELETE FROM reactions WHERE message_id IN (SELECT id FROM messages WHERE group_id=?)", (gid,))
                cur.execute("DELETE FROM message_status WHERE message_id IN (SELECT id FROM messages WHERE group_id=?)", (gid,))
                cur.execute("DELETE FROM stars WHERE message_id IN (SELECT id FROM messages WHERE group_id=?)", (gid,))
                cur.execute("DELETE FROM messages WHERE group_id=?", (gid,))
                cur.execute("DELETE FROM group_members WHERE group_id=?", (gid,))
                cur.execute("DELETE FROM pinned_conversations WHERE chat_type='group' AND ref_id=?", (gid,))
                cur.execute("DELETE FROM groups WHERE id=?", (gid,))
                conn.commit()
                # emit deleted to all
                all_members = [me["id"]] + [m["user_id"] for m in members]
                for uid in all_members:
                    socketio.emit("group_deleted", {"group_id": gid}, room=f"user_{uid}")
                conn.close()
                return jsonify({"ok": True})
        # else:
        #     conn.close(); return jsonify({"error":"Aksi tidak dikenal"}), 400
    else:
        rows = conn.execute("""
            SELECT g.id, g.name, g.owner_id FROM groups g
            JOIN group_members gm ON g.id = gm.group_id
            WHERE gm.user_id = ?
            ORDER BY g.id DESC
        """, (me["id"],)).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

@app.route("/api/conversations")
def api_conversations():
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    me = current_user()
    conn = db()
    # Fetch accepted contacts and order them by the last message time
    peers = conn.execute("""
        SELECT
            u.id, u.username, COALESCE(u.display_name, u.username) AS display_name,
            u.avatar_path, u.created_at, u.bio, u.last_seen,
            (
                SELECT MAX(m.created_at)
                FROM messages m
                WHERE m.chat_type = 'direct'
                AND ((m.sender_id = ? AND m.receiver_id = u.id) OR (m.sender_id = u.id AND m.receiver_id = ?))
            ) AS last_message_time
        FROM users u
        JOIN user_contacts uc ON uc.user_id = ? AND uc.contact_id = u.id AND uc.status = 'accepted'
        ORDER BY last_message_time DESC, u.display_name ASC
    """, (me["id"], me["id"], me["id"])).fetchall()

    groups = conn.execute("""
        SELECT g.id, g.name, g.owner_id, g.avatar_path FROM groups g
        JOIN group_members gm ON g.id = gm.group_id
        WHERE gm.user_id = ?
        ORDER BY g.id DESC
    """, (me["id"],)).fetchall()
    # pinned - only for accepted contacts
    pins = conn.execute("""
        SELECT chat_type, ref_id FROM pinned_conversations WHERE user_id=? ORDER BY position ASC, pinned_at DESC
    """, (me["id"],)).fetchall()
    # filter pinned to only existing accepted contacts or groups
    accepted_peer_ids = {p["id"] for p in peers}
    accepted_group_ids = {g["id"] for g in groups}
    pinned = []
    for x in pins:
        if (x["chat_type"] == "direct" and x["ref_id"] in accepted_peer_ids) or \
           (x["chat_type"] == "group" and x["ref_id"] in accepted_group_ids):
            pinned.append(dict(x))

    # --- Efficiently fetch all previews and unread counts ---
    # 1. Fetch all direct message previews
    direct_previews_rows = conn.execute("""
        WITH LastMessages AS (
            SELECT *, ROW_NUMBER() OVER(PARTITION BY CASE WHEN sender_id=? THEN receiver_id ELSE sender_id END ORDER BY id DESC) as rn
            FROM messages WHERE chat_type='direct' AND (sender_id=? OR receiver_id=?)
        )
        SELECT * FROM LastMessages WHERE rn=1
    """, (me["id"], me["id"], me["id"])).fetchall()
    direct_previews = {row['receiver_id'] if row['sender_id'] == me["id"] else row['sender_id']: dict(row) for row in direct_previews_rows}

    # 2. Fetch all group message previews
    group_previews_rows = conn.execute("""
        WITH LastMessages AS (
            SELECT *, ROW_NUMBER() OVER(PARTITION BY group_id ORDER BY id DESC) as rn
            FROM messages WHERE chat_type='group' AND group_id IN (SELECT group_id FROM group_members WHERE user_id=?)
        )
        SELECT * FROM LastMessages WHERE rn=1
    """, (me["id"],)).fetchall()
    group_previews = {row['group_id']: dict(row) for row in group_previews_rows}

    # 3. Fetch all direct unread counts
    direct_unread_rows = conn.execute("""
        SELECT m.sender_id, COUNT(m.id) as cnt FROM messages m
        LEFT JOIN message_status ms ON ms.message_id = m.id AND ms.user_id = ?
        WHERE m.chat_type='direct' AND m.receiver_id = ? AND ms.read_at IS NULL AND m.deleted = 0
        GROUP BY m.sender_id
    """, (me["id"], me["id"])).fetchall()
    direct_unread = {row['sender_id']: row['cnt'] for row in direct_unread_rows}

    # 4. Fetch all group unread counts
    group_unread_rows = conn.execute("""
        SELECT m.group_id, COUNT(m.id) as cnt FROM messages m
        LEFT JOIN message_status ms ON ms.message_id = m.id AND ms.user_id = ?
        WHERE m.chat_type='group' AND m.sender_id != ? AND ms.read_at IS NULL AND m.deleted = 0
        AND m.group_id IN (SELECT group_id FROM group_members WHERE user_id=?)
        GROUP BY m.group_id
    """, (me["id"], me["id"], me["id"])).fetchall()
    group_unread = {row['group_id']: row['cnt'] for row in group_unread_rows}

    conn.close()

    # Prepare response data
    peer_data = []
    for p in peers:
        peer_data.append({
            "id": p["id"],
            "username": p["username"],
            "display_name": p["display_name"],
            "avatar_path": p["avatar_path"],
            "bio": p["bio"],
            "created_at": p["created_at"],
            "preview": direct_previews.get(p["id"]),
            "unread_count": direct_unread.get(p["id"], 0)
        })
    group_data = [{"id": g["id"], "name": g["name"], "owner_id": g["owner_id"], "avatar_path": g["avatar_path"], "preview": group_previews.get(g["id"]), "unread_count": group_unread.get(g["id"], 0)} for g in groups]
    return jsonify({
        "peers": peer_data,
        "groups": group_data,
        "pinned": pinned
    })

@app.route("/api/pin", methods=["POST"])
def api_pin():
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    me = current_user()
    data = request.json or {}
    chat_type = data.get("chat_type")
    ref_id = int(data.get("ref_id"))
    toggle = bool(data.get("toggle", True))
    conn = db()
    cur = conn.cursor()
    exists = cur.execute("""
        SELECT 1 FROM pinned_conversations WHERE user_id=? AND chat_type=? AND ref_id=?
    """, (me["id"], chat_type, ref_id)).fetchone()
    if exists and toggle:
        cur.execute("DELETE FROM pinned_conversations WHERE user_id=? AND chat_type=? AND ref_id=?", (me["id"], chat_type, ref_id))
    elif not exists:
        cur.execute("INSERT INTO pinned_conversations (user_id, chat_type, ref_id, position, pinned_at) VALUES (?,?,?,?,?)",
                    (me["id"], chat_type, ref_id, 0, now_str()))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/pin_message", methods=["POST"])
def api_pin_message():
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    me = current_user()
    data = request.json or {}
    message_id = int(data.get("message_id"))
    action = data.get("action", "pin")  # "pin" or "unpin"
    conn = db()
    # Get message info
    msg = conn.execute("SELECT group_id FROM messages WHERE id=? AND chat_type='group'", (message_id,)).fetchone()
    if not msg:
        conn.close()
        return jsonify({"error": "Invalid message or not a group message"}), 400
    group_id = msg["group_id"]
    # Check if user is member of group (any role can pin)
    member = conn.execute("SELECT 1 FROM group_members WHERE group_id=? AND user_id=?", (group_id, me["id"])).fetchone()
    if not member:
        conn.close()
        return jsonify({"error": "You are not a member of this group"}), 403
    # Check if pinned
    pinned = conn.execute("SELECT id FROM pinned_messages WHERE group_id=? AND message_id=?", (group_id, message_id)).fetchone()
    if action == "pin" and not pinned:
        conn.execute("INSERT INTO pinned_messages (message_id, pinned_by, group_id, pinned_at) VALUES (?,?,?,?)",
                     (message_id, me["id"], group_id, now_str()))
    elif action == "unpin" and pinned:
        conn.execute("DELETE FROM pinned_messages WHERE id=?", (pinned["id"],))
    conn.commit()
    conn.close()
    # Emit to group for real-time update
    socketio.emit("message_pinned", {"message_id": message_id, "group_id": group_id, "action": action, "pinned_by": me["id"]}, room=f"group_{group_id}")
    return jsonify({"ok": True, "action": action})

@app.route("/api/pinned_messages/<int:group_id>")
def api_pinned_messages(group_id):
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    me = current_user()
    conn = db()
    # Check if member
    member = conn.execute("SELECT 1 FROM group_members WHERE group_id=? AND user_id=?", (group_id, me["id"])).fetchone()
    if not member:
        conn.close()
        return jsonify({"error":"not member"}), 403
    # Get pinned messages with sender details
    rows = conn.execute("""
        SELECT pm.id, pm.message_id, pm.pinned_at, m.content, m.content_type, m.file_path, m.deleted,
               u.display_name AS sender_name, u.avatar_path AS sender_avatar
        FROM pinned_messages pm
        JOIN messages m ON pm.message_id = m.id
        JOIN users u ON u.id = m.sender_id
        WHERE pm.group_id=? AND m.chat_type='group'
        ORDER BY pm.pinned_at DESC
    """, (group_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/group_info/<int:gid>")
def api_group_info(gid):
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    me = current_user()
    conn = db()
    # Check if member
    member = conn.execute("SELECT 1 FROM group_members WHERE group_id=? AND user_id=?", (gid, me["id"])).fetchone()
    if not member:
        conn.close()
        return jsonify({"error":"not member"}), 403
    grp = conn.execute("SELECT id, name, owner_id, avatar_path FROM groups WHERE id=?", (gid,)).fetchone()
    if not grp:
        conn.close()
        return jsonify({"error":"group not found"}), 404
    members = conn.execute("SELECT u.id, u.username, COALESCE(u.display_name, u.username) AS display_name, u.avatar_path, u.bio, gm.role FROM users u JOIN group_members gm ON u.id=gm.user_id WHERE gm.group_id=? ORDER BY gm.role DESC, u.display_name", (gid,)).fetchall()
    conn.close()
    return jsonify({
        "id": grp["id"],
        "name": grp["name"],
        "owner_id": grp["owner_id"],
        "avatar_path": grp["avatar_path"],
        "members": [dict(m) for m in members]
    })

@app.route("/api/groups/<int:group_id>", methods=["PUT", "DELETE", "POST"])
def api_group_admin(group_id):
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    me = current_user()
    conn = db()
    group = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    if not group:
        conn.close()
        return jsonify({"error": "Group not found"}), 404
    if request.method == "PUT":
        # admin operations: rename, add/remove members (owner only)
        if group["owner_id"] != me["id"]:
            conn.close()
            return jsonify({"error": "Only owner can perform admin actions"}), 403
        data = request.json or {}
        action = data.get("action")
        if action == "rename":
            new_name = (data.get("name") or "").strip()
            if not new_name:
                conn.close()
                return jsonify({"error": "Name cannot be empty"}), 400
            conn.execute("UPDATE groups SET name=? WHERE id=?", (new_name, group_id))
            conn.commit()
            conn.close()
            socketio.emit("group_updated", {"group_id": group_id, "name": new_name}, room=f"group_{group_id}")
            return jsonify({"ok": True})
        elif action == "add_members":
            ids = data.get("user_ids") or []
            cur = conn.cursor()
            added = []
            for uid in ids:
                exists = cur.execute("SELECT 1 FROM group_members WHERE group_id=? AND user_id=?", (group_id, uid)).fetchone()
                if not exists:
                    cur.execute("INSERT INTO group_members (group_id, user_id, role) VALUES (?,?, 'member')", (group_id, uid))
                    added.append(uid)
            conn.commit()
            conn.close()
            socketio.emit("group_members_updated", {"group_id": group_id}, room=f"group_{group_id}")
            for uid in added:
                socketio.emit("added_to_group", {"group_id": group_id, "name": group["name"]}, room=f"user_{uid}")
            return jsonify({"ok": True})
        elif action == "remove_members":
            ids = data.get("user_ids") or []
            cur = conn.cursor()
            for uid in ids:
                if uid == me["id"]:  # owner cannot remove themselves?
                    continue
                cur.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?", (group_id, uid))
            conn.commit()
            conn.close()
            socketio.emit("group_members_updated", {"group_id": group_id}, room=f"group_{group_id}")
            return jsonify({"ok": True})
        elif action == "delete_group":
            # Check members
            members = conn.execute("SELECT user_id FROM group_members WHERE group_id=? ORDER BY user_id", (group_id,)).fetchall()
            # Delete messages, etc.
            cur = conn.cursor()
            # Delete mentions, reactions, message_status, stars
            msg_ids = conn.execute("SELECT id FROM messages WHERE group_id=?", (group_id,)).fetchall()
            for msg in msg_ids:
                mid = msg["id"]
                cur.execute("DELETE FROM mentions WHERE message_id=?", (mid,))
                cur.execute("DELETE FROM reactions WHERE message_id=?", (mid,))
                cur.execute("DELETE FROM message_status WHERE message_id=?", (mid,))
                cur.execute("DELETE FROM stars WHERE message_id=?", (mid,))
            cur.execute("DELETE FROM messages WHERE group_id=?", (group_id,))
            cur.execute("DELETE FROM group_members WHERE group_id=?", (group_id,))
            cur.execute("DELETE FROM pinned_conversations WHERE chat_type='group' AND ref_id=?", (group_id,))
            cur.execute("DELETE FROM groups WHERE id=?", (group_id,))
            conn.commit()
            conn.close()
            # Notify members
            all_members = [me["id"]] + [m["user_id"] for m in members]
            for uid in all_members:
                socketio.emit("group_deleted", {"group_id": group_id}, room=f"user_{uid}")
            return jsonify({"message": "Group deleted"})
        else:
            conn.close()
            return jsonify({"error": "Unknown action"}), 400
    elif request.method == "DELETE":
        # delete group
        if group["owner_id"] != me["id"]:
            conn.close()
            return jsonify({"error": "Only owner can delete group"}), 403
        # Check members
        members = conn.execute("SELECT user_id FROM group_members WHERE group_id=? ORDER BY user_id", (group_id,)).fetchall()
        # Delete messages, etc.
        cur = conn.cursor()
        # Delete mentions, reactions, message_status, stars
        msg_ids = conn.execute("SELECT id FROM messages WHERE group_id=?", (group_id,)).fetchall()
        for msg in msg_ids:
            mid = msg["id"]
            cur.execute("DELETE FROM mentions WHERE message_id=?", (mid,))
            cur.execute("DELETE FROM reactions WHERE message_id=?", (mid,))
            cur.execute("DELETE FROM message_status WHERE message_id=?", (mid,))
            cur.execute("DELETE FROM stars WHERE message_id=?", (mid,))
        cur.execute("DELETE FROM messages WHERE group_id=?", (group_id,))
        cur.execute("DELETE FROM group_members WHERE group_id=?", (group_id,))
        cur.execute("DELETE FROM pinned_conversations WHERE chat_type='group' AND ref_id=?", (group_id,))
        cur.execute("DELETE FROM groups WHERE id=?", (group_id,))
        conn.commit()
        conn.close()
        # Notify members
        all_members = [me["id"]] + [m["user_id"] for m in members]
        for uid in all_members:
            socketio.emit("group_deleted", {"group_id": group_id}, room=f"user_{uid}")
        return jsonify({"message": "Group deleted"})
    elif request.method == "POST":
        # This is for leaving a group
        is_member = conn.execute("SELECT role FROM group_members WHERE group_id=? AND user_id=?", (group_id, me["id"])).fetchone()
        if not is_member:
            conn.close()
            return jsonify({"error": "You are not a member of this group"}), 403

        if is_member["role"] == "owner":
            # For simplicity, we prevent the owner from leaving. They must delete the group.
            # A more complex implementation could transfer ownership.
            conn.close()
            return jsonify({"error": "Owner cannot leave the group, must delete it instead."}), 400

        # Insert system message before leaving
        conn.execute("""
            INSERT INTO messages (chat_type, sender_id, group_id, content, content_type, created_at)
            VALUES ('group', ?, ?, ?, 'system', ?)
        """, (me["id"], group_id, f"{me['username']} telah keluar dari grup", now_str()))

        conn.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?", (group_id, me["id"]))
        conn.commit()
        conn.close()
        socketio.emit("member_left", {"group_id": group_id, "user_id": me["id"], "username": me["username"]}, room=f"group_{group_id}")
        return jsonify({"ok": True, "message": "You have left the group."})
    elif request.method == "PUT":
        # This is the correct place for the delete_group action from the old /api/groups PUT
        if group["owner_id"] != me["id"]:
            conn.close()
            return jsonify({"error": "Only owner can delete group"}), 403
        
        data = request.json or {}
        action = data.get("action")
        if action == "delete_group":
            cur = conn.cursor()
            # Delete related data first
            cur.execute("DELETE FROM mentions WHERE message_id IN (SELECT id FROM messages WHERE group_id=?)", (group_id,))
            cur.execute("DELETE FROM reactions WHERE message_id IN (SELECT id FROM messages WHERE group_id=?)", (group_id,))
            cur.execute("DELETE FROM message_status WHERE message_id IN (SELECT id FROM messages WHERE group_id=?)", (group_id,))
            cur.execute("DELETE FROM stars WHERE message_id IN (SELECT id FROM messages WHERE group_id=?)", (group_id,))
            cur.execute("DELETE FROM pinned_conversations WHERE chat_type='group' AND ref_id=?", (group_id,))
            cur.execute("DELETE FROM messages WHERE group_id=?", (group_id,))
            cur.execute("DELETE FROM group_members WHERE group_id=?", (group_id,))
            cur.execute("DELETE FROM groups WHERE id=?", (group_id,))
            conn.commit()
            conn.close()
            socketio.emit("group_deleted", {"group_id": group_id}, room=f"user_{me['id']}") # Notify self to refresh
            return jsonify({"ok": True, "message": "Group deleted successfully"})
        else:
            conn.close()
            return jsonify({"error": "Invalid action for this endpoint"}), 400

# --------------------------
# APIs: Messages / Stars / Reactions / Mentions / Export
# --------------------------
def _msg_reply_preview(conn, msg_id):
    if not msg_id: return None
    r = conn.execute("""
        SELECT m.id, m.content, m.content_type, m.file_path, m.deleted, u.display_name AS sender_name
        FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.id=?
    """, (msg_id,)).fetchone()
    return dict(r) if r else None

@app.route("/api/messages")
def api_messages():
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    me = current_user()
    chat_type = request.args.get("chat_type")
    limit = int(request.args.get("limit","40"))
    before_id = request.args.get("before_id")
    conn = db()
    params = [chat_type]
    q = """
        SELECT m.*, u.display_name AS sender_name, u.avatar_path AS sender_avatar
        FROM messages m
        JOIN users u ON u.id=m.sender_id
        WHERE m.chat_type=?
    """
    if chat_type == "direct":
        peer_id = int(request.args.get("peer_id"))
        q += " AND ((m.sender_id=? AND m.receiver_id=?) OR (m.sender_id=? AND m.receiver_id=?))"
        params += [me["id"], peer_id, peer_id, me["id"]]
    elif chat_type == "group":
        group_id = int(request.args.get("group_id"))
        q += " AND m.group_id=?"
        params.append(group_id)
    else:
        conn.close(); return jsonify({"error":"chat_type invalid"}), 400
    if before_id:
        q += " AND m.id < ?"; params.append(int(before_id))
    q += " ORDER BY m.id DESC LIMIT ?"; params.append(limit)
    rows = conn.execute(q, tuple(params)).fetchall()

    message_ids = [r['id'] for r in rows]
    if not message_ids:
        conn.close()
        return jsonify([])

    # --- Efficiently fetch all related data in bulk (N+1 fix) ---
    placeholders = ','.join('?' for _ in message_ids)

    # 1. Bulk fetch reactions
    reactions_rows = conn.execute(f"SELECT message_id, emoji, COUNT(*) as cnt FROM reactions WHERE message_id IN ({placeholders}) GROUP BY message_id, emoji", message_ids).fetchall()
    reactions_map = {mid: [] for mid in message_ids}
    for r in reactions_rows:
        reactions_map[r['message_id']].append(dict(r))

    # 2. Bulk fetch starred status for the current user
    starred_rows = conn.execute(f"SELECT message_id FROM stars WHERE user_id=? AND message_id IN ({placeholders})", [me["id"]] + message_ids).fetchall()
    starred_set = {r['message_id'] for r in starred_rows}

    # 3. Bulk fetch read status
    status_map = {}
    if chat_type == "direct":
        peer_id = int(request.args.get("peer_id"))
        status_rows = conn.execute(f"SELECT message_id, delivered_at, read_at FROM message_status WHERE user_id=? AND message_id IN ({placeholders})", [peer_id] + message_ids).fetchall()
        status_map = {r['message_id']: dict(r) for r in status_rows}
    else: # group
        status_rows = conn.execute(f"SELECT message_id, COUNT(*) as total, SUM(CASE WHEN delivered_at IS NOT NULL THEN 1 ELSE 0 END) as delivered_count, SUM(CASE WHEN read_at IS NOT NULL THEN 1 ELSE 0 END) as read_count FROM message_status WHERE message_id IN ({placeholders}) GROUP BY message_id", message_ids).fetchall()
        status_map = {r['message_id']: dict(r) for r in status_rows}

    my_read_rows = conn.execute(f"SELECT message_id, read_at FROM message_status WHERE user_id=? AND message_id IN ({placeholders})", [me["id"]] + message_ids).fetchall()
    my_read_map = {r['message_id']: r['read_at'] for r in my_read_rows}

    # 4. Bulk fetch reply/forward previews
    preview_ids = {r['reply_to'] for r in rows if r['reply_to']}
    preview_ids.update({r['forwarded_from'] for r in rows if r['forwarded_from']})
    previews = {}
    if preview_ids:
        preview_placeholders = ','.join('?' for _ in preview_ids)
        preview_rows = conn.execute(f"SELECT m.id, m.content, m.content_type, m.file_path, m.deleted, u.display_name AS sender_name FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.id IN ({preview_placeholders})", list(preview_ids)).fetchall()
        previews = {row['id']: dict(row) for row in preview_rows}

    # --- Assemble final message objects ---
    messages = []
    for r in rows:
        msg = dict(r)
        if chat_type == "direct":
            if msg["sender_id"] == me["id"]:
                st = status_map.get(msg["id"])
                msg["status"] = {"delivered": bool(st and st["delivered_at"]), "read": bool(st and st["read_at"])}
        else:
            st = status_map.get(msg["id"], {})
            msg["status"] = {"delivered_count": st.get("delivered_count", 0), "read_count": st.get("read_count", 0)}

        msg["my_read"] = bool(my_read_map.get(msg["id"]))
        msg["reactions"] = reactions_map.get(msg["id"], [])
        msg["starred"] = msg["id"] in starred_set
        msg["reply"] = previews.get(msg.get("reply_to"))
        msg["forwarded"] = previews.get(msg.get("forwarded_from"))
        messages.append(msg)

    conn.close()
    messages = list(reversed(messages))
    return jsonify(messages)

@app.route("/api/message_status/<int:mid>")
def api_message_status(mid):
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    conn = db()
    rows = conn.execute("""
        SELECT ms.user_id, ms.delivered_at, ms.read_at, u.display_name
        FROM message_status ms JOIN users u ON u.id=ms.user_id
        WHERE ms.message_id=?
        ORDER BY COALESCE(ms.read_at, '') DESC, COALESCE(ms.delivered_at, '') DESC
    """, (mid,)).fetchall()
    conn.close()
    return jsonify([dict(x) for x in rows])

@app.route("/api/stars", methods=["GET", "POST"])
def api_stars():
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    me = current_user()
    conn = db()
    if request.method == "POST":
        mid = int((request.json or {}).get("message_id"))
        ex = conn.execute("SELECT 1 FROM stars WHERE message_id=? AND user_id=?", (mid, me["id"])).fetchone()
        if ex:
            conn.execute("DELETE FROM stars WHERE message_id=? AND user_id=?", (mid, me["id"]))
            conn.commit(); conn.close()
            return jsonify({"ok": True, "starred": False})
        else:
            conn.execute("INSERT INTO stars (message_id, user_id, created_at) VALUES (?,?,?)", (mid, me["id"], now_str()))
            conn.commit(); conn.close()
            return jsonify({"ok": True, "starred": True})
    else:
        rows = conn.execute("""
            SELECT m.*, u.display_name AS sender_name
            FROM stars s
            JOIN messages m ON m.id=s.message_id
            JOIN users u ON u.id=m.sender_id
            WHERE s.user_id=? ORDER BY s.created_at DESC
        """, (me["id"],)).fetchall()
        conn.close()
        return jsonify([dict(x) for x in rows])

@app.route("/api/search")
def api_search():
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    me = current_user()
    q = (request.args.get("q") or "").strip()
    if not q: return jsonify([])
    conn = db()
    rows = conn.execute("""
        SELECT m.*, u.display_name AS sender_name
        FROM messages m JOIN users u ON u.id=m.sender_id
        WHERE m.deleted=0 AND m.content LIKE ?
        AND (
          (m.chat_type='direct' AND (m.sender_id=? OR m.receiver_id=?))
          OR
          (m.chat_type='group' AND m.group_id IN (SELECT group_id FROM group_members WHERE user_id=?))
        )
        ORDER BY m.id DESC LIMIT 150
    """, (f"%{q}%", me["id"], me["id"], me["id"])).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/upload", methods=["POST"])
def upload():
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    if "file" not in request.files:
        return jsonify({"error":"No file"}), 400
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error":"Empty filename"}), 400
    if allowed_file(f.filename):
        ext = f.filename.rsplit(".",1)[1].lower()
        safe = secure_filename(f"{uuid4().hex}.{ext}")
        f.save(os.path.join(UPLOAD_DIR, safe))
        return jsonify({"ok": True, "path": safe, "url": url_for('uploads', fname=safe)})
    return jsonify({"error":"File type not allowed"}), 400

@app.route("/export")
def export_chat():
    if not login_required():
        return jsonify({"error":"unauthorized"}), 401
    me = current_user()
    chat_type = request.args.get("chat_type")
    conn = db()
    if chat_type == "direct":
        peer_id = int(request.args.get("peer_id"))
        rows = conn.execute("""
            SELECT m.*, u.display_name AS sender_name
            FROM messages m JOIN users u ON u.id=m.sender_id
            WHERE chat_type='direct' AND ((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?))
            ORDER BY m.id ASC
        """, (me["id"], peer_id, peer_id, me["id"])).fetchall()
    else:
        gid = int(request.args.get("group_id"))
        rows = conn.execute("""
            SELECT m.*, u.display_name AS sender_name
            FROM messages m JOIN users u ON u.id=m.sender_id
            WHERE chat_type='group' AND group_id=?
            ORDER BY m.id ASC
        """, (gid,)).fetchall()
    conn.close()
    payload = [dict(r) for r in rows]
    resp = make_response(jsonify(payload))
    resp.headers["Content-Disposition"] = "attachment; filename=chat_export.json"
    return resp

@app.route("/api/group_avatar/<int:group_id>", methods=["POST"])
def api_group_avatar(group_id):
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    me = current_user()
    conn = db()
    group = conn.execute("SELECT owner_id, name FROM groups WHERE id=?", (group_id,)).fetchone()
    if not group or group["owner_id"] != me["id"]:
        conn.close()
        return jsonify({"error": "Hanya owner yang bisa mengubah avatar"}), 403

    if "file" not in request.files:
        conn.close()
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    if f.filename == "" or not allowed_file(f.filename):
        conn.close()
        return jsonify({"error": "File tidak valid"}), 400

    ext = f.filename.rsplit(".", 1)[1].lower()
    fname = secure_filename(f"group_{group_id}_{uuid4().hex}.{ext}")
    save_path = os.path.join(AVATAR_DIR, fname)
    f.save(save_path)

    avatar_path = f"avatars/{fname}"
    conn.execute("UPDATE groups SET avatar_path=? WHERE id=?", (avatar_path, group_id))
    conn.commit()

    # Fetch members before closing connection
    members = conn.execute("SELECT user_id FROM group_members WHERE group_id=?", (group_id,)).fetchall()
    conn.close()

    # Emit to group members
    socketio.emit("group_avatar_updated", {"group_id": group_id, "avatar_path": avatar_path}, room=f"group_{group_id}")
    for m in members:
        socketio.emit("group_avatar_updated", {"group_id": group_id, "avatar_path": avatar_path}, room=f"user_{m['user_id']}")

    return jsonify({"ok": True, "avatar_path": avatar_path})

@app.route("/api/tray_unread", methods=["POST"])
def api_tray_unread():
    """Update system tray icon with unread count"""
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401

    data = request.json or {}
    count = int(data.get("count", 0))

    # Update the tray icon
    update_tray_unread_count(count)

    return jsonify({"ok": True})

# --------------------------
# APIs: New Features
# --------------------------
@app.route("/api/quick_templates", methods=["GET", "POST", "DELETE"])
def api_quick_templates():
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    me = current_user()
    conn = db()
    
    if request.method == "GET":
        rows = conn.execute("""
            SELECT id, title, content, shortcut, created_at 
            FROM quick_templates 
            WHERE user_id=? 
            ORDER BY created_at DESC
        """, (me["id"],)).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    
    elif request.method == "POST":
        data = request.json or {}
        title = (data.get("title") or "").strip()
        content = (data.get("content") or "").strip()
        shortcut = (data.get("shortcut") or "").strip() or None
        
        if not title or not content:
            conn.close()
            return jsonify({"error": "Title and content required"}), 400
        
        conn.execute("""
            INSERT INTO quick_templates (user_id, title, content, shortcut, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (me["id"], title, content, shortcut, now_str()))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    
    elif request.method == "DELETE":
        data = request.json or {}
        template_id = data.get("id")
        if not template_id:
            conn.close()
            return jsonify({"error": "ID required"}), 400
        
        conn.execute("DELETE FROM quick_templates WHERE id=? AND user_id=?", (template_id, me["id"]))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

@app.route("/api/disappearing_messages", methods=["GET", "POST"])
def api_disappearing_messages():
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    me = current_user()
    conn = db()
    
    if request.method == "GET":
        chat_type = request.args.get("chat_type")
        ref_id = request.args.get("ref_id")
        if not chat_type or not ref_id:
            conn.close()
            return jsonify({"duration_seconds": 0})
        
        row = conn.execute("""
            SELECT duration_seconds FROM disappearing_messages 
            WHERE chat_type=? AND ref_id=?
        """, (chat_type, int(ref_id))).fetchone()
        conn.close()
        return jsonify({"duration_seconds": row["duration_seconds"] if row else 0})
    
    elif request.method == "POST":
        data = request.json or {}
        chat_type = data.get("chat_type")
        ref_id = data.get("ref_id")
        duration = int(data.get("duration_seconds", 0))
        
        if not chat_type or not ref_id:
            conn.close()
            return jsonify({"error": "chat_type and ref_id required"}), 400
        
        if duration > 0:
            conn.execute("""
                INSERT OR REPLACE INTO disappearing_messages 
                (chat_type, ref_id, duration_seconds, enabled_by, enabled_at)
                VALUES (?, ?, ?, ?, ?)
            """, (chat_type, int(ref_id), duration, me["id"], now_str()))
        else:
            conn.execute("""
                DELETE FROM disappearing_messages 
                WHERE chat_type=? AND ref_id=?
            """, (chat_type, int(ref_id)))
        
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

@app.route("/api/blocked_contacts", methods=["GET"])
def api_blocked_contacts():
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    me = current_user()
    conn = db()
    
    rows = conn.execute("""
        SELECT u.id, u.username, COALESCE(u.display_name, u.username) AS display_name, 
               u.avatar_path, bc.blocked_at
        FROM blocked_contacts bc
        JOIN users u ON bc.blocked_user_id = u.id
        WHERE bc.user_id=?
        ORDER BY bc.blocked_at DESC
    """, (me["id"],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/block_contact", methods=["POST", "DELETE"])
def api_block_contact():
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    me = current_user()
    data = request.json or {}
    target_id = data.get("user_id")
    
    if not target_id:
        return jsonify({"error": "user_id required"}), 400
    
    conn = db()
    
    if request.method == "POST":
        # Block contact
        conn.execute("""
            INSERT OR IGNORE INTO blocked_contacts (user_id, blocked_user_id, blocked_at)
            VALUES (?, ?, ?)
        """, (me["id"], int(target_id), now_str()))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    
    elif request.method == "DELETE":
        # Unblock contact
        conn.execute("""
            DELETE FROM blocked_contacts 
            WHERE user_id=? AND blocked_user_id=?
        """, (me["id"], int(target_id)))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

@app.route("/api/chat_statistics", methods=["GET"])
def api_chat_statistics():
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    me = current_user()
    conn = db()
    
    # Count sent messages
    sent = conn.execute("""
        SELECT COUNT(*) as cnt FROM messages WHERE sender_id=? AND deleted=0
    """, (me["id"],)).fetchone()
    
    # Count received messages
    received = conn.execute("""
        SELECT COUNT(*) as cnt FROM messages 
        WHERE (receiver_id=? OR group_id IN (SELECT group_id FROM group_members WHERE user_id=?))
        AND sender_id!=? AND deleted=0
    """, (me["id"], me["id"], me["id"])).fetchone()
    
    # Count files shared
    files = conn.execute("""
        SELECT COUNT(*) as cnt FROM messages 
        WHERE sender_id=? AND content_type IN ('image', 'file', 'audio') AND deleted=0
    """, (me["id"],)).fetchone()
    
    conn.close()
    
    return jsonify({
        "total_sent": sent["cnt"] if sent else 0,
        "total_received": received["cnt"] if received else 0,
        "files_shared": files["cnt"] if files else 0
    })

# --------------------------
# Socket.IO Events
# --------------------------
online_users = set()

@socketio.on("connect")
def on_connect():
    u = current_user()
    if not u: return False
    online_users.add(u["id"])
    join_room(f"user_{u['id']}")
    conn = db()
    # join all user groups
    gids = conn.execute("SELECT group_id FROM group_members WHERE user_id=?", (u["id"],)).fetchall()
    conn.close()
    for gid in gids:
        join_room(f"group_{gid['group_id']}")
    emit("presence", {"user_id": u["id"], "online": True}, broadcast=True)
    emit("presence_update", {"online_users": list(online_users)}) # Send full list to new user

@socketio.on("disconnect")
def on_disconnect():
    u = current_user()
    if u:
        if u["id"] in online_users:
            online_users.discard(u["id"])
            emit("presence", {"user_id": u["id"], "online": False}, broadcast=True)
        # Update last_seen on disconnect as well for more accuracy
        update_last_seen(u["id"])

@socketio.on("typing")
def on_typing(data):
    u = current_user()
    if not u: return
    chat_type = data.get("chat_type")
    if chat_type == "direct":
        emit("typing", {"from": u["id"], "typing": bool(data.get("typing")), "chat_type": "direct"}, room=f"user_{int(data.get('peer_id'))}")
    elif chat_type == "group":
        emit("typing", {"from": u["id"], "typing": bool(data.get("typing")), "chat_type": "group", "group_id": int(data.get('group_id'))}, room=f"group_{int(data.get('group_id'))}", include_self=False)

def _get_message(mid: int):
    conn = db()
    r = conn.execute("""
        SELECT m.*, u.display_name as sender_name, u.avatar_path as sender_avatar
        FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.id=?
    """, (mid,)).fetchone()
    if not r:
        conn.close(); return None
    msg = dict(r)
    mid = msg["id"]
    # group status
    if msg["chat_type"] == "group":
        st = conn.execute("""
            SELECT COUNT(*) total,
                   SUM(CASE WHEN delivered_at IS NOT NULL THEN 1 ELSE 0 END) delivered_count,
                   SUM(CASE WHEN read_at IS NOT NULL THEN 1 ELSE 0 END) read_count
            FROM message_status WHERE message_id=?
        """, (mid,)).fetchone()
        msg["status"] = {"delivered_count": st["delivered_count"] if st else 0,
                         "read_count": st["read_count"] if st else 0}
    
    # Efficiently fetch reply/forward previews
    preview_ids = {p_id for p_id in [msg.get("reply_to"), msg.get("forwarded_from")] if p_id}
    previews = {_p["id"]: dict(_p) for _p in conn.execute(f"SELECT m.id, m.content, m.content_type, m.file_path, m.deleted, u.display_name AS sender_name FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.id IN ({','.join('?' for _ in preview_ids)})", tuple(preview_ids))} if preview_ids else {}
    msg["reply"] = previews.get(msg.get("reply_to"))
    msg["forwarded"] = previews.get(msg.get("forwarded_from"))

    reacts = conn.execute("SELECT emoji, COUNT(*) cnt FROM reactions WHERE message_id=? GROUP BY emoji", (mid,)).fetchall()
    msg["reactions"] = [dict(x) for x in reacts]
    return msg
    conn.close() # Pastikan koneksi ditutup

def _insert_mentions(conn, message_id, chat_type, peer_id=None, group_id=None, content=""):
    # extract mentions in format @Display Name or @username (simple split)
    if not content or chat_type != "group" or not group_id:
        return []
    words = content.split()
    names = set([w[1:] for w in words if w.startswith("@") and len(w) > 1])
    if not names: return []
    # map names -> user_ids by display_name or username in that group
    q = """
      SELECT u.id, u.username, u.display_name FROM users u
      JOIN group_members gm ON gm.user_id=u.id
      WHERE gm.group_id=?
    """
    rows = conn.execute(q, (group_id,)).fetchall()
    name2id = {}
    for r in rows:
        name2id[r["username"]] = r["id"]
        # also allow display_name token without spaces (fallback: lower no-space)
        name2id[(r["display_name"] or "").replace(" ", "")] = r["id"]
    mentioned_ids = set()
    for nm in names:
        key = nm
        if key in name2id:
            mentioned_ids.add(name2id[key])
        else:
            # try no-space version
            ns = nm.replace(" ", "")
            if ns in name2id: mentioned_ids.add(name2id[ns])
    for uid in mentioned_ids:
        conn.execute("INSERT OR IGNORE INTO mentions (message_id, user_id) VALUES (?,?)", (message_id, uid))
    return list(mentioned_ids)

@socketio.on("send_message")
def on_send_message(data):
    """
    data = {
      chat_type: 'direct'|'group',
      peer_id?: int,
      group_id?: int,
      content: str,
      content_type: 'text'|'image'|'file'|'audio',
      file_path?: str,
      reply_to?: int,
      forwarded_from?: int
    }
    """
    u = current_user()
    if not u: return
    update_last_seen(u["id"])
    chat_type = data.get("chat_type")
    content = (data.get("content") or "").strip()
    content_type = (data.get("content_type") or "text").lower()
    file_path = data.get("file_path")
    reply_to = data.get("reply_to")
    forwarded_from = data.get("forwarded_from")
    created_at = now_str()

    conn = db()
    cur = conn.cursor()

    if chat_type == "direct":
        peer_id = int(data.get("peer_id"))
        cur.execute("""
            INSERT INTO messages (chat_type, sender_id, receiver_id, content, content_type, file_path, reply_to, forwarded_from, created_at)
            VALUES ('direct', ?, ?, ?, ?, ?, ?, ?, ?)
        """, (u["id"], peer_id, content, content_type, file_path, reply_to, forwarded_from, created_at))
        mid = cur.lastrowid
        cur.execute("INSERT OR IGNORE INTO message_status (message_id, user_id) VALUES (?, ?)", (mid, peer_id))
        conn.commit()
        msg = _get_message(mid)
        emit("message", msg, room=f"user_{u['id']}")
        emit("message", msg, room=f"user_{peer_id}")
        # delivered
        if peer_id in online_users:
            cur.execute("UPDATE message_status SET delivered_at=? WHERE message_id=? AND user_id=?", (now_str(), mid, peer_id))
            conn.commit()
            emit("delivered", {"message_id": mid, "to_user_id": peer_id}, room=f"user_{u['id']}")
    elif chat_type == "group":
        group_id = int(data.get("group_id"))
        cur.execute("""
            INSERT INTO messages (chat_type, sender_id, group_id, content, content_type, file_path, reply_to, forwarded_from, created_at)
            VALUES ('group', ?, ?, ?, ?, ?, ?, ?, ?)
        """, (u["id"], group_id, content, content_type, file_path, reply_to, forwarded_from, created_at))
        mid = cur.lastrowid
        members = conn.execute("SELECT user_id FROM group_members WHERE group_id=?", (group_id,)).fetchall()
        for m in members:
            if m["user_id"] != u["id"]:
                cur.execute("INSERT OR IGNORE INTO message_status (message_id, user_id) VALUES (?, ?)", (mid, m["user_id"]))
        # mentions
        mentioned_ids = _insert_mentions(conn, mid, "group", group_id=group_id, content=content)
        conn.commit()
        conn.close() # Close connection before calling _get_message
        msg = _get_message(mid)
        emit("message", msg, room=f"group_{group_id}")
        # delivery marks
        for m in members:
            if m["user_id"] != u["id"] and m["user_id"] in online_users:
                cur.execute("UPDATE message_status SET delivered_at=? WHERE message_id=? AND user_id=?", (now_str(), mid, m["user_id"]))
        cur.close()
        conn.commit()
        emit("group_status_refresh", {"message_id": mid}, room=f"group_{group_id}")
        # notify mentioned users
        for uid in mentioned_ids:
            emit("mentioned", {"message_id": mid, "by": u["id"]}, room=f"user_{uid}")

@socketio.on("mark_read")
def on_mark_read(data):
    u = current_user()
    if not u: return
    update_last_seen(u["id"])
    conn = db(); cur = conn.cursor(); now = now_str()
    if data.get("chat_type") == "direct":
        peer_id = int(data.get("peer_id"))
        mids = conn.execute("""
            SELECT id FROM messages
            WHERE chat_type='direct' AND sender_id=? AND receiver_id=? AND deleted=0
        """, (peer_id, u["id"])).fetchall()
        for m in mids:
            # Ensure message_status record exists before updating
            cur.execute("INSERT OR IGNORE INTO message_status (message_id, user_id) VALUES (?, ?)", (m["id"], u["id"]))
            cur.execute("UPDATE message_status SET read_at=? WHERE message_id=? AND user_id=?", (now, m["id"], u["id"]))
            emit("read", {"message_id": m["id"], "by_user_id": u["id"]}, room=f"user_{peer_id}")
    else:
        group_id = int(data.get("group_id"))
        mids = conn.execute("""
            SELECT id FROM messages
            WHERE chat_type='group' AND group_id=? AND sender_id!=? AND deleted=0
        """, (group_id, u["id"])).fetchall()
        for m in mids:
            # Group messages should already have status records created when sent, but ensure anyway
            cur.execute("INSERT OR IGNORE INTO message_status (message_id, user_id) VALUES (?, ?)", (m["id"], u["id"]))
            cur.execute("UPDATE message_status SET read_at=? WHERE message_id=? AND user_id=?", (now, m["id"], u["id"]))
        emit("group_status_refresh", {}, room=f"group_{group_id}")
    conn.commit(); conn.close()
    emit("read_receipt_ack", {"chat_type": data.get("chat_type")}) # Acknowledge back to sender

@socketio.on("edit_message")
def on_edit_message(data):
    u = current_user()
    if not u: return
    mid = int(data.get("message_id"))
    new_content = (data.get("new_content") or "").strip()
    conn = db()
    msg = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    if not msg or msg["sender_id"] != u["id"] or msg["deleted"] == 1:
        conn.close(); return
    conn.execute("UPDATE messages SET content=?, edited=1, updated_at=? WHERE id=?", (new_content, now_str(), mid))
    conn.commit()
    updated = _get_message(mid)
    conn.close()
    if msg["chat_type"] == "direct":
        emit("message_edited", updated, room=f"user_{msg['sender_id']}")
        emit("message_edited", updated, room=f"user_{msg['receiver_id']}")
    else:
        emit("message_edited", updated, room=f"group_{msg['group_id']}")

@socketio.on("delete_message")
def on_delete_message(data):
    u = current_user()
    if not u: return
    mid = int(data.get("message_id"))
    conn = db()
    msg = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    if not msg or msg["sender_id"] != u["id"] or msg["deleted"] == 1:
        conn.close(); return
    conn.execute("UPDATE messages SET deleted=1, updated_at=? WHERE id=?", (now_str(), mid))
    conn.commit(); conn.close()
    if msg["chat_type"] == "direct":
        emit("message_deleted", {"message_id": mid}, room=f"user_{msg['sender_id']}")
        emit("message_deleted", {"message_id": mid}, room=f"user_{msg['receiver_id']}")
    else:
        emit("message_deleted", {"message_id": mid}, room=f"group_{msg['group_id']}")

@socketio.on("pin_message")
def on_pin_message(data):
    u = current_user()
    if not u: return
    message_id = int(data.get("message_id"))
    action = data.get("action", "pin")  # "pin" or "unpin"
    # This uses the same logic as the HTTP API /api/pin_message
    conn = db()
    # Get message info
    msg = conn.execute("SELECT group_id FROM messages WHERE id=? AND chat_type='group'", (message_id,)).fetchone()
    if not msg:
        conn.close()
        return jsonify({"error": "Invalid message or not a group message"}), 400
    group_id = msg["group_id"]
    # Check if user is member of group (we allow all members for now)
    member = conn.execute("SELECT 1 FROM group_members WHERE group_id=? AND user_id=?", (group_id, u["id"])).fetchone()
    if not member:
        conn.close()
        return
    # Check if pinned
    pinned = conn.execute("SELECT id FROM pinned_messages WHERE group_id=? AND message_id=?", (group_id, message_id)).fetchone()
    if action == "pin" and not pinned:
        conn.execute("INSERT INTO pinned_messages (message_id, pinned_by, group_id, pinned_at) VALUES (?,?,?,?)",
                     (message_id, u["id"], group_id, now_str()))
        conn.commit()
    elif action == "unpin" and pinned:
        conn.execute("DELETE FROM pinned_messages WHERE id=?", (pinned["id"],))
        conn.commit()
    else:
        conn.close()
        return
    conn.close()
    # Emit to group for real-time update
    socketio.emit("message_pinned", {"message_id": message_id, "group_id": group_id, "action": action, "pinned_by": u["id"]}, room=f"group_{group_id}")

@socketio.on("react_message")
def on_react_message(data):
    u = current_user()
    if not u: return
    mid = int(data.get("message_id"))
    emoji = (data.get("emoji") or "").strip()
    if not emoji: return
    conn = db()
    existed = conn.execute("SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?", (mid, u["id"], emoji)).fetchone()
    if existed:
        conn.execute("DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?", (mid, u["id"], emoji))
    else:
        conn.execute("INSERT OR REPLACE INTO reactions (message_id, user_id, emoji, created_at) VALUES (?,?,?,?)", (mid, u["id"], emoji, now_str()))
    conn.commit()
    reacts = conn.execute("SELECT emoji, COUNT(*) cnt FROM reactions WHERE message_id=? GROUP BY emoji", (mid,)).fetchall()
    conn.close()
    emit("reactions_update", {"message_id": mid, "summary": [dict(x) for x in reacts]}, broadcast=True)

# Video Call Handlers
@socketio.on("video_call_offer")
def on_video_call_offer(data):
    global call_status
    u = current_user()
    if not u: return False
    to_user = int(data.get("to"))
    if to_user == u["id"]: return
    if call_status.get(to_user, False):
        # Send system message to caller indicating target is busy
        conn = db()
        conn.execute("""
            INSERT INTO messages (chat_type, sender_id, receiver_id, content, content_type, created_at)
            VALUES ('direct', ?, ?, ?, 'system', ?)
        """, (u["id"], to_user, f"📞 {get_display_name(to_user)} sedang dalam panggilan lain", now_str()))
        mid = conn.lastrowid
        conn.execute("INSERT OR IGNORE INTO message_status (message_id, user_id) VALUES (?, ?)", (mid, to_user))
        conn.commit()
        conn.close()
        # Emit message to caller
        msg = _get_message(mid)
        emit("message", msg, room=f"user_{u['id']}")
        return
    call_status[u["id"]] = True
    call_status[to_user] = True
    emit("video_call_offer", {"from": u["id"], "offer": data.get("offer")}, room=f"user_{to_user}")

@socketio.on("video_call_answer")
def on_video_call_answer(data):
    u = current_user()
    if not u: return
    to_user = int(data.get("to"))
    emit("video_call_answer", {"from": u["id"], "answer": data.get("answer")}, room=f"user_{to_user}")

@socketio.on("video_call_ice")
def on_video_call_ice(data):
    u = current_user()
    if not u: return
    to_user = int(data.get("to"))
    emit("video_call_ice", {"from": u["id"], "candidate": data.get("candidate")}, room=f"user_{to_user}")

@socketio.on("video_call_end")
def on_video_call_end(data):
    global call_status
    u = current_user()
    if not u: return
    try:
        to_user_id = data.get("to")
        if not to_user_id: return  # No target, silently ignore
        to_user = int(to_user_id)
        if call_status.get(u["id"]):
            del call_status[u["id"]]
        if call_status.get(to_user):
            del call_status[to_user]
        emit("video_call_end", {"from": u["id"]}, room=f"user_{to_user}")
    except (ValueError, TypeError):
        pass  # Ignore invalid data

@socketio.on("notepad_edit")
def on_notepad_edit(data):
    # Forward notepad edit to the other participant in the call
    to_user_id = data.get('to')
    if to_user_id:
        emit('notepad_edit', {'from': current_user()['id'], 'content': data.get('content')}, room=f"user_{to_user_id}")

# --------------------------
# Templates (Login + App)
# --------------------------
TPL_LOGIN = r"""
<!DOCTYPE html>
<html lang="id">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Masuk • Chat</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

    :root {
      --bg: #111b21;
      --panel: #202c33;
      --text: #e9edef;
      --text-secondary: #8696a0;
      --accent: #00a884;
      --accent-hover: #008069;
      --input-bg: #2a3942;
      --border: #374248;
      --success: #10b981;
      --error: #ef4444;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }

    .container {
      width: 100%;
      max-width: 420px;
      padding: 40px 20px;
      text-align: center;
    }

    .logo {
      margin-bottom: 32px;
    }

    .logo .icon {
      width: 80px;
      height: 80px;
      margin: 0 auto 20px;
      background: var(--accent);
      border-radius: 24px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 40px;
      color: white;
    }

    .logo h1 {
      font-size: 32px;
      font-weight: 700;
      margin: 0;
    }

    .logo p {
      color: var(--text-secondary);
      font-size: 16px;
      margin-top: 8px;
    }

    .form {
      display: none;
    }

    .form.active {
      display: block;
      animation: fadeIn 0.4s ease-out;
    }

    .form-group {
      margin-bottom: 20px;
      text-align: left;
    }

    .form-group label {
      display: block;
      margin-bottom: 8px;
      font-size: 14px;
      font-weight: 500;
      color: var(--text-secondary);
    }

    .form-group input {
      width: 100%;
      padding: 14px 18px;
      background: var(--input-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      color: var(--text);
      font-size: 16px;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      font-family: inherit;
    }

    .form-group input::placeholder {
      color: var(--text-secondary);
    }

    .form-group input:focus {
      outline: none;
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(0, 168, 132, 0.2);
    }

    .btn {
      width: 100%;
      padding: 16px 20px;
      background: var(--accent);
      border: none;
      border-radius: 12px;
      color: white;
      font-size: 16px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      margin-top: 16px;
    }

    .btn:hover {
      background: var(--accent-hover);
      transform: translateY(-2px);
      box-shadow: 0 4px 20px rgba(0, 168, 132, 0.3);
    }

    .message {
      padding: 12px 16px;
      border-radius: 8px;
      margin-bottom: 20px;
      font-size: 14px;
      text-align: center;
    }

    .message.error {
      background: rgba(239, 68, 68, 0.1);
      border: 1px solid rgba(239, 68, 68, 0.2);
      color: #fecaca;
    }

    .message.success {
      background: rgba(16, 185, 129, 0.1);
      border: 1px solid rgba(16, 185, 129, 0.2);
      color: #bbf7d0;
    }

    .form-switch {
      margin-top: 24px;
      font-size: 14px;
      color: var(--text-secondary);
    }
    .form-switch a {
      color: var(--accent);
      font-weight: 600;
      text-decoration: none;
      cursor: pointer;
    }

    @keyframes fadeIn {
      from {
        opacity: 0;
        transform: translateY(10px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

  </style>
</head>
<body>
  <div class="container">
    <div class="logo">
      <div class="icon">💬</div>
      <h1>Selamat Datang</h1>
      <p>Masuk untuk melanjutkan ke Rubycon Chats (Powered by Devin D.M)</p>
    </div>

    {% if error %}<div class="message error">{{error}}</div>{% endif %}
    {% if success %}<div class="message success">{{success}}</div>{% endif %}

    <form id="loginForm" class="form active" action="/login" method="post">
      <div class="form-group">
        <input name="username" placeholder="Username" required />
      </div>
      <div class="form-group">
        <input type="password" name="password" placeholder="Password" required />
      </div>
      <button type="submit" class="btn">Masuk</button>
      <div class="form-switch">
        Belum punya akun? <a onclick="switchForm('register')">Daftar sekarang</a>
      </div>
    </form>

    <form id="registerForm" class="form" action="/register" method="post">
      <div class="form-group">
        <input name="username" placeholder="Username (tidak bisa diubah)" required />
      </div>
      <div class="form-group">
        <input type="password" name="password" placeholder="Buat password" required />
      </div>
      <button type="submit" class="btn">Buat Akun</button>
      <div class="form-switch">
        Sudah punya akun? <a onclick="switchForm('login')">Masuk</a>
      </div>
    </form>
  </div>

  <script>
    function switchForm(formName) {
      document.getElementById('loginForm').classList.toggle('active', formName === 'login');
      document.getElementById('registerForm').classList.toggle('active', formName === 'register');
    }
  </script>

</body>
</html>
"""

TPL_CHAT = r"""
<!DOCTYPE html>
<html lang="id">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Rubycon Indonesian Chats</title>
  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/cropperjs@1.5.13/dist/cropper.min.css">
    <script src="https://cdn.jsdelivr.net/npm/cropperjs@1.5.13/dist/cropper.min.js"></script>
    <script>
      (() => {
        const theme = localStorage.getItem('chatTheme') || 'dark';
        const root = document.documentElement;
        const themes = {
          'light': {
            '--bg': '#f0f2f5', '--panel': '#f8f9fa', '--panel2': '#e3f2fd', '--card': '#ffffff',
            '--text': '#111827', '--muted': '#6b7280', '--me': '#dcfce7', '--them': '#ffffff',
            '--accent': '#2563eb', '--danger': '#e11d48', '--border': '#e5e7eb',
            '--input': '#f9fafb', '--quote': '#f3f4f6'
          },
          'midnight': { // Formerly 'blue'
            '--bg': '#0c1317', '--panel': '#101d25', '--panel2': '#182834', '--card': '#101d25',
            '--text': '#e7e9ea', '--muted': '#738796', '--me': '#0b5394', '--them': '#263949',
            '--accent': '#3b82f6', '--danger': '#f43f5e', '--border': '#2c3e50',
            '--input': '#1f2c3a', '--quote': '#1c3040'
          },
          'forest': { // Formerly 'green'
            '--bg': '#111827', '--panel': '#1f2937', '--panel2': '#374151', '--card': '#1f2937',
            '--text': '#f9fafb', '--muted': '#9ca3af', '--me': '#15803d', '--them': '#374151',
            '--accent': '#22c55e', '--danger': '#ef4444', '--border': '#4b5563',
            '--input': '#4b5563', '--quote': '#313b49'
          },
          'dark': {
            '--bg': '#0b141a', '--panel': '#111b21', '--panel2': '#202c33', '--card': '#111b21',
            '--text': '#e9edef', '--muted': '#8696a0', '--me': '#005c4b', '--them': '#202c33', // WhatsApp's dark green
            '--accent': '#25d366', '--danger': '#ef4444', '--border': '#222e35', // WhatsApp's bright green accent
            '--input': '#2a3942', '--quote': '#182229',
          }
        };
        for (let prop in themes[theme]) {
          root.style.setProperty(prop, themes[theme][prop]);
        }
        root.style.setProperty('--chip', '#334155');
        root.style.setProperty('--chipText', '#e2e8f0');
      })();
    </script>
  <style>
    :root {
      --bg: #0b141a; --panel: #111b21; --panel2: #202c33; --card: #111b21;
      --text: #e9edef; --muted: #8696a0; --me: #005c4b; --them: #202c33;
      --accent: #00a884; --danger: #ef4444; --border: #222e35; --input: #2a3942; --quote: #182229;
      --chip: #334155; --chipText: #e2e8f0;
    }
    *{box-sizing:border-box} body{margin:0; font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,"Helvetica Neue",Arial,"Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji","Android Emoji"; background:var(--bg); color:var(--text); font-size: 15px;} /* Add Android Emoji for broader support */
    .app{display:flex; height:100vh;}

    .left{width:360px; background:var(--panel); border-right:1px solid var(--border); display:flex; flex-direction:column;}
    .topbar{padding:10px 16px; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; background:var(--panel2); color:var(--text);}
    .topbar .user-info-row{display:flex; align-items:center; gap:12px; min-width:0; cursor:pointer; position: relative;}
    .topbar .actions-right{display:flex; align-items:center; gap:8px;}
    .icon-btn{background:none; border:none; color:var(--muted); font-size:22px; width:40px; height:40px; border-radius:50%; cursor:pointer; display:flex; align-items:center; justify-content:center; transition:all 0.2s ease;}
    .icon-btn:hover{background:var(--them); transform: scale(1.05);}
    .list{overflow-y:auto; padding:8px 0; overflow-x: hidden; scrollbar-width: thin; scrollbar-color: var(--them) var(--bg);}
    .list::-webkit-scrollbar { width: 8px; }
    .list::-webkit-scrollbar-track { background: transparent; }
    .list::-webkit-scrollbar-thumb { background-color: var(--them); border-radius: 10px; }
    .dropdown-menu{position:absolute; top:calc(100% + 4px); right:0; background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:6px; z-index:100; box-shadow:0 4px 12px rgba(0,0,0,.3); min-width:180px; display:none;}
    .dropdown-menu.show{display:block;} 
    .dropdown-item{display:block; width:100%; padding:8px 12px; color:var(--text); border:none; background:none; text-align:left; cursor:pointer; border-radius:4px; font-size:14px; transition:all 0.2s ease;}
    .dropdown-item:hover{background:var(--them); transform: scale(1.02);}
    .topbar .me{font-weight:700; color:var(--text);}
    .search{padding:12px; position:relative;} .search input{width:100%; padding:10px 14px; background:var(--input); color:var(--text); border:1px solid var(--border); border-radius:8px; outline:none; transition:all 0.2s;}
    .search input:focus { border-color:var(--accent); background: #475569; }
    .search-results{position:absolute; top:100%; left:0; right:0; background:var(--panel2); border:1px solid var(--border); border-radius:8px; max-height:300px; overflow-y:auto; display:none; z-index:100; box-shadow:0 4px 12px rgba(0,0,0,0.4);}
    .search-item, .pending-item { background:var(--panel); border-radius:8px; margin:4px 8px; transition:all 0.2s ease; }
    .search-item:hover, .pending-item:hover { background:var(--them) !important; transform: scale(1.02); }    
    .list{overflow-y:auto; padding:8px 0; overflow-x: hidden;}
    .section{padding:8px 16px; font-size:12px; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:0.5px;}
    .item{display:flex; gap:12px; padding:12px 16px; cursor:pointer; align-items:center; transition:all 0.2s ease;}
    .item:hover{background:var(--them); transform: scale(1.01);}
    .item.active{background:var(--them); color:var(--text);}
    .item .avatar { width:48px; height:48px; border-radius:50%; overflow:hidden; font-size:20px; display:flex; align-items:center; justify-content:center; background:var(--accent); color:white; font-weight:bold; flex-shrink:0;}
    .item .avatar-container { position: relative; }
    .online-indicator {
        position: absolute; bottom: 2px; right: 2px; width: 12px; height: 12px;
        background-color: #22c55e; border-radius: 50%; border: 2px solid var(--panel);
    }
    .avatar-display {
        display: flex; align-items: center; justify-content: center;
    }
    .item .avatar img, .item .avatar span { width:100%; height:100%; border-radius:50%; object-fit:cover; display:flex; align-items:center; justify-content:center; }
    .unread-badge { background:var(--accent); color:white; border-radius:12px; padding:3px 8px; font-size:11px; font-weight:600; min-width:20px; text-align:center; }
    .unread-dot { background:var(--accent); width:10px; height:10px; border-radius:50%; margin-left:auto; }
    #meAvatar { width:48px; height:48px; font-size:20px; }
    .actions-left{display:flex; gap:10px;}
    .btn{background:var(--accent); color:#fff; border:none; padding:10px 16px; border-radius:8px; cursor:pointer; font-weight:600; transition:all 0.2s;}
    .btn:hover{filter: brightness(1.1);}
    .btn.sec{background:var(--input); color:var(--text); border:1px solid var(--border); font-weight: 500;}
    .btn.sec:hover { background:var(--them); }

    /* Cropper Styles */
    .crop-modal {
      position: fixed; inset: 0; background: rgba(0,0,0,0.8); display: none; align-items: center; justify-content: center; z-index: 2000;
    }
    .crop-modal.show {
      display: flex;
    }
    .crop-container { background: var(--panel); padding: 20px; border-radius: 8px; max-width: 90vw; max-height: 90vh; overflow: auto; }
    .crop-image { max-width: 100%; max-height: 500px; }
    .crop-controls { display: flex; gap: 10px; margin-top: 10px; }

    .right{flex:1; display:flex; flex-direction:column;}
    .chatbar{padding:10px 16px; border-bottom:1px solid var(--border); display:flex; align-items:center; gap:12px; background:var(--panel2);}
    .chatbar .avatar.avatar-display {
        width:40px; height:40px; font-size:18px; display:none;
    }
    .chatbar .title{font-weight:700; cursor:pointer;}
    .chatbar .sub{font-size:12px; color:var(--muted);}
    .chatbar .row{display:flex; align-items:center; gap:8px;}

    .messages{flex:1; background:var(--bg); padding:20px; overflow-y:auto; display:flex; flex-direction:column; gap:12px; scrollbar-width: thin; scrollbar-color: var(--them) var(--bg);}
    .messages::-webkit-scrollbar {
      width: 8px;
    }
    .messages::-webkit-scrollbar-track {
      background: transparent;
    }
    .messages::-webkit-scrollbar-thumb {
      background-color: var(--them);
      border-radius: 10px;
    }
    .typing-indicator {
      padding: 12px 16px;
      color: var(--muted);
      font-style: italic;
      display: none;
      align-self: flex-start;
      max-width: 70%;
    }
    .bubble{max-width:70%; padding:8px 12px; border-radius:8px; position:relative; line-height:1.5; overflow-wrap: break-word;}
    .meb{align-self:flex-end; background:var(--me);} .theb{align-self:flex-start; background:var(--them);}
    .bubble.with-avatar .theb{background:#2a3942; border-radius:12px 12px 12px 4px;}
    .bubble.showing-emoji-bar{margin-bottom: 50px;}
    .sender-avatar{width:24px; height:24px; border-radius:50%; overflow:hidden; margin-right:8px; flex-shrink:0;}
    .sender-avatar img, .sender-avatar span{width:100%; height:100%; border-radius:50%; object-fit:cover; display:flex; align-items:center; justify-content:center; background:#54656f;}
    .bubble .meta{color:var(--muted); font-size:11px; display:flex; gap:6px; align-items:center; justify-content:flex-end; margin-top:4px; flex-wrap:wrap;}
    .deleted{font-style:italic; color:var(--muted);} .edited{font-size:11px; color:#cbd5e1; margin-left:6px;}
    .quote{background:var(--quote); border-left:3px solid var(--accent); padding:8px 10px; border-radius:8px; margin-bottom:8px; font-size:13px; color:var(--text);}
    .quote .qname{font-weight:600; color:var(--accent);} .quote img{max-width:120px; border-radius:6px; display:block; margin-top:4px;}
    .unread{ text-align:center; color:var(--muted); font-size:12px; padding:6px 0; }
    .reacts{display:flex; gap:6px; margin-left:auto;} .react-chip{background:var(--chip); color:var(--chipText); padding:3px 8px; border-radius:999px; font-size:11px;}
    .mention{ background:var(--accent); color:white; padding:1px 4px; border-radius:4px; font-weight:500;}
    .emoji-click{ cursor:pointer; transition:background 0.2s; }
    .emoji-click:hover{ background:rgba(255,255,255,.1); border-radius:2px; }

    .composer{padding:16px 20px; display:flex; gap:12px; align-items:center; background:var(--panel2); border-top:1px solid var(--border); position:relative;}
    .attach-menu{display:flex; flex-direction:column; gap:6px;}
    .composer textarea{flex:1; padding:12px 18px; background:var(--input); color:var(--text); border:1px solid var(--border); border-radius:8px; outline:none; font-size:15px; font-family:inherit; transition:all 0.2s; resize: none; overflow:hidden; min-height:32px; box-sizing:border-box;}
    .composer textarea:focus{border-color:var(--accent); background: #475569;}
    .input-wrapper { flex:1; position:relative; }
    .input-wrapper #msgBox { width:100%; }
    .composer textarea{ padding-right: 60px; } /* Add padding for the button */
    .composer textarea::placeholder{color:var(--muted);}
    .emoji{position:relative;} 
    .emoji-panel{
      position:absolute; 
      bottom:calc(100% + 15px); 
      right:-50px; 
      z-index:1000; 
      background:linear-gradient(145deg, var(--panel) 0%, rgba(17,27,33,0.98) 100%); 
      border:1px solid var(--accent); 
      border-radius:20px; 
      display:none; 
      width:380px; 
      height:420px;
      box-shadow:0 15px 50px rgba(0,0,0,0.5), 0 0 30px rgba(0,168,132,0.15), inset 0 1px 0 rgba(255,255,255,0.1); 
      backdrop-filter:blur(25px);
      overflow:hidden;
      flex-direction:column;
    }
    .emoji-panel-header{
      padding:16px 20px;
      background:linear-gradient(135deg, rgba(0,168,132,0.1) 0%, rgba(0,168,132,0.05) 100%);
      border-bottom:1px solid rgba(255,255,255,0.1);
    }
    .emoji-panel-title{
      font-size:18px;
      font-weight:700;
      color:var(--text);
      margin-bottom:12px;
      display:flex;
      align-items:center;
      gap:8px;
    }
    .emoji-search{
      width:100%;
      padding:10px 16px;
      background:var(--input);
      border:1px solid var(--border);
      border-radius:12px;
      color:var(--text);
      font-size:14px;
      outline:none;
      transition:all 0.3s ease;
    }
    .emoji-search:focus{
      border-color:var(--accent);
      box-shadow:0 0 0 3px rgba(0,168,132,0.2);
    }
    .emoji-tabs-container{
      display:flex;
      padding:8px 16px;
      gap:4px;
      background:rgba(0,0,0,0.2);
      border-bottom:1px solid rgba(255,255,255,0.05);
      overflow-x:auto;
    }
    .emoji-tab-btn{
      background:var(--input);
      border:1px solid var(--border);
      color:var(--muted);
      padding:8px 12px;
      border-radius:10px;
      cursor:pointer;
      font-size:16px;
      transition:all 0.3s ease;
      min-width:44px;
      text-align:center;
    }
    .emoji-tab-btn:hover{
      background:var(--them);
      transform:translateY(-2px);
    }
    .emoji-tab-btn.active{
      background:var(--accent);
      color:white;
      border-color:var(--accent);
      box-shadow:0 4px 12px rgba(0,168,132,0.3);
    }
    .emoji-content{
      flex:1;
      overflow-y:auto;
      padding:16px;
    }
    .emoji-grid{
      display:grid;
      grid-template-columns:repeat(8, 1fr);
      gap:6px;
    }
    .emoji-grid.hidden{
      display:none;
    }
    .emoji-item{
      background:transparent;
      border:none;
      color:#fff;
      padding:8px;
      border-radius:10px;
      cursor:pointer;
      font-size:28px;
      transition:all 0.2s ease;
      display:flex;
      align-items:center;
      justify-content:center;
      aspect-ratio:1;
    }
    .emoji-item:hover{
      background:var(--accent);
      transform:scale(1.2);
      box-shadow:0 4px 15px rgba(0,168,132,0.4);
    }
    .emoji-item:active{
      transform:scale(0.95);
    }
    .emoji-footer{
      padding:12px 20px;
      background:rgba(0,0,0,0.2);
      border-top:1px solid rgba(255,255,255,0.05);
      display:flex;
      justify-content:space-between;
      align-items:center;
    }
    .emoji-footer-text{
      font-size:12px;
      color:var(--muted);
    }
    .emoji-recent-title{
      font-size:14px;
      font-weight:600;
      color:var(--accent);
      margin-bottom:12px;
      display:flex;
      align-items:center;
      gap:6px;
    }
    .emoji-panel {
        scrollbar-width: thin;
        scrollbar-color: var(--accent) rgba(0,0,0,0.2);
    }
    .emoji-panel::-webkit-scrollbar { width: 6px; }
    .emoji-panel::-webkit-scrollbar-track { background: rgba(0,0,0,0.2); border-radius: 3px; }
    .emoji-panel::-webkit-scrollbar-thumb {
        background: linear-gradient(180deg, var(--accent), rgba(0,168,132,0.7)); 
        border-radius: 3px;
    }
    .emoji-panel::-webkit-scrollbar-thumb:hover {
        background: linear-gradient(180deg, #00c896, var(--accent));
    }
    .emoji.open .emoji-panel{display:block;}
    .composer .btn{background:var(--accent); color:#fff; padding:12px; border-radius:50%; width:48px; height:48px; display:flex; align-items:center; justify-content:center;}
    .composer .btn:hover{filter:brightness(1.1);}
    .composer .btn.sec{background:var(--input); color:var(--text); border:1px solid var(--border);}
    .composer .btn.sec:hover{background:var(--them);}
    .mic{position:relative;}

    .actions{position:absolute; top:30px; right:8px; background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:6px; display:flex; flex-direction:column; gap:4px; font-size:13px; z-index:50; box-shadow:0 4px 12px rgba(0,0,0,.4); display:none;}
    .act-btn{background:none; border:none; color:#cbd5e1; cursor:pointer; font-size:12px; padding:4px 8px; text-align:left; border-radius:4px;} .act-btn:hover{background:#0f1a20; color:#fff;}
    .menu-btn{position:absolute; top:6px; right:6px; background:rgba(0,0,0,0.2); border:none; border-radius:50%; color:var(--muted); cursor:pointer; font-size:16px; width:24px; height:24px; display:flex; align-items:center; justify-content:center; opacity:0; transition:opacity 0.2s; z-index:50;}
    .bubble:hover .menu-btn{opacity:1;}
    .menu-btn:hover{color:#fff; background:rgba(0,0,0,0.4);}
    .emoji-bar{position:absolute; bottom:-28px; right:8px; display:flex; gap:6px; z-index:50;}
    .emoji-bar button{background:#24434a; border:none; color:#fff; padding:4px 6px; border-radius:999px; cursor:pointer;}

    /* Edit Message Styles */
    .editing { animation: pulseEdit 0.6s ease-out; }
    .edit-textarea {
        width: 100%;
        background: var(--bg);
        color: var(--text);
        border: 2px solid var(--accent);
        border-radius: 12px;
        padding: 12px 16px;
        font-family: inherit;
        font-size: inherit;
        min-height: 80px;
        resize: vertical;
        outline: none;
        box-shadow: 0 4px 12px rgba(0, 168, 132, 0.15);
        transition: all 0.3s ease;
        line-height: 1.6;
    }
    .edit-textarea:focus {
        border-color: var(--accent);
        box-shadow: 0 4px 16px rgba(0, 168, 132, 0.25);
        background: var(--panel);
    }
    .edit-actions {
        display: flex;
        gap: 12px;
        margin-top: 12px;
        justify-content: flex-end;
        align-items: center;
    }
    .edit-actions button {
        border-radius: 20px;
        font-size: 12px;
        font-weight: 600;
        padding: 8px 16px;
        transition: all 0.3s ease;
        cursor: pointer;
        border: none;
        outline: none;
        position: relative;
        overflow: hidden;
    }
    .edit-actions .btn {
        background: linear-gradient(135deg, var(--accent), adjust-color($accent, $saturation: -50%));
        color: white;
        box-shadow: 0 2px 8px rgba(0, 168, 132, 0.3);
    }
    .edit-actions .btn:hover {
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(0, 168, 132, 0.4);
    }
    .edit-actions .btn.sec {
        background: var(--input);
        color: var(--mute);
        border: 1px solid var(--border);
    }
    .edit-actions .btn.sec:hover {
        background: var(--them);
        color: var(--text);
    }
    @keyframes pulseEdit {
        0% { transform: scale(0.98); opacity: 0.9; }
        50% { transform: scale(1.02); opacity: 1; }
        100% { transform: scale(1); opacity: 1; }
    }
    .edit-mode-icon {
        display: inline-block;
        margin-right: 6px;
        font-size: 14px;
    }

    #replyBar {
        background: var(--input);
        padding: 8px 12px;
        border-radius: 8px;
        margin-bottom: 8px;
        border-left: 4px solid var(--accent);
        display: flex;
        justify-content: space-between;
        align-items: center;
        animation: fadeIn 0.2s ease-out;
    }

    #editBar {
        background: var(--input);
        padding: 8px 12px;
        border-radius: 8px;
        margin-bottom: 8px;
        border-left: 4px solid #f59e0b; /* Orange color for edit mode */
        display: flex;
        justify-content: space-between;
        align-items: center;
        animation: fadeIn 0.2s ease-out;
    }


    .modal{ position:fixed; inset:0; background:rgba(0,0,0,.45); display:none; align-items:center; justify-content:center; }
    .modal.show{ display:flex; backdrop-filter: blur(5px); -webkit-backdrop-filter: blur(5px); }
    .dialog{ background:var(--panel); border:1px solid var(--border); border-radius:12px; width:520px; max-width:calc(100% - 24px); max-height:80vh; overflow-y:auto; padding:24px; transform: scale(0.8); opacity: 0; transition: transform 0.3s ease, opacity 0.3s ease; }
    .modal.show .dialog{ transform: scale(1); opacity: 1; }
    .dialog h3{ margin:0 0 8px; }
    .dialog label{ display:block; font-size:13px; color:var(--muted); margin:10px 0 6px; }
    .dialog input, .dialog select{ width:100%; padding:10px 12px; background:var(--input); color:var(--text); border:none; border-radius:10px; outline:none; }
    .drop-overlay{ position:fixed; inset:0; background:rgba(0,0,0,.35); display:none; align-items:center; justify-content:center; font-size:20px; color:#fff; }
    .drop-overlay.active{ display:flex; }

    .suggest{ position:absolute; bottom:100%; left:0; background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:6px; display:none; max-height:200px; overflow:auto; width:240px; }
    .suggest .sitem{ padding:6px; cursor:pointer; } .suggest .sitem:hover{ background:#173039; }
    .chip-btn{ background:var(--input); color:var(--text); border:1px solid var(--border); padding:8px 12px; border-radius:8px; cursor:pointer; font-size:13px;}

    #emoji-overlay {
      display: none; position: fixed; inset: 0; z-index: 999;
      background: rgba(0,0,0,0.1);
    }

  @keyframes settingsSlideIn {
    from {
      transform: translateY(-20px) scale(0.95);
      opacity: 0;
    }
    to {
      transform: translateY(0) scale(1);
      opacity: 1;
    }
  }

  /* Settings Styles */
    .settings-modal .modal.show .dialog {
      transform: none !important;
      opacity: 1 !important;
      border-radius: 0 !important;
      margin: 0 !important;
      width: 100vw !important;
      height: 100vh !important;
      max-width: 100vw !important;
      background: linear-gradient(135deg, var(--bg) 0%, var(--panel) 100%) !important;
      animation: settingsSlideIn 0.4s cubic-bezier(0.34, 1.56, 0.64, 1) !important;
    }
    .settings-nav { display:flex; flex-direction:column; gap:4px; }
    .settings-tab{ padding:10px 16px; background:none; border:none; color:var(--muted); text-align:left; cursor:pointer; border-radius:8px; font-size:14px; font-weight:500; transition:all 0.2s;}
    .settings-tab.active{ background:var(--input); color:var(--text);}
    .settings-tab:hover{ background:var(--input); color:var(--text);}
    .settings-tab:hover{ background:var(--them); transform: scale(1.02); } /* This was a mistake, it overwrote the panel style */
    .settings-tab-enhanced {
      border-left: 4px solid transparent;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .settings-tab-enhanced:hover {
      background: rgba(var(--accent), 0.05);
      border-left-color: rgba(var(--accent), 0.3);
      transform: translateX(4px);
    }
    .settings-tab-enhanced.active {
      background: linear-gradient(135deg, rgba(var(--accent), 0.1), rgba(var(--accent), 0.05));
      border-left-color: var(--accent);
      color: var(--text);
      .tab-indicator {
        height: 100% !important;
      }
    }
    .settings-panel{ display:none; animation: slideInRight 0.4s cubic-bezier(0.34, 1.56, 0.64, 1); }
    .settings-panel.active{ display:block;}
    .settings-container {
      padding: 24px;
      max-height: calc(100vh - 120px);
      overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: var(--them) transparent;
    }
    .settings-container::-webkit-scrollbar { width: 6px; }
    .settings-container::-webkit-scrollbar-track { background: transparent; }
    .settings-container::-webkit-scrollbar-thumb { background: var(--them); border-radius: 3px; }
    .settings-header .settings-close-btn:hover {
      background: rgba(var(--danger), 0.2) !important;
      color: var(--danger) !important;
      transform: scale(1.05);
    }
    .settings-content {
      display: flex;
      overflow: hidden;
    }
    .settings-sidebar {
      animation: slideInLeft 0.4s cubic-bezier(0.34, 1.56, 0.64, 1);
    }
    h4 {
      font-size: 15px;
      font-weight: 600;
      margin-bottom: 16px;
    }
    .field-group { margin-bottom: 16px; }
    .field-group label { display: block; font-size: 12px; color: var(--text); margin-bottom: 6px; font-weight: 500; }
    .field-group input, .field-group textarea { width: 100%; padding: 10px 12px; background: var(--input); color: var(--text); border: 1px solid var(--border); border-radius: 6px; font-size: 12px; font-family: inherit; }
    .field-group input:focus, .field-group textarea:focus { outline: none; border-color: var(--accent); }

    .theme-options{ display:grid; grid-template-columns:repeat(2,1fr); gap:16px; margin-top:16px;}
    .theme-card{ background:#2a3942; border-radius:12px; padding:20px; cursor:pointer; border:2px solid transparent; transition:border-color 0.2s;}
    .theme-card:hover{ border-color:#25d366;}
    .theme-card.active{ border-color:#25d366; box-shadow:0 0 0 1px #25d366;}
    .theme-preview{ width:60px; height:40px; border-radius:8px; margin-bottom:12px;}
    .theme-name{ font-size:16px; color:var(--text); text-align:center;}

    .switch{ position:relative; display:inline-block; width:50px; height:24px;}
    .switch input{ opacity:0; width:0; height:0;}
    .slider{ position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0; background:#3a4952; transition:.3s; border-radius:24px;}
    .slider:before{ position:absolute; content:""; height:18px; width:18px; left:3px; bottom:3px; background:#fff; transition:.3s; border-radius:50%;}
    input:checked + .slider{ background:#25d366;}
    input:checked + .slider:before{ transform:translateX(26px);}

    .group-menu{position:relative; display:inline-block;}
    .group-menu .dropdown-menu{display:none; position:absolute; top:100%; right:0; background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:6px 0; z-index:1000; box-shadow:0 4px 12px rgba(0,0,0,.3); min-width:120px;}
    .group-menu .dropdown-menu.show{display:block;}
    .group-menu .dropdown-item{display:block; width:100%; padding:8px 12px; color:var(--text); border:none; background:none; text-align:left; cursor:pointer;}
    .group-menu .dropdown-item:hover{background:var(--accent);}
    .group-member-avatar{width:24px; height:24px;}

    .group-step{display:none;}
    .group-step.active{display:block;}

    #groupAvatarContainer { position: relative; cursor: pointer; }
    #groupAvatarContainer .overlay {
        position: absolute; inset: 0; background: rgba(0,0,0,0.5);
        color: white; display: flex; align-items: center; justify-content: center;
        font-size: 24px; border-radius: 50%; opacity: 0; transition: opacity 0.2s;
    }
    #groupAvatarContainer:hover .overlay { opacity: 1; }

    .toast-container { position:fixed; top:20px; right:20px; z-index:2000; display:flex; flex-direction:column; gap:10px; }
    #pendingRequestsList::-webkit-scrollbar,
    #starList::-webkit-scrollbar,
    #seenList::-webkit-scrollbar,
    #forwardConvList::-webkit-scrollbar {
      width: 8px;
    }
    #pendingRequestsList::-webkit-scrollbar-track,
    #starList::-webkit-scrollbar-track,
    #seenList::-webkit-scrollbar-track,
    #forwardConvList::-webkit-scrollbar-track {
      background: transparent;
    }
    #pendingRequestsList::-webkit-scrollbar-thumb,
    #starList::-webkit-scrollbar-thumb,
    #seenList::-webkit-scrollbar-thumb,
    #forwardConvList::-webkit-scrollbar-thumb,
    #addMembersUserList::-webkit-scrollbar-thumb,
    #removeMembersUserList::-webkit-scrollbar-thumb,
    #groupInfoMembersList::-webkit-scrollbar-thumb {
      background-color: var(--them);
      border-radius: 10px;
    }

    /* Member lists for better scrolling */
    #addMembersUserList,
    #removeMembersUserList,
    #groupInfoMembersList {
      max-height: 400px;
      overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: var(--them) transparent;
    }

    #addMembersUserList::-webkit-scrollbar,
    #removeMembersUserList::-webkit-scrollbar,
    #groupInfoMembersList::-webkit-scrollbar {
      width: 8px;
    }

    #addMembersUserList::-webkit-scrollbar-thumb,
    #removeMembersUserList::-webkit-scrollbar-thumb,
    #groupInfoMembersList::-webkit-scrollbar-thumb {
      background-color: var(--them);
      border-radius: 10px;
    }

    .toast {
        background: var(--panel); color: var(--text); padding: 15px 20px;
        border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.4);
        border-left: 4px solid var(--accent); display: flex;
        align-items: center; gap: 15px;
        animation: slideInRight 0.4s ease-out, fadeOut 0.5s ease-in 4.5s forwards;
    }
    @keyframes slideInRight { from { transform: translateX(110%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
    @keyframes fadeOut { from { opacity: 1; } to { opacity: 0; } }
    @keyframes ripple {
      from { transform: scale(0); opacity: 1; }
      to { transform: scale(4); opacity: 0; }
    }
    @keyframes slideInLeft { from { transform: translateX(-100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
    @keyframes slideInRight { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
    @keyframes shake {
      0%, 100% { transform: translateX(0); }
      10%, 30%, 50%, 70%, 90% { transform: translateX(-5px); }
      20%, 40%, 60%, 80% { transform: translateX(5px); }
    }
    .shake {
      animation: shake 0.5s ease-in-out;
    }


  </style>
</head>
<body>
  <div class="app">
    <div class="left">
      <div class="topbar">
        <div class="user-info-row" id="myProfileBtn">
          <div class="avatar" id="meAvatar"><span>{{ (user.display_name or user.username or '?')[0] }}</span></div>
          <div style="flex: 1; min-width: 0;">
            <div class="me" style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{{user.display_name or user.username}}</div>
            <div style="color:var(--muted); font-size:12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">@{{user.username}}</div>
          </div>
        </div>
        <div class="actions-right">
          <button class="icon-btn" id="btnNewGroup" title="Grup Baru">➕</button>
          <button class="icon-btn" id="btnSettings" title="Pengaturan & Pesan Berbintang">⚙️</button>
          <button class="icon-btn" onclick="showModal('logoutModal', true)" title="Keluar">🔓</button>
        </div>
      </div>
      <div class="search" style="border-bottom: 1px solid var(--border); padding: 8px 16px;">
        <div style="position:relative; display:flex; align-items:center; gap:8px;">
            <input id="searchBox" placeholder="🔍 Cari atau mulai chat baru" style="padding: 10px 16px; width:100%;"/>
            <button id="searchCancelBtn" class="icon-btn" style="display:none; font-size:18px; position:absolute; right:8px; top:50%; transform:translateY(-50%);">✕</button>
        </div>
      </div>
      <div id="leftPanelContent" style="flex:1; overflow-y:auto; position:relative;">
        <div class="list" id="convList" style="position:absolute; inset:0; overflow-y:auto;"></div>
        <div class="search-results" id="searchResults" style="position:absolute; inset:0; background:var(--panel); overflow-y:auto; z-index:5; display:none;"></div>
      </div>
    </div>

    <div class="right">
      <div class="chatbar">
        <div class="avatar avatar-display" id="chatAvatar"></div>
        <div style="flex:1;">
            <div class="title" id="chatTitle">Pilih percakapan</div>
            <div class="sub" id="chatSub"></div>
        </div>
        <div class="row">
          <button class="chip-btn" id="btnPin">📌 Pin</button>
          <button class="chip-btn" id="btnVideoCallTop" title="Video Call">📹</button>
          <div class="group-menu" id="groupMenuContainer" style="display:none;">
            <button class="chip-btn" id="btnGroupMenu">⋯</button>
            <div class="dropdown-menu">
              <button class="dropdown-item" id="searchInGroup">🔍 Search in This Group</button>
              <button class="dropdown-item" id="groupInfoBtn">ℹ️ Group Info</button>
              <hr style="border-top: 1px solid var(--border); margin: 4px 0;">
              <button class="dropdown-item" id="btnLeaveGroupFromMenu" style="color: var(--danger); display:none;">🚪 Keluar dari Grup</button>
              <button class="dropdown-item" id="btnDeleteGroupFromMenu" style="color: var(--danger); display:none;">🗑️ Hapus Grup</button>
            </div>
          </div>
        </div>
      </div>

      <div class="messages" id="messages">
        <div class="section">Tidak ada percakapan dipilih.</div>
        <div id="typingIndicator" style="display:none;"></div>
      </div>

      <div class="composer" style="flex-direction:column; align-items:stretch;">
        <div id="replyBar" style="display: none;">
            <!-- Reply content will be injected here by JS -->
        </div>
        <div id="editBar" style="display: none;">
            <!-- Edit mode content will be injected here by JS -->
        </div>

        <div style="display:flex; gap:12px; align-items:center;">
        <div id="attachBox" style="position:relative; display:flex; gap:8px;">
          <button class="btn sec" id="btnAttach">📎</button>
          <div id="attachMenu" class="attach-menu" style="position:absolute; bottom:100%; margin-bottom:8px; background:var(--panel); border:1px solid var(--border); border-radius:8px; display:none; padding:8px; box-shadow: 0 4px 12px rgba(0,0,0,0.3);">
          <button id="btnDoc" class="act-btn">📎 Dokumen</button>
          <button id="btnCam" class="act-btn">📸 Kamera</button>
          <button id="btnSticker" class="act-btn">🎡 Buat Stiker</button>
          </div>
        </div>

        <div class="input-wrapper" style="flex:1; position:relative;">
          <textarea id="msgBox" placeholder="Tulis pesan… (@mention anggota grup, bisa paste gambar / drag & drop file)" disabled rows="1" style="resize: none; overflow:hidden;"></textarea>
          <div id="filePreview"></div>
          <div class="sticker-btn" id="stickerBox" style="position:absolute; right:48px; top:50%; transform:translateY(-50%);">
            <button class="btn sec" id="btnStickerMain" style="background:none; border:none; font-size:20px; padding:4px;" title="Stiker">🎨</button>
          </div>
          <div class="emoji" id="emojiBox" style="position:absolute; right:12px; top:50%; transform:translateY(-50%);">
            <button class="btn sec" id="btnEmoji" style="background:none; border:none; font-size:20px; padding:4px;">😊</button>
            <div class="emoji-panel" id="emojiPanel">
              <div class="emoji-panel-header">
                <div class="emoji-panel-title">😊 Emoji</div>
                <input type="text" class="emoji-search" id="emojiSearch" placeholder="Cari emoji..." />
              </div>
              <div class="emoji-tabs-container" id="emojiTabsContainer">
                <!-- Category tabs will be generated by JS -->
              </div>
              <div class="emoji-content" id="emojiContent">
                <!-- Emoji grids will be generated by JS -->
              </div>
              <div class="emoji-footer">
                <div class="emoji-footer-text">Klik emoji untuk menambahkan</div>
              </div>
            </div>
          </div>
        </div>

        <div class="mic" id="micBox">
          <button class="btn sec" id="btnRec">🎤</button>
        </div>
        <button class="btn" id="btnSend" disabled>➤</button>
        </div>
      </div>

    </div>
  </div>

    <!-- Modal Logout Confirmation -->
    <div class="modal" id="logoutModal">
      <div class="dialog" style="width:400px; text-align:center;">
          <h3 style="margin-bottom: 16px;">Konfirmasi Logout</h3>
          <p style="color: var(--muted); font-size: 15px; margin-bottom: 24px;">
              Apakah Anda yakin ingin keluar dari sesi ini?
          </p>
          <div class="row" style="justify-content:center; gap:12px;">
              <button type="button" class="btn sec" onclick="showModal('logoutModal', false)" style="flex:1;">Batal</button>
              <button type="button" class="btn" onclick="window.location.href = '/logout';" style="flex:1; background: var(--danger);">Ya, Logout</button>
          </div>
      </div>
    </div>


  <!-- Modal Grup -->
  <div class="modal" id="groupModal">
    <div class="dialog" style="width:480px; max-width:90vw;">
      <!-- Step 1: Group Info -->
      <div id="groupStep1" class="group-step active">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:24px;">
          <h3 style="margin:0; font-size:20px; font-weight:700;">Grup Baru</h3>
          <button class="btn sec" id="grpCancel" style="padding:6px 10px; font-size:14px;">✕</button>
        </div>
        <div style="display:flex; align-items:center; gap:20px; margin-bottom:24px;">
          <label for="groupAvatarInput" id="groupAvatarContainer">
            <div id="groupAvatarPreview" class="avatar" style="width:80px; height:80px; font-size:32px; background:var(--input); color:var(--muted); border-radius:50%; display:flex; align-items:center; justify-content:center;">
              <div class="overlay">📷</div>
            </div>
          </label>
          <input type="file" id="groupAvatarInput" accept="image/*" style="display:none;">
          <input id="groupName" placeholder="Nama grup..." style="flex:1; padding:12px; border:none; border-bottom: 2px solid var(--border); background:transparent; color:var(--text); font-size:16px; outline:none;">
        </div>
        <div style="text-align:right;">
          <button class="btn" id="btnNextStep">Berikutnya →</button>
        </div>
      </div>

      <!-- Step 2: Add Members -->
      <div id="groupStep2" class="group-step">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
          <button class="btn sec" id="btnBackStep" style="padding:6px 10px; font-size:14px;">← Kembali</button>
          <h3 style="margin:0; font-size:20px; font-weight:700;">Tambah Anggota</h3>
          <button class="btn sec" id="grpCancel2" style="padding:6px 10px; font-size:14px;">✕</button>
        </div>
        <div id="selectedMembersPreview" style="display:flex; gap:8px; flex-wrap:wrap; padding:8px; border-bottom:1px solid var(--border); min-height:40px;"></div>
        <input id="memberSearch" placeholder="Cari kontak untuk ditambahkan..." style="width:100%; padding:10px; margin:12px 0; border:1px solid var(--border); border-radius:8px; background:var(--input); color:var(--text);">
        <div id="groupMembersList" style="max-height:250px; overflow-y:auto;"></div>
        <div style="text-align:right; margin-top:24px;">
          <button class="btn" id="grpCreate" style="padding:12px 24px; font-size:14px;">✓ Buat Grup</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Modal Starred -->
  <div class="modal" id="starModal">
    <div class="dialog">
      <h3>Pesan Berbintang</h3>
      <div id="starList" style="max-height:360px; overflow:auto;"></div>
      <div class="row" style="justify-content:flex-end; margin-top:12px;">
        <button class="btn" id="starClose">Tutup</button>
      </div>
    </div>
  </div>

  <!-- Modal Group Admin -->
  <div class="modal" id="adminModal">
    <div class="dialog">
      <h3>Kelola Grup</h3>
      <label>Nama Grup</label><input id="adminGrpName" />
      <label>Tambah Anggota</label><select multiple id="adminAddMembers" size="6"></select>
      <label>Hapus Anggota</label><select multiple id="adminDelMembers" size="6"></select>
      <div class="row" style="justify-content:flex-end; margin-top:12px;">
        <button class="btn sec" id="adminCancel">Batal</button>
        <button class="btn" id="adminSave">Simpan</button>
      </div>
    </div>
  </div>

  <!-- Modal SeenBy -->
  <div class="modal" id="seenModal">
    <div class="dialog">
      <h3>Status Baca</h3>
      <div id="seenList" style="max-height:300px; overflow:auto;"></div>
      <div class="row" style="justify-content:flex-end; margin-top:12px;">
        <button class="btn" id="seenClose">Tutup</button>
      </div>
    </div>
  </div>

  <!-- Modal Group Info -->
  <div class="modal" id="groupInfoModal">
    <div class="dialog" style="width:480px; max-width:90vw; padding:0; overflow:hidden;">
      <div style="padding:24px 24px 16px; text-align:center; background:var(--panel); position:relative;">
            <button onclick="showModal('groupInfoModal', false)" style="position:absolute; top:8px; right:8px; background:none; border:none; color:#8696a0; font-size:20px; cursor:pointer;">✕</button>            
            <div id="groupInfoAvatar" class="avatar avatar-display" style="width:96px; height:96px; margin:0 auto 12px; border:4px solid var(--panel); font-size:40px; background:var(--accent); border-radius: 50%;">G</div>
            <div style="display:flex; align-items:center; justify-content:center; gap:8px;">
                <h3 id="groupInfoName" style="margin:0; font-size:22px;">Nama Grup</h3>
                <button id="btnGroupRename" class="icon-btn" style="font-size:18px; display:none;">✏️</button>
                <button id="btnGroupAvatar" class="icon-btn" style="font-size:18px; display:none;">📷</button>
            </div>
            <p id="groupInfoMemberCount" style="color:var(--muted); font-size:14px; margin-top:4px;"></p>
        </div>

        <div style="max-height: 250px; overflow-y: auto; padding: 8px 24px; background: var(--bg);">
            <h4 style="font-size:13px; color:var(--muted); margin:8px 0; text-transform:uppercase; letter-spacing:0.5px;">Anggota</h4>
            <div id="groupInfoMembersList"></div>
        </div>

        <div id="groupOwnerActions" style="padding:16px 24px; background:var(--panel); border-top:1px solid var(--border); display:none;">
            <div class="dropdown-item" id="btnAddMembers" style="color:var(--accent);">➕ Tambah Anggota</div>
            <div class="dropdown-item" id="btnRemoveMembers" style="color:var(--accent);">➖ Hapus Anggota</div>
        </div>

        <div id="groupLeaveActions" style="padding:16px 24px; background:var(--panel); border-top:1px solid var(--border);">
            <div class="dropdown-item" id="btnLeaveOrDeleteGroup" style="color:var(--danger);"></div>
        </div>
    </div>
  </div>

  <!-- Modal Remove Members -->
  <div class="modal" id="removeMembersModal">
    <div class="dialog" style="width:500px; max-width:90vw;">
      <h3 id="removeMembersTitle">Hapus Anggota</h3>
      <p style="font-size:14px; color:var(--muted);">Pilih anggota yang ingin Anda hapus dari grup.</p>
      <div id="removeMembersUserList" style="max-height:300px; overflow-y:auto; border:1px solid var(--border); border-radius:8px; padding:8px; margin-top:16px;">
        <!-- User list will be populated by JS -->
      </div>
      <div class="row" style="justify-content:flex-end; margin-top:24px; gap:12px;">
        <button class="btn sec" id="removeMembersCancel">Batal</button>
        <button class="btn" id="removeMembersConfirm" style="background-color: var(--danger);">Hapus</button>
      </div>
    </div>
  </div>

  <!-- Modal Add Members -->
  <div class="modal" id="addMembersModal">
    <div class="dialog" style="width:500px; max-width:90vw;">
      <h3 id="addMembersTitle">Tambah Anggota</h3>
      <div style="margin: 16px 0;">
        <input id="addMemberSearch" placeholder="Cari username untuk ditambahkan..." style="width:100%; padding:10px; border:1px solid var(--border); border-radius:8px; background:var(--input); color:var(--text);">
      </div>
      <div id="addMembersUserList" style="max-height:300px; overflow-y:auto; border:1px solid var(--border); border-radius:8px; padding:8px;">
        <!-- User list will be populated by JS -->
      </div>
      <div class="row" style="justify-content:flex-end; margin-top:24px; gap:12px;">
        <button class="btn sec" id="addMembersCancel">Batal</button>
        <button class="btn" id="addMembersConfirm">Tambah</button>
      </div>
    </div>
  </div>

      <!-- Modal Video Call -->
  <div class="modal" id="videoCallModal">
    <div class="dialog" style="width:1280px; height:720px; max-width:95vw; max-height:95vh; display:flex; flex-direction:row; background:var(--panel); border-radius:16px; overflow:hidden; box-shadow:0 10px 40px rgba(0,0,0,0.5);">
      <!-- Video Call Container -->
      <div style="flex:1; display:flex; flex-direction:column; border-right:1px solid var(--border);">
        <!-- Header -->
        <div style="padding:12px 20px; background:linear-gradient(135deg, var(--panel) 0%, rgba(0,168,132,0.1) 100%); border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; font-weight:600;">
          <span>Video Call</span>
          <div id="callTimer" style="font-size:14px; color:var(--muted); font-weight:500;">00:00</div>
        </div>

        <!-- Messages during call -->
        <div id="callMessages" style="flex:1; overflow-y:auto; padding:12px; background:var(--bg); display:none; border-bottom:1px solid var(--border); max-height:200px;">
          <div style="text-align:center; color:var(--muted); font-size:12px; margin-bottom:8px;">💬 Messages during call</div>
        </div>
        <!-- Video Container -->
        <div style="position:relative; flex:1; background:#000; display:flex; align-items:center; justify-content:center; overflow:hidden;">
          <video id="remoteVideo" autoplay style="width:100%; height:100%; object-fit:cover;"></video>
          <!-- Local Video Overlay (Right Bottom, Full HD ratio) -->
          <div style="position:absolute; bottom:20px; right:20px; width:240px; height:135px; border-radius:12px; overflow:hidden; border:3px solid rgba(255,255,255,0.8); box-shadow:0 4px 12px rgba(0,0,0,0.3);">
            <video id="localVideo" autoplay muted style="width:100%; height:100%; object-fit:cover;"></video>
          </div>
          <!-- Call Status Overlay (Top Left) -->
          <div id="callStatus" style="position:absolute; top:20px; left:20px; background:linear-gradient(135deg, rgba(0,0,0,0.8), rgba(0,0,0,0.6)); color:white; padding:8px 16px; border-radius:24px; font-size:14px; font-weight:500; backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);">Connecting...</div>
          <!-- You Label -->
          <div style="position:absolute; bottom:155px; right:20px; background:linear-gradient(135deg, rgba(0,0,0,0.8), rgba(0,0,0,0.6)); color:white; padding:4px 12px; border-radius:24px; font-size:12px; text-align:center; font-weight:500;">You</div>
          <!-- Fullscreen Toggle -->
          <div style="position:absolute; top:20px; right:20px; background:linear-gradient(135deg, rgba(0,0,0,0.8), rgba(0,0,0,0.6)); color:white; padding:8px 12px; border-radius:24px; font-size:14px; font-weight:500; cursor:pointer; backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);" onclick="toggleFullscreen()">⛶ Fullscreen</div>
        </div>
        <!-- Controls -->
        <div style="padding:16px 20px; background:var(--panel2); border-top:1px solid var(--border); display:flex; justify-content:space-between; align-items:center;">
          <div style="display:flex; gap:8px;">
            <button class="btn sec" id="btnToggleNotepad" onclick="toggleNotepad()" style="background:var(--input); color:var(--text); border:1px solid var(--border); padding:10px 16px; border-radius:20px; font-size:13px; transition:all 0.2s;">📝 Toggle Notepad</button>
            <button class="btn sec" id="btnTakePhoto" onclick="takePhotoDuringCall()" style="background:var(--input); color:var(--text); border:1px solid var(--border); padding:10px 16px; border-radius:20px; font-size:13px; transition:all 0.2s;">📸 Take Photo</button>
            <button class="btn sec" id="btnSharePhoto" onclick="sharePhotoDuringCall()" style="background:var(--input); color:var(--text); border:1px solid var(--border); padding:10px 16px; border-radius:20px; font-size:13px; transition:all 0.2s;">🖼️ Share Photo</button>
            <button class="btn sec" id="btnShareFile" onclick="shareFileDuringCall()" style="background:var(--input); color:var(--text); border:1px solid var(--border); padding:10px 16px; border-radius:20px; font-size:13px; transition:all 0.2s;">📎 Share File</button>
          </div>
          <button class="btn" id="btnEndCall" style="background:linear-gradient(135deg, var(--danger), #dc2626); border:none; color:white; padding:10px 24px; border-radius:20px; font-size:13px; font-weight:600; transition:all 0.2s; box-shadow:0 4px 12px rgba(220, 38, 38, 0.3);">End Call</button>
        </div>
      </div>

      <!-- Live Notepad Sidebar -->
      <div id="liveNotepad" style="width:350px; background:var(--bg); border-radius:0 16px 16px 0; display:flex; flex-direction:column; overflow:hidden; display:none;">
        <!-- Notepad Header -->
        <div style="padding:16px 20px; background:var(--panel); border-bottom:1px solid var(--border);">
          <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px;">
            <span style="font-size:18px;">📝</span>
            <h4 style="margin:0; font-size:16px; font-weight:700; color:var(--text);">Live Notes</h4>
          </div>
          <p style="margin:0; font-size:12px; color:var(--muted);">Real-time collaborative editing</p>
        </div>

        <!-- Notepad Content -->
        <div style="flex:1; padding:16px 20px; overflow:auto;">
          <div id="notepadContainer" style="width:100%; min-height:200px; background:var(--input); border-radius:8px; padding:12px; position:relative;">
            <textarea id="callNotepad" placeholder="Type notes here... Both participants can edit simultaneously!"
                      style="width:100%; min-height:180px; border:none; background:transparent; color:var(--text); font-size:14px; font-family:inherit; outline:none; resize:none; line-height:1.5;"
                      oninput="updateNotepad(this.value)"></textarea>
            <div id="notepadImages" style="margin-top:8px; display:flex; flex-wrap:wrap; gap:8px;">
              <!-- Images will be displayed here -->
            </div>
          </div>

          <!-- Notepad Status -->
          <div style="margin-top:12px; padding:8px 12px; background:var(--panel); border-radius:6px; font-size:11px; color:var(--muted);">
            💡 All changes sync instantly between participants • 📸 Photos appear as images
          </div>
        </div>

        <!-- Notepad Footer -->
        <div style="padding:12px 20px; background:var(--panel2); border-top:1px solid var(--border); display:flex; justify-content:space-between; align-items:center;">
          <div style="font-size:11px; color:var(--muted);">
            Auto-saves when call ends
          </div>
          <button class="btn sec" id="btnSharePhotoToNotepad" onclick="sharePhotoToNotepad()" style="background:var(--input); color:var(--text); border:1px solid var(--border); padding:6px 12px; border-radius:8px; font-size:12px; transition:all 0.2s;">📸 Add Photo</button>
        </div>
      </div>
    </div>
  </div>


  <!-- Modal Profile -->
  <div class="modal" id="profileModal" style="border: none; padding: 0;">
    <div class="dialog" style="width: 420px; max-width:90vw; padding:0; overflow:hidden; background:var(--panel);">
      <div id="profileHeader" style="height: 120px; background: linear-gradient(135deg, var(--them) 0%, var(--bg) 100%); position:relative;">
        <button class="btn sec" onclick="showModal('profileModal', false)" style="position:absolute; top:12px; right:12px; padding:6px 10px; font-size:14px;">✕</button>
      </div>
      <div style="padding: 0 24px 24px; margin-top:-48px; text-align:center; position:relative; z-index:2;">
        <div class="avatar" id="profileAvatar" style="width:96px; height:96px; margin:0 auto 12px; font-size:40px;"></div>
        <div style="font-size:22px; font-weight:700; color:var(--text);" id="profileName"></div>
        <div style="color:var(--text); font-size:14px; margin-top:8px; margin-bottom:16px; font-style:italic;" id="profileBio"></div>
        <div style="color:var(--muted); font-size:15px; margin-bottom:16px;" id="profileUsername"></div>
        
        <div id="profileActions" style="display:flex; justify-content:center; gap:12px; margin-bottom:24px;">
            <!-- Action buttons will be injected here -->
        </div>

        <div style="background:var(--input); padding:12px; border-radius:8px; text-align:left;">
            <div style="font-size:13px; color:var(--muted); border-bottom:1px solid var(--border); padding-bottom:8px; margin-bottom:8px;">Info</div>
            <div style="font-size:14px; color:var(--text);" id="profileJoined"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Modal Settings -->
  <div class="modal settings-modal" id="settingsModal" style="backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);">
    <div class="dialog settings-dialog" style="width: 100vw; height: 100vh; max-width: 100vw; margin: 0; border-radius: 0; background: linear-gradient(135deg, var(--bg) 0%, var(--panel) 100%); animation: settingsSlideIn 0.4s cubic-bezier(0.34, 1.56, 0.64, 1);">
      <!-- Modern Header with Glass Effect -->
      <div class="settings-header" style="background: rgba(var(--panel2), 0.8); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px); border-bottom: 1px solid rgba(var(--border), 0.5); padding: 24px 32px; display:flex; justify-content:space-between; align-items:center; position: sticky; top: 0; z-index: 10;">
        <div style="display:flex; align-items:center; gap:16px;">
          <div class="settings-icon" style="width:48px; height:48px; background: linear-gradient(135deg, var(--accent), adjust-color($accent, $saturation: -20%)); border-radius:16px; display:flex; align-items:center; justify-content:center; font-size:24px; box-shadow: 0 8px 32px rgba(var(--accent), 0.3);">⚙️</div>
          <div>
            <h1 style="margin:0; color:var(--text); font-size:24px; font-weight:700; background: linear-gradient(135deg, var(--text), var(--muted)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;">Pengaturan</h1>
            <p style="margin:0; color:var(--muted); font-size:12px; margin-top:2px;">Kustomisasi pengalaman chat Anda</p>
          </div>
        </div>
        <button class="settings-close-btn" id="settingsCancel" style="background: rgba(var(--border), 0.2); border:1px solid var(--border); color:var(--text); font-size:18px; cursor:pointer; padding:12px 16px; border-radius:12px; transition: all 0.3s ease; display:flex; align-items:center; justify-content:center; width:48px; height:48px;">
          <span style="transition: transform 0.3s ease;">✕</span>
        </button>
      </div>

      <div class="settings-content" style="display:flex; flex:1; overflow:hidden;">
        <!-- Enhanced Sidebar -->
        <div class="settings-sidebar" style="width:300px; background: rgba(var(--panel), 0.9); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px); border-right: 1px solid rgba(var(--border), 0.3); padding: 32px 0; display:flex; flex-direction:column; box-shadow: 4px 0 32px rgba(0,0,0,0.1);">
          <nav class="settings-nav" style="flex:1; padding: 0 32px;">
            <div class="settings-tab settings-tab-enhanced" data-target="starred" style="display:flex; align-items:center; gap:14px; padding:16px 20px; margin-bottom:8px; border-radius:16px 0 0 16px; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); position:relative; overflow:hidden;">
              <div class="tab-icon" style="width:32px; height:32px; background: rgba(var(--accent), 0.1); border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:16px;">⭐</div>
              <div class="tab-text" style="flex:1;">
                <div style="font-weight:600; color:var(--text); font-size:15px;">Pesan Berbintang</div>
                <div style="font-size:12px; color:var(--muted);">Pesan favorit Anda</div>
              </div>
              <div class="tab-indicator" style="width:4px; height:0%; background: var(--accent); border-radius:2px; transition: height 0.3s ease;"></div>
            </div>
            <div class="settings-tab settings-tab-enhanced active" data-target="profile" style="display:flex; align-items:center; gap:14px; padding:16px 20px; margin-bottom:8px; border-radius:16px 0 0 16px; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); position:relative; overflow:hidden;">
              <div class="tab-icon" style="width:32px; height:32px; background: rgba(var(--accent), 0.1); border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:16px;">👤</div>
              <div class="tab-text" style="flex:1;">
                <div style="font-weight:600; color:var(--text); font-size:15px;">Profil</div>
                <div style="font-size:12px; color:var(--muted);">Informasi pribadi</div>
              </div>
              <div class="tab-indicator" style="width:4px; height:0%; background: var(--accent); border-radius:2px; transition: height 0.3s ease;"></div>
            </div>
            <div class="settings-tab settings-tab-enhanced" data-target="contacts" style="display:flex; align-items:center; gap:14px; padding:16px 20px; margin-bottom:8px; border-radius:16px 0 0 16px; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); position:relative; overflow:hidden;">
              <div class="tab-icon" style="width:32px; height:32px; background: rgba(var(--accent), 0.1); border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:16px;">👥</div>
              <div class="tab-text" style="flex:1;">
                <div style="font-weight:600; color:var(--text); font-size:15px;">Permintaan Kontak</div>
                <div style="font-size:12px; color:var(--muted);">Kelola permintaan</div>
              </div>
              <div class="tab-indicator" style="width:4px; height:0%; background: var(--accent); border-radius:2px; transition: height 0.3s ease;"></div>
            </div>
            <div class="settings-tab settings-tab-enhanced" data-target="security" style="display:flex; align-items:center; gap:14px; padding:16px 20px; margin-bottom:8px; border-radius:16px 0 0 16px; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); position:relative; overflow:hidden;">
              <div class="tab-icon" style="width:32px; height:32px; background: rgba(var(--accent), 0.1); border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:16px;">🔒</div>
              <div class="tab-text" style="flex:1;">
                <div style="font-weight:600; color:var(--text); font-size:15px;">Keamanan</div>
                <div style="font-size:12px; color:var(--muted);">Password & akun</div>
              </div>
              <div class="tab-indicator" style="width:4px; height:0%; background: var(--accent); border-radius:2px; transition: height 0.3s ease;"></div>
            </div>
            <div class="settings-tab settings-tab-enhanced" data-target="appearance" style="display:flex; align-items:center; gap:14px; padding:16px 20px; margin-bottom:8px; border-radius:16px 0 0 16px; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); position:relative; overflow:hidden;">
              <div class="tab-icon" style="width:32px; height:32px; background: rgba(var(--accent), 0.1); border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:16px;">🎨</div>
              <div class="tab-text" style="flex:1;">
                <div style="font-weight:600; color:var(--text); font-size:15px;">Tampilan</div>
                <div style="font-size:12px; color:var(--muted);">Tema & gaya</div>
              </div>
              <div class="tab-indicator" style="width:4px; height:0%; background: var(--accent); border-radius:2px; transition: height 0.3s ease;"></div>
            </div>
            <div class="settings-tab settings-tab-enhanced" data-target="app" style="display:flex; align-items:center; gap:14px; padding:16px 20px; margin-bottom:8px; border-radius:16px 0 0 16px; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); position:relative; overflow:hidden;">
              <div class="tab-icon" style="width:32px; height:32px; background: rgba(var(--accent), 0.1); border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:16px;">⚙️</div>
              <div class="tab-text" style="flex:1;">
                <div style="font-weight:600; color:var(--text); font-size:15px;">Aplikasi</div>
                <div style="font-size:12px; color:var(--muted);">Pengaturan umum</div>
              </div>
              <div class="tab-indicator" style="width:4px; height:0%; background: var(--accent); border-radius:2px; transition: height 0.3s ease;"></div>
            </div>
            <div class="settings-tab settings-tab-enhanced" data-target="advanced" style="display:flex; align-items:center; gap:14px; padding:16px 20px; margin-bottom:8px; border-radius:16px 0 0 16px; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); position:relative; overflow:hidden;">
              <div class="tab-icon" style="width:32px; height:32px; background: rgba(var(--accent), 0.1); border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:16px;">🚀</div>
              <div class="tab-text" style="flex:1;">
                <div style="font-weight:600; color:var(--text); font-size:15px;">Fitur Lanjutan</div>
                <div style="font-size:12px; color:var(--muted);">Template, Pesan & Lainnya</div>
              </div>
              <div class="tab-indicator" style="width:4px; height:0%; background: var(--accent); border-radius:2px; transition: height 0.3s ease;"></div>
            </div>
          </nav>
        </div>

        <div style="flex: 1; position: relative;">
          <div id="contactsSettings" class="settings-panel">
            <h4 style="color:var(--text); margin-bottom:16px;">Permintaan Kontak</h4>
            <div id="pendingRequestsList" style="max-height:400px; overflow-y:auto;"></div>
          </div>

          <div id="starredSettings" class="settings-panel">
            <h4 style="color:var(--text); margin-bottom:16px;">Pesan Berbintang</h4>
            <div style="color:var(--muted); font-size:14px; margin-bottom:16px;">Pesan yang telah Anda beri tanda bintang akan muncul di sini untuk akses cepat.</div>
            <div id="starredMessagesList" style="max-height:400px; overflow-y:auto;"></div>
          </div>

          <div id="profileSettings" class="settings-panel active">
            <h4 style="color:var(--text); margin-bottom:16px;">Informasi Pribadi</h4>
            <form id="settingsForm" enctype="multipart/form-data">
              <div class="field-group">
                <label for="settingsDisplayName">Nama Tampilan</label>
                <input type="text" name="display_name" id="settingsDisplayName" placeholder="Nama yang ditampilkan" />
              </div>
              <div class="field-group">
                <label for="settingsBio">Bio</label>
                <textarea name="bio" id="settingsBio" placeholder="Tentang Anda..." rows="3"></textarea>
              </div>
              <div class="field-group">
                <label for="settingsAvatar">Avatar</label>
                <input type="file" name="avatar" id="settingsAvatar" accept="image/*" />
                <p style="font-size:12px; color:#8696a0; margin:4px 0;">Ukuran maksimal 5MB, format PNG/JPG/WebP</p>
              </div>

              <div style="text-align:right; margin-top:24px;">
                <button type="submit" class="btn">
                  💾 Simpan Perubahan
                </button>
              </div>
            </form>
          </div>

          <div id="appearanceSettings" class="settings-panel">
            <h4 style="color:var(--text); margin-bottom:16px;">Tema & Tampilan</h4>
            <div class="field-group">
              <label>Ganti Tema Chat</label>
              <div class="theme-options">
                <div class="theme-card active" data-theme="dark">
                  <div class="theme-preview" style="background:#0b141a;">
                    <div style="background:#111b21; height:100%;"></div>
                  </div>
                  <div class="theme-name">🌙 Gelap</div>
                </div>
                <div class="theme-card" data-theme="light">
                  <div class="theme-preview" style="background:#f9fafb;">
                    <div style="background:#ffffff; height:100%;"></div>
                  </div>
                  <div class="theme-name">☀️ Terang</div>
                </div>
                <div class="theme-card" data-theme="midnight">
                  <div class="theme-preview" style="background:#0c1317;">
                    <div style="background:#101d25; height:100%;"></div>
                  </div>
                  <div class="theme-name">🌃 Midnight</div>
                </div>
                <div class="theme-card" data-theme="forest">
                  <div class="theme-preview" style="background:#111827;">
                    <div style="background:#1f2937; height:100%;"></div>
                  </div>
                  <div class="theme-name">🌲 Forest</div>
                </div>
              </div>
            </div>
          </div>

          <div id="securitySettings" class="settings-panel">
            <h4 style="color:var(--text); margin-bottom:16px;">Keamanan Akun</h4>
            <form id="securityForm">
              <div class="field-group">
                <label for="settingsOldPassword">Password Lama</label>
                <input type="password" name="old_password" id="settingsOldPassword" placeholder="Isi untuk ubah password" />
              </div>
              <div class="field-group">
                <label for="settingsNewPassword">Password Baru</label>
                <input type="password" name="new_password" id="settingsNewPassword" placeholder="Kosongkan jika tidak ubah" />
              </div>
              <div class="field-group">
                <label for="settingsConfirmNewPassword">Konfirmasi Password Baru</label>
                <input type="password" name="confirm_new_password" id="settingsConfirmNewPassword" placeholder="Ulangi password baru" />
              </div>

              <div style="text-align:right; margin-top:24px;">
                <button type="submit" class="btn">
                  🔐 Simpan Keamanan
                </button>
              </div>
            </form>

            <div class="field-group">
              <hr style="border:none; border-top:1px solid #3a4952; margin:24px 0;">
              <label style="color: var(--danger);">Zona Berbahaya</label>
              <button id="btnDeleteAccount" class="btn" style="background: linear-gradient(135deg, #dc2626, #b91c1c); border:none; color:white;">
                🗑️ Hapus Akun
              </button>
            </div>
          </div>

          <div id="appSettings" class="settings-panel" style="animation: slideInRight 0.4s cubic-bezier(0.34, 1.56, 0.64, 1);">
            <h4 style="color:var(--text); margin-bottom:16px;">Pengaturan Aplikasi</h4>
            <div class="field-group">
              <div class="setting-row" style="display:flex; align-items:center; justify-content:space-between; padding:8px 0;">
                <div>
                  <div style="color:var(--text); font-weight:500;">🔔 Notifikasi Desktop</div>
                  <div style="font-size:12px; color:#8696a0;">Izinkan notifikasi saat aplikasi tidak aktif</div>
                </div>
                <label class="switch">
                  <input type="checkbox" id="notifToggleModal">
                  <span class="slider"></span>
                </label>
              </div>
              <div class="setting-row" style="display:flex; align-items:center; justify-content:space-between; padding:8px 0; margin-top:16px;">
                <div>
                  <div style="color:var(--text); font-weight:500;">⬇️ Ekspor Chat</div>
                  <div style="font-size:12px; color:#8696a0;">Unduh percakapan saat ini sebagai JSON</div>
                </div>
                <button class="btn sec" id="btnExportModal" style="background:#54656f; border:none;">
                  📥 Unduh
                </button>
              </div>
            </div>
          </div>

          <!-- Advanced Features Panel -->
          <div id="advancedSettings" class="settings-panel">
            <h4 style="color:var(--text); margin-bottom:16px;">Fitur Lanjutan</h4>
            
            <!-- Quick Reply Templates -->
            <div class="field-group" style="background:var(--input); padding:16px; border-radius:12px; margin-bottom:16px;">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                <div>
                  <div style="color:var(--text); font-weight:600; font-size:15px;">💬 Template Pesan Cepat</div>
                  <div style="font-size:12px; color:var(--muted);">Simpan pesan yang sering digunakan</div>
                </div>
                <button class="btn sec" id="btnAddTemplate" style="padding:6px 12px; font-size:12px;">+ Tambah</button>
              </div>
              <div id="templatesList" style="max-height:200px; overflow-y:auto;"></div>
            </div>

            <!-- Disappearing Messages -->
            <div class="field-group" style="background:var(--input); padding:16px; border-radius:12px; margin-bottom:16px;">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                <div>
                  <div style="color:var(--text); font-weight:600; font-size:15px;">⏱️ Pesan Menghilang</div>
                  <div style="font-size:12px; color:var(--muted);">Hapus pesan otomatis setelah waktu tertentu</div>
                </div>
              </div>
              <div id="disappearingStatus" style="font-size:13px; color:var(--muted); margin-bottom:12px;">Pilih percakapan terlebih dahulu</div>
              <div style="display:flex; gap:8px; flex-wrap:wrap;">
                <button class="btn sec disappearing-btn" data-seconds="0" style="padding:6px 12px; font-size:12px;">Nonaktif</button>
                <button class="btn sec disappearing-btn" data-seconds="3600" style="padding:6px 12px; font-size:12px;">1 Jam</button>
                <button class="btn sec disappearing-btn" data-seconds="86400" style="padding:6px 12px; font-size:12px;">1 Hari</button>
                <button class="btn sec disappearing-btn" data-seconds="604800" style="padding:6px 12px; font-size:12px;">7 Hari</button>
              </div>
            </div>

            <!-- Blocked Contacts -->
            <div class="field-group" style="background:var(--input); padding:16px; border-radius:12px; margin-bottom:16px;">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                <div>
                  <div style="color:var(--text); font-weight:600; font-size:15px;">🚫 Kontak Diblokir</div>
                  <div style="font-size:12px; color:var(--muted);">Kelola daftar kontak yang diblokir</div>
                </div>
              </div>
              <div id="blockedContactsList" style="max-height:200px; overflow-y:auto;"></div>
            </div>

            <!-- Chat Statistics -->
            <div class="field-group" style="background:var(--input); padding:16px; border-radius:12px;">
              <div style="margin-bottom:12px;">
                <div style="color:var(--text); font-weight:600; font-size:15px;">📊 Statistik Chat</div>
                <div style="font-size:12px; color:var(--muted);">Lihat aktivitas chat Anda</div>
              </div>
              <div id="chatStatsContent">
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
                  <div style="background:var(--panel); padding:12px; border-radius:8px; text-align:center;">
                    <div style="font-size:24px; font-weight:700; color:var(--accent);" id="statSent">0</div>
                    <div style="font-size:12px; color:var(--muted);">Pesan Terkirim</div>
                  </div>
                  <div style="background:var(--panel); padding:12px; border-radius:8px; text-align:center;">
                    <div style="font-size:24px; font-weight:700; color:var(--accent);" id="statReceived">0</div>
                    <div style="font-size:12px; color:var(--muted);">Pesan Diterima</div>
                  </div>
                  <div style="background:var(--panel); padding:12px; border-radius:8px; text-align:center;">
                    <div style="font-size:24px; font-weight:700; color:var(--accent);" id="statFiles">0</div>
                    <div style="font-size:12px; color:var(--muted);">File Dibagikan</div>
                  </div>
                  <div style="background:var(--panel); padding:12px; border-radius:8px; text-align:center;">
                    <div style="font-size:24px; font-weight:700; color:var(--accent);" id="statContacts">0</div>
                    <div style="font-size:12px; color:var(--muted);">Kontak</div>
                  </div>
                </div>
              </div>
            </div>

            <!-- Sticker Management -->
            <div class="field-group" style="background:var(--input); padding:16px; border-radius:12px; margin-top:16px;">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                <div>
                  <div style="color:var(--text); font-weight:600; font-size:15px;">🎨 Kelola Stiker</div>
                  <div style="font-size:12px; color:var(--muted);">Buat dan kelola stiker kustom Anda</div>
                </div>
                <button class="btn sec" onclick="openStickerManager()" style="padding:6px 12px; font-size:12px;">Kelola</button>
              </div>
              <div id="stickerStatsContent">
                <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px;">
                  <div style="background:var(--panel); padding:12px; border-radius:8px; text-align:center; cursor:pointer;" onclick="openStickerPicker()">
                    <div style="font-size:24px; margin-bottom:4px;">🎨</div>
                    <div style="font-size:12px; color:var(--muted);">Semua Stiker</div>
                  </div>
                  <div style="background:var(--panel); padding:12px; border-radius:8px; text-align:center; cursor:pointer;" onclick="openStickerCreator()">
                    <div style="font-size:24px; margin-bottom:4px;">✨</div>
                    <div style="font-size:12px; color:var(--muted);">Buat Baru</div>
                  </div>
                  <div style="background:var(--panel); padding:12px; border-radius:8px; text-align:center; cursor:pointer;" onclick="showFavoriteStickers()">
                    <div style="font-size:24px; margin-bottom:4px;">⭐</div>
                    <div style="font-size:12px; color:var(--muted);">Favorit</div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div id="advancedSettings" class="settings-panel">
            <h4 style="color:var(--text); margin-bottom:16px;">Fitur Lanjutan</h4>
            
            <!-- Quick Reply Templates -->
            <div class="field-group" style="background:var(--input); padding:16px; border-radius:12px; margin-bottom:16px;">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                <div>
                  <div style="color:var(--text); font-weight:600; font-size:15px;">💬 Template Pesan Cepat</div>
                  <div style="font-size:12px; color:var(--muted);">Simpan pesan yang sering digunakan</div>
                </div>
                <button class="btn sec" onclick="showAddTemplateModal()" style="padding:6px 12px; font-size:12px;">+ Tambah</button>
              </div>
              <div id="templatesList" style="max-height:200px; overflow-y:auto;"></div>
            </div>

            <!-- Disappearing Messages -->
            <div class="field-group" style="background:var(--input); padding:16px; border-radius:12px; margin-bottom:16px;">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                <div>
                  <div style="color:var(--text); font-weight:600; font-size:15px;">⏱️ Pesan Menghilang</div>
                  <div style="font-size:12px; color:var(--muted);">Hapus pesan otomatis setelah waktu tertentu</div>
                </div>
              </div>
              <div id="disappearingStatus" style="font-size:13px; color:var(--muted); margin-bottom:12px;">Pilih percakapan terlebih dahulu</div>
              <div style="display:flex; gap:8px; flex-wrap:wrap;">
                <button class="btn sec" onclick="setDisappearing(0)" style="padding:6px 12px; font-size:12px;">Nonaktif</button>
                <button class="btn sec" onclick="setDisappearing(3600)" style="padding:6px 12px; font-size:12px;">1 Jam</button>
                <button class="btn sec" onclick="setDisappearing(86400)" style="padding:6px 12px; font-size:12px;">1 Hari</button>
                <button class="btn sec" onclick="setDisappearing(604800)" style="padding:6px 12px; font-size:12px;">7 Hari</button>
              </div>
            </div>

            <!-- Blocked Contacts -->
            <div class="field-group" style="background:var(--input); padding:16px; border-radius:12px; margin-bottom:16px;">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                <div>
                  <div style="color:var(--text); font-weight:600; font-size:15px;">🚫 Kontak Diblokir</div>
                  <div style="font-size:12px; color:var(--muted);">Kelola daftar kontak yang diblokir</div>
                </div>
              </div>
              <div id="blockedContactsList" style="max-height:200px; overflow-y:auto;"></div>
            </div>

            <!-- Chat Statistics -->
            <div class="field-group" style="background:var(--input); padding:16px; border-radius:12px;">
              <div style="margin-bottom:12px;">
                <div style="color:var(--text); font-weight:600; font-size:15px;">📊 Statistik Chat</div>
                <div style="font-size:12px; color:var(--muted);">Lihat aktivitas chat Anda</div>
              </div>
              <div id="chatStatsContent">
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
                  <div style="background:var(--panel); padding:12px; border-radius:8px; text-align:center;">
                    <div style="font-size:24px; font-weight:700; color:var(--accent);" id="statSent">0</div>
                    <div style="font-size:12px; color:var(--muted);">Pesan Terkirim</div>
                  </div>
                  <div style="background:var(--panel); padding:12px; border-radius:8px; text-align:center;">
                    <div style="font-size:24px; font-weight:700; color:var(--accent);" id="statReceived">0</div>
                    <div style="font-size:12px; color:var(--muted);">Pesan Diterima</div>
                  </div>
                  <div style="background:var(--panel); padding:12px; border-radius:8px; text-align:center;">
                    <div style="font-size:24px; font-weight:700; color:var(--accent);" id="statFiles">0</div>
                    <div style="font-size:12px; color:var(--muted);">File Dibagikan</div>
                  </div>
                  <div style="background:var(--panel); padding:12px; border-radius:8px; text-align:center;">
                    <div style="font-size:24px; font-weight:700; color:var(--accent);" id="statContacts">0</div>
                    <div style="font-size:12px; color:var(--muted);">Kontak</div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Modal Delete Account -->
  <div class="modal" id="deleteAccountModal">
    <div class="dialog" style="width:450px;">
        <h3 style="color: var(--danger);">Hapus Akun Permanen</h3>
        <p style="color: var(--muted); font-size: 14px; margin-bottom: 16px;">
            Tindakan ini tidak dapat diurungkan. Semua pesan, kontak, dan data grup Anda akan dihapus secara permanen.
            Untuk melanjutkan, masukkan password Anda.
        </p>
        <form id="deleteAccountForm">
            <div class="field-group">
                <label for="deleteConfirmPassword">Password</label>
                <input type="password" name="password" id="deleteConfirmPassword" required autocomplete="current-password">
            </div>
            <div id="deleteError" style="color: var(--danger); font-size: 13px; margin-top: 8px; display: none;"></div>
            <div class="row" style="justify-content:flex-end; margin-top:24px; gap:12px;">
                <button type="button" class="btn sec" onclick="showModal('deleteAccountModal', false)">Batal</button>
                <button type="submit" class="btn" style="background: var(--danger);">
                    Hapus Akun Saya
                </button>
            </div>
        </form>
    </div>
  </div>


  <!-- Modal Preview -->
  <div class="modal" id="previewModal">
    <div class="dialog" style="width:600px; max-width:90vw; height:80vh; display:flex; flex-direction:column;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; flex-shrink:0;">
        <h3 style="margin:0;">Pratinjau File</h3>
        <button class="btn sec" id="previewCancel" style="padding:6px 10px; font-size:14px;">✕</button>
      </div>
      <div id="previewFile" style="flex:1; background:var(--bg); border-radius:8px; display:flex; align-items:center; justify-content:center; overflow:auto; padding:10px;">
        <!-- Preview content (image, pdf, or icon) will be injected here -->
      </div>
      <div class="composer" style="border-top:none; padding:16px 0 0 0; margin-top:16px; flex-shrink:0;">
        <input id="previewCaption" type="text" placeholder="Tambahkan caption..." style="flex:1; padding:12px 18px; background:var(--input); color:var(--text); border:1px solid var(--border); border-radius:24px; outline:none; font-size:15px;">
        <button class="btn" id="previewSend" style="width:48px; height:48px; border-radius:50%;">➤</button>
      </div>
    </div>
  </div>

  <!-- Modal Image Viewer -->
  <div class="modal" id="imageModal" style="background:rgba(0,0,0,.85); backdrop-filter: blur(8px); cursor:pointer;">
    <span id="imageModalClose" style="position:absolute; top:15px; right:35px; color:#fff; font-size:40px; font-weight:bold; cursor:pointer; z-index: 1001;">&times;</span>
    <img id="imageModalContent" style="margin:auto; display:block; max-width:90%; max-height:90%; border-radius: 8px;">
  </div>

  <!-- Modal Crop Image -->
  <div class="crop-modal" id="cropModal">
    <div class="crop-container">
      <h4 style="margin-bottom:16px; color:var(--text);">Crop Image</h4>
      <img id="cropImage" class="crop-image" style="max-width:100%; height:auto;">
      <div class="crop-controls">
        <button class="btn sec" id="cropCancel">Cancel</button>
        <button class="btn" id="cropConfirm">Confirm</button>
      </div>
    </div>
  </div>

  <!-- Modal Forward -->
  <div class="modal" id="forwardModal">
    <div class="dialog" style="width:480px; max-width:90vw;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">
        <h3 style="margin:0; font-size:20px; font-weight:700;">Teruskan Pesan ke...</h3>
        <button class="btn sec" id="forwardCancel" style="padding:6px 10px; font-size:14px;">✕</button>
      </div>
      <input id="forwardSearch" placeholder="Cari chat..." style="width:100%; padding:10px; margin-bottom:12px; border:1px solid var(--border); border-radius:8px; background:var(--input); color:var(--text);">
      <div id="forwardConvList" style="max-height:300px; overflow-y:auto;">
        <!-- Conversation list will be populated here -->
      </div>
      <div style="text-align:right; margin-top:24px;">
        <button class="btn" id="forwardConfirm" style="padding:12px 24px; font-size:14px;">Teruskan</button>
      </div>
    </div>
  </div>

  <!-- Modal Sticker Picker -->
  <div class="modal" id="stickerPickerModal">
    <div class="dialog" style="width:600px; max-width:90vw; height:70vh; max-height:80vh; display:flex; flex-direction:column; padding:0;">
          <div style="padding:16px 20px; background:var(--panel); border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; flex-shrink:0;">
            <h3 style="margin:0; font-size:18px; font-weight:700;">🎨 Stiker Saya</h3>
            <div style="display:flex; gap:8px;">
              <button class="btn" id="btnManageStickers" style="padding:8px 16px; font-size:13px; background:var(--accent); color:white; border:none; border-radius:8px; cursor:pointer; font-weight:600;">⚙️ Kelola Stiker</button>
              <button class="btn sec" id="btnCreateSticker" style="padding:8px 12px; font-size:13px;">+ Buat Stiker</button>
              <button class="btn sec" id="stickerPickerCancel" style="padding:6px 10px; font-size:14px;">✕</button>
            </div>
          </div>
          
          <!-- Sticker Management Quick Menu -->
          <div style="padding:12px 16px; background:var(--input); border-bottom:1px solid var(--border); display:flex; gap:12px; flex-shrink:0;">
            <button onclick="openStickerCreator()" style="flex:1; background:var(--accent); color:white; border:none; padding:12px; border-radius:8px; cursor:pointer; font-size:13px; font-weight:600; display:flex; align-items:center; justify-content:center; gap:8px;">
              ✨ Buat Stiker Baru
            </button>
            <button onclick="deleteAllStickers()" style="flex:1; background:var(--danger); color:white; border:none; padding:12px; border-radius:8px; cursor:pointer; font-size:13px; font-weight:600; display:flex; align-items:center; justify-content:center; gap:8px;">
              🗑️ Hapus Semua Stiker
            </button>
          </div>
      
      <!-- Sticker Tabs -->
      <div style="display:flex; padding:8px 16px; gap:8px; background:var(--panel2); border-bottom:1px solid var(--border); flex-shrink:0; overflow-x:auto;">
        <button class="sticker-tab-btn active" data-tab="recent" style="background:var(--accent); color:white; padding:8px 16px; border-radius:20px; border:none; cursor:pointer; font-size:13px; white-space:nowrap;">Terbaru</button>
        <button class="sticker-tab-btn" data-tab="favorites" style="background:var(--input); color:var(--text); padding:8px 16px; border-radius:20px; border:1px solid var(--border); cursor:pointer; font-size:13px; white-space:nowrap;">⭐ Favorit</button>
        <button class="sticker-tab-btn" data-tab="all" style="background:var(--input); color:var(--text); padding:8px 16px; border-radius:20px; border:1px solid var(--border); cursor:pointer; font-size:13px; white-space:nowrap;">Semua</button>
        <button class="sticker-tab-btn" data-tab="packs" style="background:var(--input); color:var(--text); padding:8px 16px; border-radius:20px; border:1px solid var(--border); cursor:pointer; font-size:13px; white-space:nowrap;">📦 Pack</button>
      </div>
      
      <!-- Search Stickers -->
          <div style="padding:12px 16px; flex-shrink:0;">
            <input id="stickerSearch" placeholder="🔍 Cari stiker..." style="width:100%; padding:10px 14px; background:var(--input); color:var(--text); border:1px solid var(--border); border-radius:8px; outline:none; font-size:14px;">
          </div>
          
          <!-- Quick Management Links -->
          <div style="display:flex; padding:0 16px 8px 16px; gap:8px; flex-shrink:0; overflow-x:auto;">
            <button class="sticker-tab-btn" onclick="openStickerCreator()" style="background:var(--accent); color:white; padding:6px 12px; border-radius:16px; border:none; cursor:pointer; font-size:12px; white-space:nowrap;">✨ Buat Stiker</button>
            <button class="sticker-tab-btn" onclick="renderStickerGrid('favorites')" style="background:var(--input); color:var(--text); padding:6px 12px; border-radius:16px; border:1px solid var(--border); cursor:pointer; font-size:12px; white-space:nowrap;">⭐ Favorit Saya</button>
            <button class="sticker-tab-btn" onclick="deleteAllStickers()" style="background:var(--input); color:var(--danger); padding:6px 12px; border-radius:16px; border:1px solid var(--danger); cursor:pointer; font-size:12px; white-space:nowrap;">🗑️ Hapus Semua</button>
          </div>
          
          <!-- Sticker Grid -->
      <div id="stickerGrid" style="flex:1; overflow-y:auto; padding:8px 16px; display:grid; grid-template-columns:repeat(5, 1fr); gap:8px; align-content:start;">
        <div style="grid-column:1/-1; text-align:center; color:var(--muted); padding:40px 20px;">
          <div style="font-size:48px; margin-bottom:16px;">🎨</div>
          <div style="font-size:16px; margin-bottom:8px;">Belum ada stiker</div>
          <div style="font-size:14px;">Klik "Buat Stiker" untuk membuat stiker pertama Anda</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Modal Sticker Creator -->
  <div class="modal" id="stickerCreatorModal">
    <div class="dialog" style="width:700px; max-width:90vw; height:80vh; max-height:90vh; display:flex; flex-direction:column; padding:0;">
      <div style="padding:16px 20px; background:var(--panel); border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; flex-shrink:0;">
        <h3 style="margin:0; font-size:18px; font-weight:700;">✨ Buat Stiker Baru</h3>
        <button class="btn sec" id="stickerCreatorCancel" style="padding:6px 10px; font-size:14px;">✕</button>
      </div>
      
      <!-- Creator Tabs -->
      <div style="display:flex; padding:8px 16px; gap:8px; background:var(--panel2); border-bottom:1px solid var(--border); flex-shrink:0;">
        <button class="creator-tab-btn active" data-mode="image" style="background:var(--accent); color:white; padding:10px 20px; border-radius:8px; border:none; cursor:pointer; font-size:14px; font-weight:600;">🖼️ Dari Gambar</button>
        <button class="creator-tab-btn" data-mode="text" style="background:var(--input); color:var(--text); padding:10px 20px; border-radius:8px; border:1px solid var(--border); cursor:pointer; font-size:14px; font-weight:600;">📝 Teks</button>
      </div>
      
      <div style="flex:1; overflow-y:auto; padding:20px;">
        <!-- Image Sticker Mode -->
        <div id="imageStickerMode" class="creator-mode active">
          <div style="display:flex; gap:20px; height:100%;">
            <!-- Upload Area -->
            <div style="flex:1; display:flex; flex-direction:column;">
              <div id="stickerUploadArea" style="flex:1; border:2px dashed var(--border); border-radius:12px; display:flex; flex-direction:column; align-items:center; justify-content:center; cursor:pointer; background:var(--input); transition:all 0.3s; min-height:200px;">
                <div style="font-size:64px; margin-bottom:16px;">📷</div>
                <div style="font-size:16px; color:var(--text); margin-bottom:8px;">Klik atau drag & drop gambar</div>
                <div style="font-size:13px; color:var(--muted);">PNG, JPG, WebP (Max 5MB)</div>
              </div>
              <input type="file" id="stickerFileInput" accept="image/*" style="display:none;">
              
              <!-- Preview Area -->
              <div id="stickerPreviewArea" style="display:none; margin-top:16px; text-align:center;">
                <div style="position:relative; display:inline-block;">
                  <img id="stickerPreviewImg" style="max-width:200px; max-height:200px; border-radius:12px; border:2px solid var(--border);">
                  <button id="removeStickerImg" style="position:absolute; top:-8px; right:-8px; background:var(--danger); color:white; border:none; border-radius:50%; width:24px; height:24px; cursor:pointer; font-size:14px;">✕</button>
                </div>
              </div>
            </div>
            
            <!-- Effects Panel -->
            <div style="width:250px; display:flex; flex-direction:column; gap:12px;">
              <h4 style="margin:0; font-size:14px; color:var(--text);">🎨 Efek</h4>
              
              <div style="display:flex; flex-direction:column; gap:8px;">
                <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                  <input type="checkbox" id="effectGrayscale" style="width:18px; height:18px;">
                  <span style="font-size:13px;">Grayscale</span>
                </label>
                
                <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                  <input type="checkbox" id="effectSepia" style="width:18px; height:18px;">
                  <span style="font-size:13px;">Sepia</span>
                </label>
                
                <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                  <input type="checkbox" id="effectInvert" style="width:18px; height:18px;">
                  <span style="font-size:13px;">Invert</span>
                </label>
                
                <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                  <input type="checkbox" id="effectBlur" style="width:18px; height:18px;">
                  <span style="font-size:13px;">Blur</span>
                </label>
                
                <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
                  <input type="checkbox" id="effectSharpen" style="width:18px; height:18px;">
                  <span style="font-size:13px;">Sharpen</span>
                </label>
              </div>
              
              <div style="margin-top:8px;">
                <label style="font-size:13px; display:block; margin-bottom:4px;">Kecerahan</label>
                <input type="range" id="effectBrightness" min="0.5" max="2" step="0.1" value="1" style="width:100%;">
              </div>
              
              <div>
                <label style="font-size:13px; display:block; margin-bottom:4px;">Kontras</label>
                <input type="range" id="effectContrast" min="0.5" max="2" step="0.1" value="1" style="width:100%;">
              </div>
              
              <div>
                <label style="font-size:13px; display:block; margin-bottom:4px;">Saturasi</label>
                <input type="range" id="effectSaturation" min="0" max="2" step="0.1" value="1" style="width:100%;">
              </div>
              
              <!-- Tags -->
              <div style="margin-top:8px;">
                <label style="font-size:13px; display:block; margin-bottom:4px;">Tag (pisahkan dengan koma)</label>
                <input type="text" id="stickerTags" placeholder="contoh: lucu, hewan, kucing" style="width:100%; padding:8px; background:var(--input); color:var(--text); border:1px solid var(--border); border-radius:6px; font-size:13px;">
              </div>
            </div>
          </div>
        </div>
        
        <!-- Text Sticker Mode -->
        <div id="textStickerMode" class="creator-mode" style="display:none;">
          <div style="display:flex; gap:20px; height:100%;">
            <!-- Text Input & Preview -->
            <div style="flex:1; display:flex; flex-direction:column; gap:16px;">
              <div>
                <label style="font-size:14px; display:block; margin-bottom:8px; font-weight:600;">Teks Stiker</label>
                <textarea id="textStickerInput" placeholder="Ketik teks stiker di sini..." rows="3" style="width:100%; padding:12px; background:var(--input); color:var(--text); border:1px solid var(--border); border-radius:8px; font-size:16px; resize:none;"></textarea>
              </div>
              
              <!-- Text Preview -->
              <div style="flex:1; display:flex; align-items:center; justify-content:center; background:var(--input); border-radius:12px; min-height:200px;">
                <div id="textStickerPreview" style="font-size:32px; font-weight:bold; color:#FFFFFF; text-align:center; padding:20px; text-shadow:2px 2px 4px rgba(0,0,0,0.8);">
                  Ketik teks...
                </div>
              </div>
            </div>
            
            <!-- Text Options -->
            <div style="width:250px; display:flex; flex-direction:column; gap:12px;">
              <h4 style="margin:0; font-size:14px; color:var(--text);">⚙️ Pengaturan Teks</h4>
              
              <div>
                <label style="font-size:13px; display:block; margin-bottom:4px;">Ukuran Font</label>
                <input type="range" id="textFontSize" min="24" max="72" value="48" style="width:100%;">
                <div style="font-size:12px; color:var(--muted); text-align:center;" id="fontSizeValue">48px</div>
              </div>
              
              <div>
                <label style="font-size:13px; display:block; margin-bottom:4px;">Warna Teks</label>
                <input type="color" id="textColor" value="#FFFFFF" style="width:100%; height:40px; border:none; border-radius:6px; cursor:pointer;">
              </div>
              
              <div>
                <label style="font-size:13px; display:block; margin-bottom:4px;">Warna Background</label>
                <input type="color" id="textBgColor" value="#000000" style="width:100%; height:40px; border:none; border-radius:6px; cursor:pointer;">
                <label style="display:flex; align-items:center; gap:8px; margin-top:8px; cursor:pointer;">
                  <input type="checkbox" id="textTransparentBg" style="width:18px; height:18px;">
                  <span style="font-size:13px;">Transparan</span>
                </label>
              </div>
              
              <div>
                <label style="font-size:13px; display:block; margin-bottom:4px;">Gaya Font</label>
                <select id="textFontStyle" style="width:100%; padding:8px; background:var(--input); color:var(--text); border:1px solid var(--border); border-radius:6px;">
                  <option value="bold">Bold</option>
                  <option value="normal">Normal</option>
                  <option value="italic">Italic</option>
                </select>
              </div>
            </div>
          </div>
        </div>
      </div>
      
      <!-- Footer Actions -->
      <div style="padding:16px 20px; background:var(--panel2); border-top:1px solid var(--border); display:flex; justify-content:flex-end; gap:12px; flex-shrink:0;">
        <button class="btn sec" id="stickerCreatorClose" style="padding:10px 20px;">Batal</button>
        <button class="btn" id="saveSticker" style="padding:10px 24px; font-weight:600;">💾 Simpan Stiker</button>
      </div>
    </div>
  </div>

  <div class="drop-overlay" id="dropOverlay">Lepaskan file di sini untuk mengunggah…</div>

  <div id="emoji-overlay"></div>

  <div class="suggest" id="mentionSuggest"></div>

  <div id="toastContainer" class="toast-container"></div>

  <script>
    // No search functionality here - moved inside init() to avoid scope issues
  </script>

  <script>
    // --- Global State ---
    const me = {{ user|tojson }};
    let socket = null;
    let conversations = { peers: [], groups: [], pinned: [] };
    let currentChat = null; // {type:'direct'|'group', id, title, owner_id?}
    let loadingHistory = false, earliestId = null, typingTimer = null, typingState = false, windowFocused = true;
    let replyTo = null, forwardMsg = null, mediaRecorder = null, chunks = [];
    let groupAvatarFile = null;
    let groupMembersCache = {}; // group_id -> [{id, display_name, username, avatar_path}]
    let mentionOpen = false;
    let currentProfileUser = null;
    let onlineUserIds = new Set();
    let pendingInterval = null;
    let pinnedMessageIds = {}; // message_id -> true for current group
    let currentPinnedMessages = []; // list of pinned messages for current group
    let editTarget = null;
    let isEditing = false; // Track if we're in edit mode
    let drafts = {}; // Store draft messages per chat: { 'direct_peerId': msg, 'group_groupId': msg }
    // Video call globals
    let localStream = null;
    let remoteStream = null;
    let peerConnection = null;
    let callInProgress = false;
    let callTargetUserId = null;
    let callType = null; // 'outgoing' or 'incoming'
    let iceCandidateQueue = []; // Queue for early ICE candidates
    let callStartTime = null; // Track call start time for duration
    // Improved ICE servers configuration for better NAT traversal
    // Using STUN and TURN servers for local networking and remote connections
    const configuration = {
      iceServers: [
        { urls: 'stun:stun1.l.google.com:19302' },
        { urls: 'stun:stun2.l.google.com:19302' },
        { urls: 'stun:stun3.l.google.com:19302' },
        { urls: 'stun:stun4.l.google.com:19302' },
        { urls: 'stun:stun.services.mozilla.com' },
        { urls: 'turn:openrelay.metered.ca:80', username: 'openrelayproject', credential: 'openrelayproject' }
      ]
    };

    function getCallerDisplayName(uid) {
      const peer = conversations.peers.find(p => p.id == uid);
      return peer ? peer.display_name : null;
    }

    async function updatePendingIndicator() {
      try {
        const pending = await fetch('/api/pending_requests').then(r => r.json());
        let badge = qs('#pendingBadge');
        const targetContainer = qs('.topbar .user-info-row'); // Target foto profil
        if (!badge) {
          badge = el('<span id="pendingBadge" style="background:var(--danger); color:#fff; border-radius:50%; padding:1px 5px; font-size:10px; position:absolute; top:0; left:38px; border:2px solid var(--panel2);"></span>');
          if (targetContainer) targetContainer.prepend(badge);
        }
        if (pending.length > 0) {
          badge.textContent = pending.length;
          badge.style.display = 'inline-block';
        } else {
          badge.style.display = 'none';
        }
      } catch (err) {
        console.error('Error updating pending indicator:', err);
      }
    }

    function updateTotalUnreadIndicator() {
      let total = 0;
      conversations.peers.forEach(p => total += p.unread_count || 0);
      conversations.groups.forEach(g => total += g.unread_count || 0);
      let badge = qs('#totalUnreadBadge');
      const target = qs('.topbar');
      if (!badge) {
        badge = el('<span id="totalUnreadBadge" style="background:var(--accent); color:#fff; border-radius:50%; padding:2px 6px; font-size:10px; position:absolute; top:10px; left:50%; transform:translateX(-50%);"></span>');
        if (target) target.appendChild(badge);
      }
      if (total > 0) {
        badge.textContent = total > 99 ? '99+' : total;
        badge.style.display = 'block';
      } else {
        badge.style.display = 'none';
      }
      // Update window title
      const baseTitle = 'Rubycon Indonesian Chats';
      document.title = total > 0 ? `(${total}) ${baseTitle}` : baseTitle;

      // Update system tray icon
      updateTrayUnreadCount();
    }

    function updateTrayUnreadCount() {
      // Send unread count to server for tray icon update
      fetch('/api/tray_unread', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({count: getTotalUnreadCount()})
      }).catch(err => console.log('Tray update failed:', err));
    }

    function getTotalUnreadCount() {
      let total = 0;
      conversations.peers.forEach(p => total += p.unread_count || 0);
      conversations.groups.forEach(g => total += g.unread_count || 0);
      return total;
    }

    const emojiCategories = {
      'Muka': ['😀','😁','😂','🤣','😊','😍','😘','😎','😇','🙂','🤔','😮','😢','😡','🥺','😴','😃','😄','😅','😆','😉','😋','😌','😏','😒','😓','😔','😕','😖','😗','😙','😚','🥰','😝','😜','😛','😞','😟','😠','😣','😤','😥','😦','😧','😨','😩','😪','😫','😬','😭','😯','🙁','🙃','🙄','🤨','😐','😑','😶','😵','🤐','😷','🤒','🤕','🤑','🤠','😈','👿','👹','👺','🤡','💩','👻','👽','👾','🤖','😺','😸','😹','😻','😼','😽','🙀','😿','😾'],
      'Gestur': ['👍','👎','🙏','👏','👋','🖐','✋','🖖','👌','🤏','✌️','🤞','🤟','🤘','🤙','🤝','🙌','👐','👤','👥','👶','👦','👧','👨','👩','👱‍♀️','👱','👴','👵','👲','👳','👳‍♀️','🧕','👮','👮‍♀️','👩‍⚕️','👨‍⚕️','👩‍🌾','👨‍🌾','👩‍🍳','👨‍🍳','👩‍🎤','👨‍🎤','👩‍🏫','👨‍🏫','👩‍🏭','👨‍🏭','👩‍💼','👨‍💼','👩‍🔬','👨‍🔬','👩‍💻','👨‍💻','👩‍🎨','👨‍🎨','👩‍✈️','👨‍✈️','🚶','🚶‍♀️','🏃','🏃‍♀️','💃','🕺','👯','👯‍♀️','🧗','🧗‍♀️','🏄','🏄‍♀️','🏇','⛹️','⛹️‍♀️','🏊','🏊‍♀️','🏋️','🏋️‍♀️','🚴','🚴‍♀️','🤸','🤸‍♀️','🤽','🤽‍♀️','🤾','🤾‍♀️','🤹','🤹‍♀️','🗣️'],
      'Aktivitas': ['🔥','🎉','💯','✅','❌','⚠️','⭐','🌟','💡','📌','📷','🎤','🎶','🎧','🎸','🎹','🎺','🎻','🥁','🎷','📻','📱','☎️','📞','📟','📡','💻','⌨️','🖥️','🖱️','🖲️','💽','💾','💿','📀','📼','📺','📸','📹','🎞','📽️','🎪','🎭','🎨','🎬','🎼','🪓','🔨','🛠️','🗜️','⚙️','🔧','⛏️'],
      'Makanan': ['🍎','🍐','🍊','🍋','🍌','🍉','🍇','🍓','🍈','🍒','🍑','🥝','🥭','🍍','🥥','🥐','🥖','🥨','🥯','🧀','🥚','🍳','🥓','🥩','🍗','🍖','🌭','🍔','🍟','🍕','🦪','🦀','🦞','🍤','🍣','🍱','🍛','🍜','🍝','🍠','🍞','🥗','🥘','🧆','🥙','🥪','🌯','🌮'],
      'Binatang': ['🐶','🐱','🐭','🐹','🐰','🦊','🐻','🐼','🐨','🐯','🦁','🐮','🐷','🐽','🐸','🐵','🙈','🙉','🙊','🐒','🐔','🐧','🐦','🐤','🐣','🐥','🦆','🦅','🦉','🦇','🐺','🐗','🐴','🦄','🐝','🐛','🦋','🐌','🐞','🐜','🦗','🕷️','🕸️','🦂','🐢','🐍','🦎','🐙','🦑','🦐','🦞','🦀','🐡','🐠','🐟','🐳','🐋','🦈','🐊','🐅','🐆','🦓','🦍','🦧','🐘','🦛','🦏','🐪','🐫','🦒','🦘','🐃','🐂','🐄','🐎','🐖','🐏','🐑','🦙','🐐','🦌','🐕','🐩','🦮','🐕‍🦺','🐈','🐈‍⬛','🐇','🐁','🐀','🐿️','🦫','🦔','🦦','🦥','🦡','🐾'],
      'Hati': ['❤️','🧡','💛','💚','💙','💜','🖤','🤍','🤎','❤️‍🔥','❤️‍🩹','💌','💘','💝','💖','💗','💓','💞','💕','💟','❣️','💔']
    };
    const emoticonMap = {
      ':)': '🙂',
      ':(': '☹️',
      ';)': '😉',
      ':D': '😄',
      'XD': '😆',
      ':o': '😲',
      ':O': '😲',
      ':P': '😜',
      ':p': '😜',
      'lol': '😂',
      'LOL': '😂',
      'haha': '😄',
      'HAHA': '😄',
      ':*': '😘',
      ';)': '😉',
      ':|': '😐',
      ':wink:': '😉',
      ':kiss:': '😘',
      ':heart:': '❤️',
      '<3': '❤️',
      ':thumbsup:': '👍',
      ':coffee:': '☕',
      ':fire:': '🔥',
      ':cool:': '😎',
      ':sun:': '☀️',
      ':rain:': '🌧️',
      ':moon:': '🌙',
      ':star:': '⭐'
    };
    function convertTextToEmoji(text) {
      let converted = text;
      Object.keys(emoticonMap).forEach(key => {
        // Use word boundaries for whole words
        converted = converted.replace(new RegExp('\\b' + key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b', 'gi'), emoticonMap[key]);
      });
      return converted;
    }
    const EMOJI_REACT = ['👍','❤️','😂','😮','😢','🙏'];

    // Desktop notifications setup
    async function requestNotificationPermission() {
      if (!('Notification' in window)) {
        alert('Browser tidak mendukung notifikasi desktop');
        return false;
      }

      if (Notification.permission === 'denied') {
        alert('Akses notifikasi desktop ditolak. Harap izinkan melalui pengaturan browser.');
        return false;
      }

      if (Notification.permission !== 'granted') {
        const permission = await Notification.requestPermission();
        return permission === 'granted';
      }

      return true;
    }

    function showDesktopNotification(title, body, icon = null) {
      const notifsEnabled = localStorage.getItem('desktopNotifs') === 'true' || localStorage.getItem('desktopNotifs') === null; // Default true

      if (!notifsEnabled || Notification.permission !== 'granted') {
        return;
      }

      const options = {
        body: body || '',
        icon: icon || '/favicon.ico',
        badge: '/favicon.ico',
        timestamp: Date.now(),
        silent: false,
        tag: 'chat-notification', // Group related notifications
        renotify: false,
        requireInteraction: false
      };

      const notification = new Notification(title || 'Rubycon Chat', options);

      // Auto close after 5 seconds
      setTimeout(() => {
        notification.close();
      }, 5000);

      // Click to focus window
      notification.onclick = (e) => {
        e.preventDefault();
        window.focus();
        notification.close();
      };

      return notification;
    }

    // Notification sound
    function playNotificationSound() {
      try {
        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const oscillator = audioCtx.createOscillator();
        const gainNode = audioCtx.createGain();
        oscillator.connect(gainNode);
        gainNode.connect(audioCtx.destination);
        oscillator.frequency.setValueAtTime(800, audioCtx.currentTime);
        gainNode.gain.setValueAtTime(0.3, audioCtx.currentTime);
        oscillator.start(audioCtx.currentTime);
        oscillator.stop(audioCtx.currentTime + 0.1);
      } catch(e) { console.log('Sound not supported'); }
    }

    // --- Helpers ---
    const qs = (s, el=document) => el.querySelector(s);
    const qsa = (s, el=document) => [...el.querySelectorAll(s)];
    function fmtTime(ts){ if(!ts) return ''; const d=new Date(ts.replace(' ','T')+'Z'); return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}); }
    function el(html){ const e=document.createElement('div'); e.innerHTML=html.trim(); return e.firstChild; }
    function wrapEmojis(text, mid) {
      if (!text) return '';
      // Simple emoji regex that works in most browsers
      const emojiRegex = /(?:[\u2700-\u27bf]|(?:\ud83c[\udde6-\uddff]){2}|[\ud800-\udbff][\udc00-\udfff]|[\u0023-\u0039]\ufe0f?\u20e3|\u3299|\u3297|\u303d|\u3030|\u24c2|\ud83c[\udd70-\udd71]|\ud83c[\udd7e-\udd7f]|\ud83c\udd8e|\ud83c[\udd91-\udd9a]|\ud83c[\udde6-\uddff]|\ud83c[\ude01-\ude02]|\ud83c\ude1a|\ud83c\ude2f|\ud83c[\ude32-\ude3a]|\ud83c[\ude50-\ude51]|\u203c|\u2049|[\u25aa-\u25ab]|\u25b6|\u25c0|[\u25fb-\u25fe]|\u00a9|\u00ae|\u2122|\u2139|\ud83c\udc04|[\u2600-\u26FF]|\u2b05|\u2b06|\u2b07|\u2b1b|\u2b1c|\u2b50|\u2b55|\u231a|\u231b|\u2328|\u23cf|[\u23e9-\u23f3]|[\u23f8-\u23fa]|\ud83c\udccf|\u2934|\u2935|[\u2190-\u21ff])/g;
      return text.replace(emojiRegex, (match) => `<span class="emoji-click" onclick="reactToMessage('${mid}', '${match}')">${match}</span>`);
    }
    function reactToMessage(mid, emoji) {
      socket.emit('react_message', {message_id: parseInt(mid), emoji: emoji});
    }
    function notify(title, opts={}){ if(!windowFocused && Notification && Notification.permission==='granted') new Notification(title, opts); }
    function setComposerEnabled(ok){ qs('#msgBox').disabled=!ok; qs('#btnSend').disabled=!ok; }
    function escapeHtml(s){ if(!s) return ''; return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
    function scrollToMessage(mid) {
  let loadAttempts = 0;
  function attemptScroll() {
    const node = qs(`[data-mid="${mid}"]`);
    if (node) {
      node.scrollIntoView({behavior:'smooth', block:'center'});
      node.style.boxShadow='0 0 0 2px #00a884 inset';
      setTimeout(()=>node.style.boxShadow='none', 1200);
      // Open actions menu to show pin button
      const menuBtn = node.querySelector('.menu-btn');
      if (menuBtn) {
        const actions = menuBtn.nextElementSibling;
        if (actions) {
          actions.style.display = 'flex';
        }
      }
      return;
    }
    // If not found and can load more, try loading older messages
    if (earliestId && loadAttempts < 5) {
      loadAttempts++;
      loadOlder(false).then(() => {
        setTimeout(attemptScroll, 200); // Wait for render
      });
    } else if (loadAttempts >= 5) {
      // Give up after 5 attempts (up to 100 messages)
      console.log('Message not found after loading older messages');
    }
  }
  attemptScroll();
}
    function showModal(id, show=true){ const m=qs('#'+id); if(!m) return; if(show){ m.classList.add('show'); const pinnedContainer = qs('#pinnedMessagesContainer'); if (pinnedContainer) pinnedContainer.style.display = 'none'; } else m.classList.remove('show'); }

    function showToast(message, options = {}) {
      const container = qs('#toastContainer');
      const toast = el(`<div class="toast">${message}</div>`);
      if (options.title) {
        toast.innerHTML = `<strong>${options.title}</strong><br>${message}`;
      }
      if (options.actions) {
        const actionsDiv = el('<div style="margin-top: 10px; display: flex; gap: 10px;"></div>');
        options.actions.forEach(action => {
          const btn = el(`<button class="btn sec" style="padding: 5px 10px; font-size: 12px;">${action.label}</button>`);
          btn.onclick = () => {
            action.callback();
            toast.remove();
          };
          actionsDiv.appendChild(btn);
        });
        toast.appendChild(actionsDiv);
      }
      container.appendChild(toast);
      setTimeout(() => toast.remove(), 4500);
    }
    function avatarHTML(path, name){ return `<div class="avatar">${path ? `<img src="/uploads/${path}" alt="${name}" style="width:100%; height:100%; max-width:100%; max-height:100%; border-radius:50%; object-fit:cover;" onerror="this.parentElement.innerHTML = '<span>${ (name||'?').slice(0,1).toUpperCase()}</span>' " />` : `<span>${(name||'?').slice(0,1).toUpperCase()}</span>`}</div>`; }

    function getFileIcon(filePath) {
      if (!filePath) return '📎';
      const ext = filePath.split('.').pop().toLowerCase();
      const icons = {
        'pdf': '📄', 'doc': '📝', 'docx': '📝', 'xls': '📊', 'xlsx': '📊',
        'ppt': '📈', 'pptx': '📈', 'jpg': '🖼️', 'jpeg': '🖼️', 'png': '🖼️',
        'gif': '🖼️', 'webp': '🖼️', 'mp3': '🎵', 'wav': '🎵', 'webm': '🎥',
        'mp4': '🎥', 'avi': '🎥', 'mov': '🎥', 'txt': '📄', 'zip': '📦'
      };
      return icons[ext] || '📎';
    }

    // --- Global functions for search & contacts ---
async function performSearch(query) {
  try {
    const [resultsRes, pendingRes] = await Promise.all([
      fetch(`/api/search_users?username=${encodeURIComponent(query)}`),
      fetch('/api/pending_requests')
    ]);
    const results = await resultsRes.json();
    const pending = await pendingRes.json();
    renderSearchResults(results, pending);
  } catch(err) {
    console.error('Search error:', err);
  }
}

    async function addContact(username, event) {
      event.stopPropagation();
      try {
        const res = await fetch('/api/add_contact', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({username: username})
        });
        if(res.ok) {
          const data = await res.json();
          alert('Kontak berhasil ditambahkan!');
          await refreshConversations();
          renderConversations();
          performSearch(qs('#searchBox').value.trim()); // Refresh search results
        } else {
          const err = await res.json();
          alert(err.error || 'Gagal menambahkan kontak');
        }
      } catch(err) {
        console.error('Add contact error:', err);
        alert('Terjadi kesalahan');
      }
    }

    async function removeContact(userId, event) {
      event.stopPropagation();
      if (!confirm('Hapus kontak ini?')) return;
      try {
        const res = await fetch('/api/remove_contact', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({user_id: userId})
        });
        if(res.ok) {
          alert('Kontak berhasil dihapus!');
          await refreshConversations();
          renderConversations();
          performSearch(qs('#searchBox').value.trim()); // Refresh search results
        } else {
          const err = await res.json();
          alert(err.error || 'Gagal menghapus kontak');
        }
      } catch(err) {
        console.error('Remove contact error:', err);
        alert('Terjadi kesalahan');
      }
    }

    function renderSearchResults(results) {
      const box = qs('#searchResults');
      box.innerHTML = '';

      if(results.length === 0) {
        box.innerHTML = '<div style="padding:12px; color:#8696a0;">Tidak ada pengguna ditemukan</div>';
      } else {
        results.forEach(user => {
          // Define a smaller avatar size for search results
          const avatarInSearchHTML = (path, name) => {
            const avatarContent = path ? `<img src="/uploads/${path}" alt="${name}" style="width:100%; height:100%; border-radius:50%; object-fit:cover;">` : `<span>${(name||'?')[0].toUpperCase()}</span>`;
            return `<div class="avatar" style="width:40px; height:40px; font-size:16px;">${avatarContent}</div>`;
          };
          const item = el(`
            <div class="search-item" style="display:flex; align-items:center; gap:12px; padding:12px 16px; cursor:pointer;" data-user-id="${user.id}" data-user-name="${escapeHtml(user.display_name)}" data-user-avatar="${user.avatar_path || ''}">
              ${avatarInSearchHTML(user.avatar_path, user.display_name)}
              <div style="flex:1;">
                <div style="font-weight:600; color:#e9edef;">${escapeHtml(user.username)}</div>
                <div style="font-size:13px; color:#8696a0;">${user.display_name && user.display_name !== user.username ? escapeHtml(user.display_name) : ''}</div>
                ${user.contact_status === 'accepted' ? '<div style="color:#25d366; font-size:12px;">✅ Dalam kontak</div>' : user.contact_status === 'pending' ? '<div style="color:#f59e0b; font-size:12px;">⏳ Pending</div>' : user.has_incoming_request ? '<div style="color:#25d366; font-size:12px;">📨 Ada permintaan masuk</div>' : ''}
              </div>
              ${user.contact_status === 'accepted' ? `<button class="btn sec" onclick="removeContact('${user.id}', event)">Hapus Kontak</button>` : user.contact_status === 'pending' ? `<button class="btn sec" onclick="addContact('${user.username}', event)">Kirim Ulang</button>` : `<button class="btn sec" onclick="addContact('${user.username}', event)">+ Tambah</button>`}
            </div>
          `);
          item.onclick = (e) => {
            if (e.target.tagName === 'BUTTON') return; // Jangan jalankan jika tombol yang diklik
            if (user.contact_status === 'accepted') {
              openChat('direct', user.id, user.display_name, null, user.avatar_path);
              hideSearchResults();
            } else {
              alert('Tambahkan pengguna sebagai kontak terlebih dahulu untuk memulai obrolan.');
            }
          };
          box.appendChild(item);
        });
      }

      box.style.display = 'block';
    }

    function hideSearchResults() {
      qs('#searchResults').style.display = 'none';
      qs('#convList').style.display = 'block';
      qs('#searchCancelBtn').style.display = 'none';
    }

    document.addEventListener('visibilitychange', ()=>{ windowFocused = !document.hidden; });

    async function init(){
      await refreshConversations();
      renderConversations();
      await preloadUsersToGroupModal();
      await loadMeAvatar();

      // Profile modal click
      qs('#meAvatar').parentElement.style.cursor = 'pointer'; // Make clickable
      qs('#meAvatar').parentElement.onclick = async () => {
        const info = await fetch('/api/me').then(r=>r.json());
        qs('#profileAvatar').innerHTML = avatarHTML(info.avatar_path, info.display_name);
        qs('#profileName').textContent = info.display_name || info.username;
        qs('#profileBio').textContent = info.bio || '';
        qs('#profileUsername').textContent = '@' + info.username;
        const joined = new Date(info.created_at.replace(' ','T'));
      const now = new Date(); // This is fine, it's for text calculation, not URL
        const days = Math.floor((now - joined) / (1000 * 60 * 60 * 24));
        let joinedText = '';
        if (days < 1) joinedText = 'Baru bergabung';
        else if (days < 30) joinedText = days + ' hari lalu';
        else if (days < 365) joinedText = Math.floor(days/30) + ' bulan lalu';
        else joinedText = Math.floor(days/365) + ' tahun lalu';
        qs('#profileJoined').textContent = 'Bergabung ' + joinedText;
        showModal('profileModal', true);
      };

      socket = io({transports:["websocket"]});
    socket.on('message', handleIncomingMessage);
    socket.on('delivered', ({message_id}) => markTicks(message_id, 'delivered'));
    socket.on('read', ({message_id}) => markTicks(message_id, 'read'));
    // Update tray icon when messages are received or read
    socket.on('message', () => updateTrayUnreadCount());
    socket.on('read_receipt_ack', () => updateTrayUnreadCount());
      socket.on('message_edited', updateEdited);
      socket.on('message_deleted', async ({message_id}) => { updateDeleted(message_id); await refreshConversations(); renderConversations(); });
      socket.on('typing', ({from, typing}) => updateTyping(from, typing));
      socket.on('group_created', async () => { await refreshConversations(); renderConversations(); });
      socket.on('group_updated', async ({group_id, name}) => { if(currentChat && currentChat.type==='group' && currentChat.id===group_id){ currentChat.title=name; qs('#chatTitle').textContent=name; } await refreshConversations(); renderConversations();});
      socket.on('group_members_updated', async () => { if(currentChat?.type==='group'){ delete groupMembersCache[currentChat.id]; } });
      socket.on('added_to_group', ({group_id, name}) => {
        alert(`You were added to group: ${name}`);
        refreshConversations(); renderConversations();
      });
      socket.on('group_deleted', ({group_id}) => {
        alert('Group was deleted');
        if(currentChat && currentChat.type==='group' && currentChat.id===group_id){
          // If the current chat is the one being deleted, clear the right panel
          currentChat = null;
          qs('#messages').innerHTML='<div class="section">Grup ini telah dihapus.</div>';
          qs('#chatTitle').textContent='Pilih percakapan';
          qs('#chatSub').textContent = '';
          setComposerEnabled(false);
        } else {
          currentChat = null;
          qs('#messages').innerHTML='<div class="section">Tidak ada percakapan dipilih.</div>';
          qs('#chatTitle').textContent='Pilih percakapan';
        }
        refreshConversations(); renderConversations();
      });
      socket.on('read_receipt_ack', async (data) => {
          await refreshConversations();
          renderConversations();
      });
      socket.on('reactions_update', ({message_id, summary}) => updateReactions(message_id, summary));
      socket.on('presence_update', (data) => {
          onlineUserIds = new Set(data.online_users);
          renderConversations(); // Re-render to show online status
      });
      socket.on('presence', (data) => {
          if (data.online) {
              onlineUserIds.add(data.user_id);
          } else {
              onlineUserIds.delete(data.user_id);
          }
          renderConversations(); // Re-render to show online status
      });
      socket.on('mentioned', ({message_id}) => { notify('Anda disebut', { body:'Anda disebut di sebuah pesan.' }); });
      socket.on('contact_request', ({from: senderId, from_username, from_display_name, to: myId}) => {
        showToast(`Dari: @${from_username}`, {
            title: 'Permintaan Pertemanan',
            actions: [
                {
                    label: 'Terima',
                    callback: () => acceptContact(senderId)
                },
                {
                    label: 'Tolak',
                    callback: () => rejectContact(senderId)
                }
            ]
        });
        updatePendingIndicator();
      });
      socket.on('group_avatar_updated', ({group_id, avatar_path}) => {
        // Update conversations.groups
        const group = conversations.groups.find(g => g.id === group_id);
        if (group) {
          group.avatar_path = avatar_path;
        }
        // Update current chat if viewing
        if (currentChat && currentChat.id === group_id) {
          currentChat.avatar_path = avatar_path;
          qs('#chatAvatar').innerHTML = avatarHTML(avatar_path, currentChat.title);
        }
        // Update modal if open
        if (qs('#groupInfoModal').classList.contains('show') && qs('#groupInfoAvatar')) {
          // Fetch current group info to ensure it's the right modal
          const modalName = qs('#groupInfoName').textContent;
          const modalGroup = conversations.groups.find(g => g.name === modalName && g.id === group_id);
          if (modalGroup) {
            qs('#groupInfoAvatar').innerHTML = avatarHTML(avatar_path, modalGroup.name);
          }
        }
        // Re-render conversations to update list
        renderConversations();
      });
      socket.on('contact_accepted', ({by: accepterId}) => {
        alert('Permintaan kontak Anda diterima');
        refreshConversations(); renderConversations();
      });
      socket.on('contact_rejected', ({by: rejecterId}) => {
        alert('Permintaan kontak Anda ditolak');
      });
      socket.on('group_avatar_updated', ({group_id, avatar_path}) => {
        // Update conversations.groups
        const group = conversations.groups.find(g => g.id === group_id);
        if (group) {
          group.avatar_path = avatar_path;
        }
        // Update current chat if viewing
        if (currentChat && currentChat.id === group_id) {
          currentChat.avatar_path = avatar_path;
          qs('#chatAvatar').innerHTML = avatarHTML(avatar_path, currentChat.title);
        }
        // Update modal if open
        if (qs('#groupInfoModal').classList.contains('show') && qs('#groupInfoAvatar')) {
          // Fetch current group info to ensure it's the right modal
          const modalName = qs('#groupInfoName').textContent;
          const modalGroup = conversations.groups.find(g => g.name === modalName && g.id === group_id);
          if (modalGroup) {
            qs('#groupInfoAvatar').innerHTML = avatarHTML(avatar_path, modalGroup.name);
          }
        }
        // Re-render conversations to update list
        renderConversations();
      });
      socket.on('message_pinned', async ({message_id, group_id, action, pinned_by}) => {
        if (currentChat && currentChat.id === group_id) {
          if (action === 'pin') {
            pinnedMessageIds[message_id] = true;
          } else {
            delete pinnedMessageIds[message_id];
          }
          // Update buttons on existing bubbles
          const loadedMessages = qsa('#messages .bubble');
          loadedMessages.forEach(bubble => {
            const mid = bubble.dataset.mid;
            if (mid == message_id) {
              const buttons = bubble.querySelectorAll('.act-btn');
              buttons.forEach(btn => {
                if(btn.textContent === 'Unpin' || btn.textContent === 'Pin') {
                  btn.textContent = action === 'pin' ? 'Unpin' : 'Pin';
                }
              });
            }
          });
          // Refresh pinned display
          await loadAndDisplayPinnedMessages();
        }
        showToast(`Pesan ${action === 'pin' ? 'disematkan' : 'dilepaskan'}.`);
      });
      // Video call handlers
      socket.on('video_call_offer', ({from: fromUserId, offer}) => {
        if (callInProgress) {
          socket.emit('video_call_busy', {to: fromUserId});
          return;
        }
        showVideoCallIncoming(fromUserId, offer);
      });
      socket.on('video_call_answer', ({from: fromUserId, answer}) => {
        if (peerConnection) {
          peerConnection.setRemoteDescription(new RTCSessionDescription(answer));
        }
      });
      socket.on('video_call_ice', ({from: fromUserId, candidate}) => {
        if (peerConnection && candidate) {
          peerConnection.addIceCandidate(new RTCIceCandidate(candidate));
        } else if (candidate && callType === 'incoming' && !callInProgress) {
          iceCandidateQueue.push(new RTCIceCandidate(candidate));
        }
      });
        socket.on('video_call_end', ({from: fromUserId}) => {
          if (callInProgress && callTargetUserId === fromUserId) {
            endCall();
          }
        });

        socket.on('notepad_edit', (data) => {
          if (callInProgress && data.from == callTargetUserId) {
            // Update notepad dengan content dari lawan bicara
            const content = data.content || '';
            callNotepadContent = content;
            qs('#callNotepad').value = content;
            // Update local display with rendered content
            renderNotepadContent(content);
          }
        });

        // Added missing notepad initial sync + disable keypad on mobile
        function updateNotepad(content) {
          if (!callInProgress) return;
          callNotepadContent = content;
          // Send changes to other participant
          socket.emit('notepad_edit', {
            to: callTargetUserId,
            content: content,
            timestamp: Date.now()
          });
          // Update local display
          renderNotepadContent(content);
        }
        window.updateNotepad = updateNotepad; // Make it globally available

        function renderNotepadContent(content) {
          const notepadImages = qs('#notepadImages');

          // Clear existing images
          notepadImages.innerHTML = '';

          if (!content) {
            return;
          }

          // Parse markdown images and extract them
          const imageRegex = /!\[([^\]]*)\]\(([^)]+)\)/g;
          let match;

          while ((match = imageRegex.exec(content)) !== null) {
            const alt = match[1];
            const src = match[2];

            // Create image element
            const imgElement = el(`<img src="${src}" style="max-width:120px; max-height:120px; border-radius:8px; object-fit:cover; border:2px solid var(--border); margin:4px; cursor:pointer;" alt="${alt}" onclick="showImageModal('${src}')">`);
            notepadImages.appendChild(imgElement);
          }
        }

        // Timer update for call duration
        let callTimerInterval;
        function startCallTimer() {
          if (callTimerInterval) clearInterval(callTimerInterval);
          if (!callStartTime) return;

          callTimerInterval = setInterval(() => {
            if (!callStartTime) return;
            const elapsed = Date.now() - callStartTime;
            const minutes = Math.floor(elapsed / 60000);
            const seconds = Math.floor((elapsed % 60000) / 1000);
            qs('#callTimer').textContent = minutes.toString().padStart(2, '0') + ':' + seconds.toString().padStart(2, '0');
          }, 1000);
        }

        function stopCallTimer() {
          if (callTimerInterval) {
            clearInterval(callTimerInterval);
            callTimerInterval = null;
          }
        }
      socket.on('video_call_busy', () => {
        alert('User is busy in another call.');
        endCall();
      });

      qs('#btnVideoCallTop').onclick = startVideoCall;
      qs('#btnEndCall').onclick = endCall;

      // Attach dropdown - with null safety
      const btnAttach = qs('#btnAttach');
      if (btnAttach) {
        btnAttach.onclick = () => {
          const m = qs('#attachMenu');
          if (m) m.style.display = m.style.display === 'flex' ? 'none' : 'flex';
        };
      }
      const btnDoc = qs('#btnDoc');
      if (btnDoc) {
        btnDoc.onclick = () => {
          chooseFile();
          hideAttachMenu();
        };
      }
      const btnCam = qs('#btnCam');
      if (btnCam) {
        btnCam.onclick = () => {
          chooseImage();
          hideAttachMenu();
        };
      }
      const btnVoice = qs('#btnVoice');
      if (btnVoice) {
        btnVoice.onclick = () => {
          toggleRecord();
          hideAttachMenu();
        };
      }

      // Drag & Drop
      const overlay = qs('#dropOverlay');
      ['dragenter','dragover'].forEach(evt => document.addEventListener(evt, (e)=>{ e.preventDefault(); overlay.classList.add('active'); }));
      ['dragleave'].forEach(evt => document.addEventListener(evt, (e)=>{ e.preventDefault(); overlay.classList.remove('active'); }));      document.addEventListener('drop', (e) => { if(!currentChat) return; e.preventDefault(); overlay.classList.remove('active'); const f = e.dataTransfer?.files?.[0]; if(f){ handleFileUpload(f); } });      document.addEventListener('paste', (e) => {
        if(!currentChat) return;
        const items = e.clipboardData?.items || [];
        for (const it of items) {
          if (it.type.startsWith('image/')) { handleFileUpload(it.getAsFile()); e.preventDefault(); break; }
        }
      });

      // Emoji Picker - Initialize with new structure
      const emojiContentContainer = qs('#emojiContent');
      const emojiTabsContainer = qs('#emojiTabsContainer');
      const emojiSearch = qs('#emojiSearch');
      const categoryIcons = { 'Muka': '😀', 'Gestur': '👍', 'Aktivitas': '🎉', 'Makanan': '🍎', 'Binatang': '🐶', 'Hati': '❤️' };

      // Create all emoji grids
      Object.keys(emojiCategories).forEach((cat, index) => {
        // Create content grid
        const grid = el(`<div class="emoji-grid" id="emoji-grid-${cat}" style="display:${index === 0 ? 'grid' : 'none'};"></div>`);
        emojiCategories[cat].forEach(emoji => {
          const btn = el(`<button class="emoji-item">${emoji}</button>`);
          btn.onclick = () => insertEmoji(emoji);
          btn.setAttribute('data-emoji', emoji);
          grid.appendChild(btn);
        });
        emojiContentContainer.appendChild(grid);

        // Create tab button
        const tabBtn = el(`<button class="emoji-tab-btn${index === 0 ? ' active' : ''}" data-target="${cat}" title="${cat}">${categoryIcons[cat] || '❓'}</button>`);
        tabBtn.onclick = () => {
          // Update active tab
          qsa('.emoji-tab-btn', emojiTabsContainer).forEach(t => t.classList.remove('active'));
          tabBtn.classList.add('active');
          
          // Show corresponding grid
          qsa('.emoji-grid', emojiContentContainer).forEach(g => g.style.display = 'none');
          qs(`#emoji-grid-${cat}`, emojiContentContainer).style.display = 'grid';
        };
        emojiTabsContainer.appendChild(tabBtn);
      });

      // Create "All" grid for search results
      const allGrid = el('<div class="emoji-grid" id="emoji-grid-all" style="display:none;"></div>');
      emojiContentContainer.appendChild(allGrid);

      // Add search functionality
      if (emojiSearch) {
        emojiSearch.addEventListener('input', (e) => {
          const query = e.target.value.toLowerCase().trim();
          
          if (!query) {
            // Show category tabs and first category grid
            qsa('.emoji-tab-btn', emojiTabsContainer).forEach(t => t.style.display = '');
            qsa('.emoji-grid', emojiContentContainer).forEach((g, i) => {
              g.style.display = i === 0 ? 'grid' : 'none';
            });
            qs('#emoji-grid-all').style.display = 'none';
            return;
          }

          // Hide category tabs when searching
          qsa('.emoji-tab-btn', emojiTabsContainer).forEach(t => t.style.display = 'none');
          qsa('.emoji-grid:not(#emoji-grid-all)', emojiContentContainer).forEach(g => {
            g.style.display = 'none';
          });

          // Search all emojis
          const allGrid = qs('#emoji-grid-all');
          allGrid.innerHTML = '';
          let found = false;

          Object.values(emojiCategories).flat().forEach(emoji => {
            const btn = el(`<button class="emoji-item">${emoji}</button>`);
            btn.onclick = () => insertEmoji(emoji);
            allGrid.appendChild(btn);
            found = true;
          });

          allGrid.style.display = found ? 'grid' : 'none';
        });
      }

      const emojiBox = qs('#emojiBox');
      const emojiOverlay = qs('#emoji-overlay');
      if(qs('#btnEmoji')) qs('#btnEmoji').onclick = (e) => {
        e.stopPropagation();
        const isOpen = emojiBox.classList.toggle('open'); panel.style.display = isOpen ? 'flex' : 'none';
        emojiOverlay.style.display = isOpen ? 'block' : 'none';
        emojiOverlay.style.pointerEvents = isOpen ? 'none' : 'auto';
      };
      // Close emoji panel when clicking outside
      document.addEventListener('click', (e) => {
        if (emojiBox.classList.contains('open') && !emojiBox.contains(e.target)) {
          if (emojiBox) emojiBox.classList.remove('open');
          emojiOverlay.style.display = 'none';
          emojiOverlay.style.pointerEvents = 'auto';
        }
      });


      // Voice Record
      if(qs('#btnRec')) qs('#btnRec').onclick = toggleRecord;

      // Preview Modal Handlers
      qs('#previewCancel').onclick = () => {
          showModal('previewModal', false);
          const fileURL = qs('#previewFile')?.dataset.url;
          if (fileURL) URL.revokeObjectURL(fileURL); // Clean up blob URL
      };
      qs('#previewSend').onclick = () => uploadAndSend();

      qs('#msgBox').addEventListener('input', () => { debounceTyping(true); handleMentionSuggest(); autoResizeTextarea(); });
qs('#msgBox').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          sendOrPreview();
        }
      });

      // Set send button click
      qs('#btnSend').onclick = sendOrPreview;
      qs('#msgBox').addEventListener('blur', () => debounceTyping(false));

      // New Group
      setupGroupModal();
      // Group menu
      qs('#btnGroupMenu').onclick = (e) => {
        e.stopPropagation();
        const menu = qs('.dropdown-menu', qs('#groupMenuContainer'));
        menu.classList.toggle('show');
      };
      qs('#searchInGroup').onclick = () => {
        const query = prompt('Enter search query for this group:');
        if(query && currentChat){
          alert('Global search is available via the ⭐ button. For group-specific search, this feature is not implemented yet in simple mode.');
        }
        qs('.dropdown-menu').classList.remove('show');
      };
      qs('#groupInfoBtn').onclick = () => {
        if(currentChat && currentChat.type==='group'){
          showGroupInfoModal(currentChat.id);
        }
        qs('.dropdown-menu').classList.remove('show');
      };
      document.addEventListener('click', (e) => {
        if (!qs('#groupMenuContainer').contains(e.target)) {
          qs('.dropdown-menu').classList.remove('show');
        }
      });

      // Starred modal
      if(qs('#btnStarred')) qs('#btnStarred').onclick = openStarred;
      if(qs('#starClose')) qs('#starClose').onclick = () => showModal('starModal', false);

      // Seen modal
      if(qs('#seenClose')) qs('#seenClose').onclick = () => showModal('seenModal', false);

      // Pin & Export
      if(qs('#btnPin')) qs('#btnPin').onclick = togglePinCurrent;
      if(qs('#btnExport')) qs('#btnExport').onclick = doExport;



      // Dropdown menu for more options
      const btnMoreOptions = qs('#btnMoreOptions');
      if (btnMoreOptions) {
        btnMoreOptions.addEventListener('click', (e) => {
          e.stopPropagation();
          const menu = qs('#moreOptionsMenu');
          menu.classList.toggle('show');
        });
      }
      // Close dropdown when clicking outside
      document.addEventListener('click', (e) => {
        const menu = qs('#moreOptionsMenu');
        const btn = qs('#btnMoreOptions');
        if (menu && menu.classList.contains('show') && btn && !btn.contains(e.target) && !menu.contains(e.target)) {
          menu.classList.remove('show');
        }
      });

      // Settings modal - with null safety
      const btnSettings = qs('#btnSettings');
      if (btnSettings) btnSettings.onclick = async () => {
        console.log('Settings button clicked');
        btnSettings.style.opacity = '0.7';
        try {
          const meinfo = await fetch('/api/me').then(r=>r.json()).catch(e => { console.error('Failed to fetch user info:', e); throw e; });
          qs('#settingsDisplayName').value = meinfo.display_name;
          qs('#settingsBio').value = meinfo.bio || '';
          // Load notification preference
          qs('#notifToggleModal').checked = localStorage.getItem('desktopNotifs') === 'true';
          showModal('settingsModal', true);
        } catch (error) {
          alert('Gagal membuka pengaturan: ' + error.message);
        } finally {
          btnSettings.style.opacity = '1';
        }
      };
      // Notification toggle - with null safety
      const notifToggleModal = qs('#notifToggleModal');
      if (notifToggleModal) notifToggleModal.onchange = (e) => {
        localStorage.setItem('desktopNotifs', e.target.checked);
      };
      // Export button in settings - with null safety
      const btnExportModal = qs('#btnExportModal');
      if (btnExportModal) btnExportModal.onclick = async () => {
        if (!currentChat) {
          alert('Pilih percakapan terlebih dahulu untuk mengekspor');
          return;
        }
        const q = currentChat.type === 'direct' ? `?chat_type=direct&peer_id=${currentChat.id}` : `?chat_type=group&group_id=${currentChat.id}`;
        window.open('/export' + q, '_blank');
        showModal('settingsModal', false);
      };
      // Settings cancel button - with null safety
      const settingsCancel = qs('#settingsCancel');
      if (settingsCancel) settingsCancel.onclick = () => showModal('settingsModal', false);
      let selectedCroppedCanvas = null;
      function initCropper(imageElement, callbackConfirm, callbackCancel) {
        return new Promise((resolve) => {
          const img = new Image();
          img.onload = () => {
            const cropper = new Cropper(imageElement, {
              aspectRatio: 1,
              viewMode: 1,
              guides: false,
              background: false,
              responsive: true,
              restore: false,
              center: true,
              highlight: false,
              cropBoxMovable: false,
              cropBoxResizable: false,
              toggleDragModeOnDblclick: false,
            });
            resolve(cropper);
          };
          img.src = imageElement.src;
        });
      }

      qs('#settingsAvatar').onchange = async (e) => {
        const file = e.target.files[0];
        if (file) {
          const imageURL = URL.createObjectURL(file);
          const cropImage = qs('#cropImage');
          cropImage.src = imageURL;

          // Wait for image to load
          await new Promise((resolve) => {
            cropImage.onload = resolve;
          });

          showModal('cropModal', true);

          try {
            if (window.cropperInstance) {
              window.cropperInstance.destroy();
            }
            window.cropperInstance = new Cropper(cropImage, {
              aspectRatio: 1,
              viewMode: 1,
              guides: false,
              background: false,
              responsive: true,
              restore: false,
              center: true,
              highlight: false,
              cropBoxMovable: false,
              cropBoxResizable: false,
              toggleDragModeOnDblclick: false,
            });

            qs('#cropConfirm').onclick = () => {
              if (window.cropperInstance) {
                const canvas = window.cropperInstance.getCroppedCanvas({ width: 200, height: 200 });
                canvas.toBlob((blob) => {
                  const croppedFile = new File([blob], 'avatar.jpg', { type: 'image/jpeg' });
                  const dt = new DataTransfer();
                  dt.items.add(croppedFile);
                  qs('#settingsAvatar').files = dt.files;
                  showModal('cropModal', false);
                  window.cropperInstance.destroy();
                  window.cropperInstance = null;
                  URL.revokeObjectURL(imageURL);
                });
              }
            };

            qs('#cropCancel').onclick = () => {
              showModal('cropModal', false);
              if (window.cropperInstance) {
                window.cropperInstance.destroy();
                window.cropperInstance = null;
              }
              URL.revokeObjectURL(imageURL);
            };
          } catch (err) {
            console.error('Failed to initialize cropper:', err);
            alert('Error initializing cropper: ' + err.message);
            showModal('cropModal', false);
            URL.revokeObjectURL(imageURL);
          }
        }
      };

      qs('#settingsForm').onsubmit = async (e) => {
        e.preventDefault();
        const formData = new FormData(e.target);
        const res = await fetch('/api/profile/update', {method:'POST', body: formData});
        if(res.ok){
          showModal('settingsModal', false);
          alert('Profile diperbarui');
          // Update global user object with new data
          const updatedMe = await fetch('/api/me').then(r=>r.json());
          me.display_name = updatedMe.display_name;
          me.bio = updatedMe.bio;
          me.username = updatedMe.username;
          me.avatar_path = updatedMe.avatar_path;
          // Update avatar
          const av = qs('#meAvatar');
          if(av) av.innerHTML = avatarHTML(updatedMe.avatar_path, updatedMe.display_name);
          // Update display name in topbar
          const topbarMe = qs('.topbar .me');
          if(topbarMe && updatedMe.display_name) topbarMe.textContent = updatedMe.display_name;
          await refreshConversations();
          renderConversations();
        } else {
          const err = await res.json();
          alert(err.error || 'Error updating profile');
        }
      };

      qs('#securityForm').onsubmit = async (e) => {
        e.preventDefault();
        const formData = new FormData(e.target);
        const res = await fetch('/api/profile/update', {method:'POST', body: formData});
        if(res.ok){
          showModal('settingsModal', false);
          alert('Keamanan diperbarui');
        } else {
          const err = await res.json();
          alert(err.error || 'Error updating security');
        }
      };

      qs('#btnDeleteAccount').onclick = () => {
        showModal('deleteAccountModal', true);
        qs('#deleteConfirmPassword').value = '';
        qs('#deleteError').style.display = 'none';
      };

      qs('#deleteAccountForm').onsubmit = async (e) => {
          e.preventDefault();
          const password = qs('#deleteConfirmPassword').value;
          const errorDiv = qs('#deleteError');
          errorDiv.style.display = 'none';

          const formData = new FormData();
          formData.append('password', password);
          const res = await fetch('/api/profile/delete', { method: 'POST', body: formData });
          if (res.ok) {
              alert('Akun berhasil dihapus.');
              window.location.href = '/logout';
          } else {
              const err = await res.json();
              errorDiv.textContent = err.error || 'Gagal menghapus akun.';
              errorDiv.style.display = 'block';
          }
      };

      // Mention suggest click outside
      document.addEventListener('click', (e)=>{ if(!qs('#mentionSuggest').contains(e.target) && e.target!==qs('#msgBox')) hideMentionSuggest(); });

      // Settings tabs with smooth animations
      const settingsTabs = document.querySelectorAll('.settings-tab');
      const tabIndicators = document.querySelectorAll('.tab-indicator');

      settingsTabs.forEach(tab => {
        tab.onclick = (e) => {
          // Get the target panel
          const targetPanel = qs('#' + tab.dataset.target + 'Settings');

          // Update active tab
          settingsTabs.forEach(t => {
            t.classList.remove('active');
            t.querySelector('.tab-indicator').style.height = '0%';
          });
          document.querySelectorAll('.settings-panel').forEach(p => p.classList.remove('active'));

          tab.classList.add('active');
          targetPanel.classList.add('active');

          // Animate indicator bar
          tab.querySelector('.tab-indicator').style.height = '100%';

          // Load content for specific tabs
          if (tab.dataset.target === 'contacts') {
            loadPendingRequests();
          } else if (tab.dataset.target === 'starred') {
            loadStarredMessages();
          }

          // Add subtle ripple effect
          const ripple = document.createElement('div');
          ripple.style.position = 'absolute';
          ripple.style.borderRadius = '50%';
          ripple.style.background = 'rgba(255, 255, 255, 0.2)';
          ripple.style.transform = 'scale(0)';
          ripple.style.animation = 'ripple 0.6s linear';
          ripple.style.left = (e.clientX - tab.getBoundingClientRect().left) + 'px';
          ripple.style.top = (e.clientY - tab.getBoundingClientRect().top) + 'px';
          ripple.style.width = '10px';
          ripple.style.height = '10px';
          tab.appendChild(ripple);
          setTimeout(() => ripple.remove(), 600);
        };
      });

      // Event listener for starred message buttons
      document.addEventListener('click', function(e) {
        if (e.target.classList.contains('open-msg-btn')) {
          const btn = e.target;
          const message = {
            id: btn.dataset.msgId,
            chat_type: btn.dataset.chatType,
            group_id: btn.dataset.chatId,
            sender_id: btn.textContent.includes('Grup') ? null : btn.dataset.chatId // Simple assumption
          };
          openMessageInChat(message, e);
        }
      });

      async function loadPendingRequests() {
        console.log('Loading pending requests...');
        const list = qs('#pendingRequestsList');
        list.innerHTML = '<div style="color:#8696a0;">Memuat...</div>';
        try {
          const response = await fetch('/api/pending_requests');
          console.log('API Response status:', response.status);
          const requests = await response.json();
          console.log('Requests received:', requests.length, requests);
          list.innerHTML = '';
          if (requests.length === 0) {
            console.log('No pending requests found');
            list.innerHTML = '<div style="color:#8696a0;">Tidak ada permintaan kontak pending.</div>';
            return;
          }
          console.log('Rendering requests...');
          requests.forEach(req => {
            console.log('Rendering request:', req);
            // Define a smaller, consistent avatar size for pending requests
            const pendingAvatarHTML = (path, name) => {
              const avatarContent = path ? `<img src="/uploads/${path}" alt="${name}" style="width:100%; height:100%; border-radius:50%; object-fit:cover;">` : `<span>${(name||'?')[0].toUpperCase()}</span>`;
              return `<div class="avatar" style="width:40px; height:40px; font-size:16px;">${avatarContent}</div>`;
            };
            const item = el(`<div style="display:flex; align-items:flex-start; padding:12px; border:1px solid var(--border); border-radius:8px; margin:8px 0; background:var(--panel);"></div>`);
            item.innerHTML = `
              <div style="flex:1;">
                <div style="display:flex; align-items:center; margin-bottom:4px;">
                  <div style="margin-right:12px;">${pendingAvatarHTML(req.avatar_path, req.display_name)}</div>
                  <div><strong>${escapeHtml(req.display_name)}</strong><br><small>@${escapeHtml(req.username)}</small></div>
                </div>
                <div style="font-size:12px; color:#8696a0;">Dikirim ${new Date(req.added_at.replace(' ','T')).toLocaleDateString()}</div>
              </div>
              <div style="display:flex; gap:6px;">
                <button class="btn sec" onclick="acceptContact(${req.user_id})">Terima</button>
                <button class="btn sec" onclick="rejectContact(${req.user_id})">Tolak</button>
              </div>
            `;
            list.appendChild(item);
          });
          console.log('Requests rendered');
        } catch (err) {
          console.error('Error loading pending requests:', err);
          list.innerHTML = '<div style="color:#ef4444;">Gagal memuat: ' + err.message + '</div>';
        }
      }

    async function loadStarredMessages() {
      console.log('Loading starred messages...');
      const list = qs('#starredMessagesList');
      list.innerHTML = '<div style="color:#8696a0;">Memuat...</div>';
      try {
        const response = await fetch('/api/stars');
        console.log('Stars API Response status:', response.status);
        const messages = await response.json();
        console.log('Starred messages received:', messages.length, messages);
        list.innerHTML = '';
        if (messages.length === 0) {
          console.log('No starred messages found');
          list.innerHTML = '<div style="color:#8696a0; text-align:center; padding:40px;">Tidak ada pesan berbintang. Klik ⭐ pada pesan untuk menambahkannya ke favorit.</div>';
          return;
        }
        console.log('Rendering starred messages...');
        messages.forEach(m => {
          console.log('Rendering starred message:', m);
          const item = el(`<div style="padding:12px; border:1px solid var(--border); border-radius:8px; margin:8px 0; background:var(--panel); cursor:pointer;"></div>`);
          item.innerHTML = `
            <div style="display:flex; align-items:center; margin-bottom:8px;">
              <div class="avatar" style="width:32px; height:32px; font-size:14px; margin-right:12px;">${avatarHTML('', m.sender_name)}</div>
              <div>
                <div style="font-weight:600; color:var(--text); font-size:15px;">${escapeHtml(m.sender_name)}</div>
                <div style="font-size:12px; color:var(--muted);">${fmtTime(m.created_at)} • ${m.chat_type === 'group' ? 'Grup' : 'Chat Pribadi'}</div>
              </div>
            </div>
            <div style="color:var(--text); font-size:14px; line-height:1.4; margin-bottom:8px;">
              ${m.content_type === 'image' ?
                `<img src="/uploads/${m.file_path}" style="max-width:200px; max-height:150px; border-radius:8px; object-fit:cover;">` :
                m.content_type === 'file' ?
                  `<div>📎 ${escapeHtml(m.content || 'File')}</div>` :
                  `<div>${convertTextToEmoji(highlightMentions(m.content || ''))}</div>`
              }
            </div>
            <div style="text-align:right;">
              <button class="btn sec open-msg-btn" data-msg-id="${m.id}" data-chat-type="${m.chat_type}" data-chat-id="${m.chat_type === 'group' ? m.group_id : (m.sender_id === me.id ? m.receiver_id : m.sender_id)}" style="padding:6px 12px; font-size:12px;">Buka Chat</button>
            </div>
          `;
          list.appendChild(item);
        });
        console.log('Starred messages rendered');
      } catch (err) {
        console.error('Error loading starred messages:', err);
        list.innerHTML = '<div style="color:#ef4444;">Gagal memuat pesan berbintang: ' + err.message + '</div>';
      }
    }

    function openMessageInChat(message, event) {
      event.stopPropagation();
      // First close the settings modal
      showModal('settingsModal', false);
      const peerId = (message.sender_id === me.id) ? message.receiver_id : message.sender_id;
      const peer = conversations.peers.find(p => p.id === peerId);
      const peerName = peer ? peer.display_name : 'Unknown User';

      // Open the appropriate chat
      if (message.chat_type === 'direct') {
        // For direct messages - determine the peer ID
        openChat('direct', peerId, peerName);
      } else {
        // For group messages
        const group = conversations.groups.find(g => g.id === message.group_id);
        openChat('group', message.group_id, group ? group.name : `Grup #${message.group_id}`, null, group ? group.avatar_path : null);
      }

      // Scroll to the message after chat loads
      setTimeout(() => {
        scrollToMessage(message.id);
      }, 1000);
    }

    async function acceptContact(uid) {
      try {
        const res = await fetch(`/api/contact_request`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({request_user_id: uid, action: 'accept'})
        });
        if (res.ok) {
          loadPendingRequests();
          await refreshConversations();
          renderConversations();
          updateTotalUnreadIndicator();
        } else {
          alert('Gagal menerima kontak');
        }
      } catch (err) {
        alert('Error');
      }
    }

      async function rejectContact(uid) {
        if (!confirm('Tolak permintaan kontak?')) return;
        try {
          const res = await fetch(`/api/contact_request`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({request_user_id: uid, action: 'reject'})
          });
          if (res.ok) {
            loadPendingRequests();
          } else {
            alert('Gagal menolak kontak');
          }
        } catch (err) {
          alert('Error');
        }
      }

      await updatePendingIndicator();

      // --- New Search Logic ---
      const searchInput = qs('#searchBox');
      const searchCancelBtn = qs('#searchCancelBtn');
      const convListContainer = qs('#convList');
      const searchResultsContainer = qs('#searchResults');

      const debouncedSearch = debounce(performSearch, 300);

      searchInput.addEventListener('input', (e) => {
          const query = e.target.value.trim();
          if (query.length > 0) {
              convListContainer.style.display = 'none';
              searchResultsContainer.style.display = 'block';
              searchCancelBtn.style.display = 'block';
              debouncedSearch(query);
          } else {
              hideSearchResults();
          }
      });

      searchCancelBtn.addEventListener('click', () => {
          searchInput.value = '';
          hideSearchResults();
      });



      pendingInterval = setInterval(updatePendingIndicator, 10000);

      // Theme selection
      document.querySelectorAll('.theme-card').forEach(card => {
        card.onclick = (e) => {
          const theme = card.dataset.theme;
          localStorage.setItem('chatTheme', theme);
          applyTheme(theme);
          updateThemeUI(theme);
        };
      });

      // Hide search results when clicking outside
      document.addEventListener('click', function(e) {
        const searchBox = qs('#searchBox');
        const results = qs('#searchResults');
        if(e.target !== searchBox && !searchBox.contains(e.target) && e.target !== results && !results.contains(e.target)) {
          hideSearchResults();
        }
      });

      // Load and apply saved theme
      const savedTheme = localStorage.getItem('chatTheme') || 'dark'; // default to dark
      applyTheme(savedTheme);
      updateThemeUI(savedTheme);

    }

    function applyTheme(theme){
      const root = document.documentElement;
      if(theme === 'light'){
        root.style.setProperty('--bg', '#f0f2f5'); root.style.setProperty('--panel', '#f8f9fa');
        root.style.setProperty('--panel2', '#ffffff'); root.style.setProperty('--card', '#ffffff');
        root.style.setProperty('--text', '#111827'); root.style.setProperty('--muted', '#6b7280');
        root.style.setProperty('--me', '#dcfce7'); root.style.setProperty('--them', '#ffffff');
        root.style.setProperty('--accent', '#2563eb'); root.style.setProperty('--danger', '#e11d48');
        root.style.setProperty('--border', '#e5e7eb'); root.style.setProperty('--input', '#f9fafb');
        root.style.setProperty('--quote', '#f3f4f6');
      } else if (theme === 'midnight') {
        root.style.setProperty('--bg', '#0c1317'); root.style.setProperty('--panel', '#101d25');
        root.style.setProperty('--panel2', '#182834'); root.style.setProperty('--card', '#101d25');
        root.style.setProperty('--text', '#e7e9ea'); root.style.setProperty('--muted', '#738796');
        root.style.setProperty('--me', '#0b5394'); root.style.setProperty('--them', '#263949');
        root.style.setProperty('--accent', '#3b82f6'); root.style.setProperty('--danger', '#f43f5e');
        root.style.setProperty('--border', '#2c3e50'); root.style.setProperty('--input', '#1f2c3a');
        root.style.setProperty('--quote', '#1c3040');
      } else if (theme === 'forest') {
        root.style.setProperty('--bg', '#111827'); root.style.setProperty('--panel', '#1f2937');
        root.style.setProperty('--panel2', '#374151'); root.style.setProperty('--card', '#1f2937');
        root.style.setProperty('--text', '#f9fafb'); root.style.setProperty('--muted', '#9ca3af');
        root.style.setProperty('--me', '#15803d'); root.style.setProperty('--them', '#374151');
        root.style.setProperty('--accent', '#22c55e'); root.style.setProperty('--danger', '#ef4444');
        root.style.setProperty('--border', '#4b5563'); root.style.setProperty('--input', '#4b5563');
        root.style.setProperty('--quote', '#313b49');
      } else {
        // dark theme (default)
        root.style.setProperty('--bg', '#0b141a'); 
        root.style.setProperty('--panel', '#111b21'); 
        root.style.setProperty('--panel2', '#202c33'); 
        root.style.setProperty('--card', '#111b21'); 
        root.style.setProperty('--text', '#e9edef');
        root.style.setProperty('--muted', '#8696a0');
        root.style.setProperty('--me', '#005c4b'); 
        root.style.setProperty('--them', '#202c33'); 
        root.style.setProperty('--accent', '#25d366'); 
        root.style.setProperty('--danger', '#ef4444');
        root.style.setProperty('--border', '#222e35');
        root.style.setProperty('--input', '#2a3942');
        root.style.setProperty('--quote', '#182229');
      }
    }

    function updateThemeUI(theme){
      document.querySelectorAll('.theme-card').forEach(card => {
        card.classList.toggle('active', card.dataset.theme === theme);
      });
    }

    async function loadMeAvatar(){
      const meinfo = await fetch('/api/me').then(r=>r.json());
      const av = qs('#meAvatar'); av.innerHTML = avatarHTML(meinfo.avatar_path, meinfo.display_name, meinfo.id);
    }

    function openUserProfile(p) {
        const modal = qs('#profileModal');
        if (modal.classList.contains('show')) {
            return; // Prevent opening if already open, avoiding flash on double clicks
        }
        currentProfileUser = p;
        qs('#profileAvatar').innerHTML = avatarHTML(p.avatar_path, p.display_name, p.id);
        qs('#profileName').textContent = p.display_name || p.username;
        qs('#profileBio').textContent = p.bio || '';
        qs('#profileUsername').textContent = '@' + p.username;

        let joinedText = 'Info bergabung tidak tersedia';
        if (p.created_at) {
            const joined = new Date(p.created_at.replace(' ', 'T'));
            const now = new Date();
            const days = Math.floor((now - joined) / (1000 * 60 * 60 * 24));
            if (days < 1) joinedText = 'Baru bergabung hari ini';
            else if (days < 30) joinedText = `Bergabung ${days} hari yang lalu`;
            else if (days < 365) joinedText = `Bergabung ${Math.floor(days / 30)} bulan yang lalu`;
            else joinedText = `Bergabung ${Math.floor(days / 365)} tahun yang lalu`;
        }
        qs('#profileJoined').textContent = joinedText;

        const actionsContainer = qs('#profileActions');
        actionsContainer.innerHTML = ''; // Clear previous actions

        if (p.id === me.id) {
            // Actions for my own profile
            const settingsBtn = el('<button class="btn">⚙️ Edit Profil</button>');
            settingsBtn.onclick = () => {
                showModal('profileModal', false);
                showModal('settingsModal', true);
            };
            actionsContainer.appendChild(settingsBtn);
        } else {
            // Actions for other user's profile
            const msgBtn = el('<button class="btn">💬 Kirim Pesan</button>');
            msgBtn.onclick = () => {
                showModal('profileModal', false);
                openChat('direct', p.id, p.display_name);
            };
            actionsContainer.appendChild(msgBtn);
            const removeBtn = el('<button class="btn sec" style="background-color: var(--danger); color:white;">Hapus Kontak</button>');
            removeBtn.onclick = () => removeContactFromProfile();
            actionsContainer.appendChild(removeBtn);
        }
        showModal('profileModal', true);
    }

    async function removeContactFromProfile() {
      if (!currentProfileUser) return;
      if (!confirm('Hapus kontak ini?')) return;
      try {
        const res = await fetch('/api/remove_contact', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({user_id: currentProfileUser.id})
        });
        if(res.ok) {
          alert('Kontak berhasil dihapus!');
          await refreshConversations();
          renderConversations();
          showModal('profileModal', false);
        } else {
          alert('Gagal menghapus kontak');
        }
      } catch(err) {
        alert('Terjadi kesalahan');
      }
    }

    function resetGroupModal() {
        qs('#groupStep1').classList.add('active');
        qs('#groupStep2').classList.remove('active');
        qs('#groupName').value = '';
        qs('#groupAvatarPreview').innerHTML = '<div class="overlay">📷</div>';
        qs('#selectedMembersPreview').innerHTML = '';
        qs('#groupMembersList').innerHTML = '';
        groupAvatarFile = null;
    }

    async function createGroup() {
        const name = qs('#groupName').value.trim();
        if (!name) {
            alert('Nama grup wajib diisi.');
            return;
        }
        const members = qsa('#groupMembersList input[type="checkbox"]:checked').map(cb => parseInt(cb.value));

        const groupRes = await fetch('/api/groups', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, members })
        });

        if (groupRes.ok) {
            const groupData = await groupRes.json();
            const groupId = groupData.group_id;

            if (groupAvatarFile) {
                const formData = new FormData();
                formData.append('file', groupAvatarFile);
                await fetch(`/api/group_avatar/${groupId}`, { method: 'POST', body: formData });
            }

            resetGroupModal();
            showModal('groupModal', false);
            await refreshConversations();
            renderConversations();
        } else {
            alert('Gagal membuat grup.');
        }
    }

    async function preloadUsersToGroupModal(){
      // Preload users into group modal checkbox list
      const users = await fetch('/api/users').then(r=>r.json());
      const list = qs('#groupMembersList');
      list.innerHTML = '';
      conversations.peers.forEach(u => { // Use peers from conversations to only show contacts
        const item = el(`<div style="display:flex; align-items:center; padding:10px 0; gap:10px;" data-user-id="${u.id}"></div>`);
        const checkbox = el(`<input type="checkbox" id="user_${u.id}" value="${u.id}" style="cursor:pointer; width:20px; height:20px;">`);
        const avatar = el(`<div class="avatar" style="width:40px; height:40px; font-size:16px;">${avatarHTML(u.avatar_path, u.display_name)}</div>`);
        const label = el(`<label for="user_${u.id}" style="flex:1; cursor:pointer; padding:5px 0;">${escapeHtml(u.display_name || u.username)}</label>`);
        
        item.append(checkbox, avatar, label);
        list.appendChild(item);

        checkbox.onchange = () => {
            const previewContainer = qs('#selectedMembersPreview');
            previewContainer.innerHTML = '';
            qsa('#groupMembersList input:checked').forEach(cb => {
                const user = conversations.peers.find(usr => usr.id == cb.value);
                if (user) {
                    previewContainer.innerHTML += `<div class="chip-btn" style="display:flex; align-items:center; gap:4px;">${escapeHtml(user.display_name)} <span style="cursor:pointer;" onclick="document.getElementById('user_${user.id}').click();">×</span></div>`;
                }
            });
        };
      });
    }
    qs('#memberSearch').oninput = (e) => {
        const query = e.target.value.toLowerCase();
        qsa('#groupMembersList > div').forEach(item => {
            const userId = item.dataset.userId;
            const user = conversations.peers.find(p => p.id == userId);
            item.style.display = user && user.display_name.toLowerCase().includes(query) ? 'flex' : 'none';
        });
    };

    function chooseFile(){
      const inp = document.createElement('input');
      inp.type = 'file';
      inp.accept = "*";
      inp.capture = "";
      inp.onchange = (e) => handleFileUpload(e.target.files[0]);
      inp.click();
    }
    function chooseImage(){
      const inp = document.createElement('input');
      inp.type = 'file';
      inp.accept = "image/*";
      inp.capture = "environment";
      inp.onchange = (e) => handleFileUpload(e.target.files[0]);
      inp.click();
    }
    function hideAttachMenu(){
      qs('#attachMenu').style.display = 'none';
    }

    let selectedFile = null;
    let croppedImageBlob = null;
    let isStickerMode = false;

    // Global functions for contact management
    async function loadPendingRequests() {
      console.log('Loading pending requests...');
      const list = qs('#pendingRequestsList');
      list.innerHTML = '<div style="color:#8696a0;">Memuat...</div>';
      try {
        const response = await fetch('/api/pending_requests');
        console.log('API Response status:', response.status);
        const requests = await response.json();
        console.log('Requests received:', requests.length, requests);
        list.innerHTML = '';
        if (requests.length === 0) {
          console.log('No pending requests found');
          list.innerHTML = '<div style="color:#8696a0;">Tidak ada permintaan kontak pending.</div>';
          return;
        }
        console.log('Rendering requests...');
        requests.forEach(req => {
          console.log('Rendering request:', req);
          const item = el(`<div style="display:flex; align-items:flex-start; padding:12px; border:1px solid var(--border); border-radius:8px; margin:8px 0; background:var(--panel);"></div>`);
          item.innerHTML = `
            <div style="flex:1;">
              <div style="display:flex; align-items:center; margin-bottom:4px;">
                <div style="margin-right:8px;">${avatarHTML(req.avatar_path, req.display_name)}</div>
                <div><strong>${escapeHtml(req.display_name)}</strong><br><small>@${escapeHtml(req.username)}</small></div>
              </div>
              <div style="font-size:12px; color:#8696a0;">Dikirim ${new Date(req.added_at.replace(' ','T')).toLocaleDateString()}</div>
            </div>
            <div style="display:flex; gap:6px;">
              <button class="btn sec" onclick="acceptContact(${req.user_id})">Terima</button>
              <button class="btn sec" onclick="rejectContact(${req.user_id})">Tolak</button>
            </div>
          `;
          list.appendChild(item);
        });
        console.log('Requests rendered');
      } catch (err) {
        console.error('Error loading pending requests:', err);
        list.innerHTML = '<div style="color:#ef4444;">Gagal memuat: ' + err.message + '</div>';
      }
    }

    async function acceptContact(uid) {
      try {
        const res = await fetch(`/api/contact_request`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({request_user_id: uid, action: 'accept'})
        });
        if (res.ok) {
          loadPendingRequests();
          updatePendingIndicator();
          alert('Kontak diterima!');
          await refreshConversations();
          renderConversations();
        } else {
          alert('Gagal menerima kontak');
        }
      } catch (err) {
        alert('Error');
      }
    }

    async function rejectContact(uid) {
      if (!confirm('Tolak permintaan kontak?')) return;
      try {
        const res = await fetch(`/api/contact_request`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({request_user_id: uid, action: 'reject'})
        });
        if (res.ok) {
          loadPendingRequests();
          updatePendingIndicator();
        } else {
          alert('Gagal menolak kontak');
        }
      } catch (err) {
        alert('Error');
      }
    }

    function showPreviewModal(file) {
        if (!file) return;
        selectedFile = file;
        const previewContainer = qs('#previewFile');
        const captionInput = qs('#previewCaption');
        captionInput.value = '';
        previewContainer.innerHTML = ''; // Clear previous

        const fileURL = URL.createObjectURL(file);
        previewContainer.dataset.url = fileURL; // Store for cleanup

        const isImg = file.type.startsWith('image/');
        const isPdf = file.type === 'application/pdf';

        if (isImg) {
            previewContainer.innerHTML = `<img src="${fileURL}" style="max-width:100%; max-height:500px; object-fit:contain; border-radius:8px;">`;
        } else if (isPdf) {
            // Enhanced PDF preview with better styling
            previewContainer.innerHTML = `<div style="display:flex; flex-direction:column; align-items:center; gap:10px; height:100%;">
                <div style="font-weight:600; color:var(--text); text-align:center;">📄 ${escapeHtml(file.name)}</div>
                <embed src="${fileURL}#toolbar=0&view=FitH" type="application/pdf" style="width:100%; height:450px; border-radius:8px; border:1px solid var(--border);">
                <div style="text-align:center; color:var(--muted); padding:20px;">
                    <p>Browser tidak mendukung preview PDF. <a href="${fileURL}" target="_blank" style="color:var(--accent); text-decoration:underline;">Buka di tab baru</a>.</p>
                </div>
            </div>`;
        } else {
            previewContainer.innerHTML = `<div style="text-align:center; color:var(--muted); padding:20px;"><div style="font-size:64px;">📄</div><p style="margin-top:16px; font-weight:bold; font-size:18px;">${escapeHtml(file.name)}</p><p style="font-size:14px;">${(file.size / 1024).toFixed(1)} KB</p></div>`;
        }

        showModal('previewModal', true);
    }

    function sendOrPreview(){
      const txt = qs('#msgBox').value.trim();
      if (!txt) return;

      if (isEditing && editTarget) {
          // Save edited message
          socket.emit('edit_message', { message_id: editTarget.id, new_content: txt });
          exitEditMode();
      } else {
          // Send new message
          sendMessage(txt, 'text', null);
          qs('#msgBox').value = '';
          clearReplyBar();
      }
      autoResizeTextarea();
    }

    async function uploadAndSend() {
        if (!selectedFile) return;
        qs('#previewSend').disabled = true; // Prevent double-sending
        const caption = qs('#previewCaption').value.trim();
        const fd = new FormData();
        fd.append('file', selectedFile);

        const up = await fetch('/upload', { method: 'POST', body: fd }).then(r => r.json());
        if (!up.ok) {
            alert(up.error || 'Gagal mengunggah file');
            qs('#previewSend').disabled = false;
            return;
        }

        const isImg = selectedFile.type.startsWith('image/');
        const isAudio = selectedFile.type.startsWith('audio/');
        sendMessage(caption, isAudio ? 'audio' : (isImg ? 'image' : 'file'), up.path);

        showModal('previewModal', false);
        qs('#previewSend').disabled = false;
    }

    async function createGroup(){
      const name = qs('#groupName').value.trim();
      if(!name){ alert('Nama grup wajib'); return; }
      const selected = Array.from(document.querySelectorAll('#groupMembersList input[type="checkbox"]:checked')).map(cb => parseInt(cb.value));
      const res = await fetch('/api/groups', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name, members:selected})});
      if(res.ok){
        showModal('groupModal', false);
        qs('#groupName').value = '';
        // Reset steps
        qs('#groupStep1').classList.add('active');
        qs('#groupStep2').classList.remove('active');
        await refreshConversations(); renderConversations();
      } else {
        alert('Gagal buat grup');
      }
    }

    async function refreshConversations(){
      conversations = await fetch('/api/conversations').then(r=>r.json());
    }

    function renderConversations(){
      const box = qs('#convList'); box.innerHTML = '';
      // Pinned
      if ((conversations.pinned||[]).length){
        box.appendChild(el('<div class="section">📌 Pinned</div>'));
        conversations.pinned.forEach(pin => {
          const isOnline = onlineUserIds.has(pin.ref_id);
          if(pin.chat_type==='direct'){
            const p = conversations.peers.find(x=>x.id===pin.ref_id); if(!p) return;
            const prev = p.preview ? previewText(p.preview) : '';
            const node = el(`
              <div class="item" data-type="direct" data-id="${p.id}">
                <div class="avatar-container">
                  ${avatarHTML(p.avatar_path, p.display_name)}
                  ${isOnline ? '<div class="online-indicator"></div>' : ''}
                </div>
                <div class="meta"><div class="name">${p.display_name}</div><div class="preview">${prev}</div></div>
                <div class="badge">📌</div>
              </div>`);
            node.onclick = () => openChat('direct', p.id, p.display_name);
            box.appendChild(node);
          }else{
            const g = conversations.groups.find(x=>x.id===pin.ref_id); if(!g) return;
            const prev = g.preview ? previewText(g.preview) : '';
            const node = el(`
              <div class="item" data-type="group" data-id="${g.id}">
                <div class="avatar">G</div>
                <div class="meta"><div class="name">${g.name}</div><div class="preview">${prev}</div></div>
                <div class="badge">📌</div>
              </div>`);
            node.onclick = () => openChat('group', g.id, g.name, g.owner_id);
            box.appendChild(node);
          }
        });
      }
      // Direct
      box.appendChild(el('<div class="section">Chats Pribadi</div>'));
      conversations.peers.forEach(p=>{
        const isTyping = p.is_typing;
        const prev = isTyping ? '<i style="color:var(--accent);">mengetik…</i>' : (p.preview ? previewText(p.preview) : '');
        const unreadBadge = p.unread_count > 0 ? (p.unread_count > 1 ? `<div class="unread-badge">${p.unread_count}</div>` : `<div class="unread-dot"></div>`) : '';
        const node = el(`
          <div class="item" data-type="direct" data-id="${p.id}" data-name="${p.display_name}" data-avatar="${p.avatar_path || ''}">
            <div class="avatar-container">
              ${avatarHTML(p.avatar_path, p.display_name)}
              ${onlineUserIds.has(p.id) ? '<div class="online-indicator"></div>' : ''}
            </div>
            <div class="meta"><div class="name">${p.display_name}</div><div class="preview">${prev}</div></div>
    <div class="badge">${unreadBadge}</div>
          </div>`);
        node.onclick = () => openChat('direct', p.id, p.display_name, null, p.avatar_path);
        // Avatar click for profile
        qs('.avatar', node).onclick = (e) => {
          e.stopPropagation();
          openUserProfile(p);
        };
        box.appendChild(node);
      });
      // Groups
      box.appendChild(el('<div class="section">Grup</div>'));
      conversations.groups.forEach(g=>{
        const isTyping = g.is_typing;
        const prev = isTyping ? '<i style="color:var(--accent);">mengetik…</i>' : (g.preview ? previewText(g.preview) : '');
        const unreadBadge = g.unread_count > 0 ? (g.unread_count > 1 ? `<div class="unread-badge">${g.unread_count}</div>` : `<div class="unread-dot"></div>`) : '';
        const node = el(`
          <div class="item" data-type="group" data-id="${g.id}" data-name="${g.name}" data-avatar="${g.avatar_path || ''}" >
            ${avatarHTML(g.avatar_path, g.name)}
            <div class="meta"><div class="name">${g.name}</div><div class="preview">${prev}</div></div>
            <div class="badge">${unreadBadge}</div>
          </div>`);
        node.onclick = () => openChat('group', g.id, g.name, g.owner_id, g.avatar_path);
        box.appendChild(node);
      });
    }

    function previewText(p){ if(!p) return ''; if(p.deleted) return '• Pesan dihapus'; if(p.content_type==='sticker') return '• Sticker'; if(p.content_type!=='text') return '• '+p.content_type.toUpperCase(); const txt = p.content||''; return txt.length > 60 ? txt.slice(0, 60) + '...' : txt; }

    async function openChat(type, id, title, owner_id=null, avatar_path=null){
      currentChat = { type, id, title, owner_id, avatar_path };
      earliestId = null;
      qs('#chatTitle').textContent = title;
      const chatAvatar = qs('#chatAvatar');
      chatAvatar.innerHTML = avatarHTML(avatar_path, title);
      chatAvatar.style.display = 'flex';
      // Highlight active conversation
      qsa('.item.active').forEach(el => el.classList.remove('active'));
      const activeItem = qs(`.item[data-type="${type}"][data-id="${id}"]`); // This might fail if avatar path has quotes
      if(activeItem) activeItem.classList.add('active');
      // Reset pinned messages for groups
      pinnedMessageIds = {};
      currentPinnedMessages = [];
      let subText = '';
      if (type === 'group') {
        subText = 'Grup';
        // Load pinned messages first for groups to set pinnedMessageIds before rendering
        await loadAndDisplayPinnedMessages();
      } else if (type === 'direct') {
        const peer = conversations.peers.find(p => p.id === id) || { last_seen: null };
        if (onlineUserIds.has(id)) {
          subText = 'Aktif sekarang';
        } else if (peer.last_seen) {
          const last = new Date(peer.last_seen.replace(' ', 'T'));
          const now = new Date();
          const diffMs = now - last;
          const diffMins = Math.floor(diffMs / 60000);
          const diffHours = Math.floor(diffMins / 60);
          const diffDays = Math.floor(diffHours / 24);
          if (diffMins < 2) subText = 'Baru saja online';
          else if (diffMins < 60) subText = `Terakhir online ${diffMins}m lalu`;
          else if (diffHours < 2) subText = `Terakhir online ${diffHours}h lalu`;
          else if (diffHours < 24) subText = `Terakhir online ${diffHours}h ${diffMins % 60}m lalu`;
          else if (diffDays < 2) subText = 'Terakhir online kemarin';
          else if (diffDays < 7) subText = `Terakhir online ${diffDays} hari lalu`;
          else if (diffDays < 30) {
            const weeks = Math.floor(diffDays / 7);
            if (weeks < 2) subText = 'Seminggu lalu online';
            else if (weeks < 4) subText = `${weeks} minggu lalu online`;
            else subText = 'Sebulan lalu online';
          } else {
            const months = Math.floor(diffDays / 30);
            if (months < 2) subText = 'Sebulan lalu online';
            else if (months < 12) subText = `${months} bulan lalu online`;
            else {
              const options = { year: 'numeric', month: 'short', day: 'numeric' };
              subText = `Offline sejak ${last.toLocaleDateString('id-ID', options)}`;
            }
          }
        } else {
          subText = 'Pengguna offline';
        }
      }
      qs('#chatSub').textContent = subText;
      qs('#messages').innerHTML = '';
      clearReplyBar();
      await loadOlder(true);
      setComposerEnabled(true);

      // --- Unread Bubble Fix ---
      // 1. Optimistically update the UI first for instant feedback.
      if(type === 'direct') {
        const peer = conversations.peers.find(p => p.id === id);
        if(peer) peer.unread_count = 0;
      } else {
        const group = conversations.groups.find(g => g.id === id);
        if(group) group.unread_count = 0;
      }
      renderConversations(); // This will immediately remove the bubble.
      // 2. Then, tell the server to mark as read.
      socket.emit('mark_read', type==='direct' ? {chat_type:'direct', peer_id:id} : {chat_type:'group', group_id:id});

      // update pin button icon
      await updatePinButton();
      // preload members for mentions
      if(type==='group') await ensureGroupMembers(id);

      // Add click handler for direct chat titles to show profile
      if(type === 'direct') {
        qs('#chatTitle').onclick = async () => {
          // Find the peer object
          const peer = conversations.peers.find(p => p.id === id);
          if(peer) {
            openUserProfile(peer);
          }
        };
      } else if(type === 'group') {
        qs('#chatTitle').onclick = () => showGroupInfoModal(id);
        await loadAndDisplayPinnedMessages(); // Load and display pinned messages for groups
      } else {
        qs('#chatTitle').onclick = null;
      }
      // Show/hide group menu
      if (type === 'group') {
        qs('#groupMenuContainer').style.display = 'block';
        const isOwner = owner_id === me.id;
        qs('#btnLeaveGroupFromMenu').style.display = isOwner ? 'none' : 'block';
        qs('#btnDeleteGroupFromMenu').style.display = isOwner ? 'block' : 'none';
        qs('#btnDeleteGroupFromMenu').onclick = () => { if(confirm('Yakin ingin menghapus grup ini secara permanen?')) deleteGroup(id); };
        qs('#btnLeaveGroupFromMenu').onclick = () => { if(confirm('Yakin ingin keluar dari grup ini?')) leaveGroup(id); };
        await loadAndDisplayPinnedMessages(); // Load and display pinned messages for groups
      } else {
        qs('#groupMenuContainer').style.display = 'none';
        // Hide pinned messages container when not in group chat
        const pinnedContainer = qs('#pinnedMessagesContainer');
        if (pinnedContainer) pinnedContainer.style.display = 'none';
      }
    }

    async function updatePinButton(){
      const pinned = conversations.pinned || [];
      const isPinned = pinned.some(p => p.chat_type===currentChat.type && p.ref_id===currentChat.id);
      qs('#btnPin').textContent = isPinned ? '📌 Unpin' : '📌 Pin';
    }

    async function loadOlder(scrollBottom=false){
      if(!currentChat) return;
      loadingHistory = true;
      let url = `/api/messages?chat_type=${currentChat.type}&limit=200`;
      if (currentChat.type==='direct') url += `&peer_id=${currentChat.id}`; else url += `&group_id=${currentChat.id}`;
      if (earliestId) url += `&before_id=${earliestId}`;
      const list = await fetch(url).then(r=>r.json());
      const box = qs('#messages'); const prevH = box.scrollHeight;

      const nodes = [];
      list.forEach(m=>{
        if(!earliestId || m.id < earliestId) earliestId = m.id;
        const node = renderMessage(m);
        nodes.push(node);
      });
      // List is DESC (newer IDs first), but to prepend oldest first, reverse nodes
      nodes.reverse();
      nodes.forEach(node => {
        box.insertBefore(node, box.firstChild);
      });
      // Adjust scroll to keep the same content visible
      const addedHeight = box.scrollHeight - prevH;
      if(scrollBottom) setTimeout(() => box.scrollTop = box.scrollHeight, 0);
      else box.scrollTop += addedHeight + 120; // Keep the previous view, account for the added height
      loadingHistory = false;
    }

    function renderMessage(m){
      const mine = m.sender_id === me.id;
      let bubClasses = `bubble ${mine?'meb':'theb'}`;
      let avatarDiv = null;

      // Add sender avatar for other's messages in groups
      if (!mine && currentChat?.type === 'group') {
        bubClasses += ' with-avatar';
        avatarDiv = el(`<div class="sender-avatar">${avatarHTML(m.sender_avatar, m.sender_name)}</div>`);
        avatarDiv.onclick = (e) => {
          e.stopPropagation();
          openUserProfile({id: m.sender_id, username: '', display_name: m.sender_name, avatar_path: m.sender_avatar, created_at: ''});
        };
      }

      // Add unread indicator for unread messages from others
      let unreadIndicator = null;
      if (!mine && !m.my_read && currentChat?.type === 'direct') {
        unreadIndicator = el('<div class="unread-indicator" style="position:absolute; left:-8px; top:50%; transform:translateY(-50%); width:6px; height:6px; background:var(--accent); border-radius:50%; border:2px solid var(--bg);"></div>');
      }

      const bub = el(`<div class="${bubClasses}" data-mid="${m.id}" style="position:relative;"></div>`);

      // For system messages, center the content
      if (m.content_type === 'system') {
        bub.style.textAlign = 'center';
        bub.style.fontStyle = 'italic';
        bub.style.opacity = '0.8';
      }

      // menu button
      const menuBtn = el(`<button class="menu-btn" title="Options">⋯</button>`);
      const actions = el(`<div class="actions"></div>`);
      const replyBtn = el(`<button class="act-btn">Balas</button>`);
      const forwardBtn = el(`<button class="act-btn">Teruskan</button>`);
      const reactBtn = el(`<button class="act-btn">Reaksi</button>`);

      // Disabled sender name to prevent UI covering issues
      const starBtn = el(`<button class="act-btn">${m.starred ? 'Unstar' : 'Star'}</button>`);
      actions.appendChild(replyBtn); actions.appendChild(forwardBtn); actions.appendChild(reactBtn); actions.appendChild(starBtn);
      if (currentChat?.type==='group' && currentChat?.owner_id === me.id){
        // tombol admin status bisa ditambah jika butuh per pesan
        // Admin status buttons can be added if needed per message
      }
    if (mine && !m.deleted){
        const editBtn = el('<button class="act-btn">Edit</button>');
        const delBtn = el('<button class="act-btn">Delete</button>');
        if (m.content_type === 'text') { // Hanya tampilkan tombol edit untuk pesan teks
            actions.appendChild(editBtn);
        }
        actions.appendChild(delBtn);
        editBtn.onclick = () => {
            actions.style.display = 'none'; // Sembunyikan menu aksi
            enterEditMode(m);
        };
        delBtn.onclick = ()=>{ if(confirm('Delete message for everyone?')) socket.emit('delete_message',{message_id:m.id}); menuBtn.click(); };
      }

    // Pin/Unpin for group admin (TEMP: allow all members for testing)
    // Pin/Unpin for group admin (TEMP: allow all members for testing)
    if (currentChat?.type === 'group') {
        const pinBtn = el(`<button class="act-btn">${m.id in pinnedMessageIds ? 'Unpin' : 'Pin'}</button>`);
        actions.appendChild(pinBtn);
        pinBtn.onclick = () => {
            const action = m.id in pinnedMessageIds ? 'unpin' : 'pin';
            socket.emit('pin_message', {message_id: m.id, action: action});
            menuBtn.click();
        };
    }

    if (actions.children.length > 0) {
        bub.appendChild(menuBtn);
        bub.appendChild(actions);
    }
      menuBtn.onclick = () => { const tgt=actions; tgt.style.display = tgt.style.display === 'flex' ? 'none' : 'flex'; };
      document.addEventListener('click', (e) => { if (!bub.contains(e.target)) actions.style.display = 'none'; });
      // Only append menu if there are actions inside it
      if (actions.children.length > 0) {
        bub.appendChild(menuBtn);
        bub.appendChild(actions);
      }

      // reply quote
      if (m.reply && m.reply.id){
        const q = m.reply;
        const qbox = el('<div class="quote"></div>');
        let preview = '';
        if(q.deleted) preview = '<i class="deleted">Pesan dihapus</i>';
        else if(q.content_type==='image') preview = (q.file_path?`/uploads/${q.file_path}`:'') + escapeHtml(q.content||'');
        else if(q.content_type==='file') preview = '📎 ' + escapeHtml(q.content || 'FILE');
        else preview = escapeHtml(q.content || '');
        qbox.innerHTML = `<div class="qname">${escapeHtml(q.sender_name)}</div>` + preview;
        qbox.onclick = () => scrollToMessage(q.id);
        bub.appendChild(qbox);
      }

      // body
      const body = el('<div class="content"></div>');

      // sender name for group messages from others
      const senderNameDiv = (!mine && currentChat?.type === 'group') ? el(`<div class="sender-name" style="color:var(--accent); font-weight:600; font-size:12px; margin-bottom:4px;">${escapeHtml(m.sender_name)}</div>`) : null;
      if (m.deleted){
        body.innerHTML = '<span class="deleted">Pesan ini telah dihapus</span>';
      } else if (m.content_type==='image' && m.file_path){
        const c = m.content ? convertTextToEmoji(highlightMentions(m.content)) : '';
        const wrapped = c ? wrapEmojis(c, m.id) : '';
        body.innerHTML = `<img src="/uploads/${m.file_path}" style="max-width:200px; height:auto; border-radius:8px; cursor:pointer;" onclick="showImageModal('/uploads/${m.file_path}')">` + (m.content?(' <br><span class="cap">' + wrapped + '</span>'):'');
      } else if (m.content_type==='sticker' && m.file_path){
        body.innerHTML = `<img src="/uploads/${m.file_path}" style="width:100px; height:100px; border-radius:12px; cursor:pointer;" onclick="showImageModal('/uploads/${m.file_path}')" onerror="this.style.display='none'; this.parentElement.innerHTML='📸 Sticker (failed to load)'">`;
      } else if (m.content_type==='file' && m.file_path){
        const c = m.content ? convertTextToEmoji(highlightMentions(m.content)) : '';
        const wrapped = c ? wrapEmojis(c, m.id) : '';
        // WhatsApp-style file display
        const fileName = m.file_path.split('/').pop().split('_').slice(1).join('_') || m.file_path.split('/').pop(); // Extract actual name if UUID prefixed
        const fileIcon = getFileIcon(m.file_path);
        body.innerHTML = `<div style="display:flex; align-items:center; gap:12px; padding:8px 12px; background:var(--them); border-radius:8px; border-left:4px solid var(--accent); max-width:300px;">
          <div style="font-size:24px; color:var(--accent);">${fileIcon}</div>
          <div style="flex:1; min-width:0;">
            <div style="font-weight:600; color:var(--text); font-size:14px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(fileName)}</div>
          </div>
          <a href="/uploads/${m.file_path}" target="_blank" download style="font-size:20px; color:var(--accent); text-decoration:none;">⬇️</a>
        </div>` + (c ? ('<br><br>' + wrapped) : '');
      } else if (m.content_type==='audio' && m.file_path){
        const c = m.content ? convertTextToEmoji(highlightMentions(m.content)) : '';
        const wrapped = c ? wrapEmojis(c, m.id) : '';
        body.innerHTML = `<audio src="/uploads/${m.file_path}" controls style="max-width:200px;"></audio>` + (m.content?('<div>'+wrapped+'</div>'):'');
      } else {
        const rawContent = m.content || '';
        let c = convertTextToEmoji(highlightMentions(rawContent));
        c = c.replace(/\n/g, '<br>');  // Handle multi-line messages from Shift+Enter
        body.innerHTML = wrapEmojis(c, m.id);
      }

      // meta
      const meta = el('<div class="meta"></div>');
      const lmeta = [];
      lmeta.push('<span>'+fmtTime(m.created_at)+'</span>');
      if (m.forwarded) lmeta.push('<span style="color:#b0e4d6;">• Diteruskan</span>');
      if (m.edited && !m.deleted) lmeta.push('<span class="edited">(Edited)</span>');
      meta.innerHTML = lmeta.join(' ');

      // ticks / group info
      if (mine && m.chat_type==='direct'){
        const ticks = el('<span class="ticks"></span>');
        ticks.innerHTML = '<span class="s1">✔</span>';
        if (m.status?.delivered) ticks.innerHTML = '<span class="s2">✔✔</span>';
        if (m.status?.read) ticks.innerHTML = '<span class="s3">✔✔</span>';
        meta.appendChild(ticks);
      } else if (m.chat_type==='group' && mine){
        const info = el('<span class="group-status">✓✓ '+(m.status?.read_count||0)+' dibaca</span>');
        info.onclick = async (ev)=>{ ev.preventDefault(); const list = await fetch('/api/message_status/'+m.id).then(r=>r.json()); const box = qs('#seenList'); box.innerHTML=''; list.forEach(x=>{ const li=el('<div style="display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px dashed #2a3942;"></div>'); li.innerHTML = `<div>${escapeHtml(x.display_name)}</div><div style="color:#cbd5e1; font-size:12px;">${x.read_at?('Read '+x.read_at): (x.delivered_at?('Delivered '+x.delivered_at):'Sent')}</div>`; box.appendChild(li); }); showModal('seenModal', true); };
        meta.appendChild(info);
      }

      // reactions
      const reacts = el('<div class="reacts"></div>');
      (m.reactions||[]).forEach(r => reacts.appendChild(el(`<span class="react-chip">${r.emoji} ${r.cnt}</span>`)));
      meta.appendChild(reacts);

      // wire action handlers
      replyBtn.onclick = () => setReplyBar(m);
      forwardBtn.onclick = () => { forwardMsg = m; openForward(); };
      reactBtn.onclick = () => { const bar=el('<div class="emoji-bar"></div>'); EMOJI_REACT.forEach(e=>{ const b=el(`<button>${e}</button>`); b.onclick=(ev2)=>{ ev2.preventDefault(); socket.emit('react_message',{message_id:m.id, emoji:e}); setTimeout(()=>{ try{bub.classList.remove('showing-emoji-bar'); bar.remove();}catch{} }, 100); }; bar.appendChild(b); }); bub.appendChild(bar); bub.classList.add('showing-emoji-bar'); setTimeout(()=>{ try{bub.classList.remove('showing-emoji-bar'); bar.remove();}catch{} }, 4000); };
      reactBtn.onclick = () => {
        const bar=el('<div class="emoji-bar"></div>');
        // Apply dynamic positioning based on message alignment
        if (mine) { bar.style.right = '8px'; } else { bar.style.left = '8px'; }
        EMOJI_REACT.forEach(e=>{ const b=el(`<button>${e}</button>`); b.onclick=(ev2)=>{ ev2.preventDefault(); socket.emit('react_message',{message_id:m.id, emoji:e}); setTimeout(()=>{ bub.classList.remove('showing-emoji-bar'); bar.remove(); }, 100); }; bar.appendChild(b); });
        bub.appendChild(bar); bub.classList.add('showing-emoji-bar'); setTimeout(()=>{ try{bub.classList.remove('showing-emoji-bar'); bar.remove();}catch{} }, 4000);
      };
      starBtn.onclick = async () => { const res = await fetch('/api/stars',{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({message_id:m.id})}).then(r=>r.json()); starBtn.textContent = res.starred ? 'Unstar' : 'Star'; };

      if (avatarDiv) bub.appendChild(avatarDiv);
      if (senderNameDiv) bub.appendChild(senderNameDiv);
      bub.appendChild(body);
      bub.appendChild(meta);
      return bub;
    }

    function animateMoveToTop(element) {
      if (!element) return;
      const parent = element.parentNode;
      if (!parent) return;

      // 1. Get height for smooth animation
      const height = element.offsetHeight;

      // 2. Animate out
      element.style.transition = 'max-height 0.3s ease-in-out, opacity 0.3s ease-in-out, transform 0.3s ease-in-out';
      element.style.maxHeight = height + 'px';
      element.style.overflow = 'hidden';

      requestAnimationFrame(() => {
        element.style.maxHeight = '0px';
        element.style.opacity = '0';
        element.style.transform = 'scaleY(0.8)';
      });

      // 3. After animation, move to top and animate in
      setTimeout(() => {
        parent.prepend(element);
        element.style.opacity = '1';
        element.style.transform = 'scaleY(1)';
        element.style.maxHeight = height + 'px';
      }, 350);
    }

    function updateEdited(msg){ const node=qs(`[data-mid="${msg.id}"]`); if(node){ let c = convertTextToEmoji(highlightMentions(msg.content||'')); c = c.replace(/\n/g, '<br>'); node.querySelector('.content').innerHTML = wrapEmojis(c, msg.id); let e=node.querySelector('.edited'); if(!e) node.querySelector('.meta').insertAdjacentHTML('beforeend','<span class="edited">(Edited)</span>'); } }
    function updateDeleted(mid){ const node=qs(`[data-mid="${mid}"]`); if(node) node.querySelector('.content').innerHTML = '<span class="deleted">Pesan ini telah dihapus</span>'; }
    function updateTyping(from, typing){
      if(!currentChat) return;
      const subText = typing ? (currentChat.type==='direct' ? 'mengetik…' : 'anggota mengetik…') : '';
      const subEl = qs('#chatSub');
      if(subEl) subEl.textContent = subText;
      const ti = qs('#typingIndicator');
      if(!ti) return;
      const messagesEl = qs('#messages');
      if(!messagesEl) return;
      if(typing){
        if(currentChat.type==='direct' && from===currentChat.id){
          ti.textContent = 'mengetik…';
        }else if(currentChat.type==='group' && from!==me.id){
          ti.textContent = 'mengetik…';
        }else{
          ti.textContent = '';
        }
        ti.style.display = 'block';
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }else{
        ti.style.display = 'none';
        ti.textContent = '';
      }
    }
    function updateReactions(mid, summary){ const node=qs(`[data-mid="${mid}"]`); if(!node) return; let box=node.querySelector('.reacts'); if(!box){ box=el('<div class="reacts"></div>'); node.querySelector('.meta').appendChild(box); } box.innerHTML=''; summary.forEach(r=> box.appendChild(el(`<span class="react-chip">${r.emoji} ${r.cnt}</span>`))); }
    function markTicks(mid, state){ const node=qs(`[data-mid="${mid}"] .ticks`); if(!node) return; if(state==='delivered') node.innerHTML='<span class="s2">✔✔</span>'; if(state==='read') node.innerHTML='<span class="s3">✓✓</span>'; }

function handleIncomingMessage(m) {
    const isForCurrentChat = currentChat &&
        ((currentChat.type === 'direct' && m.chat_type === 'direct' && (m.sender_id === currentChat.id || m.receiver_id === currentChat.id)) ||
         (currentChat.type === 'group' && m.chat_type === 'group' && m.group_id === currentChat.id));

    if (isForCurrentChat) {
        // Message for the currently open chat
        qs('#messages').appendChild(renderMessage(m));
        qs('#messages').scrollTop = qs('#messages').scrollHeight + 1000;

        // Also add to call messages if video call modal is open
        if (callInProgress && qs('#videoCallModal').classList.contains('show')) {
            const callMessages = qs('#callMessages');
            if (callMessages) {
                callMessages.style.display = 'block';
                callMessages.appendChild(renderMessage(m));
                callMessages.scrollTop = callMessages.scrollHeight;
            }
        }

        if (m.sender_id !== me.id) {
            socket.emit('mark_read', currentChat.type === 'direct' ? {chat_type:'direct', peer_id: currentChat.id} : {chat_type:'group', group_id: currentChat.id});
        }
    } else {
        // Message for another chat, update conversation list without reload
        const isMentioned = (m.content || '').includes(`@${me.username}`);

        // Desktop notification
        const notifsEnabled = localStorage.getItem('desktopNotifs') !== 'false'; // Default true
        if (notifsEnabled && !windowFocused && Notification.permission === 'granted') {
            let title = m.sender_name;
            let body = '';

            if (m.content_type === 'image') {
                body = '🖼️ Gambar';
            } else if (m.content_type === 'file') {
                body = '📎 File';
            } else if (m.content_type === 'audio') {
                body = '🎵 Pesan suara';
            } else if (m.content_type === 'sticker') {
                body = '🎡 Sticker';
            } else {
                body = m.content || 'Pesan baru';
            }

            if (isMentioned) {
                title = `💬 @${me.username} ${m.sender_name}`;
                body = m.content || 'Anda disebut';
            }

            // Add chat context for groups
            if (m.chat_type === 'group') {
                const groupName = conversations.groups.find(g => g.id === m.group_id)?.name || 'Grup';
                title = `${m.sender_name} (${groupName})`;
            }

            showDesktopNotification(title, body);
        }

        // Legacy notification system
        if (isMentioned) {
            notify(`Anda dimention oleh ${m.sender_name}`, { body: m.content });
        } else {
            notify(m.sender_name, { body: m.content || (m.content_type || '').toUpperCase() });
        }

        // Play sound for unread messages if not currently viewing this chat
        if (localStorage.getItem('soundsEnabled') !== 'false') {
            playNotificationSound();
        }

        let conv, convElement;
        if (m.chat_type === 'direct') {
            const peerId = m.sender_id === me.id ? m.receiver_id : m.sender_id;
            conv = conversations.peers.find(p => p.id === peerId);
            convElement = qs(`.item[data-type="direct"][data-id="${peerId}"]`);
        } else { // group
            conv = conversations.groups.find(g => g.id === m.group_id);
            convElement = qs(`.item[data-type="group"][data-id="${m.group_id}"]`);
        }

        if (conv && convElement) {
            conv.unread_count = (conv.unread_count || 0) + 1;
            conv.preview = m; // Update preview with the full message object

            // Update the UI for that specific item
            qs('.preview', convElement).innerHTML = previewText(m);
            const badgeContainer = qs('.badge', convElement);
            if (badgeContainer) badgeContainer.innerHTML = conv.unread_count > 0 ? `<div class="unread-badge">${conv.unread_count}</div>` : '';

            // Animate the element to the top
            animateMoveToTop(convElement);
        }

        // Update total unread count in title
        updateTotalUnreadIndicator();
    }
}

    function sendMessage(text, content_type, file_path){
      if(!currentChat) return;
      const payload = { chat_type: currentChat.type, content: text, content_type, file_path, reply_to: replyTo?.id || null };
      if(currentChat.type==='direct') payload.peer_id = currentChat.id; else payload.group_id = currentChat.id;
      socket.emit('send_message', payload);
    }

    async function handleFileUpload(file){
        if (!file || !currentChat) return;

        if (isStickerMode) {
            // Sticker mode: crop and resize to 200x200
            selectedFile = file;
            dryCropSticker();
        } else if (file.type.startsWith('image/')) {
            // Normal image
            selectedFile = file;
            showPreviewModal(file);
        } else {
            // Non-images
            selectedFile = file;
            showPreviewModal(file);
        }
    }

    async function dryCropSticker() {
        if (!selectedFile) return;

        const cropImage = qs('#cropImage');
        const imageURL = URL.createObjectURL(selectedFile);
        cropImage.src = imageURL;

        cropImage.onload = async () => {
            try {
                if (window.cropperInstance) {
                    window.cropperInstance.destroy();
                }
                window.cropperInstance = new Cropper(cropImage, {
                    aspectRatio: 1, // Square
                    viewMode: 1,
                    guides: false,
                    background: false,
                    responsive: true,
                    restore: false,
                    center: true,
                    highlight: false,
                    cropBoxMovable: false,
                    cropBoxResizable: false,
                    toggleDragModeOnDblclick: false,
                });

                qs('#cropConfirm').onclick = () => {
                    const canvas = window.cropperInstance.getCroppedCanvas({ width: 200, height: 200 });
                    canvas.toBlob(async (blob) => {
                        const croppedFile = new File([blob], 'sticker.jpg', { type: 'image/jpeg' });
                        await sendSticker(croppedFile);
                        showModal('cropModal', false);
                        window.cropperInstance.destroy();
                        window.cropperInstance = null;
                        URL.revokeObjectURL(imageURL);
                        isStickerMode = false; // Reset mode
                    });
                };

                qs('#cropCancel').onclick = () => {
                    showModal('cropModal', false);
                    if (window.cropperInstance) {
                        window.cropperInstance.destroy();
                        window.cropperInstance = null;
                    }
                    URL.revokeObjectURL(imageURL);
                    isStickerMode = false; // Reset mode
                };
            } catch (err) {
                console.error('Failed to initialize cropper for sticker:', err);
                alert('Error initializing cropper: ' + err.message);
                showModal('cropModal', false);
                URL.revokeObjectURL(imageURL);
                isStickerMode = false;
            }
        };

        showModal('cropModal', true);
    }

    async function sendSticker(file) {
        const fd = new FormData();
        fd.append('file', file);

        const up = await fetch('/upload', { method: 'POST', body: fd }).then(r => r.json());
        if (!up.ok) {
            alert('Gagal upload stiker: ' + (up.error || 'Unknown error'));
            return;
        }

        const payload = {
            chat_type: currentChat.type,
            content_type: 'sticker',
            file_path: up.path
        };
        if (currentChat.type === 'direct') payload.peer_id = currentChat.id;
        else payload.group_id = currentChat.id;

        socket.emit('send_message', payload);
    }

    // Voice Recorder
    async function toggleRecord(){
      if(mediaRecorder && mediaRecorder.state==='recording'){ mediaRecorder.stop(); return; }
      try{
        if (location.protocol !== 'https:' && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
          alert('Mikrofon memerlukan koneksi aman (HTTPS) atau localhost. Akses melalui localhost:8080 atau gunakan HTTPS.');
          return;
        }
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
          alert('Mikrofon tidak didukung pada browser ini.');
          return;
        }
        const stream = await navigator.mediaDevices.getUserMedia({audio:true}); chunks=[];
        mediaRecorder = new MediaRecorder(stream);
        mediaRecorder.ondataavailable = e => { if(e.data.size>0) chunks.push(e.data); };
        mediaRecorder.onstop = async ()=>{
          const blob = new Blob(chunks, {type: 'audio/webm'});
          const file = new File([blob], 'voice.webm', {type:'audio/webm'});
          await handleFileUpload(file);
          stream.getTracks().forEach(t=>t.stop());
        };
        mediaRecorder.start();
        qs('#btnRec').textContent = '⏹️';
        setTimeout(()=>{ if(mediaRecorder && mediaRecorder.state==='recording'){ mediaRecorder.stop(); qs('#btnRec').textContent='🎤'; } }, 120000); // auto stop 2m
      }catch(err){
        console.error(err);
        if(err.name === 'NotAllowedError'){
          alert('Akses mikrofon ditolak. Izinkan akses mikrofon untuk browser ini.');
        } else if(err.name === 'NotFoundError'){
          alert('Mikrofon tidak ditemukan.');
        } else {
          alert('Terjadi kesalahan saat mengakses mikrofon: ' + err.message);
        }
      }
      mediaRecorder.addEventListener('stop', ()=> qs('#btnRec').textContent = '🎤');
    }

    // Emoji
    function insertEmoji(e){ const box=qs('#msgBox'); const s=box.selectionStart||box.value.length; const v=box.value; box.value=v.slice(0,s)+e+v.slice(s); box.focus(); }

    // Reply bar
    function setReplyBar(m){
      replyTo = { id:m.id, sender_name:m.sender_name, content:m.content, content_type:m.content_type, file_path:m.file_path, deleted:m.deleted };
      const bar = qs('#replyBar');
      const preview = m.deleted ? '<i>Pesan dihapus</i>' : (m.content_type !== 'text' ? `🖼️ ${m.content_type.toUpperCase()}` : escapeHtml(m.content || ''));
      bar.innerHTML = `
        <div style="flex:1; overflow:hidden;">
          <div style="font-weight:600; color:var(--accent);">${escapeHtml(m.sender_name)}</div>
          <div style="font-size:13px; color:var(--muted);">${preview}</div>
        </div>
        <button class="icon-btn" onclick="clearReplyBar()" style="font-size:18px;">✕</button>`;
      bar.style.display = 'flex';
      qs('#msgBox').focus();
    }
    function clearReplyBar(){ replyTo=null; const bar=qs('#replyBar'); bar.style.display='none'; bar.innerHTML=''; }

    // Edit Mode
    function enterEditMode(m) {
        if (isEditing) exitEditMode(); // Exit previous edit mode if any
        isEditing = true;
        editTarget = m;
        clearReplyBar(); // Cannot reply and edit at the same time

        const bar = qs('#editBar');
        bar.innerHTML = `
            <div style="flex:1; overflow:hidden;">
                <div style="font-weight:600; color:#f59e0b;">✏️ Edit Pesan</div>
                <div style="font-size:13px; color:var(--muted);">${escapeHtml(m.content || '')}</div>
            </div>
            <button class="icon-btn" onclick="exitEditMode()" style="font-size:18px;">✕</button>`;
        bar.style.display = 'flex';

        const msgBox = qs('#msgBox');
        msgBox.value = m.content || '';
        msgBox.focus();
        autoResizeTextarea();
    }

    function exitEditMode() {
        isEditing = false; editTarget = null; qs('#editBar').style.display = 'none'; qs('#msgBox').value = ''; autoResizeTextarea();
    }

    // Forward
    async function openForward() {
        if (!forwardMsg) return;

        const listContainer = qs('#forwardConvList');
        listContainer.innerHTML = ''; // Clear previous list

        // Combine peers and groups into one list for forwarding
        const destinations = [
            ...conversations.peers.map(p => ({ type: 'direct', id: p.id, name: p.display_name, avatar: p.avatar_path })),
            ...conversations.groups.map(g => ({ type: 'group', id: g.id, name: g.name, avatar: null }))
        ];

        destinations.forEach(dest => {
            const item = el(`<div class="user-checkbox-item" style="display:flex; align-items:center; gap:12px; padding:8px; border-radius:6px; cursor:pointer;"><input type="checkbox" data-type="${dest.type}" value="${dest.id}" style="width:18px; height:18px;"><div class="avatar" style="width:32px; height:32px; font-size:14px;">${avatarHTML(dest.avatar, dest.name)}</div><span>${escapeHtml(dest.name)}</span></div>`);
            item.onclick = (e) => { if (e.target.type !== 'checkbox') item.querySelector('input').click(); };
            listContainer.appendChild(item);
        });

        qs('#forwardSearch').oninput = (e) => {
            const query = e.target.value.toLowerCase();
            qsa('.user-checkbox-item', listContainer).forEach(item => {
                const name = item.textContent.toLowerCase();
                item.style.display = name.includes(query) ? 'flex' : 'none';
            });
        };

        qs('#forwardConfirm').onclick = () => {
            const selected = qsa('input[type="checkbox"]:checked', listContainer);
            if (selected.length === 0) {
                alert('Pilih minimal satu tujuan.');
                return;
            }

            selected.forEach(checkbox => {
                const destType = checkbox.dataset.type;
                const destId = parseInt(checkbox.value);
                const payload = { content: forwardMsg.content || '', content_type: forwardMsg.content_type, file_path: forwardMsg.file_path, forwarded_from: forwardMsg.id, chat_type: destType, ...(destType === 'direct' ? { peer_id: destId } : { group_id: destId }) };
                socket.emit('send_message', payload);
            });

            showModal('forwardModal', false);
            forwardMsg = null; // Clear after forwarding
            alert(`${selected.length} pesan berhasil diteruskan.`);
        };

        qs('#forwardCancel').onclick = () => showModal('forwardModal', false);
        showModal('forwardModal', true);
    }

    // Stars
    async function openStarred(){
      const list = await fetch('/api/stars').then(r=>r.json());
      const box = qs('#starList'); box.innerHTML='';
      if(!list.length){ box.innerHTML='<div style="color:#8696a0;">Belum ada pesan berbintang.</div>'; showModal('starModal', true); return; }
      list.forEach(m=>{
        const row = el('<div style="padding:8px 0; border-bottom:1px dashed #2a3942;"></div>');
        row.innerHTML = `<div style="font-size:12px; color:#94a3b8;">${escapeHtml(m.sender_name)} • ${fmtTime(m.created_at)} • ${m.chat_type==='group'?'Grup #'+m.group_id:'Direct'}</div>` +
                        `<div>${escapeHtml(m.content|| (m.content_type||'').toUpperCase())}</div>`;
        box.appendChild(row);
      });
      showModal('starModal', true);
    }

    // Pin conversation
    async function togglePinCurrent(){
      if(!currentChat) return;
      await fetch('/api/pin',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({chat_type: currentChat.type, ref_id: currentChat.id, toggle:true})});
      await refreshConversations(); renderConversations(); await updatePinButton();
    }

    // Export chat
    function doExport(){
      if(!currentChat) return;
      const q = currentChat.type==='direct' ? `?chat_type=direct&peer_id=${currentChat.id}` : `?chat_type=group&group_id=${currentChat.id}`;
      window.open('/export'+q, '_blank');
    }

    // Typing
    function debounce(fn, ms=300){ let t; return (...args)=>{ clearTimeout(t); t=setTimeout(()=>fn(...args), ms); }; }
    function debounceTyping(isTyping){
      if(!currentChat) return;
      clearTimeout(typingTimer);
      if(isTyping){
        if(!typingState){ typingState=true; socket.emit('typing', currentChat.type==='direct'?{chat_type:'direct', peer_id: currentChat.id, typing:true}:{chat_type:'group', group_id: currentChat.id, typing:true}); }
        typingTimer = setTimeout(()=>{ typingState=false; socket.emit('typing', currentChat.type==='direct'?{chat_type:'direct', peer_id: currentChat.id, typing:false}:{chat_type:'group', group_id: currentChat.id, typing:false}); }, 1200);
      } else {
        typingState=false; socket.emit('typing', currentChat.type==='direct'?{chat_type:'direct', peer_id: currentChat.id, typing:false}:{chat_type:'group', group_id: currentChat.id, typing:false});
      }
    }

    // Mentions
    async function ensureGroupMembers(gid){
      if(groupMembersCache[gid]) return groupMembersCache[gid];
      // pakai /api/groups (GET) + /api/users? -> untuk demo, ambil semua users lalu filter via server? Lebih mudah tambahkan endpoint,
      // tapi untuk sederhana, kita pakai search lokal: panggil /api/users dan tampilkan semua (cukup untuk demo).
      const users = await fetch('/api/users').then(r=>r.json());
      groupMembersCache[gid] = users; return users;
    }
    function handleMentionSuggest(){
      if(currentChat?.type!=='group') return hideMentionSuggest();
      const box = qs('#msgBox'); const val = box.value; const pos = box.selectionStart;
      const textBefore = val.slice(0, pos);
      const at = textBefore.lastIndexOf('@');
      if(at === -1) return hideMentionSuggest();
      const token = textBefore.slice(at+1);
      if(/\s/.test(token)) return hideMentionSuggest();
      // show suggest
      const sug = qs('#mentionSuggest'); sug.innerHTML='';
      const gid = currentChat.id;
      const list = (groupMembersCache[gid]||[]).filter(u => (u.username||'').toLowerCase().startsWith(token.toLowerCase())).slice(0,10);
      if(!list.length){ hideMentionSuggest(); return; }
      list.forEach(u=>{
        const it = el(`<div class="sitem">@${escapeHtml(u.username)}</div>`);
        it.onclick = ()=>{ insertMentionAt(box, at, token.length+1, '@'+u.username ); hideMentionSuggest(); };
        sug.appendChild(it);
      });
      const rect = box.getBoundingClientRect();
      sug.style.left = rect.left + 120 + 'px'; // simple pos
      sug.style.bottom = (window.innerHeight - rect.top + 8) + 'px';
      sug.style.display = 'block'; mentionOpen = true;
    }
    function insertMentionAt(input, atIndex, len, mention){
      const v = input.value; input.value = v.slice(0, atIndex) + mention + ' ' + v.slice(atIndex+len); input.focus();
    }
    function hideMentionSuggest(){ const s=qs('#mentionSuggest'); s.style.display='none'; mentionOpen=false; }

    function highlightMentions(text){
      if (!text) return '';
      // highlight tokens starting with @ (no-space names)
      return escapeHtml(text).replace(/@([A-Za-z0-9_]+)/g, '<span class="mention">@$1</span>');
    }

    function autoResizeTextarea() {
      const textarea = qs('#msgBox');
      if (textarea) {
        textarea.style.height = 'auto';
        textarea.style.height = Math.min(textarea.scrollHeight, 150) + 'px'; // Max 150px
      }
    }

    function showGroupInfoModal(gid){
      fetch('/api/group_info/'+gid).then(r=>r.json()).then(group => {
        const isOwner = group.owner_id === me.id;
        qs('#groupInfoName').textContent = group.name;
      qs('#groupInfoAvatar').innerHTML = avatarHTML(group.avatar_path, group.name, group.id);
        const list = qs('#groupInfoMembersList');
        list.innerHTML = '';
        group.members.forEach(m => {
          const div = document.createElement('div');
          div.style.display = 'flex';
          div.style.alignItems = 'center';
          div.style.gap = '12px';
          div.style.padding = '10px 0';
        div.innerHTML = `<div class="avatar" style="width:40px; height:40px; font-size:16px;">${avatarHTML(m.avatar_path, m.display_name, m.id)}</div> <div style="flex:1;"><div>${escapeHtml(m.display_name)}</div>${m.role === 'owner' ? '<small style="color:var(--accent);">Owner</small>' : ''}</div>`;
          list.appendChild(div);
        });
        qs('#groupInfoMemberCount').textContent = `${group.members.length} anggota`;

        qs('#btnGroupRename').style.display = isOwner ? 'flex' : 'none';
        qs('#btnGroupAvatar').style.display = isOwner ? 'inline-block' : 'none';
        qs('#groupOwnerActions').style.display = isOwner ? 'block' : 'none';

        if(isOwner){
          qs('#groupOwnerActions').style.display = 'block';
          qs('#btnGroupRename').onclick = () => { const newName = prompt('New name:', group.name); if(newName) renameGroup(gid, newName); };
          qs('#btnGroupAvatar').onclick = () => {
            const input = document.createElement('input');
            input.type = 'file';
            input.accept = 'image/*';
            input.onchange = (e) => {
              const file = e.target.files[0];
              if (file) {
                uploadGroupAvatar(file, gid);
              }
            };
            input.click();
          };
      qs('#btnAddMembers').onclick = () => { showAddMembersModal(gid); };
          qs('#btnRemoveMembers').onclick = () => { showGroupModalForRemove(gid); };
          qs('#btnLeaveOrDeleteGroup').textContent = '🗑️ Hapus Grup';
          qs('#btnLeaveOrDeleteGroup').onclick = () => { if(confirm('Yakin ingin menghapus grup ini secara permanen?')) deleteGroup(gid); };
        } else {
          qs('#btnLeaveOrDeleteGroup').textContent = '🚪 Keluar dari Grup';
          qs('#btnLeaveOrDeleteGroup').onclick = () => { if(confirm('Yakin ingin keluar dari grup ini?')) leaveGroup(gid); };
        }
        showModal('groupInfoModal', true);
      });
    }

    function renameGroup(gid, newName){
      if (!newName || !newName.trim()) { alert('Name cannot be empty'); return; }
      fetch('/api/groups', {
        method:'PUT',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({group_id:gid, action:'rename', name:newName.trim()})
      }).then(r=>r.json()).then(res => {
        alert('Renamed');
        showModal('groupInfoModal', false);
        if(currentChat && currentChat.id===gid){
          currentChat.title = newName;
          qs('#chatTitle').textContent = newName;
        }
        refreshConversations(); renderConversations();
      });
    }

    async function showAddMembersModal(gid) {
      showModal('groupInfoModal', false); // Close the info modal first
      const listContainer = qs('#addMembersUserList');
      listContainer.innerHTML = 'Memuat...';
      showModal('addMembersModal', true);

      try {
        const [allUsers, groupInfo] = await Promise.all([
          fetch('/api/users').then(r => r.json()),
          fetch('/api/group_info/' + gid).then(r => r.json())
        ]);

        const memberIds = new Set(groupInfo.members.map(m => m.id));
        const usersToAdd = allUsers.filter(u => !memberIds.has(u.id));

        listContainer.innerHTML = '';
        if (usersToAdd.length === 0) {
          listContainer.innerHTML = '<div style="text-align:center; color:var(--muted); padding:20px;">Semua user sudah menjadi anggota.</div>';
        } else {
          usersToAdd.forEach(u => {
            const item = el(`<div class="user-checkbox-item" style="display:flex; align-items:center; gap:12px; padding:8px; border-radius:6px; cursor:pointer;"><input type="checkbox" value="${u.id}" style="width:18px; height:18px;"><div class="avatar" style="width:32px; height:32px; font-size:14px;">${avatarHTML(u.avatar_path, u.display_name)}</div><span>${escapeHtml(u.display_name)}</span></div>`);
            item.onclick = (e) => { if(e.target.type !== 'checkbox') item.querySelector('input').click(); };
            listContainer.appendChild(item);
          });
        }

        qs('#addMemberSearch').oninput = (e) => {
          const query = e.target.value.toLowerCase();
          qsa('.user-checkbox-item', listContainer).forEach(item => {
            const name = item.textContent.toLowerCase();
            item.style.display = name.includes(query) ? 'flex' : 'none';
          });
        };

        qs('#addMembersConfirm').onclick = async () => {
          const selectedIds = qsa('input[type="checkbox"]:checked', listContainer).map(cb => parseInt(cb.value));
          if (selectedIds.length > 0) {
            await fetch(`/api/groups/${gid}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'add_members', user_ids: selectedIds }) });
            showModal('addMembersModal', false);
            alert(`${selectedIds.length} anggota berhasil ditambahkan.`);
          }
        };
        qs('#addMembersCancel').onclick = () => showModal('addMembersModal', false);
      } catch (err) {
        listContainer.innerHTML = '<div style="color:var(--danger);">Gagal memuat daftar user.</div>';
      }
    }

    async function showGroupModalForRemove(gid) {
        showModal('groupInfoModal', false); // Close the info modal first
        const listContainer = qs('#removeMembersUserList');
        listContainer.innerHTML = 'Memuat...';
        showModal('removeMembersModal', true);

        try {
            const groupInfo = await fetch('/api/group_info/' + gid).then(r => r.json());
            const membersToRemove = groupInfo.members.filter(m => m.id !== me.id);

            listContainer.innerHTML = '';
            if (membersToRemove.length === 0) {
                listContainer.innerHTML = '<div style="text-align:center; color:var(--muted); padding:20px;">Tidak ada anggota lain untuk dihapus.</div>';
            } else {
                membersToRemove.forEach(m => {
                    const item = el(`<div class="user-checkbox-item" style="display:flex; align-items:center; gap:12px; padding:8px; border-radius:6px; cursor:pointer;"><input type="checkbox" value="${m.id}" style="width:18px; height:18px;"><div class="avatar" style="width:32px; height:32px; font-size:14px;">${avatarHTML(m.avatar_path, m.display_name, m.id)}</div><span>${escapeHtml(m.display_name)}</span></div>`);
                    item.onclick = (e) => { if (e.target.type !== 'checkbox') item.querySelector('input').click(); };
                    listContainer.appendChild(item);
                });
            }

            qs('#removeMembersConfirm').onclick = async () => {
                const selectedIds = qsa('input[type="checkbox"]:checked', listContainer).map(cb => parseInt(cb.value));
                if (selectedIds.length > 0 && confirm(`Yakin ingin menghapus ${selectedIds.length} anggota?`)) {
                    await fetch(`/api/groups/${gid}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'remove_members', user_ids: selectedIds }) });
                    showModal('removeMembersModal', false);
                    alert(`${selectedIds.length} anggota berhasil dihapus.`);
                }
            };
            qs('#removeMembersCancel').onclick = () => showModal('removeMembersModal', false);
        } catch (err) {
            listContainer.innerHTML = '<div style="color:var(--danger);">Gagal memuat daftar anggota.</div>';
        }
    }

    function deleteGroup(gid){
      fetch(`/api/groups/${gid}`, {
        method:'PUT', // Or DELETE, but we'll use PUT to match the new endpoint structure
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({action:'delete_group'})
      }).then(async r => {
        if(r.ok){
            showModal('groupInfoModal', false);
            await refreshConversations();
            renderConversations();
            alert('Grup berhasil dihapus.');
        } else {
            const err = await r.json();
            alert(err.error || 'Gagal menghapus grup.');
        }
      });
    }

    async function leaveGroup(gid) {
        try {
            const res = await fetch(`/api/groups/${gid}`, { method: 'POST', headers: { 'Content-Type': 'application/json' } });
            const data = await res.json();
            if (res.ok) {
                alert('Anda telah keluar dari grup.');
                showModal('groupInfoModal', false);
                await refreshConversations();
                renderConversations();
                // If currently viewing the group, close it
                if (currentChat && currentChat.type === 'group' && currentChat.id === gid) {
                    currentChat = null;
                    qs('#messages').innerHTML='<div class="section">Grup ini telah ditinggalkan.</div>';
                    qs('#chatTitle').textContent='Pilih percakapan';
                    qs('#chatSub').textContent = '';
                    setComposerEnabled(false);
                }
            } else {
                alert(data.error || 'Gagal keluar dari grup.');
            }
        } catch (err) { alert('Terjadi kesalahan.'); }
    }

    async function loadAndDisplayPinnedMessages() {
      try {
        const res = await fetch('/api/pinned_messages/' + currentChat.id);
        if (!res.ok) return;
        const pinnedMessages = await res.json();
        currentPinnedMessages = pinnedMessages;

        pinnedMessageIds = {};
        pinnedMessages.forEach(pm => {
          // Fetch full message details for the pinned message
          if (pm.message_id) {
            pinnedMessageIds[pm.message_id] = true;
          }
        });

        let pinnedContainer = qs('#pinnedMessagesContainer');
        if (!pinnedContainer) {
          pinnedContainer = el('<div id="pinnedMessagesContainer" style="background: var(--panel); border-bottom: 1px solid var(--border); padding: 12px; position: sticky; top: 0; z-index: 10;"></div>');
          qs('.chatbar').insertAdjacentElement('afterend', pinnedContainer);
        }

        if (pinnedMessages.length === 0 || currentChat?.type !== 'group') {
          pinnedContainer.style.display = 'none';
        } else {
          pinnedContainer.style.display = 'block';
          let html = `<div style="background: var(--panel); padding: 4px 12px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 8px; font-size: 12px; font-weight: 600; color: var(--text);"><span>📌</span><span style="flex: 1;">Pinned Messages</span></div>`;
          html += `<div style="padding: 6px 12px 6px 12px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center;">`;
          pinnedMessages.forEach(pm => {
            const content = pm.deleted ? '✗ Pesan dihapus' : pm.content_type === 'image' ? '📷 Gambar' : pm.content_type === 'file' ? '📄 File' : (pm.content || 'Pesan').slice(0, 15) + (pm.content?.length > 15 ? '...' : '');
            html += `<div class="pinned-msg" style="display: flex; align-items: center; gap: 4px; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 4px 6px; max-width: 160px; cursor: pointer; font-size: 11px;" data-mid="${pm.message_id}">`;
            html += `<div class="avatar" style="width: 16px; height: 16px; font-size: 8px; flex-shrink: 0;">${avatarHTML(pm.sender_avatar, pm.sender_name)}</div>`;
            html += `<div style="flex: 1; min-width: 0;">`;
            html += `<div style="font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; line-height: 1.2;">${escapeHtml(pm.sender_name)}</div>`;
            html += `<div style="color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; line-height: 1.2;">${escapeHtml(content)}</div>`;
            html += `</div>`;
            html += `<button onclick="unpinMessage(${pm.message_id}); event.stopPropagation();" style="background:none; border:none; color:var(--muted); font-size:12px; cursor:pointer; padding:1px; line-height: 1; margin-left: 2px;">×</button>`;
            html += `</div>`;
          });
          html += `</div>`;
          pinnedContainer.innerHTML = html;

          // Add click event listeners to pinned messages
          const pinnedMsgs = pinnedContainer.querySelectorAll('.pinned-msg');
          pinnedMsgs.forEach(msg => {
            msg.addEventListener('click', () => {
              const mid = parseInt(msg.dataset.mid);
              scrollToMessage(mid);
            });
          });
        }
      } catch (err) {
        console.error('Error loading pinned messages:', err);
      }
    }

    // Unpin a message
    async function unpinMessage(messageId) {
      try {
        const res = await fetch('/api/pin_message', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message_id: messageId, action: 'unpin' })
        });
        if (res.ok) {
          // Update local pinnedMessageIds
          delete pinnedMessageIds[messageId];
          // Update buttons on existing bubbles
          const loadedMessages = qsa('#messages .bubble');
          loadedMessages.forEach(bubble => {
            const mid = bubble.dataset.mid;
            if (mid == messageId) {
              const buttons = bubble.querySelectorAll('.act-btn');
              buttons.forEach(btn => {
                if(btn.textContent === 'Unpin') {
                  btn.textContent = 'Pin';
                }
              });
            }
          });
          // Reload pinned display
          await loadAndDisplayPinnedMessages();
          showToast('Pesan berhasil dilepaskan.');
        } else {
          alert('Gagal unpin pesan');
        }
      } catch (err) {
        console.error('Error unpinning message:', err);
      }
    }

    function setupGroupModal() {
      qs('#btnNewGroup').onclick = () => {
        preloadUsersToGroupModal(); showModal('groupModal', true);
      };

      // Attach btnAttach and btnDoc
      if(qs('#btnAttach')) qs('#btnAttach').onclick = () => {
        const m = qs('#attachMenu');
        if(m) m.style.display = m.style.display === 'flex' ? 'none' : 'flex';
      };
      if(qs('#btnDoc')) qs('#btnDoc').onclick = () => {
        chooseFile();
        hideAttachMenu();
      };
      if(qs('#btnCam')) qs('#btnCam').onclick = () => {
        chooseImage();
        hideAttachMenu();
      };
      if(qs('#btnVoice')) qs('#btnVoice').onclick = toggleRecord;
      if(qs('#btnVoice')) qs('#btnVoice').onclick = () => {
        toggleRecord();
        hideAttachMenu();
      };
      if(qs('#btnSticker')) qs('#btnSticker').onclick = () => {
        isStickerMode = true;
        chooseImage();
        hideAttachMenu();
      };

      // Preview modal buttons
      qs('#previewCancel').onclick = () => showModal('previewModal', false);
      qs('#previewSend').onclick = () => uploadAndSend();
      qs('#msgBox').addEventListener('input', () => { debounceTyping(true); handleMentionSuggest(); });
qs('#msgBox').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          sendOrPreview();
        }
      });

      // Set send button click
      qs('#btnSend').onclick = sendOrPreview;
      qs('#msgBox').addEventListener('blur', () => debounceTyping(false));

      // New Group
      qs('#grpCreate').onclick = createGroup;
      qs('#btnNextStep').onclick = () => {
        qs('#groupStep1').classList.remove('active');
        qs('#groupStep2').classList.add('active');
        preloadUsersToGroupModal();
      };
      qs('#btnBackStep').onclick = () => {
        qs('#groupStep1').classList.add('active');
        qs('#groupStep2').classList.remove('active');
      };
      qs('#grpCancel').onclick = () => showModal('groupModal', false);
      qs('#grpCancel2').onclick = () => showModal('groupModal', false);

      // Handle group avatar selection
      qs('#groupAvatarInput').onchange = (e) => {
        const file = e.target.files[0];
        if (file) {
          groupAvatarFile = file;
          qs('#groupAvatarPreview').innerHTML = `<img src="${URL.createObjectURL(file)}" style="width:100%; height:100%; border-radius:50%; object-fit:cover;">`;
        }
      };
      qs('#grpCancel').onclick = () => showModal('groupModal', false);
      qs('#grpCancel2').onclick = () => showModal('groupModal', false);
    }

    async function uploadGroupAvatar(file, gid) {
      const formData = new FormData();
      formData.append('file', file);
      const res = await fetch(`/api/group_avatar/${gid}`, {
        method: 'POST',
        body: formData
      });
      if (res.ok) {
        const data = await res.json();
        // Update the avatar in the modal and in conversations
        document.getElementById('groupInfoAvatar').innerHTML = `<img src="/uploads/${data.avatar_path}?v=${Date.now()}" style="width:100%; height:100%; border-radius:50%;">`;
        // Update in conversations list if visible
        if (currentChat && currentChat.id === gid) {
          qs('#chatAvatar').innerHTML = `<img src="/uploads/${data.avatar_path}?v=${Date.now()}" style="width:100%; height:100%; border-radius:50%;">`;
          currentChat.avatar_path = data.avatar_path; // Update currentChat
        }
        alert('Avatar grup berhasil diperbarui!');
        // Refresh conversations to update avatar in list
        await refreshConversations();
        renderConversations();
      } else {
        alert('Gagal upload avatar grup.');
      }
    }

    function showImageModal(src) {
        const modal = qs('#imageModal');
        const modalImg = qs('#imageModalContent');
        modalImg.src = src;
        showModal('imageModal', true);
    }

    // Close image modal
    qs('#imageModal').onclick = (e) => {
        if (e.target.id !== 'imageModalContent') {
            showModal('imageModal', false);
        }
    };

    // Video Call Functions
    let callNotepadContent = ''; // Store notepad content for current call

    // Notepad functions
    function updateNotepad(content) {
      if (!callInProgress) return;
      callNotepadContent = content;
      // Send changes to other participant
      socket.emit('notepad_edit', {
        to: callTargetUserId,
        content: content,
        timestamp: Date.now()
      });
    }

    function toggleNotepad() {
      const notepad = qs('#liveNotepad');
      const btn = qs('#btnToggleNotepad');
      if (notepad.style.display === 'flex') {
        notepad.style.display = 'none';
        btn.textContent = '📝 Toggle Notepad';
      } else {
        notepad.style.display = 'flex';
        btn.textContent = '📝 Hide Notepad';
      }
    }

    async function startVideoCall() {
      if (!currentChat || currentChat.type !== 'direct') {
        alert('Video call requires a direct chat.');
        return;
      }

      try {
        localStream = await navigator.mediaDevices.getUserMedia({
          video: true,
          audio: true,
        });

        // Reset notepad content for new call
        callNotepadContent = '';
        qs('#callNotepad').value = '';

        // Create peer connection
        peerConnection = new RTCPeerConnection(configuration);

        // Add local stream to peer connection
        localStream.getTracks().forEach(track => peerConnection.addTrack(track, localStream));

        // Set up remote stream
        peerConnection.ontrack = (event) => {
          console.log('Caller ontrack fired:', event);
          if (event.streams && event.streams[0]) {
            remoteStream = event.streams[0];
            console.log('Caller remoteStream tracks:', remoteStream.getTracks());
            qs('#remoteVideo').srcObject = remoteStream;
            qs('#remoteVideo').play().catch(e => console.log('Autoplay blocked caller:', e));
            qs('#callStatus').textContent = 'Receiving video...';
            // Force status to Connected when video is received
            setTimeout(() => {
              if (remoteStream.getVideoTracks().length > 0 || remoteStream.getAudioTracks().length > 0) {
                qs('#callStatus').textContent = 'Connected';
                callStartTime = Date.now(); // Start tracking call duration only when actually connected
                startCallTimer();
              }
            }, 1000);
          }
        };

        // ICE candidate handling
        peerConnection.onicecandidate = (event) => {
          console.log("Caller: ICE candidate generated", event.candidate);
          if (event.candidate) {
            socket.emit('video_call_ice', {
              to: currentChat.id,
              candidate: event.candidate,
            });
          }
        };

        // Create offer and send
        const offer = await peerConnection.createOffer();
        await peerConnection.setLocalDescription(offer);

        // Monitor connection state after local description is set
        peerConnection.onconnectionstatechange = () => {
          console.log('Caller connection state:', peerConnection.connectionState);
          if (peerConnection.connectionState === 'connected') {
            qs('#callStatus').textContent = 'Connected';
            callStartTime = Date.now(); // Start tracking call duration
          } else if (peerConnection.connectionState === 'failed') {
            qs('#callStatus').textContent = 'Connection failed';
            // Optionally end call
            setTimeout(endCall, 2000);
          }
        };

        socket.emit('video_call_offer', {
          to: currentChat.id,
          offer: offer,
        });

        // Set up local video
        qs('#localVideo').srcObject = localStream;
        showModal('videoCallModal', true);
        callTargetUserId = currentChat.id;
        callType = 'outgoing';
        callInProgress = true;

      } catch (err) {
        console.error('Error starting video call:', err);
        alert('Error accessing camera/microphone: ' + err.message);
      }
    }

    async function showVideoCallIncoming(fromUserId, offer) {
      // Check if a call is already in progress
      if (callInProgress) {
        socket.emit('video_call_busy', {to: fromUserId});
        return;
      }

      callTargetUserId = fromUserId;
      callType = 'incoming';

      // Get caller info from conversations data
      const caller = conversations.peers.find(p => p.id == fromUserId);
      const callerName = caller ? caller.display_name : getCallerDisplayName(fromUserId);

      // Enhanced incoming call modal with caller info
      const incomingModal = el(`<div id="incomingCallModal" class="modal show shake" style="backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);">
        <div class="dialog" style="width: 420px; padding: 0; overflow: hidden; border-radius: 16px; box-shadow: 0 20px 40px rgba(0,0,0,0.3);">
          <!-- Header with caller avatar -->
          <div style="background: linear-gradient(135deg, var(--accent), rgba(var(--accent), 0.8)); padding: 30px 24px; text-align: center;">
            <div style="margin: 0 auto 16px; width: 96px; height: 96px; border-radius: 50%; overflow: hidden; border: 4px solid rgba(255,255,255,0.3); position: relative;">
              <div class="avatar" style="width: 100%; height: 100%; background: var(--bg);">${avatarHTML(caller?.avatar_path, callerName)}</div>
              <div style="position: absolute; bottom: 8px; right: 8px; width: 24px; height: 24px; background: #22c55e; border-radius: 50%; border: 3px solid var(--accent);"></div>
            </div>
            <h3 style="margin: 0; color: white; font-size: 24px; font-weight: 600; text-shadow: 0 2px 4px rgba(0,0,0,0.1);">Video Call Incoming</h3>
            <p style="margin: 8px 0 0; color: rgba(255,255,255,0.9); font-size: 16px; opacity: 0.9;">From ${callerName || 'User ' + fromUserId}</p>
            ${caller?.bio ? `<p style="margin: 6px 0 0; color: rgba(255,255,255,0.8); font-size: 14px; font-style: italic;">"${caller.bio}"</p>` : ''}
          </div>

          <!-- Action buttons -->
          <div style="padding: 24px; background: var(--panel); display: flex; gap: 16px; justify-content: center;">
            <button id="acceptCallBtn" class="btn" style="background: linear-gradient(135deg, var(--accent), rgba(var(--accent), 0.8)); border: none; padding: 12px 32px; border-radius: 50px; color: white; font-weight: 600; font-size: 16px; cursor: pointer; transition: all 0.3s ease; box-shadow: 0 4px 12px rgba(var(--accent), 0.3);">
              📞 Accept Call
            </button>
            <button class="btn sec" onclick="rejectVideoCall()" style="background: var(--input); border: 2px solid var(--border); padding: 12px 32px; border-radius: 50px; font-weight: 600; font-size: 16px; cursor: pointer; transition: all 0.3s ease;">
              ❌ Reject
            </button>
          </div>

          <!-- Auto-reject countdown -->
          <div style="text-align: center; padding: 8px 24px 16px; background: var(--panel2); font-size: 12px; color: var(--muted);">
            Call will be rejected in <span id="countdown" style="font-weight: 600; color: var(--text);">30</span> seconds
          </div>
        </div>
      </div>`);
      document.body.appendChild(incomingModal);

      qs('#acceptCallBtn').onclick = () => acceptVideoCall(fromUserId, offer);

      // Auto-reject countdown
      let timeLeft = 30;
      const countdownEl = qs('#countdown');
      const countdownInterval = setInterval(() => {
        timeLeft--;
        if (countdownEl) countdownEl.textContent = timeLeft;
        if (timeLeft <= 0) {
          clearInterval(countdownInterval);
          if (!callInProgress) {
            rejectVideoCall();
          }
        }
      }, 1000);

      // Auto-reject after 30 seconds if modal still exists
      setTimeout(() => {
        clearInterval(countdownInterval);
        if (!callInProgress && qs('#incomingCallModal')) {
          rejectVideoCall();
        }
      }, 31000);
    }

    async function acceptVideoCall(fromUserId, offer) {
      try {
        localStream = await navigator.mediaDevices.getUserMedia({
          video: true,
          audio: true,
        });

        // Remove incoming call modal
        qs('#incomingCallModal').remove();

        callTargetUserId = fromUserId;
        callType = 'incoming';
        callInProgress = true;

        // Create peer connection
        peerConnection = new RTCPeerConnection(configuration);

        // Set up remote stream handler *before* setting remote description
        peerConnection.ontrack = (event) => {
          if (event.streams && event.streams[0]) {
            qs('#remoteVideo').srcObject = event.streams[0];
          }
        };

        // Add local stream
        localStream.getTracks().forEach((track) => {
          peerConnection.addTrack(track, localStream);
        });

        // Set remote description from the offer
        await peerConnection.setRemoteDescription(new RTCSessionDescription(offer));

        // Add queued ICE candidates
        for (const candidate of iceCandidateQueue) {
          if (peerConnection) {
            try {
              await peerConnection.addIceCandidate(candidate);
            } catch (e) {
              console.error('Error adding queued ICE candidate:', e);
            }
          }
        }
        iceCandidateQueue = [];

        // Create answer
        const answer = await peerConnection.createAnswer();
        await peerConnection.setLocalDescription(answer);

        // Monitor connection state after local description is set
        peerConnection.onconnectionstatechange = () => {
          if (peerConnection.connectionState === 'connected') {
            qs('#callStatus').textContent = 'Connected';
          } else if (peerConnection.connectionState === 'failed') {
            qs('#callStatus').textContent = 'Connection failed';
            setTimeout(endCall, 2000);
          }
        };

        socket.emit('video_call_answer', {
          to: fromUserId,
          answer: answer,
        });

        // ICE candidate handling
        peerConnection.onicecandidate = (event) => {
          console.log("Receiver: ICE candidate generated", event.candidate);
          if (event.candidate) {
            socket.emit('video_call_ice', {
              to: fromUserId,
              candidate: event.candidate,
            });
          }
        };

        // Set up local video
        qs('#localVideo').srcObject = localStream;
        showModal('videoCallModal', true);

      } catch (err) {
        console.error('Error accepting video call:', err);
        alert('Error accepting call: ' + err.message);
      }
    }

    function rejectVideoCall() {
      socket.emit('video_call_end', {to: callTargetUserId});
      if (qs('#incomingCallModal')) {
        qs('#incomingCallModal').remove();
      }
      // Send system message about call rejection to the chat
      socket.emit('send_message', {
        chat_type: 'direct',
        content: '📞 Panggilan video ditolak',
        content_type: 'system',
        peer_id: callTargetUserId
      });
      callInProgress = false;
      callTargetUserId = null;
      callType = null;
    }

    function endCall() {
      if (peerConnection) {
        peerConnection.close();
        peerConnection = null;
      }
      if (localStream) {
        localStream.getTracks().forEach(track => track.stop());
        localStream = null;
        qs('#localVideo').srcObject = null;
      }
      if (remoteStream) {
        remoteStream.getTracks().forEach(track => track.stop());
        remoteStream = null;
        qs('#remoteVideo').srcObject = null;
      }

      // Send call duration message if call actually connected
      if (callStartTime && currentChat) {
        const duration = Date.now() - callStartTime;
        const minutes = Math.floor(duration / 60000);
        const seconds = Math.floor((duration % 60000) / 1000);
        const durationText = `${minutes} menit ${seconds} detik`;
        socket.emit('send_message', {
          chat_type: currentChat.type,
          content: `📞 Panggilan video berakhir setelah ${durationText}.`,
          content_type: 'system',
          ...(currentChat.type === 'direct' ? { peer_id: currentChat.id } : { group_id: currentChat.id })
        });
      }

      socket.emit('video_call_end', {to: callTargetUserId});

      showModal('videoCallModal', false);
      callInProgress = false;
      callTargetUserId = null;
      callType = null;
      callStartTime = null;
    }

    async function takePhotoDuringCall() {
      if (!localStream || !callInProgress) {
        alert('Tidak ada panggilan video aktif.');
        return;
      }

      try {
        // Create canvas to capture video frame
        const canvas = document.createElement('canvas');
        const video = qs('#localVideo');
        canvas.width = video.videoWidth || 640;
        canvas.height = video.videoHeight || 480;

        const ctx = canvas.getContext('2d');
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

        // Convert canvas to blob
        canvas.toBlob(async (blob) => {
          if (!blob) {
            alert('Gagal mengambil foto.');
            return;
          }

          // Create file from blob
          const photoFile = new File([blob], `call_photo_${Date.now()}.jpg`, { type: 'image/jpeg' });

          // Upload and send as message
          const fd = new FormData();
          fd.append('file', photoFile);

          const up = await fetch('/upload', { method: 'POST', body: fd }).then(r => r.json());
          if (!up.ok) {
            alert(up.error || 'Gagal mengunggah foto');
            return;
          }

          // Send as message in current chat
          sendMessage('', 'image', up.path);

          // Show success feedback
          const btn = qs('#btnTakePhoto');
          const originalText = btn.textContent;
          btn.textContent = '📸 ✓';
          btn.style.background = 'var(--accent)';
          setTimeout(() => {
            btn.textContent = originalText;
          }, 1500);

        }, 'image/jpeg', 0.9); // High quality JPEG

      } catch (err) {
        console.error('Error taking photo:', err);
        alert('Gagal mengambil foto: ' + err.message);
      }
    }

    async function sharePhotoDuringCall() {
      if (!callInProgress) {
        alert('Tidak ada panggilan video aktif.');
        return;
      }

      if (!currentChat) {
        alert('Tidak ada percakapan aktif.');
        return;
      }

      try {
        // Create photo input element
        const photoInput = document.createElement('input');
        photoInput.type = 'file';
        photoInput.multiple = false;
        photoInput.accept = 'image/*'; // Only images
        photoInput.capture = 'environment'; // Prefer camera on mobile

        photoInput.onchange = async (e) => {
          const file = e.target.files[0];
          if (!file) return;

          // Check file size (10MB limit for photos)
          if (file.size > 10 * 1024 * 1024) {
            alert('Foto terlalu besar. Maksimal 10MB.');
            return;
          }

          // Show loading feedback
          const btn = qs('#btnSharePhoto');
          const originalText = btn.textContent;
          btn.textContent = '🖼️ ⏳';
          btn.disabled = true;

          try {
            // Upload the photo
            const fd = new FormData();
            fd.append('file', file);

            const up = await fetch('/upload', { method: 'POST', body: fd }).then(r => r.json());
            if (!up.ok) {
              alert(up.error || 'Gagal mengunggah foto');
              return;
            }

            // Send as image message in current chat
            sendMessage('', 'image', up.path);

            // Show success feedback
            btn.textContent = '🖼️ ✓';
            btn.style.background = 'var(--accent)';
            setTimeout(() => {
              btn.textContent = originalText;
              btn.disabled = false;
              btn.style.background = '';
            }, 1500);

          } catch (err) {
            console.error('Error sharing photo:', err);
            alert('Gagal membagikan foto: ' + err.message);
            btn.textContent = originalText;
            btn.disabled = false;
          }
        };

        // Trigger photo picker/camera
        photoInput.click();

      } catch (err) {
        console.error('Error in sharePhotoDuringCall:', err);
        alert('Gagal membuka kamera/file picker: ' + err.message);
      }
    }

    async function shareFileDuringCall() {
      if (!callInProgress) {
        alert('Tidak ada panggilan video aktif.');
        return;
      }

      if (!currentChat) {
        alert('Tidak ada percakapan aktif.');
        return;
      }

      try {
        // Create file input element
        const fileInput = document.createElement('input');
        fileInput.type = 'file';
        fileInput.multiple = false; // Single file for now
        fileInput.accept = '*/*'; // Accept all file types

        fileInput.onchange = async (e) => {
          const file = e.target.files[0];
          if (!file) return;

          // Check file size (30MB limit)
          if (file.size > 30 * 1024 * 1024) {
            alert('File terlalu besar. Maksimal 30MB.');
            return;
          }

          // Show loading feedback
          const btn = qs('#btnShareFile');
          const originalText = btn.textContent;
          btn.textContent = '📎 ⏳';
          btn.disabled = true;

          try {
            // Upload the file
            const fd = new FormData();
            fd.append('file', file);

            const up = await fetch('/upload', { method: 'POST', body: fd }).then(r => r.json());
            if (!up.ok) {
              alert(up.error || 'Gagal mengunggah file');
              return;
            }

            // Determine content type
            let contentType = 'file';
            if (file.type.startsWith('image/')) {
              contentType = 'image';
            } else if (file.type.startsWith('audio/')) {
              contentType = 'audio';
            }

            // Send as message in current chat
            sendMessage('', contentType, up.path);

            // Show success feedback
            btn.textContent = '📎 ✓';
            btn.style.background = 'var(--accent)';
            setTimeout(() => {
              btn.textContent = originalText;
              btn.disabled = false;
              btn.style.background = '';
            }, 1500);

          } catch (err) {
            console.error('Error sharing file:', err);
            alert('Gagal membagikan file: ' + err.message);
            btn.textContent = originalText;
            btn.disabled = false;
          }
        };

        // Trigger file picker
        fileInput.click();

      } catch (err) {
        console.error('Error in shareFileDuringCall:', err);
        alert('Gagal membuka file picker: ' + err.message);
      }
    }

    async function sharePhotoToNotepad() {
      if (!callInProgress) {
        alert('Tidak ada panggilan video aktif.');
        return;
      }

      try {
        // Create photo input element
        const photoInput = document.createElement('input');
        photoInput.type = 'file';
        photoInput.accept = 'image/*';
        photoInput.capture = 'environment'; // Prefer camera on mobile

        photoInput.onchange = async (e) => {
          const file = e.target.files[0];
          if (!file) return;

          // Check file size (10MB limit for notepad photos)
          if (file.size > 10 * 1024 * 1024) {
            alert('Foto terlalu besar. Maksimal 10MB.');
            return;
          }

          // Show loading feedback
          const btn = qs('#btnSharePhotoToNotepad');
          const originalText = btn.textContent;
          btn.textContent = '📸 ⏳';
          btn.disabled = true;

          try {
            // Upload the photo
            const fd = new FormData();
            fd.append('file', file);

            const up = await fetch('/upload', { method: 'POST', body: fd }).then(r => r.json());
            if (!up.ok) {
              alert(up.error || 'Gagal mengunggah foto');
              return;
            }

            // Add photo to notepad content and display
            const notepad = qs('#callNotepad');
            const notepadImages = qs('#notepadImages');
            const currentContent = notepad.value || '';
            const photoMarkdown = `\n\n![Shared Photo](/uploads/${up.path})\n\n`;
            const newContent = currentContent + photoMarkdown;

            // Add image to display area
            const imgElement = el(`<img src="/uploads/${up.path}" style="max-width:120px; max-height:120px; border-radius:8px; object-fit:cover; border:2px solid var(--border);" alt="Shared photo" onclick="showImageModal('/uploads/${up.path}')">`);
            notepadImages.appendChild(imgElement);

            // Update notepad and sync
            notepad.value = newContent;
            updateNotepad(newContent);

            // Show success feedback
            btn.textContent = '📸 ✓';
            btn.style.background = 'var(--accent)';
            setTimeout(() => {
              btn.textContent = originalText;
              btn.disabled = false;
              btn.style.background = '';
            }, 1500);

          } catch (err) {
            console.error('Error sharing photo to notepad:', err);
            alert('Gagal membagikan foto ke notepad: ' + err.message);
            btn.textContent = originalText;
            btn.disabled = false;
          }
        };

        // Trigger photo picker/camera
        photoInput.click();

      } catch (err) {
        console.error('Error in sharePhotoToNotepad:', err);
        alert('Gagal membuka kamera/file picker: ' + err.message);
      }
    }

    function toggleFullscreen() {
      const modal = qs('#videoCallModal .dialog');
      if (!document.fullscreenElement) {
        // Enter fullscreen
        if (modal.requestFullscreen) {
          modal.requestFullscreen();
        } else if (modal.mozRequestFullScreen) { // Firefox
          modal.mozRequestFullScreen();
        } else if (modal.webkitRequestFullscreen) { // Chrome, Safari and Edge
          modal.webkitRequestFullscreen();
        } else if (modal.msRequestFullscreen) { // IE/Edge
          modal.msRequestFullscreen();
        }
      } else {
        // Exit fullscreen
        if (document.exitFullscreen) {
          document.exitFullscreen();
        } else if (document.mozCancelFullScreen) { // Firefox
          document.mozCancelFullScreen();
        } else if (document.webkitExitFullscreen) { // Chrome, Safari and Edge
          document.webkitExitFullscreen();
        } else if (document.msExitFullscreen) { // IE/Edge
          document.msExitFullscreen();
        }
      }
    }

    // ==================== STICKER FEATURE JAVASCRIPT ====================
    
    // Sticker state
    let stickerCreatorMode = 'image'; // 'image' or 'text'
    let stickerImageData = null;
    let allUserStickers = [];
    let favoriteStickers = [];
    let recentStickers = [];
    
    // Initialize sticker event handlers
    function initStickerHandlers() {
        // Main sticker button (next to emoji)
        const btnStickerMain = qs('#btnStickerMain');
        if (btnStickerMain) {
            btnStickerMain.onclick = (e) => {
                e.stopPropagation();
                openStickerPicker();
            };
        }
        
        // Sticker picker button in attach menu
        const btnSticker = qs('#btnSticker');
        if (btnSticker) {
            btnSticker.onclick = () => {
                openStickerPicker();
                hideAttachMenu();
            };
        }
        
        // Manage stickers button
        const btnManageStickers = qs('#btnManageStickers');
        if (btnManageStickers) {
            btnManageStickers.onclick = () => {
                openStickerCreator();
            };
        }
        
        // Sticker picker cancel
        const stickerPickerCancel = qs('#stickerPickerCancel');
        if (stickerPickerCancel) {
            stickerPickerCancel.onclick = () => showModal('stickerPickerModal', false);
        }
        
        // Create sticker button
        const btnCreateSticker = qs('#btnCreateSticker');
        if (btnCreateSticker) {
            btnCreateSticker.onclick = () => openStickerCreator();
        }
        
        // Sticker creator cancel buttons
        const stickerCreatorCancel = qs('#stickerCreatorCancel');
        if (stickerCreatorCancel) {
            stickerCreatorCancel.onclick = () => {
                showModal('stickerCreatorModal', false);
                showModal('stickerPickerModal', true);
            };
        }
        
        const stickerCreatorClose = qs('#stickerCreatorClose');
        if (stickerCreatorClose) {
            stickerCreatorClose.onclick = () => {
                showModal('stickerCreatorModal', false);
                showModal('stickerPickerModal', true);
            };
        }
        
        // Save sticker button
        const saveStickerBtn = qs('#saveSticker');
        if (saveStickerBtn) {
            saveStickerBtn.onclick = () => saveSticker();
        }
        
        // Sticker tabs
        qsa('.sticker-tab-btn').forEach(btn => {
            btn.onclick = () => {
                const tab = btn.dataset.tab;
                qsa('.sticker-tab-btn').forEach(b => {
                    b.style.background = 'var(--input)';
                    b.style.color = 'var(--text)';
                    b.style.border = '1px solid var(--border)';
                });
                btn.style.background = 'var(--accent)';
                btn.style.color = 'white';
                btn.style.border = 'none';
                renderStickerGrid(tab);
            };
        });
        
        // Sticker search
        const stickerSearch = qs('#stickerSearch');
        if (stickerSearch) {
            stickerSearch.oninput = (e) => {
                const query = e.target.value.toLowerCase().trim();
                if (!query) {
                    renderStickerGrid('all');
                    return;
                }
                
                const filtered = allUserStickers.filter(s => {
                    const tags = (s.tags || []).join(' ').toLowerCase();
                    return tags.includes(query);
                });
                
                const grid = qs('#stickerGrid');
                if (filtered.length === 0) {
                    grid.innerHTML = '<div style="grid-column:1/-1; text-align:center; color:var(--muted); padding:40px 20px;"><div style="font-size:48px; margin-bottom:16px;">🔍</div><div style="font-size:16px;">Tidak ada stiker ditemukan</div></div>';
                } else {
                    grid.innerHTML = filtered.map(s => `
                        <div class="sticker-item" style="position:relative; cursor:pointer; border-radius:8px; overflow:hidden; background:var(--input); aspect-ratio:1;" onclick="sendStickerMessage(${s.id}, '${s.url}')">
                            <img src="${s.thumb_url || s.url}" style="width:100%; height:100%; object-fit:cover;" onerror="this.src='${s.url}'">
                            <div style="position:absolute; top:4px; right:4px; display:flex; gap:4px;">
                                <button onclick="event.stopPropagation(); toggleStickerFavorite(${s.id})" style="background:rgba(0,0,0,0.5); border:none; color:white; width:24px; height:24px; border-radius:50%; cursor:pointer; font-size:12px;">${favoriteStickers.find(f => f.id === s.id) ? '⭐' : '☆'}</button>
                                <button onclick="event.stopPropagation(); deleteSticker(${s.id})" style="background:rgba(0,0,0,0.5); border:none; color:white; width:24px; height:24px; border-radius:50%; cursor:pointer; font-size:12px;">🗑️</button>
                            </div>
                        </div>
                    `).join('');
                }
            };
        }
        
        // Creator mode tabs
        qsa('.creator-tab-btn').forEach(btn => {
            btn.onclick = () => {
                switchCreatorMode(btn.dataset.mode);
            };
        });
        
        // Sticker upload area
        const stickerUploadArea = qs('#stickerUploadArea');
        const stickerFileInput = qs('#stickerFileInput');
        
        if (stickerUploadArea && stickerFileInput) {
            stickerUploadArea.onclick = () => stickerFileInput.click();
            
            stickerUploadArea.ondragover = (e) => {
                e.preventDefault();
                stickerUploadArea.style.borderColor = 'var(--accent)';
                stickerUploadArea.style.background = 'rgba(var(--accent), 0.1)';
            };
            
            stickerUploadArea.ondragleave = (e) => {
                e.preventDefault();
                stickerUploadArea.style.borderColor = 'var(--border)';
                stickerUploadArea.style.background = 'var(--input)';
            };
            
            stickerUploadArea.ondrop = (e) => {
                e.preventDefault();
                stickerUploadArea.style.borderColor = 'var(--border)';
                stickerUploadArea.style.background = 'var(--input)';
                const file = e.dataTransfer.files[0];
                if (file && file.type.startsWith('image/')) {
                    handleStickerFile(file);
                }
            };
            
            stickerFileInput.onchange = (e) => {
                const file = e.target.files[0];
                if (file) {
                    handleStickerFile(file);
                }
            };
        }
        
        // Remove sticker image button
        const removeStickerImg = qs('#removeStickerImg');
        if (removeStickerImg) {
            removeStickerImg.onclick = (e) => {
                e.stopPropagation();
                stickerImageData = null;
                qs('#stickerPreviewArea').style.display = 'none';
                qs('#stickerUploadArea').style.display = 'flex';
                qs('#stickerFileInput').value = '';
            };
        }
        
        // Text sticker input
        const textStickerInput = qs('#textStickerInput');
        if (textStickerInput) {
            textStickerInput.oninput = () => updateTextStickerPreview();
        }
        
        // Text sticker options
        ['textFontSize', 'textColor', 'textBgColor', 'textTransparentBg', 'textFontStyle'].forEach(id => {
            const el = qs('#' + id);
            if (el) {
                el.oninput = () => updateTextStickerPreview();
                el.onchange = () => updateTextStickerPreview();
            }
        });
    }
    
    // Open sticker picker
    function openStickerPicker() {
        showModal('stickerPickerModal', true);
        loadStickers();
    }
    
    // Load stickers from API
    async function loadStickers() {
        try {
            const [allRes, favRes, recentRes] = await Promise.all([
                fetch('/api/stickers'),
                fetch('/api/stickers/favorites'),
                fetch('/api/stickers/recent')
            ]);
            
            allUserStickers = await allRes.json();
            favoriteStickers = await favRes.json();
            recentStickers = await recentRes.json();
            
            renderStickerGrid('recent');
        } catch (e) {
            console.error('Error loading stickers:', e);
        }
    }
    
    // Render sticker grid based on tab
    function renderStickerGrid(tab) {
        const grid = qs('#stickerGrid');
        let stickers = [];
        
        switch(tab) {
            case 'recent':
                stickers = recentStickers;
                break;
            case 'favorites':
                stickers = favoriteStickers;
                break;
            case 'all':
                stickers = allUserStickers;
                break;
            case 'packs':
                grid.innerHTML = '<div style="grid-column:1/-1; text-align:center; color:var(--muted); padding:40px 20px;"><div style="font-size:48px; margin-bottom:16px;">📦</div><div style="font-size:16px; margin-bottom:8px;">Fitur Pack Segera Hadir</div></div>';
                return;
        }
        
        if (stickers.length === 0) {
            grid.innerHTML = `<div style="grid-column:1/-1; text-align:center; color:var(--muted); padding:40px 20px;">
                <div style="font-size:48px; margin-bottom:16px;">🎨</div>
                <div style="font-size:16px; margin-bottom:8px;">Belum ada stiker</div>
                <div style="font-size:14px;">Klik "Buat Stiker" untuk membuat stiker pertama Anda</div>
            </div>`;
            return;
        }
        
        grid.innerHTML = stickers.map(s => `
            <div class="sticker-item" style="position:relative; cursor:pointer; border-radius:8px; overflow:hidden; background:var(--input); aspect-ratio:1;" onclick="sendStickerMessage(${s.id}, '${s.url}')">
                <img src="${s.thumb_url || s.url}" style="width:100%; height:100%; object-fit:cover;" onerror="this.src='${s.url}'">
                <div style="position:absolute; top:4px; right:4px; display:flex; gap:4px;">
                    <button onclick="event.stopPropagation(); toggleStickerFavorite(${s.id})" style="background:rgba(0,0,0,0.5); border:none; color:white; width:24px; height:24px; border-radius:50%; cursor:pointer; font-size:12px;">${favoriteStickers.find(f => f.id === s.id) ? '⭐' : '☆'}</button>
                    <button onclick="event.stopPropagation(); deleteSticker(${s.id})" style="background:rgba(0,0,0,0.5); border:none; color:white; width:24px; height:24px; border-radius:50%; cursor:pointer; font-size:12px;">🗑️</button>
                </div>
            </div>
        `).join('');
    }
    
    // Send sticker as message
    async function sendStickerMessage(stickerId, stickerUrl) {
        if (!currentChat) {
            alert('Pilih percakapan terlebih dahulu');
            return;
        }
        
        // Record usage
        await fetch(`/api/stickers/${stickerId}/use`, { method: 'POST' });
        
        // Send sticker message
        const payload = {
            chat_type: currentChat.type,
            content_type: 'sticker',
            file_path: stickerUrl.replace('/uploads/', '')
        };
        
        if (currentChat.type === 'direct') {
            payload.peer_id = currentChat.id;
        } else {
            payload.group_id = currentChat.id;
        }
        
        socket.emit('send_message', payload);
        showModal('stickerPickerModal', false);
    }
    
    // Toggle sticker favorite
    async function toggleStickerFavorite(stickerId) {
        const isFav = favoriteStickers.find(f => f.id === stickerId);
        
        if (isFav) {
            await fetch(`/api/stickers/${stickerId}/favorite`, { method: 'DELETE' });
        } else {
            await fetch(`/api/stickers/${stickerId}/favorite`, { method: 'POST' });
        }
        
        await loadStickers();
    }
    
    // Delete sticker
    async function deleteSticker(stickerId) {
        if (!confirm('Hapus stiker ini?')) return;
        
        await fetch(`/api/stickers/${stickerId}`, { method: 'DELETE' });
        await loadStickers();
        showToast('Stiker berhasil dihapus');
    }
    
    // Open sticker creator
    function openStickerCreator() {
        showModal('stickerPickerModal', false);
        showModal('stickerCreatorModal', true);
        resetStickerCreator();
    }
    
    // Open sticker manager (opens picker in management mode)
    function openStickerManager() {
        showModal('settingsModal', false);
        openStickerPicker();
    }
    
    // Delete all stickers
    async function deleteAllStickers() {
        if (allUserStickers.length === 0) {
            showToast('Tidak ada stiker untuk dihapus');
            return;
        }
        
        if (!confirm(`Hapus SEMUA ${allUserStickers.length} stiker? Tindakan ini tidak dapat dibatalkan!`)) return;
        
        try {
            // Delete all stickers one by one
            for (const sticker of allUserStickers) {
                await fetch(`/api/stickers/${sticker.id}`, { method: 'DELETE' });
            }
            
            showToast(`${allUserStickers.length} stiker berhasil dihapus`);
            await loadStickers();
        } catch (e) {
            console.error('Error deleting all stickers:', e);
            alert('Gagal menghapus semua stiker: ' + e.message);
        }
    }
    
    // Show favorite stickers
    function showFavoriteStickers() {
        showModal('settingsModal', false);
        openStickerPicker();
        // Auto-select favorites tab
        setTimeout(() => {
            qsa('.sticker-tab-btn').forEach(b => {
                b.style.background = 'var(--input)';
                b.style.color = 'var(--text)';
                b.style.border = '1px solid var(--border)';
            });
            const favBtn = qs('[data-tab="favorites"]');
            if (favBtn) {
                favBtn.style.background = 'var(--accent)';
                favBtn.style.color = 'white';
                favBtn.style.border = 'none';
                renderStickerGrid('favorites');
            }
        }, 100);
    }
    
    // Reset sticker creator
    function resetStickerCreator() {
        stickerImageData = null;
        qs('#stickerPreviewArea').style.display = 'none';
        qs('#stickerUploadArea').style.display = 'flex';
        qs('#textStickerInput').value = '';
        qs('#textStickerPreview').textContent = 'Ketik teks...';
        
        // Reset effects
        qs('#effectGrayscale').checked = false;
        qs('#effectSepia').checked = false;
        qs('#effectInvert').checked = false;
        qs('#effectBlur').checked = false;
        qs('#effectSharpen').checked = false;
        qs('#effectBrightness').value = 1;
        qs('#effectContrast').value = 1;
        qs('#effectSaturation').value = 1;
        qs('#stickerTags').value = '';
        
        // Reset text options
        qs('#textFontSize').value = 48;
        qs('#fontSizeValue').textContent = '48px';
        qs('#textColor').value = '#FFFFFF';
        qs('#textBgColor').value = '#000000';
        qs('#textTransparentBg').checked = false;
        qs('#textFontStyle').value = 'bold';
    }
    
    // Switch sticker creator mode
    function switchCreatorMode(mode) {
        stickerCreatorMode = mode;
        
        // Update tabs
        qsa('.creator-tab-btn').forEach(btn => {
            btn.style.background = btn.dataset.mode === mode ? 'var(--accent)' : 'var(--input)';
            btn.style.color = btn.dataset.mode === mode ? 'white' : 'var(--text)';
            btn.style.border = btn.dataset.mode === mode ? 'none' : '1px solid var(--border)';
        });
        
        // Show/hide panels
        qs('#imageStickerMode').style.display = mode === 'image' ? 'block' : 'none';
        qs('#textStickerMode').style.display = mode === 'text' ? 'block' : 'none';
    }
    
    // Handle sticker file upload
    function handleStickerFile(file) {
        if (!file) return;
        
        if (file.size > 5 * 1024 * 1024) {
            alert('File terlalu besar. Maksimal 5MB.');
            return;
        }
        
        const reader = new FileReader();
        reader.onload = (e) => {
            stickerImageData = e.target.result;
            qs('#stickerPreviewImg').src = stickerImageData;
            qs('#stickerUploadArea').style.display = 'none';
            qs('#stickerPreviewArea').style.display = 'block';
        };
        reader.readAsDataURL(file);
    }
    
    // Update text sticker preview
    function updateTextStickerPreview() {
        const text = qs('#textStickerInput').value || 'Ketik teks...';
        const fontSize = qs('#textFontSize').value;
        const textColor = qs('#textColor').value;
        const bgColor = qs('#textBgColor').value;
        const transparent = qs('#textTransparentBg').checked;
        const fontStyle = qs('#textFontStyle').value;
        
        const preview = qs('#textStickerPreview');
        preview.textContent = text;
        preview.style.fontSize = fontSize + 'px';
        preview.style.color = textColor;
        preview.style.backgroundColor = transparent ? 'transparent' : bgColor;
        preview.style.fontWeight = fontStyle === 'bold' ? 'bold' : 'normal';
        preview.style.fontStyle = fontStyle === 'italic' ? 'italic' : 'normal';
        
        qs('#fontSizeValue').textContent = fontSize + 'px';
    }
    
    // Save sticker
    async function saveSticker() {
        try {
            let response;
            
            if (stickerCreatorMode === 'image') {
                if (!stickerImageData) {
                    alert('Pilih gambar terlebih dahulu');
                    return;
                }
                
                // Build effects object
                const effects = {
                    grayscale: qs('#effectGrayscale').checked,
                    sepia: qs('#effectSepia').checked,
                    invert: qs('#effectInvert').checked,
                    blur: qs('#effectBlur').checked ? 2 : 0,
                    sharpen: qs('#effectSharpen').checked,
                    brightness: parseFloat(qs('#effectBrightness').value),
                    contrast: parseFloat(qs('#effectContrast').value),
                    saturation: parseFloat(qs('#effectSaturation').value)
                };
                
                // Check if any effect is applied
                const hasEffects = effects.grayscale || effects.sepia || effects.invert || 
                                   effects.blur > 0 || effects.sharpen || 
                                   effects.brightness !== 1 || effects.contrast !== 1 || effects.saturation !== 1;
                
                if (hasEffects) {
                    // Create sticker with effects using base64
                    const tags = qs('#stickerTags').value.split(',').map(t => t.trim()).filter(t => t);
                    
                    response = await fetch('/api/stickers/create_effect', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            image_data: stickerImageData,
                            effects: effects,
                            tags: tags
                        })
                    });
                } else {
                    // Create simple sticker from file
                    const blob = await fetch(stickerImageData).then(r => r.blob());
                    const file = new File([blob], 'sticker.png', { type: 'image/png' });
                    
                    const formData = new FormData();
                    formData.append('file', file);
                    
                    const tags = qs('#stickerTags').value.split(',').map(t => t.trim()).filter(t => t);
                    if (tags.length > 0) {
                        formData.append('tags', JSON.stringify(tags));
                    }
                    
                    response = await fetch('/api/stickers/create', {
                        method: 'POST',
                        body: formData
                    });
                }
            } else {
                // Text sticker
                const text = qs('#textStickerInput').value.trim();
                if (!text) {
                    alert('Masukkan teks stiker');
                    return;
                }
                
                const transparent = qs('#textTransparentBg').checked;
                
                response = await fetch('/api/stickers/create_text', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        text: text,
                        font_size: parseInt(qs('#textFontSize').value),
                        text_color: qs('#textColor').value,
                        bg_color: transparent ? '#00000000' : qs('#textBgColor').value + 'FF',
                        font_style: qs('#textFontStyle').value,
                        tags: ['text', 'custom']
                    })
                });
            }
            
            const result = await response.json();
            
            if (result.ok) {
                showToast('Stiker berhasil dibuat! 🎉');
                showModal('stickerCreatorModal', false);
                showModal('stickerPickerModal', true);
                await loadStickers();
            } else {
                alert(result.error || 'Gagal membuat stiker');
            }
        } catch (e) {
            console.error('Error saving sticker:', e);
            alert('Gagal menyimpan stiker: ' + e.message);
        }
    }
    
    // ==================== NEW FEATURES JAVASCRIPT ====================
    
    // Load data when advanced settings tab is clicked
    async function loadAdvancedFeatures() {
      if (!currentChat) {
        qs('#disappearingStatus').textContent = 'Pilih percakapan terlebih dahulu';
      } else {
        // Load disappearing messages status
        try {
          const res = await fetch(`/api/disappearing_messages?chat_type=${currentChat.type}&ref_id=${currentChat.id}`);
          const data = await res.json();
          if (data.duration_seconds > 0) {
            const hours = data.duration_seconds / 3600;
            const days = hours / 24;
            if (days >= 1) {
              qs('#disappearingStatus').textContent = `Aktif: ${days} hari`;
            } else {
              qs('#disappearingStatus').textContent = `Aktif: ${hours} jam`;
            }
          } else {
            qs('#disappearingStatus').textContent = 'Nonaktif';
          }
        } catch (e) {
          qs('#disappearingStatus').textContent = 'Gagal memuat status';
        }
      }
      
      // Load quick templates
      loadQuickTemplates();
      
      // Load blocked contacts
      loadBlockedContacts();
      
      // Load chat statistics
      loadChatStatistics();
    }
    
    async function loadQuickTemplates() {
      try {
        const res = await fetch('/api/quick_templates');
        const templates = await res.json();
        const list = qs('#templatesList');
        list.innerHTML = '';
        
        if (templates.length === 0) {
          list.innerHTML = '<div style="color:var(--muted); font-size:13px; text-align:center; padding:20px;">Belum ada template. Klik + Tambah untuk membuat.</div>';
          return;
        }
        
        templates.forEach(t => {
          const item = el(`<div style="display:flex; justify-content:space-between; align-items:center; padding:8px; background:var(--panel); border-radius:6px; margin-bottom:6px;">
            <div style="flex:1; min-width:0;">
              <div style="font-weight:600; font-size:13px;">${escapeHtml(t.title)}</div>
              <div style="font-size:12px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(t.content.slice(0, 50))}${t.content.length > 50 ? '...' : ''}</div>
              ${t.shortcut ? `<div style="font-size:11px; color:var(--accent); margin-top:2px;">Shortcut: ${escapeHtml(t.shortcut)}</div>` : ''}
            </div>
            <div style="display:flex; gap:4px; margin-left:8px;">
              <button class="btn sec" onclick="useTemplate('${escapeHtml(t.content.replace(/'/g, "\\'"))}')" style="padding:4px 8px; font-size:11px;">Gunakan</button>
              <button class="btn sec" onclick="deleteTemplate(${t.id})" style="padding:4px 8px; font-size:11px; background:var(--danger);">🗑️</button>
            </div>
          </div>`);
          list.appendChild(item);
        });
      } catch (e) {
        console.error('Error loading templates:', e);
      }
    }
    
    function useTemplate(content) {
      const msgBox = qs('#msgBox');
      msgBox.value = content;
      msgBox.focus();
      autoResizeTextarea();
      showToast('Template dimasukkan ke kotak pesan');
    }
    
    async function deleteTemplate(id) {
      if (!confirm('Hapus template ini?')) return;
      try {
        await fetch('/api/quick_templates', {
          method: 'DELETE',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({id: id})
        });
        loadQuickTemplates();
        showToast('Template berhasil dihapus');
      } catch (e) {
        alert('Gagal menghapus template');
      }
    }
    
    function showAddTemplateModal() {
      const modal = el(`<div class="modal show" id="addTemplateModal">
        <div class="dialog" style="width:400px;">
          <h3>Tambah Template Baru</h3>
          <div class="field-group">
            <label>Judul Template</label>
            <input type="text" id="templateTitle" placeholder="Contoh: Salam Pembuka" />
          </div>
          <div class="field-group">
            <label>Isi Pesan</label>
            <textarea id="templateContent" placeholder="Tulis pesan template..." rows="4" style="width:100%; padding:10px; background:var(--input); color:var(--text); border:1px solid var(--border); border-radius:8px; resize:vertical;"></textarea>
          </div>
          <div class="field-group">
            <label>Shortcut (opsional)</label>
            <input type="text" id="templateShortcut" placeholder="Contoh: /salam" />
          </div>
          <div class="row" style="justify-content:flex-end; gap:12px; margin-top:20px;">
            <button class="btn sec" onclick="this.closest('.modal').remove()">Batal</button>
            <button class="btn" onclick="saveTemplate()">Simpan</button>
          </div>
        </div>
      </div>`);
      document.body.appendChild(modal);
    }
    
    async function saveTemplate() {
      const title = qs('#templateTitle').value.trim();
      const content = qs('#templateContent').value.trim();
      const shortcut = qs('#templateShortcut').value.trim();
      
      if (!title || !content) {
        alert('Judul dan isi pesan wajib diisi');
        return;
      }
      
      try {
        await fetch('/api/quick_templates', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({title, content, shortcut})
        });
        qs('#addTemplateModal').remove();
        loadQuickTemplates();
        showToast('Template berhasil ditambahkan');
      } catch (e) {
        alert('Gagal menambahkan template');
      }
    }
    
    async function setDisappearing(seconds) {
      if (!currentChat) {
        alert('Pilih percakapan terlebih dahulu');
        return;
      }
      
      try {
        await fetch('/api/disappearing_messages', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            chat_type: currentChat.type,
            ref_id: currentChat.id,
            duration_seconds: seconds
          })
        });
        
        if (seconds > 0) {
          const hours = seconds / 3600;
          const days = hours / 24;
          if (days >= 1) {
            qs('#disappearingStatus').textContent = `Aktif: ${days} hari`;
          } else {
            qs('#disappearingStatus').textContent = `Aktif: ${hours} jam`;
          }
          showToast(`Pesan menghilang diaktifkan`);
        } else {
          qs('#disappearingStatus').textContent = 'Nonaktif';
          showToast('Pesan menghilang dinonaktifkan');
        }
      } catch (e) {
        alert('Gagal mengatur pesan menghilang');
      }
    }
    
    async function loadBlockedContacts() {
      try {
        const res = await fetch('/api/blocked_contacts');
        const contacts = await res.json();
        const list = qs('#blockedContactsList');
        list.innerHTML = '';
        
        if (contacts.length === 0) {
          list.innerHTML = '<div style="color:var(--muted); font-size:13px; text-align:center; padding:20px;">Tidak ada kontak yang diblokir</div>';
          return;
        }
        
        contacts.forEach(c => {
          const item = el(`<div style="display:flex; justify-content:space-between; align-items:center; padding:8px; background:var(--panel); border-radius:6px; margin-bottom:6px;">
            <div style="display:flex; align-items:center; gap:10px;">
              <div class="avatar" style="width:36px; height:36px; font-size:14px;">${avatarHTML(c.avatar_path, c.display_name)}</div>
              <div>
                <div style="font-weight:600; font-size:13px;">${escapeHtml(c.display_name)}</div>
                <div style="font-size:11px; color:var(--muted);">@${escapeHtml(c.username)}</div>
              </div>
            </div>
            <button class="btn sec" onclick="unblockContact(${c.blocked_user_id})" style="padding:4px 10px; font-size:11px;">Buka Blokir</button>
          </div>`);
          list.appendChild(item);
        });
      } catch (e) {
        console.error('Error loading blocked contacts:', e);
      }
    }
    
    async function unblockContact(userId) {
      if (!confirm('Buka blokir kontak ini?')) return;
      try {
        await fetch('/api/block_contact', {
          method: 'DELETE',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({user_id: userId})
        });
        loadBlockedContacts();
        showToast('Kontak berhasil dibuka blokirnya');
      } catch (e) {
        alert('Gagal membuka blokir');
      }
    }
    
    async function loadChatStatistics() {
      try {
        const res = await fetch('/api/chat_statistics');
        const stats = await res.json();
        
        qs('#statSent').textContent = stats.total_sent || 0;
        qs('#statReceived').textContent = stats.total_received || 0;
        qs('#statFiles').textContent = stats.files_shared || 0;
        
        // Count contacts
        const contactsRes = await fetch('/api/conversations');
        const contacts = await contactsRes.json();
        qs('#statContacts').textContent = (contacts.peers || []).length;
      } catch (e) {
        console.error('Error loading statistics:', e);
      }
    }
    
    // Attach loadAdvancedFeatures to tab click
    document.addEventListener('DOMContentLoaded', () => {
      const advancedTab = document.querySelector('[data-target="advanced"]');
      if (advancedTab) {
        advancedTab.addEventListener('click', () => {
          setTimeout(loadAdvancedFeatures, 100);
        });
      }
    });

    // ==================== NEW FEATURES JAVASCRIPT ====================
    
    // Load data when advanced settings tab is clicked
    async function loadAdvancedFeatures() {
      if (!currentChat) {
        qs('#disappearingStatus').textContent = 'Pilih percakapan terlebih dahulu';
      } else {
        // Load disappearing messages status
        try {
          const res = await fetch(`/api/disappearing_messages?chat_type=${currentChat.type}&ref_id=${currentChat.id}`);
          const data = await res.json();
          if (data.duration_seconds > 0) {
            const hours = data.duration_seconds / 3600;
            const days = hours / 24;
            if (days >= 1) {
              qs('#disappearingStatus').textContent = `Aktif: ${days} hari`;
            } else {
              qs('#disappearingStatus').textContent = `Aktif: ${hours} jam`;
            }
          } else {
            qs('#disappearingStatus').textContent = 'Nonaktif';
          }
        } catch (e) {
          qs('#disappearingStatus').textContent = 'Gagal memuat status';
        }
      }
      
      // Load quick templates
      loadQuickTemplates();
      
      // Load blocked contacts
      loadBlockedContacts();
      
      // Load chat statistics
      loadChatStatistics();
    }
    
    async function loadQuickTemplates() {
      try {
        const res = await fetch('/api/quick_templates');
        const templates = await res.json();
        const list = qs('#templatesList');
        list.innerHTML = '';
        
        if (templates.length === 0) {
          list.innerHTML = '<div style="color:var(--muted); font-size:13px; text-align:center; padding:20px;">Belum ada template. Klik + Tambah untuk membuat.</div>';
          return;
        }
        
        templates.forEach(t => {
          const item = el(`<div style="display:flex; justify-content:space-between; align-items:center; padding:8px; background:var(--panel); border-radius:6px; margin-bottom:6px;">
            <div style="flex:1; min-width:0;">
              <div style="font-weight:600; font-size:13px;">${escapeHtml(t.title)}</div>
              <div style="font-size:12px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(t.content.slice(0, 50))}${t.content.length > 50 ? '...' : ''}</div>
              ${t.shortcut ? `<div style="font-size:11px; color:var(--accent); margin-top:2px;">Shortcut: ${escapeHtml(t.shortcut)}</div>` : ''}
            </div>
            <div style="display:flex; gap:4px; margin-left:8px;">
              <button class="btn sec" onclick="useTemplate('${escapeHtml(t.content.replace(/'/g, "\\'"))}')" style="padding:4px 8px; font-size:11px;">Gunakan</button>
              <button class="btn sec" onclick="deleteTemplate(${t.id})" style="padding:4px 8px; font-size:11px; background:var(--danger);">🗑️</button>
            </div>
          </div>`);
          list.appendChild(item);
        });
      } catch (e) {
        console.error('Error loading templates:', e);
      }
    }
    
    function useTemplate(content) {
      const msgBox = qs('#msgBox');
      msgBox.value = content;
      msgBox.focus();
      autoResizeTextarea();
      showToast('Template dimasukkan ke kotak pesan');
    }
    
    async function deleteTemplate(id) {
      if (!confirm('Hapus template ini?')) return;
      try {
        await fetch('/api/quick_templates', {
          method: 'DELETE',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({id: id})
        });
        loadQuickTemplates();
        showToast('Template berhasil dihapus');
      } catch (e) {
        alert('Gagal menghapus template');
      }
    }
    
    function showAddTemplateModal() {
      const modal = el(`<div class="modal show" id="addTemplateModal">
        <div class="dialog" style="width:400px;">
          <h3>Tambah Template Baru</h3>
          <div class="field-group">
            <label>Judul Template</label>
            <input type="text" id="templateTitle" placeholder="Contoh: Salam Pembuka" />
          </div>
          <div class="field-group">
            <label>Isi Pesan</label>
            <textarea id="templateContent" placeholder="Tulis pesan template..." rows="4" style="width:100%; padding:10px; background:var(--input); color:var(--text); border:1px solid var(--border); border-radius:8px; resize:vertical;"></textarea>
          </div>
          <div class="field-group">
            <label>Shortcut (opsional)</label>
            <input type="text" id="templateShortcut" placeholder="Contoh: /salam" />
          </div>
          <div class="row" style="justify-content:flex-end; gap:12px; margin-top:20px;">
            <button class="btn sec" onclick="this.closest('.modal').remove()">Batal</button>
            <button class="btn" onclick="saveTemplate()">Simpan</button>
          </div>
        </div>
      </div>`);
      document.body.appendChild(modal);
    }
    
    async function saveTemplate() {
      const title = qs('#templateTitle').value.trim();
      const content = qs('#templateContent').value.trim();
      const shortcut = qs('#templateShortcut').value.trim();
      
      if (!title || !content) {
        alert('Judul dan isi pesan wajib diisi');
        return;
      }
      
      try {
        await fetch('/api/quick_templates', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({title, content, shortcut})
        });
        qs('#addTemplateModal').remove();
        loadQuickTemplates();
        showToast('Template berhasil ditambahkan');
      } catch (e) {
        alert('Gagal menambahkan template');
      }
    }
    
    async function setDisappearing(seconds) {
      if (!currentChat) {
        alert('Pilih percakapan terlebih dahulu');
        return;
      }
      
      try {
        await fetch('/api/disappearing_messages', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            chat_type: currentChat.type,
            ref_id: currentChat.id,
            duration_seconds: seconds
          })
        });
        
        if (seconds > 0) {
          const hours = seconds / 3600;
          const days = hours / 24;
          if (days >= 1) {
            qs('#disappearingStatus').textContent = `Aktif: ${days} hari`;
          } else {
            qs('#disappearingStatus').textContent = `Aktif: ${hours} jam`;
          }
          showToast(`Pesan menghilang diaktifkan`);
        } else {
          qs('#disappearingStatus').textContent = 'Nonaktif';
          showToast('Pesan menghilang dinonaktifkan');
        }
      } catch (e) {
        alert('Gagal mengatur pesan menghilang');
      }
    }
    
    async function loadBlockedContacts() {
      try {
        const res = await fetch('/api/blocked_contacts');
        const contacts = await res.json();
        const list = qs('#blockedContactsList');
        list.innerHTML = '';
        
        if (contacts.length === 0) {
          list.innerHTML = '<div style="color:var(--muted); font-size:13px; text-align:center; padding:20px;">Tidak ada kontak yang diblokir</div>';
          return;
        }
        
        contacts.forEach(c => {
          const item = el(`<div style="display:flex; justify-content:space-between; align-items:center; padding:8px; background:var(--panel); border-radius:6px; margin-bottom:6px;">
            <div style="display:flex; align-items:center; gap:10px;">
              <div class="avatar" style="width:36px; height:36px; font-size:14px;">${avatarHTML(c.avatar_path, c.display_name)}</div>
              <div>
                <div style="font-weight:600; font-size:13px;">${escapeHtml(c.display_name)}</div>
                <div style="font-size:11px; color:var(--muted);">@${escapeHtml(c.username)}</div>
              </div>
            </div>
            <button class="btn sec" onclick="unblockContact(${c.blocked_user_id})" style="padding:4px 10px; font-size:11px;">Buka Blokir</button>
          </div>`);
          list.appendChild(item);
        });
      } catch (e) {
        console.error('Error loading blocked contacts:', e);
      }
    }
    
    async function unblockContact(userId) {
      if (!confirm('Buka blokir kontak ini?')) return;
      try {
        await fetch('/api/block_contact', {
          method: 'DELETE',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({user_id: userId})
        });
        loadBlockedContacts();
        showToast('Kontak berhasil dibuka blokirnya');
      } catch (e) {
        alert('Gagal membuka blokir');
      }
    }
    
    async function loadChatStatistics() {
      try {
        const res = await fetch('/api/chat_statistics');
        const stats = await res.json();
        
        qs('#statSent').textContent = stats.total_sent || 0;
        qs('#statReceived').textContent = stats.total_received || 0;
        qs('#statFiles').textContent = stats.files_shared || 0;
        
        // Count contacts
        const contactsRes = await fetch('/api/conversations');
        const contacts = await contactsRes.json();
        qs('#statContacts').textContent = (contacts.peers || []).length;
      } catch (e) {
        console.error('Error loading statistics:', e);
      }
    }
    
    // Attach loadAdvancedFeatures to tab click
    document.addEventListener('DOMContentLoaded', () => {
      const advancedTab = document.querySelector('[data-target="advanced"]');
      if (advancedTab) {
        advancedTab.addEventListener('click', () => {
          setTimeout(loadAdvancedFeatures, 100);
        });
      }
    });

    // Init
    init();
    
    // Initialize sticker handlers after DOM is ready
    setTimeout(() => {
      initStickerHandlers();
    }, 500);
  </script>
</body>
</html>
"""

# --------------------------
# System Tray Functions
# --------------------------
def start_system_tray():
    """Start system tray icon with unread message count"""
    if not PYSTRAY_AVAILABLE:
        print("[WARN] pystray not available. System tray disabled.")
        return None

    def create_icon(count=0):
        """Create tray icon with badge showing unread count"""
        try:
            from PIL import Image, ImageDraw, ImageFont
            # Create a 64x64 icon
            img = Image.new('RGBA', (64, 64), (0, 168, 132, 255))  # WhatsApp green
            draw = ImageDraw.Draw(img)

            # Draw chat bubble icon
            draw.ellipse([8, 8, 56, 56], fill=(255, 255, 255, 255))
            draw.ellipse([12, 12, 52, 52], fill=(0, 168, 132, 255))
            draw.ellipse([16, 16, 32, 32], fill=(255, 255, 255, 255))
            draw.ellipse([20, 20, 28, 28], fill=(0, 168, 132, 255))
            draw.ellipse([32, 32, 48, 48], fill=(255, 255, 255, 255))

            # Draw badge if there are unread messages
            if count > 0:
                badge_text = str(count) if count < 100 else "99+"
                try:
                    font = ImageFont.truetype("arial.ttf", 16)
                except:
                    font = ImageFont.load_default()

                # Badge background
                bbox = draw.textbbox((0, 0), badge_text, font=font)
                text_width = bbox[2] - bbox[0]
                text_height = bbox[3] - bbox[1]
                badge_width = max(text_width + 8, 20)
                badge_height = max(text_height + 4, 20)

                draw.ellipse([44, 4, 44 + badge_width, 4 + badge_height], fill=(255, 0, 0, 255))
                draw.text((44 + badge_width//2 - text_width//2, 4 + badge_height//2 - text_height//2),
                         badge_text, fill=(255, 255, 255, 255), font=font)

            return img
        except Exception as e:
            print(f"[WARN] Failed to create tray icon: {e}")
            # Fallback: simple colored square
            img = Image.new('RGBA', (64, 64), (0, 168, 132, 255))
            return img

    def open_app(icon, item):
        """Open the chat app in browser"""
        import webbrowser
        try:
            if os.path.exists(os.path.join(BASE_DIR, 'cert.pem')):
                webbrowser.open('https://127.0.0.1:8080')
            else:
                webbrowser.open('http://127.0.0.1:8080')
        except Exception as e:
            print(f"[WARN] Failed to open browser: {e}")

    def quit_app(icon, item):
        """Quit the application"""
        icon.stop()
        os._exit(0)

    # Create menu
    menu = (
        item('Open Chat', open_app),
        item('Quit', quit_app),
    )

    # Create initial icon
    icon = pystray.Icon("chat_app", create_icon(0), "Rubycon Chat", menu)

    # Start icon in separate thread
    def run_tray():
        try:
            icon.run()
        except Exception as e:
            print(f"[WARN] System tray error: {e}")

    import threading
    tray_thread = threading.Thread(target=run_tray, daemon=True)
    tray_thread.start()

    # Store icon reference for updates
    global tray_icon_instance
    tray_icon_instance = icon

    print("[INFO] System tray started")
    return icon

def get_total_unread_count():
    """Get total unread messages across all conversations for current user"""
    try:
        conn = db()
        # Get current user ID from session
        user_id = session.get('user_id')
        if not user_id:
            conn.close()
            return 0

        # Count unread direct messages
        direct_unread = conn.execute("""
            SELECT COUNT(m.id) as cnt FROM messages m
            LEFT JOIN message_status ms ON ms.message_id = m.id AND ms.user_id = ?
            WHERE m.chat_type='direct' AND m.receiver_id = ? AND ms.read_at IS NULL AND m.deleted = 0
        """, (user_id, user_id)).fetchone()

        # Count unread group messages
        group_unread = conn.execute("""
            SELECT COUNT(m.id) as cnt FROM messages m
            LEFT JOIN message_status ms ON ms.message_id = m.id AND ms.user_id = ?
            WHERE m.chat_type='group' AND m.sender_id != ? AND ms.read_at IS NULL AND m.deleted = 0
            AND m.group_id IN (SELECT group_id FROM group_members WHERE user_id=?)
        """, (user_id, user_id, user_id)).fetchone()

        conn.close()

        total = (direct_unread['cnt'] if direct_unread else 0) + (group_unread['cnt'] if group_unread else 0)
        return total
    except Exception as e:
        print(f"[WARN] Error getting unread count: {e}")
        return 0

def update_tray_unread_count(count):
    """Update the tray icon with new unread count"""
    global tray_icon_instance
    if tray_icon_instance and PYSTRAY_AVAILABLE:
        try:
            from PIL import Image, ImageDraw, ImageFont
            # Create new icon with updated count
            img = Image.new('RGBA', (64, 64), (0, 168, 132, 255))  # WhatsApp green
            draw = ImageDraw.Draw(img)

            # Draw chat bubble icon
            draw.ellipse([8, 8, 56, 56], fill=(255, 255, 255, 255))
            draw.ellipse([12, 12, 52, 52], fill=(0, 168, 132, 255))
            draw.ellipse([16, 16, 32, 32], fill=(255, 255, 255, 255))
            draw.ellipse([20, 20, 28, 28], fill=(0, 168, 132, 255))
            draw.ellipse([32, 32, 48, 48], fill=(255, 255, 255, 255))

            # Draw badge if there are unread messages
            if count > 0:
                badge_text = str(count) if count < 100 else "99+"
                try:
                    font = ImageFont.truetype("arial.ttf", 16)
                except:
                    font = ImageFont.load_default()

                # Badge background
                bbox = draw.textbbox((0, 0), badge_text, font=font)
                text_width = bbox[2] - bbox[0]
                text_height = bbox[3] - bbox[1]
                badge_width = max(text_width + 8, 20)
                badge_height = max(text_height + 4, 20)

                draw.ellipse([44, 4, 44 + badge_width, 4 + badge_height], fill=(255, 0, 0, 255))
                draw.text((44 + badge_width//2 - text_width//2, 4 + badge_height//2 - text_height//2),
                         badge_text, fill=(255, 255, 255, 255), font=font)

            # Update the icon
            tray_icon_instance.icon = img
        except Exception as e:
            print(f"[WARN] Failed to update tray icon: {e}")

# Global tray icon instance
tray_icon_instance = None

# --------------------------
# Sticker Manager Initialization
# --------------------------
sticker_manager = None
if STICKER_FEATURE_AVAILABLE:
    try:
        sticker_manager = StickerManager(DB_PATH, UPLOAD_DIR)
        print("[INFO] Sticker manager initialized successfully")
    except Exception as e:
        print(f"[WARN] Failed to initialize sticker manager: {e}")
        STICKER_FEATURE_AVAILABLE = False

# --------------------------
# APIs: Sticker Features
# --------------------------
@app.route("/api/stickers", methods=["GET"])
def api_get_stickers():
    """Get user's stickers"""
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    if not STICKER_FEATURE_AVAILABLE or not sticker_manager:
        return jsonify({"error": "Sticker feature not available"}), 503
    
    me = current_user()
    pack_id = request.args.get("pack_id")
    stickers = sticker_manager.get_user_stickers(me["id"], int(pack_id) if pack_id else None)
    return jsonify(stickers)

@app.route("/api/stickers/favorites", methods=["GET"])
def api_get_favorite_stickers():
    """Get user's favorite stickers"""
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    if not STICKER_FEATURE_AVAILABLE or not sticker_manager:
        return jsonify({"error": "Sticker feature not available"}), 503
    
    me = current_user()
    stickers = sticker_manager.get_favorite_stickers(me["id"])
    return jsonify(stickers)

@app.route("/api/stickers/recent", methods=["GET"])
def api_get_recent_stickers():
    """Get recently used stickers"""
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    if not STICKER_FEATURE_AVAILABLE or not sticker_manager:
        return jsonify({"error": "Sticker feature not available"}), 503
    
    me = current_user()
    limit = int(request.args.get("limit", 20))
    stickers = sticker_manager.get_recent_stickers(me["id"], limit)
    return jsonify(stickers)

@app.route("/api/stickers/create", methods=["POST"])
def api_create_sticker():
    """Create a new sticker"""
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    if not STICKER_FEATURE_AVAILABLE or not sticker_manager:
        return jsonify({"error": "Sticker feature not available"}), 503
    
    me = current_user()
    
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400
    
    pack_id = request.form.get("pack_id")
    tags = request.form.get("tags")
    emoji = request.form.get("emoji")
    
    if tags:
        try:
            tags = json.loads(tags)
        except:
            tags = [tags]
    
    result = sticker_manager.create_sticker_from_image(
        me["id"],
        file,
        pack_id=int(pack_id) if pack_id else None,
        tags=tags,
        emoji=emoji
    )
    
    if result:
        return jsonify({"ok": True, "sticker": result})
    else:
        return jsonify({"error": "Failed to create sticker"}), 500

@app.route("/api/stickers/create_text", methods=["POST"])
def api_create_text_sticker():
    """Create a text sticker"""
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    if not STICKER_FEATURE_AVAILABLE or not sticker_manager:
        return jsonify({"error": "Sticker feature not available"}), 503
    
    me = current_user()
    data = request.json or {}
    
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Text is required"}), 400
    
    font_size = int(data.get("font_size", 48))
    text_color = data.get("text_color", "#FFFFFF")
    bg_color = data.get("bg_color", "#00000000")
    font_style = data.get("font_style", "bold")
    pack_id = data.get("pack_id")
    tags = data.get("tags", ["text", "custom"])
    
    result = sticker_manager.create_text_sticker(
        me["id"],
        text,
        font_size=font_size,
        text_color=text_color,
        bg_color=bg_color,
        font_style=font_style,
        pack_id=int(pack_id) if pack_id else None,
        tags=tags
    )
    
    if result:
        return jsonify({"ok": True, "sticker": result})
    else:
        return jsonify({"error": "Failed to create text sticker"}), 500

@app.route("/api/stickers/create_effect", methods=["POST"])
def api_create_effect_sticker():
    """Create a sticker with effects"""
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    if not STICKER_FEATURE_AVAILABLE or not sticker_manager:
        return jsonify({"error": "Sticker feature not available"}), 503
    
    me = current_user()
    
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400
    
    effects_str = request.form.get("effects", "{}")
    try:
        effects = json.loads(effects_str)
    except:
        effects = {}
    
    pack_id = request.form.get("pack_id")
    tags = request.form.get("tags")
    
    if tags:
        try:
            tags = json.loads(tags)
        except:
            tags = [tags]
    
    result = sticker_manager.create_sticker_with_effects(
        me["id"],
        file,
        effects=effects,
        pack_id=int(pack_id) if pack_id else None,
        tags=tags
    )
    
    if result:
        return jsonify({"ok": True, "sticker": result})
    else:
        return jsonify({"error": "Failed to create sticker with effects"}), 500

@app.route("/api/stickers/<int:sticker_id>/favorite", methods=["POST", "DELETE"])
def api_toggle_favorite_sticker(sticker_id):
    """Add or remove sticker from favorites"""
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    if not STICKER_FEATURE_AVAILABLE or not sticker_manager:
        return jsonify({"error": "Sticker feature not available"}), 503
    
    me = current_user()
    
    if request.method == "POST":
        success = sticker_manager.add_to_favorites(me["id"], sticker_id)
    else:
        success = sticker_manager.remove_from_favorites(me["id"], sticker_id)
    
    if success:
        return jsonify({"ok": True})
    else:
        return jsonify({"error": "Operation failed"}), 500

@app.route("/api/stickers/<int:sticker_id>", methods=["DELETE"])
def api_delete_sticker(sticker_id):
    """Delete a sticker"""
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    if not STICKER_FEATURE_AVAILABLE or not sticker_manager:
        return jsonify({"error": "Sticker feature not available"}), 503
    
    me = current_user()
    success = sticker_manager.delete_sticker(me["id"], sticker_id)
    
    if success:
        return jsonify({"ok": True})
    else:
        return jsonify({"error": "Failed to delete sticker"}), 500

@app.route("/api/stickers/search", methods=["GET"])
def api_search_stickers():
    """Search stickers"""
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    if not STICKER_FEATURE_AVAILABLE or not sticker_manager:
        return jsonify({"error": "Sticker feature not available"}), 503
    
    me = current_user()
    query = request.args.get("q", "").strip()
    
    if not query:
        return jsonify([])
    
    stickers = sticker_manager.search_stickers(me["id"], query)
    return jsonify(stickers)

@app.route("/api/sticker_packs", methods=["GET", "POST"])
def api_sticker_packs():
    """Get or create sticker packs"""
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    if not STICKER_FEATURE_AVAILABLE or not sticker_manager:
        return jsonify({"error": "Sticker feature not available"}), 503
    
    me = current_user()
    
    if request.method == "GET":
        packs = sticker_manager.get_user_sticker_packs(me["id"])
        return jsonify(packs)
    else:
        data = request.json or {}
        name = data.get("name", "").strip()
        description = data.get("description", "").strip() or None
        is_public = bool(data.get("is_public", False))
        
        if not name:
            return jsonify({"error": "Pack name is required"}), 400
        
        pack_id = sticker_manager.create_sticker_pack(
            me["id"],
            name,
            description=description,
            is_public=is_public
        )
        
        if pack_id:
            return jsonify({"ok": True, "pack_id": pack_id})
        else:
            return jsonify({"error": "Failed to create pack"}), 500

@app.route("/api/sticker_packs/public", methods=["GET"])
def api_public_sticker_packs():
    """Get public sticker packs"""
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    if not STICKER_FEATURE_AVAILABLE or not sticker_manager:
        return jsonify({"error": "Sticker feature not available"}), 503
    
    limit = int(request.args.get("limit", 20))
    packs = sticker_manager.get_public_sticker_packs(limit)
    return jsonify(packs)

@app.route("/api/sticker_packs/<int:pack_id>/subscribe", methods=["POST"])
def api_subscribe_sticker_pack(pack_id):
    """Subscribe to a public sticker pack"""
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    if not STICKER_FEATURE_AVAILABLE or not sticker_manager:
        return jsonify({"error": "Sticker feature not available"}), 503
    
    me = current_user()
    success = sticker_manager.subscribe_to_pack(me["id"], pack_id)
    
    if success:
        return jsonify({"ok": True})
    else:
        return jsonify({"error": "Failed to subscribe"}), 500

@app.route("/api/sticker_packs/subscribed", methods=["GET"])
def api_subscribed_sticker_packs():
    """Get subscribed sticker packs"""
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    if not STICKER_FEATURE_AVAILABLE or not sticker_manager:
        return jsonify({"error": "Sticker feature not available"}), 503
    
    me = current_user()
    packs = sticker_manager.get_subscribed_packs(me["id"])
    return jsonify(packs)

@app.route("/api/stickers/<int:sticker_id>/use", methods=["POST"])
def api_record_sticker_usage(sticker_id):
    """Record sticker usage"""
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    if not STICKER_FEATURE_AVAILABLE or not sticker_manager:
        return jsonify({"error": "Sticker feature not available"}), 503
    
    me = current_user()
    sticker_manager.record_sticker_usage(me["id"], sticker_id)
    return jsonify({"ok": True})

@app.route("/api/stickers/delete_all", methods=["DELETE"])
def api_delete_all_stickers():
    """Delete all user stickers"""
    if not login_required():
        return jsonify({"error": "unauthorized"}), 401
    if not STICKER_FEATURE_AVAILABLE or not sticker_manager:
        return jsonify({"error": "Sticker feature not available"}), 503
    
    me = current_user()
    try:
        # Get all user stickers
        stickers = sticker_manager.get_user_stickers(me["id"])
        deleted_count = 0
        
        # Delete each sticker
        for sticker in stickers:
            if sticker_manager.delete_sticker(me["id"], sticker["id"]):
                deleted_count += 1
        
        return jsonify({"ok": True, "deleted_count": deleted_count})
    except Exception as e:
        return jsonify({"error": f"Failed to delete stickers: {str(e)}"}), 500

# --------------------------
# Run Single HTTPS Server
# --------------------------
if __name__ == "__main__":
    cert_path = os.path.join(BASE_DIR, 'cert.pem')
    key_path = os.path.join(BASE_DIR, 'key.pem')
    has_certs = os.path.exists(cert_path) and os.path.exists(key_path)

    # Start system tray
    tray_icon = start_system_tray()

    try:
        if has_certs:
            print("[SSL] Sertifikat SSL ditemukan. Menjalankan server HTTPS di port 8080")
            print("[INFO] PENTING: Akses aplikasi di https://127.0.0.1:8080")
            print("[INFO] Atau dari device lain di jaringan: https://[IP_ADDRESS]:8080")
            print("[INFO] Dengan sertifikat trusted mkcert (tidak ada warning unsafe)")
            socketio.run(app, host="0.0.0.0", port=8080, debug=False,
                         certfile=cert_path, keyfile=key_path)
        else:
            print("[WARN] Sertifikat SSL tidak ditemukan. Menjalankan server HTTP saja pada port 8080")
            print("[INFO] Untuk HTTPS: jalankan 'python generate_certs.py' terlebih dahulu")
            print("[INFO] Akses aplikasi di http://127.0.0.1:8080")
            socketio.run(app, host="0.0.0.0", port=8080, debug=False)
    except Exception as e:
        print(f"Error starting server: {e}")
        # Fallback for Unicode encoding issues on Windows
        try:
            if has_certs:
                print("[SSL] Sertifikat SSL ditemukan. Menjalankan server HTTPS di port 8080")
                print("[INFO] PENTING: Akses aplikasi di https://127.0.0.1:8080")
                print("[INFO] Atau dari device lain di jaringan: https://[IP_ADDRESS]:8080")
                print("[INFO] Dengan sertifikat trusted mkcert (tidak ada warning unsafe)")
                socketio.run(app, host="0.0.0.0", port=8080, debug=False,
                            certfile=cert_path, keyfile=key_path)
            else:
                print("[WARN] Sertifikat SSL tidak ditemukan. Menjalankan server HTTP saja pada port 8080")
                print("[INFO] Untuk HTTPS: jalankan 'python generate_certs.py' terlebih dahulu")
                print("[INFO] Akses aplikasi di http://127.0.0.1:8080")
                socketio.run(app, host="0.0.0.0", port=8080, debug=False)
        except UnicodeEncodeError:
            # Final fallback if Unicode issues persist
            print("[SERVER] Starting server on port 8080 (fallback mode)")
            print("[INFO] Access at http://127.0.0.1:8080")
            socketio.run(app, host="0.0.0.0", port=8080, debug=False)
