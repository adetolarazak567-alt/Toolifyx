import os
import sys
import uuid
import subprocess
import time
import logging
import threading
import shutil
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, date
from functools import partial
from pathlib import Path

from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from flask_cors import CORS

# ------------------------- CONFIGURATION -------------------------
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Use /dev/shm (RAM) if available for ultra-fast temp I/O, fallback to /tmp
RAM_DIR = "/dev/shm/toolifyx"
DISK_DIR = "/tmp/toolifyx"
UPLOAD_DIR = RAM_DIR if os.path.exists("/dev/shm") else DISK_DIR
os.makedirs(UPLOAD_DIR, exist_ok=True)

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///toolifyx.db").replace(
    "postgres://", "postgresql://"
)
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_size": 10,
    "max_overflow": 20,
    "pool_pre_ping": True,
    "pool_recycle": 300,
}
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024 * 1024  # 5GB
app.config["MAX_CONCURRENT_JOBS"] = int(os.getenv("MAX_WORKERS", os.cpu_count() or 4))
app.config["CHUNK_SIZE"] = 8 * 1024 * 1024  # 8MB optimal for most systems
app.config["FFMPEG_THREADS"] = max(1, (os.cpu_count() or 4) // 2)  # Per-job threads

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(process)d] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

db = SQLAlchemy(app)

# ------------------------- HARDWARE DETECTION -------------------------
def detect_hw_accel():
    """Detect best available hardware encoder."""
    encoders = []
    
    # Check NVENC (NVIDIA)
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=5
        )
        encoders_text = result.stdout
        
        if "h264_nvenc" in encoders_text:
            # Verify GPU availability
            nvidia_check = subprocess.run(
                ["nvidia-smi"], capture_output=True, timeout=3
            )
            if nvidia_check.returncode == 0:
                return "nvenc", ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "hq", "-rc", "vbr", "-cq", "23"]
    except Exception:
        pass
    
    # Check VideoToolbox (Apple Silicon)
    try:
        if sys.platform == "darwin" and "h264_videotoolbox" in encoders_text:
            return "videotoolbox", ["-c:v", "h264_videotoolbox", "-allow_sw", "1", "-realtime", "1"]
    except NameError:
        pass
    
    # Check VA-API (Intel/AMD Linux)
    try:
        if "h264_vaapi" in encoders_text:
            # Check if render device exists
            if os.path.exists("/dev/dri/renderD128"):
                return "vaapi", [
                    "-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128",
                    "-c:v", "h264_vaapi", "-qp", "23"
                ]
    except NameError:
        pass
    
    # CPU fallback with fastest settings
    return "cpu", [
        "-c:v", "libx264", 
        "-preset", "ultrafast",
        "-tune", "fastdecode",
        "-movflags", "+faststart",
        "-threads", str(app.config["FFMPEG_THREADS"])
    ]

HW_ACCEL_TYPE, HW_ENCODE_ARGS = detect_hw_accel()
logger.info(f"Hardware acceleration: {HW_ACCEL_TYPE} with args: {HW_ENCODE_ARGS}")

# ------------------------- MODELS -------------------------
class Job(db.Model):
    __tablename__ = 'jobs'
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    job_type = db.Column(db.String(20), nullable=False, index=True)
    filename = db.Column(db.String(300))
    status = db.Column(db.String(20), default="queued", index=True)
    progress = db.Column(db.Integer, default=0)
    error_msg = db.Column(db.String(1000))
    hw_accel = db.Column(db.String(20))
    created = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    completed = db.Column(db.DateTime)
    processing_time_ms = db.Column(db.Integer)

    def to_dict(self):
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "filename": self.filename,
            "status": self.status,
            "progress": self.progress,
            "hw_accel": self.hw_accel,
            "created": self.created.isoformat() if self.created else None,
            "completed": self.completed.isoformat() if self.completed else None,
            "processing_time_ms": self.processing_time_ms,
        }

with app.app_context():
    db.create_all()

# ------------------------- PROCESS POOL (Bypasses GIL) -------------------------
# Use ProcessPool for CPU-intensive FFmpeg work
process_executor = ProcessPoolExecutor(max_workers=app.config["MAX_CONCURRENT_JOBS"])

# In-memory job state for ultra-fast polling (falls back to DB)
job_states = {}
job_lock = threading.RLock()

# ------------------------- FAST HELPERS -------------------------
def save_file_fast(file_storage, output_path):
    """Memory-efficient streaming with optimal buffer."""
    # Use sendfile if available for zero-copy
    file_storage.save(output_path)
    # Verify write
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise IOError("File save failed or empty")

