from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp
import os
import threading
import time
import uuid
import zipfile
import io
import shutil

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

download_jobs = {}


def has_ffmpeg():
    return shutil.which('ffmpeg') is not None


def _base_opts():
    return {
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {
            'youtube': {'player_client': ['android', 'web']},
        },
        'http_headers': {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/125.0.0.0 Safari/537.36'
            ),
        },
    }


def fmt_duration(seconds):
    if not seconds:
        return 'Unknown'
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


def fmt_size(b):
    if not b:
        return ''
    if b < 1024 ** 2:
        return f'{b/1024:.1f} KB'
    if b < 1024 ** 3:
        return f'{b/1024**2:.1f} MB'
    return f'{b/1024**3:.2f} GB'


@app.route('/')
def index():
    return render_template('index.html', ffmpeg=has_ffmpeg())


@app.route('/api/info', methods=['POST'])
def get_info():
    data = request.json or {}
    url = data.get('url', '').strip()
    mode = data.get('mode', 'video')

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    try:
        # Quick flat extraction to detect type
        with yt_dlp.YoutubeDL({**_base_opts(), 'extract_flat': True}) as ydl:
            quick = ydl.extract_info(url, download=False)

        is_playlist = quick.get('_type') == 'playlist'

        if is_playlist:
            if mode == 'video':
                return jsonify({
                    'error': 'This looks like a playlist URL. Switch to Playlist mode.',
                    'suggest_mode': 'playlist'
                }), 400

            entries = [e for e in (quick.get('entries') or []) if e]
            preview = [{'title': e.get('title', 'Unknown')} for e in entries[:6]]
            return jsonify({
                'type': 'playlist',
                'title': quick.get('title', 'Unknown Playlist'),
                'channel': quick.get('uploader') or quick.get('channel', ''),
                'count': len(entries),
                'preview': preview,
            })

        else:
            # Full extraction for single video quality info
            with yt_dlp.YoutubeDL(_base_opts()) as ydl:
                info = ydl.extract_info(url, download=False)

            heights = set()
            for f in info.get('formats', []):
                h = f.get('height')
                if h and f.get('vcodec', 'none') != 'none':
                    heights.add(h)

            return jsonify({
                'type': 'video',
                'title': info.get('title', 'Unknown'),
                'thumbnail': info.get('thumbnail', ''),
                'duration': fmt_duration(info.get('duration')),
                'channel': info.get('uploader') or info.get('channel', ''),
                'quality_options': sorted(heights, reverse=True),
            })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/download', methods=['POST'])
def start_download():
    data = request.json or {}
    url = data.get('url', '').strip()
    quality = data.get('quality', 'best')
    mode = data.get('mode', 'video')

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    job_id = str(uuid.uuid4())
    download_jobs[job_id] = {
        'status': 'starting',
        'progress': 0,
        'per_file_progress': 0,
        'speed': '',
        'eta': '',
        'current_file': '',
        'current_video': 0,
        'total_videos': 1,
        'files': [],
        'error': None,
        'cancel_flag': False,
        'pause_flag': False,
    }

    t = threading.Thread(target=_run_download, args=(job_id, url, quality, mode), daemon=True)
    t.start()
    return jsonify({'job_id': job_id})


def _build_format(quality, ffmpeg):
    if quality == 'audio':
        return 'bestaudio[ext=m4a]/bestaudio/best', []

    if not ffmpeg:
        if quality == 'best':
            return 'best', []
        return f'best[height<={quality}]/best', []

    if quality == 'best':
        fmt = 'bestvideo+bestaudio/best'
    else:
        fmt = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]'

    return fmt, [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]


