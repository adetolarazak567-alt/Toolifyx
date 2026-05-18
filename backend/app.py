from flask import Flask, request, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date
import os, subprocess, uuid, threading, time

app = Flask(__name__)

# ---------------- CONFIG ----------------
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///toolifyx.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024 * 1024  # 5GB max upload

db = SQLAlchemy(app)

UPLOAD_DIR = "/tmp"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ---------------- MODELS ----------------
class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.String(64), unique=True, nullable=False)
    job_type = db.Column(db.String(20), nullable=False)   # "compress" or "mp3"
    filename = db.Column(db.String(300))
    status = db.Column(db.String(20), default="queued")    # queued / processing / done / error
    progress = db.Column(db.Integer, default=0)
    error_msg = db.Column(db.String(500))
    created = db.Column(db.DateTime, default=datetime.utcnow)
    completed = db.Column(db.DateTime)

with app.app_context():
    db.create_all()

# ---------------- IN-MEMORY JOB TRACKER ----------------
# (mirrors DB for fast polling; DB is source of truth)
active_jobs = {}

# ---------------- ROOT ----------------
@app.route("/")
def home():
    return "ToolifyX backend running ", 200

# ---------------- VIDEO COMPRESSION WORKER ----------------
def run_compress(job_id, input_path, output_path, crf):
    """Background thread: compress video with FFmpeg."""
    try:
        active_jobs[job_id]["status"] = "processing"
        active_jobs[job_id]["progress"] = 1
        _update_db(job_id, status="processing", progress=1)

        cmd = [
            "ffmpeg", "-y",
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

        # Estimate progress from output time
        for line in process.stdout:
            if "out_time_ms=" in line:
                try:
                    ms = int(line.strip().split("=")[1])
                    # Rough estimate: 5 min video = 300,000,000 us
                    percent = min(95, int(ms / 3_000_000))
                    active_jobs[job_id]["progress"] = percent
                    _update_db(job_id, progress=percent)
                except (ValueError, IndexError):
                    pass

        process.wait()

        if process.returncode != 0:
            raise RuntimeError("FFmpeg exited with code " + str(process.returncode))

        active_jobs[job_id]["progress"] = 100
        active_jobs[job_id]["status"] = "done"
        _update_db(job_id, status="done", progress=100)

        # Cleanup input
        if os.path.exists(input_path):
            os.remove(input_path)

    except Exception as e:
        active_jobs[job_id]["status"] = "error"
        active_jobs[job_id]["error"] = str(e)
        _update_db(job_id, status="error", error_msg=str(e))
        if os.path.exists(input_path):
            os.remove(input_path)

# ---------------- MP3 CONVERSION WORKER ----------------
def run_mp3(job_id, input_path, output_path, bitrate):
    """Background thread: extract audio to MP3 with FFmpeg."""
    try:
        active_jobs[job_id]["status"] = "processing"
        active_jobs[job_id]["progress"] = 1
        _update_db(job_id, status="processing", progress=1)

        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-vn",                          # no video
            "-acodec", "libmp3lame",
            "-ab", bitrate,
            "-ar", "44100",
            "-ac", "2",
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
                try:
                    ms = int(line.strip().split("=")[1])
                    percent = min(95, int(ms / 3_000_000))
                    active_jobs[job_id]["progress"] = percent
                    _update_db(job_id, progress=percent)
                except (ValueError, IndexError):
                    pass

        process.wait()

        if process.returncode != 0:
            raise RuntimeError("FFmpeg exited with code " + str(process.returncode))

        active_jobs[job_id]["progress"] = 100
        active_jobs[job_id]["status"] = "done"
        _update_db(job_id, status="done", progress=100)

        if os.path.exists(input_path):
            os.remove(input_path)

    except Exception as e:
        active_jobs[job_id]["status"] = "error"
        active_jobs[job_id]["error"] = str(e)
        _update_db(job_id, status="error", error_msg=str(e))
        if os.path.exists(input_path):
            os.remove(input_path)

# ---------------- DB HELPER ----------------
def _update_db(job_id, status=None, progress=None, error_msg=None):
    """Update job record in SQLite."""
    with app.app_context():
        job = Job.query.filter_by(job_id=job_id).first()
        if job:
            if status is not None:
                job.status = status
                if status in ("done", "error"):
                    job.completed = datetime.utcnow()
            if progress is not None:
                job.progress = progress
            if error_msg is not None:
                job.error_msg = error_msg
            db.session.commit()

# ---------------- START COMPRESSION ----------------
@app.route("/api/compress", methods=["POST"])
def compress():
    file = request.files.get("video")
    level = request.form.get("level", "medium")

    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    job_id = uuid.uuid4().hex

    crf_map = {"low": 18, "medium": 23, "high": 28}
    crf = crf_map.get(level, 23)

    input_path = os.path.join(UPLOAD_DIR, f"{job_id}_in_{file.filename}")
    output_path = os.path.join(UPLOAD_DIR, f"{job_id}.mp4")

    file.save(input_path)

    # Record in DB
    db.session.add(Job(
        job_id=job_id,
        job_type="compress",
        filename=file.filename,
        status="queued",
        progress=0
    ))
    db.session.commit()

    active_jobs[job_id] = {"progress": 0, "status": "queued"}

    t = threading.Thread(
        target=run_compress,
        args=(job_id, input_path, output_path, crf),
        daemon=True
    )
    t.start()

    return jsonify({"job_id": job_id})

# ---------------- START MP3 CONVERSION ----------------
@app.route("/api/convert-mp3", methods=["POST"])
def convert_mp3():
    file = request.files.get("video")
    bitrate = request.form.get("bitrate", "192k")

    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    job_id = uuid.uuid4().hex

    input_path = os.path.join(UPLOAD_DIR, f"{job_id}_in_{file.filename}")
    output_path = os.path.join(UPLOAD_DIR, f"{job_id}.mp3")

    file.save(input_path)

    db.session.add(Job(
        job_id=job_id,
        job_type="mp3",
        filename=file.filename,
        status="queued",
        progress=0
    ))
    db.session.commit()

    active_jobs[job_id] = {"progress": 0, "status": "queued"}

    t = threading.Thread(
        target=run_mp3,
        args=(job_id, input_path, output_path, bitrate),
        daemon=True
    )
    t.start()

    return jsonify({"job_id": job_id})

# ---------------- PROGRESS (works for both job types) ----------------
@app.route("/api/progress/<job_id>")
def progress(job_id):
    data = active_jobs.get(job_id, {"progress": 0, "status": "unknown"})
    return jsonify({
        "progress": data.get("progress", 0),
        "status": data.get("status", "unknown"),
        "error": data.get("error", None)
    })

# ---------------- DOWNLOAD COMPRESSED VIDEO ----------------
@app.route("/api/download/<job_id>")
def download(job_id):
    path = os.path.join(UPLOAD_DIR, f"{job_id}.mp4")
    if not os.path.exists(path):
        return jsonify({"error": "File not ready"}), 404

    name = request.args.get("name", "compressed.mp4")
    return send_file(path, as_attachment=True, download_name=name)

# ---------------- DOWNLOAD MP3 ----------------
@app.route("/api/download-mp3/<job_id>")
def download_mp3(job_id):
    path = os.path.join(UPLOAD_DIR, f"{job_id}.mp3")
    if not os.path.exists(path):
        return jsonify({"error": "File not ready"}), 404

    name = request.args.get("name", "audio.mp3")
    return send_file(path, as_attachment=True, download_name=name)

# ---------------- ADMIN STATS ----------------
@app.route("/admin/stats")
def stats():
    today = date.today()
    return jsonify({
        "total_jobs": Job.query.count(),
        "today_jobs": Job.query.filter(
            db.func.date(Job.created) == today
        ).count(),
        "compress_jobs": Job.query.filter_by(job_type="compress").count(),
        "mp3_jobs": Job.query.filter_by(job_type="mp3").count()
    })

# ---------------- CLEANUP OLD FILES (runs every 10 min in thread) ----------------
def cleanup_loop():
    while True:
        time.sleep(600)
        try:
            cutoff = time.time() - 3600  # 1 hour old
            for f in os.listdir(UPLOAD_DIR):
                path = os.path.join(UPLOAD_DIR, f)
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
        except Exception:
            pass

cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
cleanup_thread.start()

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