def get_video_info(file_path):
    """Fast ffprobe with cached results."""
    try:
        # Get duration and bitrate in one call
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_entries", "format=duration,bit_rate,size:stream=codec_name,width,height",
            file_path
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, 
            timeout=30, check=True
        )
        data = json.loads(result.stdout)
        
        duration = None
        if "format" in data and "duration" in data["format"]:
            try:
                duration = float(data["format"]["duration"]) * 1000
            except (ValueError, TypeError):
                pass
        
        # Check if video stream exists
        has_video = any(
            s.get("codec_type") == "video" 
            for s in data.get("streams", [])
        ) if "streams" in data else False
        
        return {"duration_ms": duration, "has_video": has_video}
    except Exception as e:
        logger.warning(f"ffprobe failed for {file_path}: {e}")
        return {"duration_ms": None, "has_video": False}

def is_valid_video(file_path):
    """Fast validation without full decode."""
    info = get_video_info(file_path)
    return info["has_video"]

# ------------------------- DB HELPERS -------------------------
def update_job_db(job_id, **kwargs):
    """Atomic DB update with retry logic."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with app.app_context():
                job = Job.query.filter_by(job_id=job_id).first()
                if not job:
                    return False
                
                for key, value in kwargs.items():
                    if hasattr(job, key):
                        setattr(job, key, value)
                
                if kwargs.get("status") in ("done", "error"):
                    job.completed = datetime.utcnow()
                
                db.session.commit()
                return True
        except Exception as e:
            db.session.rollback()
            if attempt == max_retries - 1:
                logger.error(f"DB update failed for {job_id}: {e}")
                return False
            time.sleep(0.1 * (attempt + 1))

def update_job_memory(job_id, **kwargs):
    """Ultra-fast in-memory state update."""
    with job_lock:
        if job_id not in job_states:
            job_states[job_id] = {
                "progress": 0, "status": "queued", 
                "error": None, "hw_accel": HW_ACCEL_TYPE
            }
        job_states[job_id].update(kwargs)

# ------------------------- WORKER FUNCTIONS (Picklable for ProcessPool) -------------------------
def run_ffmpeg_worker(args):
    """
    Standalone worker function for ProcessPoolExecutor.
    args: (job_id, input_path, output_path, cmd_list, total_duration_ms)
    """
    job_id, input_path, output_path, cmd, total_duration_ms = args
    start_time = time.time()
    
    try:
        # Update status via file-based signaling (works across processes)
        _write_status(job_id, "processing", 1)
        
        # Run FFmpeg with progress parsing
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # Line buffered
            universal_newlines=True
        )
        
        last_progress = 0
        for line in process.stdout:
            if "out_time_ms=" in line:
                try:
                    current_ms = int(line.strip().split("=")[1])
                    if total_duration_ms and total_duration_ms > 0:
                        percent = min(99, int((current_ms * 100) / total_duration_ms))
                        if percent > last_progress:
                            _write_status(job_id, "processing", percent)
                            last_progress = percent
                except (ValueError, IndexError):
                    pass
        
        process.wait(timeout=3600)  # 1 hour max
        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg exited with code {process.returncode}")
        
        processing_time = int((time.time() - start_time) * 1000)
        _write_status(job_id, "done", 100, processing_time=processing_time)
        
        return {"status": "done", "output_path": output_path, "processing_time_ms": processing_time}
        
    except Exception as e:
        processing_time = int((time.time() - start_time) * 1000)
        _write_status(job_id, "error", last_progress, error=str(e), processing_time=processing_time)
        return {"status": "error", "error": str(e)}
    finally:
        # Cleanup input
        if os.path.exists(input_path):
            os.remove(input_path)

def _write_status(job_id, status, progress, error=None, processing_time=None):
    """Write status to temp file for cross-process communication."""
    status_file = os.path.join(UPLOAD_DIR, f"{job_id}.status")
    data = {
        "status": status,
        "progress": progress,
        "error": error,
        "processing_time_ms": processing_time,
        "timestamp": time.time()
    }
    try:
        with open(status_file, 'w') as f:
            json.dump(data, f)
    except Exception:
        pass

def _read_status(job_id):
    """Read status from temp file."""
    status_file = os.path.join(UPLOAD_DIR, f"{job_id}.status")
    try:
        with open(status_file, 'r') as f:
            return json.load(f)
    except Exception:
        return None

# ------------------------- JOB BUILDERS -------------------------
def build_compress_job(job_id, input_path, output_path, level):
    """Build optimized compress command."""
    crf_map = {"low": 18, "medium": 23, "high": 28, "ultra": 32}
    crf = crf_map.get(level, 23)
    
    # Adjust quality based on hardware
    if HW_ACCEL_TYPE == "nvenc":
        # NVENC uses CQ instead of CRF
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", input_path,
            "-c:v", "h264_nvenc",
            "-preset", "p1",  # Fastest
            "-tune", "hq",
            "-cq", str(crf),
            "-rc", "vbr",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            "-nostats",
            output_path
        ]
    elif HW_ACCEL_TYPE == "vaapi":
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-hwaccel", "vaapi",
            "-hwaccel_device", "/dev/dri/renderD128",
            "-hwaccel_output_format", "vaapi",
            "-i", input_path,
            "-c:v", "h264_vaapi",
            "-qp", str(crf),
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            "-nostats",
            output_path
        ]
    else:
        # Optimized CPU encoding
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", input_path,
            "-c:v", "libx264",
            "-preset", "ultrafast",  # Fastest CPU preset
            "-tune", "fastdecode",
            "-crf", str(crf),
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-threads", str(app.config["FFMPEG_THREADS"]),
            "-progress", "pipe:1",
            "-nostats",
            output_path
        ]
    
    info = get_video_info(input_path)
    return (job_id, input_path, output_path, cmd, info["duration_ms"])

def build_mp3_job(job_id, input_path, output_path, bitrate):
    """Build optimized MP3 extraction command."""
    # For MP3, we can use multiple threads in LAME
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-vn",  # No video
        "-c:a", "libmp3lame",
        "-b:a", bitrate,
        "-ar", "44100",
        "-ac", "2",
        "-q:a", "0",  # Highest quality VBR
        "-threads", str(app.config["FFMPEG_THREADS"]),
        "-progress", "pipe:1",
        "-nostats",
        output_path
    ]
    
    info = get_video_info(input_path)
    return (job_id, input_path, output_path, cmd, info["duration_ms"])

# ------------------------- BACKGROUND SYNC -------------------------
def sync_job_status():
    """Background thread to sync file-based status to DB and memory."""
    while True:
        time.sleep(1)
        try:
            with job_lock:
                job_ids = list(job_states.keys())
            
            for job_id in job_ids:
                status_data = _read_status(job_id)
                if not status_data:
                    continue
                
                # Update memory
                with job_lock:
                    if job_id in job_states:
                        job_states[job_id].update({
                            "status": status_data.get("status", "unknown"),
                            "progress": status_data.get("progress", 0),
                            "error": status_data.get("error"),
                            "processing_time_ms": status_data.get("processing_time_ms")
                        })
                
                # Sync to DB if status changed significantly
                current_status = status_data.get("status")
                current_progress = status_data.get("progress", 0)
                
                # Batch DB updates: only update on state changes or every 10%
                if current_status in ("done", "error") or current_progress % 10 == 0:
                    update_job_db(
                        job_id,
                        status=current_status,
                        progress=current_progress,
                        error_msg=status_data.get("error"),
                        processing_time_ms=status_data.get("processing_time_ms")
                    )
                
                # Cleanup completed jobs from memory after 5 minutes
                if current_status in ("done", "error"):
                    completed_time = status_data.get("timestamp", 0)
                    if time.time() - completed_time > 300:
                        with job_lock:
                            job_states.pop(job_id, None)
                        status_file = os.path.join(UPLOAD_DIR, f"{job_id}.status")
                        if os.path.exists(status_file):
                            os.remove(status_file)
                        
        except Exception as e:
            logger.error(f"Sync error: {e}")

sync_thread = threading.Thread(target=sync_job_status, daemon=True)
sync_thread.start()

# ------------------------- ROUTES -------------------------
@app.route("/")
def home():
    return jsonify({
        "status": "running",
        "hw_accel": HW_ACCEL_TYPE,
        "max_workers": app.config["MAX_CONCURRENT_JOBS"],
        "upload_dir": UPLOAD_DIR
    }), 200

@app.route("/health")
def health():
    return jsonify({
        "status": "healthy",
        "hw_accel": HW_ACCEL_TYPE,
        "active_jobs": len(job_states),
        "disk_free_gb": shutil.disk_usage(UPLOAD_DIR).free // (1024**3)
    }), 200

@app.route("/api/compress", methods=["POST"])
def compress():
    if 'video' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files['video']
    level = request.form.get("level", "medium")

    if not file or not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    filename = secure_filename(file.filename)
    if not filename.lower().endswith(('.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv', '.wmv', '.m4v')):
        return jsonify({"error": "Unsupported video file type"}), 400

    job_id = uuid.uuid4().hex
    input_path = os.path.join(UPLOAD_DIR, f"{job_id}_in_{filename}")
    output_path = os.path.join(UPLOAD_DIR, f"{job_id}.mp4")

    try:
        # Fast save
        save_file_fast(file, input_path)
        
        # Quick validation
        if not is_valid_video(input_path):
            os.remove(input_path)
            return jsonify({"error": "Uploaded file is not a valid video"}), 400

        # Create DB record
        with app.app_context():
            db.session.add(Job(
                job_id=job_id, 
                job_type="compress", 
                filename=filename,
                status="queued", 
                progress=0,
                hw_accel=HW_ACCEL_TYPE
            ))
            db.session.commit()

        # Initialize memory state
        update_job_memory(job_id, progress=0, status="queued", hw_accel=HW_ACCEL_TYPE)
        
        # Build and submit job
        job_args = build_compress_job(job_id, input_path, output_path, level)
        future = process_executor.submit(run_ffmpeg_worker, job_args)
        
        # Store future reference for potential cancellation
        with job_lock:
            job_states[job_id]["future"] = future

        return jsonify({
            "job_id": job_id,
            "status": "queued",
            "hw_accel": HW_ACCEL_TYPE
        }), 202

    except Exception as e:
        logger.error(f"Compress setup failed: {e}")
        if os.path.exists(input_path):
            os.remove(input_path)
        return jsonify({"error": str(e)}), 500

@app.route("/api/convert-mp3", methods=["POST"])
def convert_mp3():
    if 'video' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files['video']
    bitrate = request.form.get("bitrate", "192k")

    if not file or not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    filename = secure_filename(file.filename)
    if not filename.lower().endswith(('.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv', '.wmv', '.m4v')):
        return jsonify({"error": "Unsupported video file type"}), 400

    job_id = uuid.uuid4().hex
    input_path = os.path.join(UPLOAD_DIR, f"{job_id}_in_{filename}")
    output_path = os.path.join(UPLOAD_DIR, f"{job_id}.mp3")

    try:
        save_file_fast(file, input_path)
        
        if not is_valid_video(input_path):
            os.remove(input_path)
            return jsonify({"error": "Uploaded file is not a valid video"}), 400

        with app.app_context():
            db.session.add(Job(
                job_id=job_id, 
                job_type="mp3", 
                filename=filename,
                status="queued", 
                progress=0,
                hw_accel="cpu"  # MP3 is CPU-bound
            ))
            db.session.commit()

        update_job_memory(job_id, progress=0, status="queued", hw_accel="cpu")
        
        job_args = build_mp3_job(job_id, input_path, output_path, bitrate)
        future = process_executor.submit(run_ffmpeg_worker, job_args)
        
        with job_lock:
            job_states[job_id]["future"] = future

        return jsonify({
            "job_id": job_id,
            "status": "queued",
            "hw_accel": "cpu"
        }), 202

    except Exception as e:
        logger.error(f"MP3 setup failed: {e}")
        if os.path.exists(input_path):
            os.remove(input_path)
        return jsonify({"error": str(e)}), 500

@app.route("/api/progress/<job_id>")
def progress(job_id):
    """Ultra-fast progress endpoint (memory-first, DB fallback)."""
    # Try memory first (sub-millisecond)
    with job_lock:
        if job_id in job_states:
            data = {
                "job_id": job_id,
                "progress": job_states[job_id].get("progress", 0),
                "status": job_states[job_id].get("status", "unknown"),
                "error": job_states[job_id].get("error"),
                "hw_accel": job_states[job_id].get("hw_accel", "cpu")
            }
            return jsonify(data)
    
    # Fallback to status file
    status_data = _read_status(job_id)
    if status_data:
        return jsonify({
            "job_id": job_id,
            "progress": status_data.get("progress", 0),
            "status": status_data.get("status", "unknown"),
            "error": status_data.get("error"),
            "hw_accel": HW_ACCEL_TYPE
        })
    
    # Final fallback to DB
    with app.app_context():
        job = Job.query.filter_by(job_id=job_id).first()
        if job:
            return jsonify(job.to_dict())
    
    return jsonify({"job_id": job_id, "progress": 0, "status": "unknown"}), 404

@app.route("/api/progress-stream/<job_id>")
def progress_stream(job_id):
    """Server-Sent Events for real-time progress (WebSocket alternative)."""
    def generate():
        last_progress = -1
        last_status = None
        
        while True:
            with job_lock:
                state = job_states.get(job_id, {})
                progress = state.get("progress", 0)
                status = state.get("status", "unknown")
            
            if progress != last_progress or status != last_status:
                last_progress = progress
                last_status = status
                
                data = json.dumps({
                    "job_id": job_id,
                    "progress": progress,
                    "status": status,
                    "error": state.get("error")
                })
                yield f"data: {data}\n\n"
                
                if status in ("done", "error"):
                    break
            
            time.sleep(0.5)  # 2Hz update rate
    
    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"  # Disable nginx buffering
        }
    )

@app.route("/api/download/<job_id>")
def download(job_id):
    path = os.path.join(UPLOAD_DIR, f"{job_id}.mp4")
    if not os.path.exists(path):
        return jsonify({"error": "File not ready"}), 404
    
    name = request.args.get("name", "compressed.mp4")
    
    # Use X-Accel-Redirect for nginx or direct send
    return send_file(
        path, 
        as_attachment=True, 
        download_name=name,
        mimetype="video/mp4"
    )

@app.route("/api/download-mp3/<job_id>")
def download_mp3(job_id):
    path = os.path.join(UPLOAD_DIR, f"{job_id}.mp3")
    if not os.path.exists(path):
        return jsonify({"error": "File not ready"}), 404
    
    name = request.args.get("name", "audio.mp3")
    return send_file(
        path, 
        as_attachment=True, 
        download_name=name,
        mimetype="audio/mpeg"
    )

@app.route("/api/jobs")
def list_jobs():
    """List recent jobs with pagination."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    
    with app.app_context():
        pagination = Job.query.order_by(Job.created.desc()).paginate(
            page=page, per_page=min(per_page, 100), error_out=False
        )
        
        return jsonify({
            "jobs": [job.to_dict() for job in pagination.items],
            "total": pagination.total,
            "pages": pagination.pages,
            "current_page": page
        })

