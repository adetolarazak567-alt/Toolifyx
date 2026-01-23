
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from dotenv import load_dotenv
import os, string, random, validators, uuid, subprocess
from datetime import datetime
from werkzeug.utils import secure_filename

# ---------------- LOAD ENV ----------------
load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URI', 'sqlite:///urls.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
migrate = Migrate(app, db)

# ---------------- URL SHORTENER ----------------
class URL(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_url = db.Column(db.String(500), nullable=False)
    short_code = db.Column(db.String(10), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    clicks = db.Column(db.Integer, default=0)

def generate_short_code(length=6):
    chars = string.ascii_letters + string.digits
    while True:
        code = ''.join(random.choice(chars) for _ in range(length))
        if not URL.query.filter_by(short_code=code).first():
            return code

@app.route("/", methods=["GET", "POST"])
def url_shortener():
    try:
        if request.method == "POST":
            original_url = request.form.get("url", "").strip()
            domain_choice = request.form.get("domain")

            if not validators.url(original_url):
                return render_template("url-shortner.html", error="Enter a valid URL.")

            if domain_choice == "shrinkme":
                shrinkme_ref = os.getenv("SHRINKME_REF", "")
                short_url = f"https://shrinkme.io/?r={shrinkme_ref}&u={original_url}"
                return render_template("url-shortner.html", short_url=short_url, original_url=original_url)

            existing = URL.query.filter_by(original_url=original_url).first()
            if existing:
                short_url = request.host_url + existing.short_code
            else:
                code = generate_short_code()
                new_url = URL(original_url=original_url, short_code=code)
                db.session.add(new_url)
                db.session.commit()
                short_url = request.host_url + code

            return render_template("url-shortner.html", short_url=short_url, original_url=original_url)

        return render_template("url-shortner.html")
    except Exception as e:
        print("[Error in URL Shortener]", e)
        return render_template("url-shortner.html", error="An unexpected error occurred.")

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

# ---------------- VIDEO COMPRESSOR ----------------
UPLOAD_FOLDER = "uploads"
COMPRESSED_FOLDER = "compressed"
ALLOWED_EXTENSIONS = {"mp4", "mov", "avi", "mkv"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(COMPRESSED_FOLDER, exist_ok=True)

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def compress_video(input_path, output_path, level="medium"):
    if level == "high":
        crf = "28"
    elif level == "low":
        crf = "18"
    else:
        crf = "23"

    command = [
        "ffmpeg",
        "-i", input_path,
        "-vcodec", "libx264",
        "-crf", crf,
        "-preset", "fast",
        "-acodec", "aac",
        "-b:a", "128k",
        "-y",
        output_path
    ]
    subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return output_path

@app.route("/api/compress", methods=["POST"])
def compress():
    if "video" not in request.files:
        return jsonify({"error": "No video uploaded"}), 400

    file = request.files["video"]
    level = request.form.get("level", "medium")
    if not allowed_file(file.filename):
        return jsonify({"error": "Invalid file type"}), 400

    filename = secure_filename(file.filename)
    input_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_{filename}")
    file.save(input_path)

    output_filename = f"compressed_{uuid.uuid4()}.mp4"
    output_path = os.path.join(COMPRESSED_FOLDER, output_filename)

    compress_video(input_path, output_path, level)

    return send_file(output_path, as_attachment=True, download_name="compressed.mp4")

@app.route("/api/test")
def test():
    return jsonify({"status": "Video compressor backend working!"})

# ---------------- MAIN ----------------
if __name__ == "__main__":
    app.run(debug=True)