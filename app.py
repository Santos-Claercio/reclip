import os
import uuid
import glob
import json
import sys
import threading
import time
import ipaddress
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_file, render_template

from yt_dlp import YoutubeDL

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", uuid.uuid4().hex)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}
jobs_lock = threading.Lock()


class StopDownload(Exception):
    pass


def is_safe_url(url):
    if url.startswith("-"):
        return False
    parsed = urlparse(url)
    if parsed.scheme in ("file", "ftp", "sftp", "data", "javascript"):
        return False
    if parsed.scheme in ("http", "https"):
        host = parsed.hostname
        if host:
            try:
                addr = ipaddress.ip_address(host)
                if addr.is_private or addr.is_loopback or addr.is_link_local:
                    return False
            except ValueError:
                pass
    return True


def run_download(job):
    job_id = job["job_id"]
    url = job["url"]
    format_choice = job.get("format_choice", "video")
    format_id = job.get("format_id", None)
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    def progress_hook(d):
        with jobs_lock:
            action = job.get("_action")
        if action in ("pause", "cancel"):
            raise StopDownload(action)

        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            pct = (downloaded / total * 100) if total > 0 else 0
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0

            job["progress"] = {
                "percent": round(pct, 1),
                "downloaded": downloaded,
                "total": total,
                "speed": int(speed),
                "eta": str(eta),
            }

        elif d["status"] == "finished":
            job["_finished_file"] = d.get("filename")

    opts = {
        "outtmpl": out_template,
        "noplaylist": True,
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "extractor_retries": 3,
        "fragment_retries": 3,
        "extractor_args": {"twitter": {"api": ["syndication"]}},
    }

    if format_choice == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
        }]
    elif format_id:
        opts["format"] = f"{format_id}+bestaudio/best"
        opts["merge_output_format"] = "mp4"
    else:
        opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"

    with jobs_lock:
        job["status"] = "downloading"
    job["progress"] = {"percent": 0, "downloaded": 0, "total": 0, "speed": 0, "eta": "0"}

    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
    except StopDownload as e:
        action = str(e)
        if action == "pause":
            with jobs_lock:
                job["status"] = "paused"
            return
        elif action == "cancel":
            for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")):
                try:
                    os.remove(f)
                except OSError:
                    pass
            for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*.part")):
                try:
                    os.remove(f)
                except OSError:
                    pass
            with jobs_lock:
                job["status"] = "cancelled"
            return
    except Exception as e:
        with jobs_lock:
            if job.get("status") not in ("paused", "cancelled"):
                job["status"] = "error"
                job["error"] = str(e)
        return

    with jobs_lock:
        action = job.get("_action")
    if action == "pause":
        with jobs_lock:
            job["status"] = "paused"
        return
    elif action == "cancel":
        for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")):
            try: os.remove(f)
            except OSError: pass
        for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*.part")):
            try: os.remove(f)
            except OSError: pass
        with jobs_lock:
            job["status"] = "cancelled"
        return

    files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
    if not files:
        with jobs_lock:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
        return

    fmt = format_choice
    if fmt == "audio":
        target = [f for f in files if f.endswith(".mp3")]
    else:
        target = [f for f in files if f.endswith(".mp4")]
    chosen = target[0] if target else files[0]

    for f in files:
        if f != chosen:
            try:
                os.remove(f)
            except OSError:
                pass

    with jobs_lock:
        job["status"] = "done"
    job["file"] = chosen
    ext = os.path.splitext(chosen)[1]
    title = job.get("title", "").strip()
    if title:
        safe = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
        job["filename"] = f"{safe}{ext}" if safe else os.path.basename(chosen)
    else:
        job["filename"] = os.path.basename(chosen)

    try:
        job["file_size"] = os.path.getsize(chosen)
    except OSError:
        job["file_size"] = 0


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not is_safe_url(url):
        return jsonify({"error": "Unsupported or internal URL"}), 400

    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "retries": 3,
            "extractor_retries": 3,
            "extractor_args": {"twitter": {"api": ["syndication"]}},
        }
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        seen_ids = set()
        formats = []
        for f in info.get("formats", []):
            fid = f.get("format_id")
            if not fid or fid in seen_ids:
                continue
            vcodec = f.get("vcodec", "none")
            if vcodec == "none":
                continue
            seen_ids.add(fid)
            height = f.get("height")
            note = f.get("format_note") or ""
            label = f"{height}p" if height else (note or fid)
            formats.append({
                "id": fid,
                "label": label,
                "height": height or 0,
            })

        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    urls = data.get("urls", [])
    if not urls or not isinstance(urls, list):
        return jsonify({"error": "No URLs provided"}), 400

    for url in urls:
        if url.strip() and not is_safe_url(url.strip()):
            return jsonify({"error": f"Unsupported or internal URL: {url}"}), 400

    format_choice = data.get("format", "video")
    titles = data.get("titles", {})
    format_ids = data.get("format_ids", {})

    jobs_created = []
    for url in urls:
        if not url.strip():
            continue
        job_id = uuid.uuid4().hex[:10]
        with jobs_lock:
            while job_id in jobs:
                job_id = uuid.uuid4().hex[:10]
        job = {
            "job_id": job_id,
            "url": url.strip(),
            "format_choice": format_choice,
            "format_id": format_ids.get(url.strip()),
            "title": (titles.get(url.strip()) or "").strip(),
            "status": "queued",
            "progress": {"percent": 0, "downloaded": 0, "total": 0, "speed": 0, "eta": "0"},
            "error": None,
            "file": None,
            "filename": None,
            "file_size": None,
            "_action": None,
            "_finished_file": None,
        }
        with jobs_lock:
            jobs[job_id] = job
        thread = threading.Thread(target=run_download, args=(job,))
        thread.daemon = True
        thread.start()
        jobs_created.append({"job_id": job_id, "url": job["url"]})

    return jsonify({"jobs": jobs_created})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "progress": job.get("progress"),
        "error": job.get("error"),
        "filename": job.get("filename"),
        "file_size": job.get("file_size"),
    })


@app.route("/api/action/<job_id>", methods=["POST"])
def job_action(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    data = request.json
    action = data.get("action")

    if action == "pause":
        with jobs_lock:
            if job["status"] != "downloading":
                return jsonify({"error": "Job is not downloading"}), 400
            job["_action"] = "pause"
        return jsonify({"status": "ok"})

    elif action == "resume":
        with jobs_lock:
            if job["status"] != "paused":
                return jsonify({"error": "Job is not paused"}), 400
            job["_action"] = None
        thread = threading.Thread(target=run_download, args=(job,))
        thread.daemon = True
        thread.start()
        return jsonify({"status": "ok"})

    elif action == "cancel":
        with jobs_lock:
            if job["status"] in ("done", "cancelled", "error"):
                return jsonify({"error": "Job is already finished"}), 400
            job["_action"] = "cancel"
        return jsonify({"status": "ok"})

    elif action == "restart":
        for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")):
            try:
                os.remove(f)
            except OSError:
                pass
        for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*.part")):
            try:
                os.remove(f)
            except OSError:
                pass
        with jobs_lock:
            job["_action"] = None
            job["status"] = "queued"
        job["progress"] = {"percent": 0, "downloaded": 0, "total": 0, "speed": 0, "eta": "0"}
        job["error"] = None
        thread = threading.Thread(target=run_download, args=(job,))
        thread.daemon = True
        thread.start()
        return jsonify({"status": "ok"})

    return jsonify({"error": "Invalid action"}), 400


@app.route("/api/file/<job_id>")
def download_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    if not os.path.exists(job["file"]):
        return jsonify({"error": "File not found on disk"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
