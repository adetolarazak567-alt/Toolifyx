from flask import Flask, request, jsonify, send_file, redirect
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date
import os, string, random, subprocess, uuid, threading, time

app = Flask(__name__)

# ---------------- CONFIG ----------------
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///toolifyx.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024 * 1024  # 5GB

db = SQLAlchemy(app)

UPLOAD_DIR = "/tmp"

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

with app.app_context():
    db.create_all()

# ---------------- ROOT ----------------
@app.route("/")
def home():
    return "ToolifyX backend running ✅", 200

# ---------------- HELPERS ----------------
def gen_code(length=6):
    chars = string.ascii_letters + string.digits
    while True:
        c = "".join(random.choice(chars) for _ in range(length))
        if not ShortURL.query.filter_by(code=c).first():
            return c

# ---------------- GLOBAL PROGRESS ----------------
compression_jobs = {}

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

# ---------------- VIDEO COMPRESSION WORKER ----------------
def run_ffmpeg(job_id, input_path, output_path, crf):
    try:
        compression_jobs[job_id]["status"] = "processing"
        compression_jobs[job_id]["progress"] = 1

        cmd = [
            "ffmpeg",
            "-y",
            "-i", input_path,
            "-vcodec", "libx264",
            "-preset", "ultrafast",
            "-crf", str(crf),
            "-acodec", "aac",
            "-movflags", "faststart",
            "-progress", "pipe:1",
            "-nostats",
            output_path
        ]

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        for line in process.stdout:
            if "out_time_ms=" in line:
                ms = int(line.split("=")[1])
                percent = min(95, int(ms / 300000))  # safe estimate
                compression_jobs[job_id]["progress"] = percent

        process.wait()

        compression_jobs[job_id]["progress"] = 100
        compression_jobs[job_id]["status"] = "done"

        with app.app_context():
            db.session.add(Compression(filename=os.path.basename(output_path)))
            db.session.commit()

        # cleanup input
        if os.path.exists(input_path):
            os.remove(input_path)

    except Exception as e:
        compression_jobs[job_id]["status"] = "error"
        compression_jobs[job_id]["error"] = str(e)

# ---------------- START COMPRESSION ----------------
@app.route("/api/compress", methods=["POST"])
def compress():
    file = request.files.get("video")
    level = request.form.get("level", "medium")

    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    job_id = uuid.uuid4().hex

    crf_map = {
        "low": 18,
        "medium": 23,
        "high": 28
    }
    crf = crf_map.get(level, 23)

    input_path = os.path.join(UPLOAD_DIR, f"{job_id}_{file.filename}")
    output_path = os.path.join(UPLOAD_DIR, f"{job_id}.mp4")

    file.save(input_path)

    compression_jobs[job_id] = {
        "progress": 0,
        "status": "queued"
    }

    t = threading.Thread(
        target=run_ffmpeg,
        args=(job_id, input_path, output_path, crf),
        daemon=True
    )
    t.start()

    return jsonify({
        "job_id": job_id,
        "download": f"/api/download/{job_id}"
    })

# ---------------- PROGRESS ----------------
@app.route("/api/progress/<job_id>")
def progress(job_id):
    return jsonify(compression_jobs.get(job_id, {"progress": 0}))

# ---------------- DOWNLOAD ----------------
@app.route("/api/download/<job_id>")
def download(job_id):
    path = os.path.join(UPLOAD_DIR, f"{job_id}.mp4")
    if not os.path.exists(path):
        return jsonify({"error": "File not ready"}), 404
    return send_file(path, as_attachment=True)

# ---------------- ADMIN ----------------
@app.route("/admin/compress/stats")
def compress_stats():
    today = date.today()
    return jsonify({
        "total": Compression.query.count(),
        "today": Compression.query.filter(
            db.func.date(Compression.created) == today
        ).count()
    })

@app.route("/admin/url/stats")
def url_stats():
    return jsonify({
        "total_links": ShortURL.query.count(),
        "total_clicks": db.session.query(db.func.sum(ShortURL.clicks)).scalar() or 0
    })

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))