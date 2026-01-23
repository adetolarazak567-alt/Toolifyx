from flask import Flask, request, jsonify, send_file, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from dotenv import load_dotenv
import os, string, random, validators, subprocess, uuid
from datetime import datetime

# ---------------- LOAD ENV ----------------
load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URI', 'sqlite:///urls.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
migrate = Migrate(app, db)

# ---------------- MODELS ----------------
class URL(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_url = db.Column(db.String(500), nullable=False)
    short_code = db.Column(db.String(10), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    clicks = db.Column(db.Integer, default=0)

# ---------------- HELPERS ----------------
def generate_short_code(length=6):
    chars = string.ascii_letters + string.digits
    while True:
        code = ''.join(random.choice(chars) for _ in range(length))
        if not URL.query.filter_by(short_code=code).first():
            return code

# ---------------- ROUTES ----------------

# ---------------- INDEX ----------------
@app.route("/")
def home():
    return app.send_static_file("index.html")

# ---------------- URL SHORTENER ----------------
@app.route("/api/shorten", methods=["POST"])
def shorten_url():
    data = request.get_json()
    original_url = data.get("url", "").strip()
    if not validators.url(original_url):
        return jsonify({"error": "Enter a valid URL"}), 400

    # Check if URL already exists
    existing = URL.query.filter_by(original_url=original_url).first()
    if existing:
        short_url = request.host_url + existing.short_code
    else:
        code = generate_short_code()
        new_url = URL(original_url=original_url, short_code=code)
        db.session.add(new_url)
        db.session.commit()
        short_url = request.host_url + code

    return jsonify({
        "original_url": original_url,
        "short_url": short_url
    })

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
@app.route("/api/compress", methods=["POST"])
def compress_video():
    if 'video' not in request.files:
        return jsonify({"error": "No video file uploaded"}), 400

    file = request.files['video']
    level = request.form.get('level', 'medium')
    output_name = request.form.get('filename', f"compressed_{uuid.uuid4().hex}.mp4")

    # Compression ratios
    cr_map = {"high": "28", "medium": "23", "low": "18"}  # lower CR means higher quality/larger file
    cr_value = cr_map.get(level, "23")

    input_path = f"/tmp/{uuid.uuid4().hex}_{file.filename}"
    output_path = f"/tmp/{output_name}"

    # Save uploaded file temporarily
    file.save(input_path)

    try:
        # Run ffmpeg compression
        subprocess.run([
            "ffmpeg", "-i", input_path,
            "-vcodec", "libx264",
            "-crf", cr_value,
            "-preset", "fast",
            "-acodec", "aac",
            "-y", output_path
        ], check=True)

        # Send file back
        return send_file(output_path, as_attachment=True, download_name=output_name)

    except subprocess.CalledProcessError as e:
        return jsonify({"error": "Video compression failed", "details": str(e)}), 500

    finally:
        # Cleanup
        if os.path.exists(input_path):
            os.remove(input_path)
        if os.path.exists(output_path):
            os.remove(output_path)

# ---------------- MAIN ----------------
if __name__ == "__main__":
    # For local testing
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)