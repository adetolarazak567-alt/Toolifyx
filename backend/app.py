from flask import Flask, request, jsonify, send_file, redirect
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date
import os, string, random, subprocess, uuid

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///toolifyx.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024 * 1024  # 5GB max

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

db.create_all()

# ---------------- HELPERS ----------------

def gen_code(length=6):
    chars = string.ascii_letters + string.digits
    while True:
        c = "".join(random.choice(chars) for _ in range(length))
        if not ShortURL.query.filter_by(code=c).first():
            return c

# ---------------- IN-MEMORY PROGRESS ----------------
compression_progress = {}

# ---------------- URL SHORTENER ----------------

@app.route("/api/shorten", methods=["POST"])
def shorten():
    url = request.json.get("url", "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Invalid URL"}), 400

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
    level = request.form.get("level", "medium")

    job_id = uuid.uuid4().hex
    compression_progress[job_id] = 0

    crf = {"low": "18", "medium": "23", "high": "28"}.get(level, "23")

    inp = f"/tmp/{job_id}_{file.filename}"
    out = f"/tmp/{job_id}.mp4"
    file.save(inp)

    cmd = [
        "ffmpeg",
        "-i", inp,
        "-preset", "ultrafast",
        "-movflags", "faststart",
        "-vcodec", "libx264",
        "-acodec", "aac",
        "-crf", crf,
        "-progress", "pipe:1",
        "-nostats",
        "-y", out
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True
    )

    for line in process.stdout:
        if "out_time_ms=" in line:
            ms = int(line.split("=")[1])
            # Approximate percentage
            compression_progress[job_id] = min(99, int(ms / 100000))
    process.wait()
    compression_progress[job_id] = 100

    # Record in DB
    db.session.add(Compression(filename=os.path.basename(out)))
    db.session.commit()

    return jsonify({
        "job_id": job_id,
        "download": f"/api/download/{job_id}"
    })

# ---------------- PROGRESS CHECK ----------------
@app.route("/api/progress/<job_id>")
def progress(job_id):
    return jsonify({"progress": compression_progress.get(job_id, 0)})

# ---------------- DOWNLOAD ----------------
@app.route("/api/download/<job_id>")
def download(job_id):
    path = f"/tmp/{job_id}.mp4"
    return send_file(path, as_attachment=True)

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

    return jsonify([{"date": str(d[0]), "count": d[1]} for d in data])

# ---------------- ADMIN — COMPRESSION ----------------
@app.route("/admin/compress/stats")
def compress_stats():
    today = date.today()
    return jsonify({
        "total": Compression.query.count(),
        "today": Compression.query.filter(
            db.func.date(Compression.created) == today
        ).count()
    })

@app.route("/admin/compress/daily")
def compress_daily():
    data = db.session.query(
        db.func.date(Compression.created),
        db.func.count(Compression.id)
    ).group_by(db.func.date(Compression.created)).all()

    return jsonify([{"date": str(d[0]), "count": d[1]} for d in data])

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))