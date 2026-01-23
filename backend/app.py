from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from dotenv import load_dotenv
import os, string, random, validators
from datetime import datetime
import uuid
import subprocess

# ---------------- Load environment variables ----------------
load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URI', 'sqlite:///urls.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# ---------------- Video folders ----------------
UPLOAD_FOLDER = "uploads"
COMPRESSED_FOLDER = "compressed"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(COMPRESSED_FOLDER, exist_ok=True)

# ---------------- Models ----------------
class URL(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_url = db.Column(db.String(500), nullable=False)
    short_code = db.Column(db.String(10), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    clicks = db.Column(db.Integer, default=0)

# ---------------- Helpers ----------------
def generate_short_code(length=6):
    chars = string.ascii_letters + string.digits
    while True:
        code = ''.join(random.choice(chars) for _ in range(length))
        if not URL.query.filter_by(short_code=code).first():
            return code

# ---------------- FFmpeg check ----------------
def check_ffmpeg():
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        print("[FFmpeg] Installed:", result.stdout.splitlines()[0])
        return True
    except FileNotFoundError:
        print("[FFmpeg] NOT installed! Video compression will fail.")
        return False

FFMPEG_AVAILABLE = check_ffmpeg()

# ---------------- Routes ----------------

@app.route("/", methods=["GET", "POST"])
def index():
    try:
        if request.method == "POST":
            original_url = (request.form.get("url") or "").strip()
            domain_choice = request.form.get("domain")

            if not validators.url(original_url):
                return render_template("index.html", error="Enter a valid URL.")

            if domain_choice == "shrinkme":
                shrinkme_ref = os.getenv("SHRINKME_REF", "")
                short_url = f"https://shrinkme.io/?r={shrinkme_ref}&u={original_url}"
                return render_template("index.html", short_url=short_url, original_url=original_url)

            existing = URL.query.filter_by(original_url=original_url).first()
            if existing:
                short_url = request.host_url + existing.short_code
            else:
                code = generate_short_code()
                new_url = URL(original_url=original_url, short_code=code)
                db.session.add(new_url)
                db.session.commit()
                short_url = request.host_url + code

            return render_template("index.html", short_url=short_url, original_url=original_url)

        return render_template("index.html")
    except Exception as e:
        print("[Error in /]", e)
        return render_template("index.html", error="An unexpected error occurred.")


@app.route("/<string:short_code>")
def redirect_short(short_code):
    url = URL.query.filter_by(short_code=short_code).first_or_404()
    url.clicks += 1
    db.session.commit()
    return redirect(url.original_url)


@app.route("/api/stats/<string:short_code>")
def stats(short_code):
    url = URL.query.filter_by(short_code=short_code).first_or_404()
    return jsonify({
        "original_url": url.original_url,
        "short_code": url.short_code,
        "clicks": url.clicks,
        "created_at": url.created_at.isoformat()
    })


# ---------------- Video Compressor ----------------
@app.route("/api/compress", methods=["POST"])
def compress_video():
    if not FFMPEG_AVAILABLE:
        return jsonify({"error": "FFmpeg is not installed on the server"}), 500

    if "video" not in request.files:
        return jsonify({"error": "No video uploaded"}), 400

    video = request.files["video"]
    level = request.form.get("level", "medium")

    uid = str(uuid.uuid4())
    input_path = os.path.join(UPLOAD_FOLDER, uid + ".mp4")
    output_path = os.path.join(COMPRESSED_FOLDER, uid + "_compressed.mp4")

    video.save(input_path)

    # Compression profiles
    if level == "high":
        crf = "32"
        preset = "veryfast"
    elif level == "low":
        crf = "22"
        preset = "slow"
    else:
        crf = "27"
        preset = "fast"

    command = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-vcodec", "libx264",
        "-crf", crf,
        "-preset", preset,
        "-acodec", "aac",
        "-b:a", "128k",
        output_path
    ]

    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return send_file(
        output_path,
        as_attachment=True,
        download_name="compressed.mp4"
    )


# ---------------- FFmpeg test route (optional) ----------------
@app.route("/ffmpeg-check")
def ffmpeg_check():
    if FFMPEG_AVAILABLE:
        return "FFmpeg is installed!"
    else:
        return "FFmpeg is NOT installed!"


# ---------------- Main ----------------
if __name__ == "__main__":
    app.run(debug=True)