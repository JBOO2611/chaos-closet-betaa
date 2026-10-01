
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, session
from pathlib import Path
import sqlite3, json, uuid, csv, io, shutil, datetime, webbrowser, threading, time, secrets, os
from PIL import Image
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE / "data")).resolve()
UPLOADS = DATA_DIR / "uploads"
BACKUPS = DATA_DIR / "backups"
DB = DATA_DIR / "chaos_closet.db"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS.mkdir(parents=True, exist_ok=True)
BACKUPS.mkdir(parents=True, exist_ok=True)

SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
BETA_INVITE_CODE = os.environ.get("BETA_INVITE_CODE", "CHAOS-BETA")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 120 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") == "production"

PLATFORMS = ["Vinted", "Depop", "Mercari", "Poshmark", "eBay"]

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_column(conn, table, column, definition):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def init_db():
    conn = db()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT,
        business_name TEXT,
        is_admin INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS listings(
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        brand TEXT,
        sku TEXT,
        category TEXT,
        size TEXT,
        condition TEXT,
        cost REAL DEFAULT 0,
        price REAL DEFAULT 0,
        min_offer REAL DEFAULT 0,
        shipping_cost REAL DEFAULT 0,
        status TEXT DEFAULT 'draft',
        description TEXT,
        sold_platform TEXT,
        sold_price REAL DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    for col, definition in [
        ("platform_fee","REAL DEFAULT 0"),
        ("sold_shipping_cost","REAL DEFAULT 0"),
        ("sold_fee","REAL DEFAULT 0"),
        ("sold_at","TEXT"),
        ("notes","TEXT"),
        ("storage_bin","TEXT"),
        ("source","TEXT"),
        ("purchase_date","TEXT"),
        ("user_id","TEXT")
    ]:
        ensure_column(conn, "listings", col, definition)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS photos(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_id TEXT,
        filename TEXT,
        sort_order INTEGER DEFAULT 0
    )""")
    ensure_column(conn, "photos", "user_id", "TEXT")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS platform_listings(
        listing_id TEXT,
        platform TEXT,
        enabled INTEGER DEFAULT 0,
        price REAL DEFAULT 0,
        remote_id TEXT,
        remote_url TEXT,
        remote_status TEXT DEFAULT 'not_connected',
        PRIMARY KEY(listing_id, platform)
    )""")
    ensure_column(conn, "platform_listings", "user_id", "TEXT")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS app_settings(
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_settings(
        user_id TEXT,
        key TEXT,
        value TEXT,
        PRIMARY KEY(user_id,key)
    )""")
    conn.commit()
    conn.close()

def current_user_id():
    return session.get("user_id")

def login_required_json():
    if not current_user_id():
        return jsonify({"error":"login required"}), 401
    return None

def compress_save(file, user_id, listing_id, index):
    user_dir = UPLOADS / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    name = f"{listing_id}_{index}_{uuid.uuid4().hex[:8]}.jpg"
    dest = user_dir / name
    img = Image.open(file.stream)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((1600,1600))
    img.save(dest, "JPEG", quality=82, optimize=True)
    return name

def listing_to_dict(row, conn, uid):
    x = dict(row)
    x["photos"] = [r["filename"] for r in conn.execute(
        "SELECT filename FROM photos WHERE listing_id=? AND user_id=? ORDER BY sort_order,id", (x["id"],uid)
    ).fetchall()]
    plats = {}
    for r in conn.execute("SELECT * FROM platform_listings WHERE listing_id=? AND user_id=?", (x["id"],uid)).fetchall():
        plats[r["platform"]] = dict(r)
    x["platforms"] = plats
    sold_price = float(x.get("sold_price") or 0)
    x["net_profit"] = round(
        sold_price - float(x.get("cost") or 0) - float(x.get("sold_shipping_cost") or 0) - float(x.get("sold_fee") or 0), 2
    ) if x.get("status") == "sold" else None
    return x

def assign_legacy_data_if_needed(uid):
    conn = db()
    # Only claim old unowned data if this is the very first account.
    count_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    if count_users == 1:
        conn.execute("UPDATE listings SET user_id=? WHERE user_id IS NULL OR user_id=''", (uid,))
        conn.execute("UPDATE photos SET user_id=? WHERE user_id IS NULL OR user_id=''", (uid,))
        conn.execute("UPDATE platform_listings SET user_id=? WHERE user_id IS NULL OR user_id=''", (uid,))
        conn.commit()
    conn.close()

@app.route("/")
def home():
    if not current_user_id():
        return redirect(url_for("login_page"))
    return render_template("index.html")

@app.route("/login")
def login_page():
    if current_user_id():
        return redirect(url_for("home"))
    return render_template("login.html")

@app.post("/api/register")
def register():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    display_name = (data.get("display_name") or "").strip()
    business_name = (data.get("business_name") or "").strip()
    invite_code = (data.get("invite_code") or "").strip()
    if invite_code != BETA_INVITE_CODE:
        return jsonify({"error":"That beta invite code is not valid."}), 403
    if not email or "@" not in email:
        return jsonify({"error":"Enter a valid email."}), 400
    if len(password) < 8:
        return jsonify({"error":"Password must be at least 8 characters."}), 400
    conn = db()
    if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        conn.close()
        return jsonify({"error":"That email already has an account."}), 409
    uid = uuid.uuid4().hex
    is_admin = 1 if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0 else 0
    conn.execute("""INSERT INTO users(id,email,password_hash,display_name,business_name,is_admin)
                    VALUES (?,?,?,?,?,?)""",
                 (uid,email,generate_password_hash(password),display_name,business_name,is_admin))
    conn.commit()
    conn.close()
    session["user_id"] = uid
    assign_legacy_data_if_needed(uid)
    return jsonify({"ok":True})

@app.post("/api/login")
def login():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error":"Email or password is incorrect."}), 401
    session["user_id"] = row["id"]
    return jsonify({"ok":True})

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok":True})

@app.get("/api/me")
def me():
    uid = current_user_id()
    if not uid:
        return jsonify({"logged_in":False})
    conn = db()
    row = conn.execute("SELECT id,email,display_name,business_name,is_admin,created_at FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return jsonify({"logged_in":True,"user":dict(row)})

@app.get("/api/listings")
def get_listings():
    uid = current_user_id()
    if not uid: return jsonify([]), 401
    conn = db()
    rows = conn.execute("SELECT * FROM listings WHERE user_id=? ORDER BY created_at DESC",(uid,)).fetchall()
    out = [listing_to_dict(r, conn, uid) for r in rows]
    conn.close()
    return jsonify(out)

@app.get("/api/listings/<lid>")
def get_listing(lid):
    uid = current_user_id()
    if not uid: return jsonify({"error":"login required"}),401
    conn = db()
    row = conn.execute("SELECT * FROM listings WHERE id=? AND user_id=?", (lid,uid)).fetchone()
    if not row:
        conn.close(); return jsonify({"error":"not found"}),404
    out = listing_to_dict(row, conn, uid)
    conn.close()
    return jsonify(out)

def upsert_platforms(conn, uid, lid, pinfo, base_price):
    for p in PLATFORMS:
        v = pinfo.get(p, {})
        existing = conn.execute("SELECT remote_status FROM platform_listings WHERE listing_id=? AND platform=? AND user_id=?",(lid,p,uid)).fetchone()
        rs = existing["remote_status"] if existing else "not_connected"
        conn.execute("""INSERT INTO platform_listings
            (listing_id,platform,enabled,price,remote_status,user_id)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(listing_id,platform) DO UPDATE SET
              enabled=excluded.enabled, price=excluded.price, user_id=excluded.user_id""",
            (lid,p,1 if v.get("enabled") else 0,float(v.get("price") or base_price),rs,uid)
        )

@app.post("/api/listings")
def create_listing():
    uid = current_user_id()
    if not uid: return jsonify({"error":"login required"}),401
    form = request.form
    lid = uuid.uuid4().hex
    conn = db()
    values = (
        lid, form.get("title","").strip(), form.get("brand","").strip(), form.get("sku","").strip(),
        form.get("category","").strip(), form.get("size","").strip(), form.get("condition",""),
        float(form.get("cost") or 0), float(form.get("price") or 0), float(form.get("min_offer") or 0),
        float(form.get("shipping_cost") or 0), form.get("status","draft"), form.get("description","").strip(),
        form.get("notes","").strip(), form.get("storage_bin","").strip(), form.get("source","").strip(),
        form.get("purchase_date","").strip(), uid
    )
    conn.execute("""INSERT INTO listings
      (id,title,brand,sku,category,size,condition,cost,price,min_offer,shipping_cost,status,description,notes,storage_bin,source,purchase_date,user_id)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
    pinfo = json.loads(form.get("platforms","{}"))
    upsert_platforms(conn, uid, lid, pinfo, values[8])
    for i, f in enumerate(request.files.getlist("photos")[:12]):
        if f and f.filename:
            name = compress_save(f, uid, lid, i)
            conn.execute("INSERT INTO photos(listing_id,filename,sort_order,user_id) VALUES (?,?,?,?)",(lid,name,i,uid))
    conn.commit()
    row = conn.execute("SELECT * FROM listings WHERE id=? AND user_id=?", (lid,uid)).fetchone()
    out = listing_to_dict(row, conn, uid)
    conn.close()
    return jsonify(out)

@app.put("/api/listings/<lid>")
def update_listing(lid):
    uid = current_user_id()
    if not uid: return jsonify({"error":"login required"}),401
    form = request.form
    conn = db()
    old = conn.execute("SELECT * FROM listings WHERE id=? AND user_id=?", (lid,uid)).fetchone()
    if not old:
        conn.close(); return jsonify({"error":"not found"}),404
    base = float(form.get("price") or 0)
    conn.execute("""UPDATE listings SET
      title=?, brand=?, sku=?, category=?, size=?, condition=?, cost=?, price=?, min_offer=?, shipping_cost=?,
      status=?, description=?, notes=?, storage_bin=?, source=?, purchase_date=?, updated_at=CURRENT_TIMESTAMP
      WHERE id=? AND user_id=?""", (
        form.get("title","").strip(), form.get("brand","").strip(), form.get("sku","").strip(),
        form.get("category","").strip(), form.get("size","").strip(), form.get("condition",""),
        float(form.get("cost") or 0), base, float(form.get("min_offer") or 0),
        float(form.get("shipping_cost") or 0), form.get("status","draft"), form.get("description","").strip(),
        form.get("notes","").strip(), form.get("storage_bin","").strip(), form.get("source","").strip(),
        form.get("purchase_date","").strip(), lid, uid
    ))
    pinfo = json.loads(form.get("platforms","{}"))
    upsert_platforms(conn, uid, lid, pinfo, base)
    existing_count = conn.execute("SELECT COUNT(*) c FROM photos WHERE listing_id=? AND user_id=?",(lid,uid)).fetchone()["c"]
    for i, f in enumerate(request.files.getlist("photos")[:max(0,12-existing_count)], start=existing_count):
        if f and f.filename:
            name = compress_save(f, uid, lid, i)
            conn.execute("INSERT INTO photos(listing_id,filename,sort_order,user_id) VALUES (?,?,?,?)",(lid,name,i,uid))
    conn.commit()
    row = conn.execute("SELECT * FROM listings WHERE id=? AND user_id=?", (lid,uid)).fetchone()
    out = listing_to_dict(row, conn, uid)
    conn.close()
    return jsonify(out)

@app.get("/api/photos/<lid>")
def get_photos(lid):
    uid = current_user_id()
    if not uid: return jsonify([]),401
    conn = db()
    rows = conn.execute("SELECT id,filename,sort_order FROM photos WHERE listing_id=? AND user_id=? ORDER BY sort_order,id",(lid,uid)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.post("/api/listings/<lid>/sold")
def sold(lid):
    uid = current_user_id()
    if not uid: return jsonify({"error":"login required"}),401
    data = request.json or {}
    conn = db()
    conn.execute("""UPDATE listings SET status='sold', sold_platform=?, sold_price=?, sold_shipping_cost=?,
                    sold_fee=?, sold_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND user_id=?""",
                 (data.get("platform",""),float(data.get("sold_price") or 0),float(data.get("sold_shipping_cost") or 0),
                  float(data.get("sold_fee") or 0),lid,uid))
    conn.execute("""UPDATE platform_listings SET remote_status='sold_or_delist_pending'
                    WHERE listing_id=? AND user_id=? AND enabled=1""",(lid,uid))
    conn.commit(); conn.close()
    return jsonify({"ok":True})

@app.post("/api/listings/<lid>/reactivate")
def reactivate(lid):
    uid = current_user_id()
    if not uid: return jsonify({"error":"login required"}),401
    conn = db()
    conn.execute("""UPDATE listings SET status='active',sold_platform='',sold_price=0,sold_shipping_cost=0,
                    sold_fee=0,sold_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?""",(lid,uid))
    conn.execute("UPDATE platform_listings SET remote_status='not_connected' WHERE listing_id=? AND user_id=?",(lid,uid))
    conn.commit(); conn.close()
    return jsonify({"ok":True})

@app.post("/api/listings/<lid>/duplicate")
def duplicate(lid):
    uid = current_user_id()
    if not uid: return jsonify({"error":"login required"}),401
    conn = db()
    old = conn.execute("SELECT * FROM listings WHERE id=? AND user_id=?", (lid,uid)).fetchone()
    if not old:
        conn.close(); return jsonify({"error":"not found"}),404
    nid = uuid.uuid4().hex
    conn.execute("""INSERT INTO listings
      (id,title,brand,sku,category,size,condition,cost,price,min_offer,shipping_cost,status,description,notes,storage_bin,source,purchase_date,user_id)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (nid,old["title"]+" COPY",old["brand"],"",old["category"],old["size"],old["condition"],old["cost"],old["price"],
       old["min_offer"],old["shipping_cost"],"draft",old["description"],old["notes"],old["storage_bin"],old["source"],old["purchase_date"],uid))
    for r in conn.execute("SELECT * FROM platform_listings WHERE listing_id=? AND user_id=?",(lid,uid)).fetchall():
        conn.execute("""INSERT INTO platform_listings(listing_id,platform,enabled,price,remote_status,user_id)
                        VALUES (?,?,?,?,?,?)""",(nid,r["platform"],r["enabled"],r["price"],"not_connected",uid))
    conn.commit(); conn.close()
    return jsonify({"ok":True,"id":nid})

@app.delete("/api/listings/<lid>")
def delete_listing(lid):
    uid = current_user_id()
    if not uid: return jsonify({"error":"login required"}),401
    conn = db()
    files = conn.execute("SELECT filename FROM photos WHERE listing_id=? AND user_id=?",(lid,uid)).fetchall()
    for r in files:
        try: (UPLOADS/uid/r["filename"]).unlink()
        except: pass
    conn.execute("DELETE FROM photos WHERE listing_id=? AND user_id=?",(lid,uid))
    conn.execute("DELETE FROM platform_listings WHERE listing_id=? AND user_id=?",(lid,uid))
    conn.execute("DELETE FROM listings WHERE id=? AND user_id=?",(lid,uid))
    conn.commit(); conn.close()
    return jsonify({"ok":True})

@app.post("/api/listings/<lid>/queue")
def queue_listing(lid):
    uid = current_user_id()
    if not uid: return jsonify({"error":"login required"}),401
    data = request.json or {}
    requested = data.get("platforms", [])
    conn = db()
    row = conn.execute("SELECT * FROM listings WHERE id=? AND user_id=?", (lid,uid)).fetchone()
    if not row:
        conn.close(); return jsonify({"error":"not found"}),404
    queued, skipped = [], []
    for p in requested:
        pr = conn.execute("SELECT * FROM platform_listings WHERE listing_id=? AND platform=? AND user_id=?",(lid,p,uid)).fetchone()
        if not pr or not pr["enabled"]:
            skipped.append({"platform":p,"reason":"not enabled"}); continue
        conn.execute("UPDATE platform_listings SET remote_status='queued' WHERE listing_id=? AND platform=? AND user_id=?",(lid,p,uid))
        queued.append(p)
    if queued and row["status"] == "draft":
        conn.execute("UPDATE listings SET status='active',updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",(lid,uid))
    conn.commit(); conn.close()
    return jsonify({"ok":True,"queued":queued,"skipped":skipped})

@app.get("/api/queue")
def get_queue():
    uid = current_user_id()
    if not uid: return jsonify([]),401
    conn = db()
    rows = conn.execute("""SELECT p.listing_id,p.platform,p.price,p.remote_status,l.title,l.brand,l.size,l.status,l.sku
                           FROM platform_listings p JOIN listings l ON l.id=p.listing_id
                           WHERE p.enabled=1 AND p.user_id=? AND l.user_id=?
                           ORDER BY CASE p.remote_status WHEN 'queued' THEN 0 WHEN 'ready' THEN 1 WHEN 'live' THEN 2 ELSE 3 END,
                                    l.updated_at DESC""",(uid,uid)).fetchall()
    out=[dict(r) for r in rows]
    conn.close()
    return jsonify(out)

@app.get("/api/export/csv")
def export_csv():
    uid = current_user_id()
    if not uid: return redirect(url_for("login_page"))
    conn = db()
    rows = conn.execute("SELECT * FROM listings WHERE user_id=? ORDER BY created_at DESC",(uid,)).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Title","Brand","SKU","Category","Size","Condition","Cost","List Price","Min Offer","Shipping Est.","Status","Sold Platform","Sold Price","Sold Shipping","Sold Fee","Net Profit","Storage Bin","Source","Purchase Date","Created","Sold At"])
    for r in rows:
        d = dict(r)
        net = ""
        if d["status"] == "sold":
            net = round(float(d.get("sold_price") or 0)-float(d.get("cost") or 0)-float(d.get("sold_shipping_cost") or 0)-float(d.get("sold_fee") or 0),2)
        writer.writerow([d["title"],d["brand"],d["sku"],d["category"],d["size"],d["condition"],d["cost"],d["price"],d["min_offer"],d["shipping_cost"],d["status"],d["sold_platform"],d["sold_price"],d["sold_shipping_cost"],d["sold_fee"],net,d["storage_bin"],d["source"],d["purchase_date"],d["created_at"],d["sold_at"]])
    conn.close()
    data = output.getvalue().encode("utf-8-sig")
    return send_file(io.BytesIO(data), mimetype="text/csv", as_attachment=True, download_name=f"chaos_closet_inventory_{datetime.date.today().isoformat()}.csv")

@app.get("/api/backup")
def backup():
    uid = current_user_id()
    if not uid: return redirect(url_for("login_page"))
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    temp = BACKUPS / f"chaos_closet_backup_{stamp}.zip"
    if temp.exists(): temp.unlink()
    shutil.make_archive(str(temp.with_suffix("")), "zip", BASE, ".")
    return send_file(temp, as_attachment=True, download_name=temp.name)


@app.get("/health")
def health():
    try:
        conn = db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return jsonify({"ok":True,"app":"Chaos Closet Community Beta"}), 200
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}), 500

@app.get("/api/admin/stats")
def admin_stats():
    uid = current_user_id()
    if not uid:
        return jsonify({"error":"login required"}), 401
    conn = db()
    u = conn.execute("SELECT is_admin FROM users WHERE id=?", (uid,)).fetchone()
    if not u or not u["is_admin"]:
        conn.close()
        return jsonify({"error":"admin only"}), 403
    stats = {
        "users": conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"],
        "listings": conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"],
        "sold": conn.execute("SELECT COUNT(*) c FROM listings WHERE status='sold'").fetchone()["c"],
        "queued": conn.execute("SELECT COUNT(*) c FROM platform_listings WHERE remote_status='queued'").fetchone()["c"],
    }
    conn.close()
    return jsonify(stats)

@app.get("/api/status")
def status():
    return jsonify({
        "app":"Chaos Closet Crosslister v8 Private Beta",
        "multi_user":True,
        "platforms":{
            "eBay":"Waiting for developer approval / credentials",
            "Depop":"Requires Depop partner approval",
            "Vinted":"Connector not configured",
            "Mercari":"Connector not configured",
            "Poshmark":"Connector not configured"
        }
    })

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5055"))
    is_cloud = bool(os.environ.get("PORT"))
    if not is_cloud:
        def open_browser():
            time.sleep(1.2)
            webbrowser.open(f"http://127.0.0.1:{port}")
        threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="0.0.0.0" if is_cloud else "127.0.0.1", port=port, debug=False)
