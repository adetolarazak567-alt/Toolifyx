import os
import uuid
import subprocess
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from flask import Flask, request, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from flask_cors import CORS

# ------------------------- CONFIGURATION -------------------------
app = Flask(__name__)
CORS(app)  # Allow frontend from any origin

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///toolifyx.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024 * 1024  # 5GB max upload
app.config["MAX_CONCURRENT_JOBS"] = 4  # Prevent resource exhaustion

UPLOAD_DIR = "/tmp"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

db = SQLAlchemy(app)

# ------------------------- MODELS -------------------------
class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.String(64), unique=True, nullable=False)
    job_type = db.Column(db.String(20), nullable=False)
    filename = db.Column(db.String(300))
    status = db.Column(db.String(20), default="queued")
    progress = db.Column(db.Integer, default=0)
    error_msg = db.Column(db.String(500))
    created = db.Column(db.DateTime, default=datetime.utcnow)
    completed = db.Column(db.DateTime)

with app.app_context():
    db.create_all()

# ------------------------- THREAD POOL & JOB TRACKER -------------------------
executor = ThreadPoolExecutor(max_workers=app.config["MAX_CONCURRENT_JOBS"])
active_jobs = {}  # job_id -> {progress, status, error}
job_lock = threading.Lock()  # To protect `active_jobs`

