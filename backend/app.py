import os
import uuid
import subprocess
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date
from flask import Flask, request, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from flask_cors import CORS

# ------------------------- CONFIGURATION -------------------------
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///toolifyx.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500MB limit

UPLOAD_DIR = "/tmp/toolifyx"
os.makedirs(UPLOAD_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

db = SQLAlchemy(app)

# ------------------------- MODELS -------------------------
class Job(db.Model):
    __tablename__ = "jobs"
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.String(64), unique=True, nullable=False)
    job_type = db.Column(db.String(20), nullable=False)
    filename = db.Column(db.String(300))
    status = db.Column(db.String(20), default="queued")
    progress = db.Column(db.Integer, default=0)
    error_msg = db.Column(db.String(1000))
    created = db.Column(db.DateTime, default=datetime.utcnow)
    completed = db.Column(db.DateTime)

with app.app_context():
    db.create_all()

# ------------------------- THREAD POOL -------------------------
executor = ThreadPoolExecutor(max_workers=2)
active_jobs = {}
job_lock = threading.Lock()

# ------------------------- HELPERS -------------------------
def get_video_duration(file_path):
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
            file_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip()) * 1000
    except Exception as e:
        logger.warning(f"Could not get duration: {e}")
        return 3_000_000

def is_video_file(file_path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=format_name", file_path],
            capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0
    except Exception:
        return False

def update_db(job_id, status=None, progress=None, error_msg=None):
    try:
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
    except Exception as e:
        logger.error(f"DB update failed: {e}")

def update_active_job(job_id, progress=None, status=None, error=None):
    with job_lock:
        if job_id not in active_jobs:
            active_jobs[job_id] = {"progress": 0, "status": "queued", "error": None}
        if progress is not None:
            active_jobs[job_id]["progress"] = progress
        if status is not None:
            active_jobs[job_id]["status"] = status
        if error is not None:
            active_jobs[job_id]["error"] = error

# ------------------------- WORKER -------------------------
def run_ffmpeg_job(job_id, input_path, output_path, cmd, total_duration_ms):
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

        process.wait(timeout=600)
        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg exited with code {process.returncode}")

        update_active_job(job_id, status="done", progress=100)
        update_db(job_id, status="done", progress=100)

    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}")
        update_active_job(job_id, status="error", error=str(e))
        update_db(job_id, status="error", error_msg=str(e))
    finally:
        if os.path.exists(input_path):
            os.remove(input_path)

def run_compress(job_id, input_path, output_path, level):
    try:
        # Get original file size
        original_size = os.path.getsize(input_path)
        
        # Get duration in seconds
        duration_probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", input_path],
            capture_output=True, text=True, timeout=30
        )
        duration_sec = float(duration_probe.stdout.strip())
        
        # Target file size percentages
        # Low = 80% of original (slightly smaller, better quality)
        # Medium = 50% of original (balanced)
        # High = 25% of original (much smaller)
        if level == "low":
            target_pct = 0.80
        elif level == "medium":
            target_pct = 0.50
        else:  # high
            target_pct = 0.25
        
        # Calculate target bitrate from desired file size
        # File size = (bitrate * duration) / 8
        # So bitrate = (target_size * 8) / duration
        target_size_bytes = original_size * target_pct
        target_bitrate = int((target_size_bytes * 8) / duration_sec)
        
        # Sanity checks: min 200k, max = original bitrate
        min_bitrate = 200_000
        max_probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=bit_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", input_path],
            capture_output=True, text=True, timeout=30
        )
        try:
            max_bitrate = int(max_probe.stdout.strip())
        except:
            max_bitrate = 5_000_000
        
        target_bitrate = max(min_bitrate, min(target_bitrate, max_bitrate))
        
        total_duration_ms = get_video_duration(input_path) or int(duration_sec * 1000)
        
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", input_path,
            "-c:v", "libx264",
            "-preset", "fast",
            "-b:v", str(target_bitrate),
            "-maxrate", str(int(target_bitrate * 1.2)),
            "-bufsize", str(target_bitrate * 2),
            "-c:a", "aac", "-b:a", "64k",
            "-movflags", "faststart",
            "-progress", "pipe:1", "-nostats",
            output_path
        ]
        
        run_ffmpeg_job(job_id, input_path, output_path, cmd, total_duration_ms)
        
    except Exception as e:
        logger.error(f"Compress setup failed: {e}")
        update_active_job(job_id, status="error", error=str(e))
        update_db(job_id, status="error", error_msg=str(e))
        if os.path.exists(input_path):
            os.remove(input_path)

def run_mp3(job_id, input_path, output_path, bitrate):
    total_duration_ms = get_video_duration(input_path)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-vn", "-acodec", "libmp3lame",
        "-ab", bitrate, "-ar", "44100", "-ac", "2",
        "-progress", "pipe:1", "-nostats",
        output_path
    ]
    run_ffmpeg_job(job_id, input_path, output_path, cmd, total_duration_ms)