def _run_download(job_id, url, quality, mode):
    ffmpeg = has_ffmpeg()
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    fmt, postprocs = _build_format(quality, ffmpeg)

    if quality == 'audio' and ffmpeg:
        postprocs = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]

    video_index = [0]

    def hook(d):
        job = download_jobs.get(job_id)
        if not job:
            return

        if job.get('cancel_flag'):
            raise Exception('Cancelled by user')

        if job.get('pause_flag'):
            job['status'] = 'paused'
            while job.get('pause_flag') and not job.get('cancel_flag'):
                time.sleep(0.1)
            if job.get('cancel_flag'):
                raise Exception('Cancelled by user')

        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            done = d.get('downloaded_bytes', 0)
            pct = int(done / total * 100) if total else 0

            speed = d.get('speed') or 0
            speed_str = f'{speed/1024/1024:.1f} MB/s' if speed else ''
            eta = d.get('eta')
            eta_str = f'{int(eta//60)}:{int(eta%60):02d}' if eta else ''

            total_vids = job.get('total_videos', 1)
            cur = video_index[0]
            if total_vids > 1:
                overall = int((cur / total_vids) * 100 + pct / total_vids)
            else:
                overall = pct

            job.update({
                'status': 'downloading',
                'progress': overall,
                'per_file_progress': pct,
                'speed': speed_str,
                'eta': eta_str,
                'current_file': os.path.basename(d.get('filename', '')),
            })

        elif d['status'] == 'finished':
            video_index[0] += 1
            job.update({
                'status': 'processing',
                'current_video': video_index[0],
                'current_file': os.path.basename(d.get('filename', '')),
            })

    opts = {
        **_base_opts(),
        'format': fmt,
        'outtmpl': os.path.join(job_dir, '%(title)s.%(ext)s'),
        'progress_hooks': [hook],
        'noplaylist': mode == 'video',
    }

    if ffmpeg and quality != 'audio':
        opts['merge_output_format'] = 'mp4'

    if postprocs:
        opts['postprocessors'] = postprocs

    try:
        # Pre-fetch playlist count
        if mode == 'playlist':
            with yt_dlp.YoutubeDL({**_base_opts(), 'extract_flat': True}) as ydl:
                pinfo = ydl.extract_info(url, download=False)
                entries = [e for e in (pinfo.get('entries') or []) if e]
                download_jobs[job_id]['total_videos'] = len(entries)

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        # Collect final files
        files = []
        for fname in sorted(os.listdir(job_dir)):
            fpath = os.path.join(job_dir, fname)
            if os.path.isfile(fpath):
                files.append({'name': fname, 'size': fmt_size(os.path.getsize(fpath))})

        download_jobs[job_id].update({
            'status': 'complete',
            'progress': 100,
            'files': files,
        })

    except Exception as exc:
        job = download_jobs[job_id]
        if job.get('cancel_flag'):
            job.update({'status': 'cancelled', 'error': 'Download cancelled'})
        else:
            job.update({'status': 'error', 'error': str(exc)})


@app.route('/api/progress/<job_id>')
def get_progress(job_id):
    job = download_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


@app.route('/api/file/<job_id>/<path:filename>')
def serve_file(job_id, filename):
    filename = os.path.basename(filename)
    filepath = os.path.join(DOWNLOAD_DIR, job_id, filename)
    if not os.path.isfile(filepath):
        return jsonify({'error': 'File not found'}), 404
    return send_file(filepath, as_attachment=True, download_name=filename)


@app.route('/api/zip/<job_id>')
def serve_zip(job_id):
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    if not os.path.isdir(job_dir):
        return jsonify({'error': 'Job not found'}), 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as zf:
        for fname in os.listdir(job_dir):
            fpath = os.path.join(job_dir, fname)
            if os.path.isfile(fpath):
                zf.write(fpath, fname)
    buf.seek(0)

    return send_file(buf, mimetype='application/zip', as_attachment=True, download_name='playlist.zip')


@app.route('/api/pause/<job_id>', methods=['POST'])
def pause_download(job_id):
    job = download_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    job['pause_flag'] = True
    return jsonify({'ok': True})


@app.route('/api/resume/<job_id>', methods=['POST'])
def resume_download(job_id):
    job = download_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    job['pause_flag'] = False
    return jsonify({'ok': True})


@app.route('/api/cancel/<job_id>', methods=['POST'])
def cancel_download(job_id):
    job = download_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    job['cancel_flag'] = True
    job['pause_flag'] = False  # unblock if currently paused
    return jsonify({'ok': True})


if __name__ == '__main__':
    app.run(debug=True, port=5000, use_reloader=False)