# ------------------------- HELPERS -------------------------
def get_video_duration(file_path):
    """Return duration in milliseconds using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
            file_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip()) * 1000  # ms
    except Exception:
        return None  # Unknown duration

def is_video_file(file_path):
    """Check if file is a valid video via ffprobe."""
    try:
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=format_name", file_path],
            capture_output=True, check=True
        )
        return True
    except subprocess.CalledProcessError:
        return False

def update_db(job_id, status=None, progress=None, error_msg=None):
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

def update_active_job(job_id, progress=None, status=None, error=None):
    """Thread‑safe update of in‑memory job tracker."""
    with job_lock:
        if job_id not in active_jobs:
            active_jobs[job_id] = {"progress": 0, "status": "queued", "error": None}
        if progress is not None:
            active_jobs[job_id]["progress"] = progress
        if status is not None:
            active_jobs[job_id]["status"] = status
        if error is not None:
            active_jobs[job_id]["error"] = error

# ------------------------- WORKER FUNCTIONS -------------------------
def run_ffmpeg_job(job_id, input_path, output_path, cmd, total_duration_ms):
    """Generic FFmpeg runner with progress tracking."""
    try:
        update_active_job(job_id, status="processing", progress=1)
        update_db(job_id, status="processing", progress=1)

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        for line in process.stdout:
            if "out_time_ms=" in line:
                try:
                    current_ms = int(line.strip().split("=")[1])
                    if total_duration_ms and total_duration_ms > 0:
                        percent = min(99, int((current_ms * 100) / total_duration_ms))
                        update_active_job(job_id, progress=percent)
                        update_db(job_id, progress=percent)
                except (ValueError, IndexError):
                    pass

        process.wait()

        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg exited with code {process.returncode}")

        update_active_job(job_id, status="done", progress=100)
        update_db(job_id, status="done", progress=100)

    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}")
        update_active_job(job_id, status="error", error=str(e))
        update_db(job_id, status="error", error_msg=str(e))
    finally:
        # Cleanup input file
        if os.path.exists(input_path):
            os.remove(input_path)

def run_compress(job_id, input_path, output_path, crf):
    """Video compression worker."""
    total_duration_ms = get_video_duration(input_path)
    if total_duration_ms is None:
        # Fallback to approximate progress (50 min video assumption)
        total_duration_ms = 3_000_000  # 50 minutes in ms

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
    run_ffmpeg_job(job_id, input_path, output_path, cmd, total_duration_ms)

def run_mp3(job_id, input_path, output_path, bitrate):
    """MP3 conversion worker."""
    total_duration_ms = get_video_duration(input_path)
    if total_duration_ms is None:
        total_duration_ms = 3_000_000

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-ab", bitrate,
        "-ar", "44100",
        "-ac", "2",
        "-progress", "pipe:1",
        "-nostats",
        output_path
    ]
    run_ffmpeg_job(job_id, input_path, output_path, cmd, total_duration_ms)

# ------------------------- ROUTES -------------------------
@app.route("/")
def home():
    return "ToolifyX backend running", 200

@app.route("/health")
def health():
    return jsonify({"status": "healthy"}), 200

@app.route("/api/compress", methods=["POST"])
def compress():
    file = request.files.get("video")
    level = request.form.get("level", "medium")

    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    # Validate file extension (optional) and content
    filename = secure_filename(file.filename)
    if not filename.lower().endswith(('.mp4', '.mov', '.avi', '.mkv', '.webm')):
        return jsonify({"error": "Unsupported video file type"}), 400

    job_id = uuid.uuid4().hex
    input_path = os.path.join(UPLOAD_DIR, f"{job_id}_in_{filename}")
    output_path = os.path.join(UPLOAD_DIR, f"{job_id}.mp4")

    file.save(input_path)

    # Verify it's really a video
    if not is_video_file(input_path):
        os.remove(input_path)
        return jsonify({"error": "Uploaded file is not a valid video"}), 400

    crf_map = {"low": 18, "medium": 23, "high": 28}
    crf = crf_map.get(level, 23)

    # DB record
    db.session.add(Job(job_id=job_id, job_type="compress", filename=filename, status="queued", progress=0))
    db.session.commit()

    update_active_job(job_id, progress=0, status="queued")

    # Submit to thread pool
    executor.submit(run_compress, job_id, input_path, output_path, crf)

    return jsonify({"job_id": job_id})

@app.route("/api/convert-mp3", methods=["POST"])
def convert_mp3():
    file = request.files.get("video")
    bitrate = request.form.get("bitrate", "192k")

    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    filename = secure_filename(file.filename)
    if not filename.lower().endswith(('.mp4', '.mov', '.avi', '.mkv', '.webm')):
        return jsonify({"error": "Unsupported video file type"}), 400

    job_id = uuid.uuid4().hex
    input_path = os.path.join(UPLOAD_DIR, f"{job_id}_in_{filename}")
    output_path = os.path.join(UPLOAD_DIR, f"{job_id}.mp3")

    file.save(input_path)

    if not is_video_file(input_path):
        os.remove(input_path)
        return jsonify({"error": "Uploaded file is not a valid video"}), 400

    db.session.add(Job(job_id=job_id, job_type="mp3", filename=filename, status="queued", progress=0))
    db.session.commit()

    update_active_job(job_id, progress=0, status="queued")

    executor.submit(run_mp3, job_id, input_path, output_path, bitrate)

    return jsonify({"job_id": job_id})

@app.route("/api/progress/<job_id>")
def progress(job_id):
    with job_lock:
        data = active_jobs.get(job_id, {"progress": 0, "status": "unknown", "error": None})
    return jsonify({
        "progress": data["progress"],
        "status": data["status"],
        "error": data["error"]
    })

@app.route("/api/download/<job_id>")
def download(job_id):
    path = os.path.join(UPLOAD_DIR, f"{job_id}.mp4")
    if not os.path.exists(path):
        return jsonify({"error": "File not ready"}), 404
    name = request.args.get("name", "compressed.mp4")
    return send_file(path, as_attachment=True, download_name=name)

@app.route("/api/download-mp3/<job_id>")
def download_mp3(job_id):
    path = os.path.join(UPLOAD_DIR, f"{job_id}.mp3")
    if not os.path.exists(path):
        return jsonify({"error": "File not ready"}), 404
    name = request.args.get("name", "audio.mp3")
    return send_file(path, as_attachment=True, download_name=name)

@app.route("/admin/stats")
def stats():
    today = date.today()
    return jsonify({
        "total_jobs": Job.query.count(),
        "today_jobs": Job.query.filter(db.func.date(Job.created) == today).count(),
        "compress_jobs": Job.query.filter_by(job_type="compress").count(),
        "mp3_jobs": Job.query.filter_by(job_type="mp3").count()
    })

# ------------------------- CLEANUP (background thread) -------------------------
def cleanup_loop():
    while True:
        time.sleep(600)
        try:
            cutoff = time.time() - 3600 * 24  # 24 hours (increased from 1h)
            for f in os.listdir(UPLOAD_DIR):
                path = os.path.join(UPLOAD_DIR, f)
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    logger.info(f"Cleaned up old file: {f}")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
cleanup_thread.start()

# ------------------------- SHUTDOWN (graceful) -------------------------
@app.teardown_appcontext
def shutdown(exception=None):
    executor.shutdown(wait=True)

# ------------------------- RUN -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)