@app.route("/admin/stats")
def stats():
    today = date.today()
    with app.app_context():
        return jsonify({
            "total_jobs": Job.query.count(),
            "today_jobs": Job.query.filter(db.func.date(Job.created) == today).count(),
            "compress_jobs": Job.query.filter_by(job_type="compress").count(),
            "mp3_jobs": Job.query.filter_by(job_type="mp3").count(),
            "hw_accel": HW_ACCEL_TYPE,
            "active_jobs": len(job_states),
            "max_workers": app.config["MAX_CONCURRENT_JOBS"]
        })

# ------------------------- CLEANUP -------------------------
def cleanup_loop():
    """Aggressive cleanup with size-based eviction."""
    while True:
        time.sleep(300)  # 5 minutes
        
        try:
            now = time.time()
            cutoff = now - 3600  # 1 hour
            
            files = []
            total_size = 0
            
            for f in os.listdir(UPLOAD_DIR):
                path = os.path.join(UPLOAD_DIR, f)
                if os.path.isfile(path):
                    mtime = os.path.getmtime(path)
                    size = os.path.getsize(path)
                    files.append((path, mtime, size))
                    total_size += size
            
            # Sort by age, oldest first
            files.sort(key=lambda x: x[1])
            
            # Delete old files
            deleted = 0
            for path, mtime, size in files:
                if mtime < cutoff:
                    os.remove(path)
                    deleted += 1
                    total_size -= size
            
            # If still over 10GB, delete oldest until under
            max_size = 10 * 1024 * 1024 * 1024  # 10GB
            while total_size > max_size and files:
                path, _, size = files.pop(0)
                if os.path.exists(path):
                    os.remove(path)
                    total_size -= size
                    deleted += 1
            
            if deleted:
                logger.info(f"Cleaned up {deleted} files, freed space")
                
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
cleanup_thread.start()

# ------------------------- SHUTDOWN HANDLER -------------------------
import atexit

def shutdown_handler():
    logger.info("Shutting down, waiting for active jobs...")
    process_executor.shutdown(wait=True, timeout=30)
    logger.info("Shutdown complete")

atexit.register(shutdown_handler)

# ------------------------- RUN -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    workers = int(os.getenv("GUNICORN_WORKERS", 1))
    
    # For development only - production uses gunicorn
    app.run(
        host="0.0.0.0", 
        port=port, 
        threaded=True,
        use_reloader=False
    )
