from flask import Flask, request, jsonify, send_file, redirect
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date
import os, string, random, subprocess, uuid

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///toolifyx.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024 * 1024

db = SQLAlchemy(app)

# ---------------- MODELS ----------------

class ShortURL(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original = db.Column(db.String(500))
    code = db.Column(db.String(10), unique=True)
    clicks = db.Column(db.Integer, default=0)
    created = db.Column(db.DateTime, default=datetime.utcnow)

class Compression(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(200))
    created = db.Column(db.DateTime, default=datetime.utcnow)

# ✅ FIX FOR RENDER / GUNICORN
with app.app_context():
    db.create_all()

# ---------------- HELPERS ----------------

def gen_code(length=6):
    chars = string.ascii_letters + string.digits
    while True:
        c = "".join(random.choice(chars) for _ in range(length))
        if not ShortURL.query.filter_by(code=c).first():
            return c

# ---------------- URL SHORTENER ----------------

@app.route("/api/shorten", methods=["POST"])
def shorten():
    url = request.json.get("url","").strip()
    if not url.startswith(("http://","https://")):
        return jsonify({"error":"Invalid URL"}),400

    existing = ShortURL.query.filter_by(original=url).first()
    if existing:
        return jsonify({"short_url": request.host_url + existing.code})

    code = gen_code()
    db.session.add(ShortURL(original=url, code=code))
    db.session.commit()

    return jsonify({"short_url": request.host_url + code})

@app.route("/<code>")
def redirect_url(code):
    u = ShortURL.query.filter_by(code=code).first_or_404()
    u.clicks += 1
    db.session.commit()
    return redirect(u.original)

# ---------------- VIDEO COMPRESS ----------------

@app.route("/api/compress", methods=["POST"])
def compress():
    file = request.files.get("video")
    level = request.form.get("level","medium")

    crf = {"low":"18","medium":"23","high":"28"}.get(level,"23")

    inp = f"/tmp/{uuid.uuid4().hex}_{file.filename}"
    out = f"/tmp/toolifyx_{uuid.uuid4().hex}.mp4"

    file.save(inp)

    cmd = [
        "ffmpeg","-i",inp,
        "-preset","ultrafast",
        "-threads","2",
        "-crf",crf,
        "-movflags","faststart",
        "-vcodec","libx264",
        "-acodec","aac",
        "-y",out
    ]

    subprocess.run(cmd, check=True)

    db.session.add(Compression(filename=os.path.basename(out)))
    db.session.commit()

    response = send_file(out, as_attachment=True)

    @response.call_on_close
    def clean():
        if os.path.exists(inp): os.remove(inp)
        if os.path.exists(out): os.remove(out)

    return response

# ---------------- ADMIN — URL ----------------

@app.route("/admin/url/stats")
def url_stats():
    return jsonify({
        "total_links": ShortURL.query.count(),
        "total_clicks": db.session.query(db.func.sum(ShortURL.clicks)).scalar() or 0
    })

@app.route("/admin/url/daily")
def url_daily():
    data = db.session.query(
        db.func.date(ShortURL.created),
        db.func.count(ShortURL.id)
    ).group_by(db.func.date(ShortURL.created)).all()

    return jsonify([{"date":str(d[0]),"count":d[1]} for d in data])

# ---------------- ADMIN — COMPRESSION ----------------

@app.route("/admin/compress/stats")
def compress_stats():
    today = date.today()
    return jsonify({
        "total": Compression.query.count(),
        "today": Compression.query.filter(
            db.func.date(Compression.created)==today
        ).count()
    })

@app.route("/admin/compress/daily")
def compress_daily():
    data = db.session.query(
        db.func.date(Compression.created),
        db.func.count(Compression.id)
    ).group_by(db.func.date(Compression.created)).all()

    return jsonify([{"date":str(d[0]),"count":d[1]} for d in data])

# ---------------- RUN ----------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",5000)))