# ------------------------- ROUTES -------------------------
@app.route("/")
def home():
    return jsonify({"status": "running", "version": "1.0"}), 200

@app.route("/health")
def health():
    return jsonify({
        "status": "healthy",
        "active_jobs": len(active_jobs),
        "upload_dir": UPLOAD_DIR,
        "disk_free": shutil.disk_usage(UPLOAD_DIR).free // (1024*1024)
    }), 200

@app.route("/api/compress", methods=["POST"])
def compress():
    logger.info("=== COMPRESS REQUEST ===")
    logger.info(f"Files: {list(request.files.keys())}")
    logger.info(f"Form: {list(request.form.keys())}")

    if "video" not in request.files:
        logger.error("No 'video' field in request.files")
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["video"]
    level = request.form.get("level", "medium")
    logger.info(f"File: {file.filename}, Level: {level}")

    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    filename = secure_filename(file.filename)
    logger.info(f"Secure filename: {filename}")

    allowed = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".3gp")
    if not filename.lower().endswith(allowed):
        logger.error(f"Bad extension: {filename}")
        return jsonify({"error": f"Unsupported file type: {filename}"}), 400

    job_id = uuid.uuid4().hex
    input_path = os.path.join(UPLOAD_DIR, f"{job_id}_in_{filename}")
    output_path = os.path.join(UPLOAD_DIR, f"{job_id}.mp4")

    try:
        logger.info(f"Saving to {input_path}")
        file.save(input_path)
        logger.info(f"Saved. Size: {os.path.getsize(input_path)} bytes")

        if not is_video_file(input_path):
            os.remove(input_path)
            return jsonify({"error": "Uploaded file is not a valid video"}), 400

        with app.app_context():
            db.session.add(Job(
                job_id=job_id, job_type="compress", 
                filename=filename, status="queued", progress=0
            ))
            db.session.commit()

        update_active_job(job_id, progress=0, status="queued")
        executor.submit(run_compress, job_id, input_path, output_path, level)
        logger.info(f"Job {job_id} submitted")

        return jsonify({"job_id": job_id}), 200

    except Exception as e:
        logger.error(f"Compress setup failed: {e}", exc_info=True)
        if os.path.exists(input_path):
            os.remove(input_path)
        return jsonify({"error": str(e)}), 500

@app.route("/api/convert-mp3", methods=["POST"])
def convert_mp3():
    logger.info("=== MP3 REQUEST ===")
    logger.info(f"Files: {list(request.files.keys())}")

    if "video" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["video"]
    bitrate = request.form.get("bitrate", "192k")

    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    filename = secure_filename(file.filename)
    allowed = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".3gp")
    if not filename.lower().endswith(allowed):
        return jsonify({"error": f"Unsupported file type: {filename}"}), 400

    job_id = uuid.uuid4().hex
    input_path = os.path.join(UPLOAD_DIR, f"{job_id}_in_{filename}")
    output_path = os.path.join(UPLOAD_DIR, f"{job_id}.mp3")

    try:
        file.save(input_path)

        if not is_video_file(input_path):
            os.remove(input_path)
            return jsonify({"error": "Uploaded file is not a valid video"}), 400

        with app.app_context():
            db.session.add(Job(
                job_id=job_id, job_type="mp3",
                filename=filename, status="queued", progress=0
            ))
            db.session.commit()

        update_active_job(job_id, progress=0, status="queued")
        executor.submit(run_mp3, job_id, input_path, output_path, bitrate)

        return jsonify({"job_id": job_id}), 200

    except Exception as e:
        logger.error(f"MP3 setup failed: {e}", exc_info=True)
        if os.path.exists(input_path):
            os.remove(input_path)
        return jsonify({"error": str(e)}), 500

@app.route("/api/progress/<job_id>")
def progress(job_id):
    with job_lock:
        data = active_jobs.get(job_id, {"progress": 0, "status": "unknown", "error": None})
    return jsonify(data), 200

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

# ------------------------- ERROR HANDLER -------------------------
@app.errorhandler(Exception)
def handle_error(e):
    logger.error(f"UNHANDLED ERROR: {str(e)}", exc_info=True)
    return jsonify({"error": "Server error: " + str(e)}), 500

# ------------------------- CLEANUP -------------------------
import shutil

def cleanup_loop():
    while True:
        time.sleep(300)
        try:
            cutoff = time.time() - 3600
            for f in os.listdir(UPLOAD_DIR):
                path = os.path.join(UPLOAD_DIR, f)
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    logger.info(f"Cleaned up: {f}")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

threading.Thread(target=cleanup_loop, daemon=True).start()

# ------------------------- RUN -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
