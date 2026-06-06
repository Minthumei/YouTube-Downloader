"""YouTube Downloader local server with real-time progress + max-FPS selection."""
import http.server
import socketserver
import json
import os
import re
import tempfile
import shutil
import subprocess
import sys
import time
import threading
import urllib.parse
import uuid
from pathlib import Path

HOST = os.environ.get('YTDL_HOST', '127.0.0.1')
PORT = int(os.environ.get('PORT', os.environ.get('YTDL_PORT', '8000')))
PUBLIC_MODE = os.environ.get('YTDL_PUBLIC_MODE', '').lower() in ('1', 'true', 'yes', 'on')
ROOT = Path(__file__).parent
COOKIE_CANDIDATES = [
    os.environ.get('YTDL_COOKIES_FILE', ''),
    '/etc/secrets/cookies.txt',
    str(ROOT / 'cookies.txt'),
]
ENV_COOKIES_FILE = None
HEARTBEAT_TIMEOUT = 10
last_ping = None  # Web UI が一度でも ping したら時刻が入る。None の間は拡張のみ利用とみなし終了しない
ping_lock = threading.Lock()
httpd_ref = {'server': None}

QUALITY_HEIGHT = {'480p': 480, '720p': 720, '1080p': 1080, '2K': 1440, '4K': 2160}
MP3_BITRATE = {'128Kbps': '128', '256Kbps': '256', '320Kbps': '320'}

jobs = {}
jobs_lock = threading.Lock()
PROGRESS_RE = re.compile(r'\[download\]\s+([\d.]+)%')

# ダウンロード完了後の自動終了用
shutdown_timer = {'t': None}
shutdown_lock = threading.Lock()
SHUTDOWN_GRACE = 3  # 秒: ファイル送信完了からこの秒数後に終了


def cancel_pending_shutdown():
    with shutdown_lock:
        if shutdown_timer['t'] is not None:
            shutdown_timer['t'].cancel()
            shutdown_timer['t'] = None


def schedule_idle_shutdown():
    if PUBLIC_MODE:
        return

    # Web UI 利用中 (ハートビートあり) は対象外。拡張からの利用時のみ自動終了
    with ping_lock:
        web_ui_active = last_ping is not None
    if web_ui_active:
        return

    def _maybe():
        with jobs_lock:
            running = any(j.get('status') == 'running' for j in jobs.values())
        if running:
            return  # まだDL中なら終了しない
        print('\nダウンロード完了。サーバーを終了します。')
        _shutdown()

    with shutdown_lock:
        if shutdown_timer['t'] is not None:
            shutdown_timer['t'].cancel()
        timer = threading.Timer(SHUTDOWN_GRACE, _maybe)
        timer.daemon = True
        shutdown_timer['t'] = timer
        timer.start()


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    return name.strip()[:120] or 'download'


def get_cookies_file():
    global ENV_COOKIES_FILE
    cookies_text = os.environ.get('YTDL_COOKIES_TEXT', '').strip()
    if cookies_text:
        if ENV_COOKIES_FILE is None or not Path(ENV_COOKIES_FILE).exists():
            fp = Path(tempfile.gettempdir()) / 'ytdl_cookies.txt'
            fp.write_text(cookies_text.replace('\\n', '\n') + '\n', encoding='utf-8')
            ENV_COOKIES_FILE = str(fp)
        return ENV_COOKIES_FILE

    for candidate in COOKIE_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def build_cmd(url, fmt, quality, out_template):
    cmd = [sys.executable, '-m', 'yt_dlp', '-o', out_template,
           '--no-playlist', '--windows-filenames',
           '--newline', '--progress', '--no-warnings']
    cookies_file = get_cookies_file()
    if cookies_file:
        cmd += ['--cookies', cookies_file]
    if fmt == 'mp4':
        if quality == 'max':
            # No height cap: take the highest resolution, then highest FPS
            cmd += [
                '-f', 'bv*+ba/b',
                '-S', 'res,fps,vcodec:h264,ext:mp4:m4a',
                '--merge-output-format', 'mp4',
            ]
        else:
            h = QUALITY_HEIGHT.get(quality, 720)
            # Sort: prefer matching height, then highest FPS, then H.264/mp4 for compatibility
            cmd += [
                '-f', f'bv*[height<={h}]+ba/b[height<={h}]',
                '-S', f'res:{h},fps,vcodec:h264,ext:mp4:m4a',
                '--merge-output-format', 'mp4',
            ]
    elif fmt == 'mp3':
        if quality == 'max':
            cmd += ['-f', 'bestaudio', '-x', '--audio-format', 'mp3', '--audio-quality', '0']
        else:
            br = MP3_BITRATE.get(quality, '256')
            cmd += ['-f', 'bestaudio', '-x', '--audio-format', 'mp3', '--audio-quality', br + 'K']
    elif fmt == 'wav':
        cmd += ['-f', 'bestaudio', '-x', '--audio-format', 'wav']
    else:
        raise ValueError('unknown format')
    cmd.append(url)
    return cmd


def run_job(job_id, url, fmt, quality):
    tempdir = tempfile.mkdtemp(prefix='ytdl_')
    out_template = os.path.join(tempdir, '%(title)s.%(ext)s')
    try:
        cmd = build_cmd(url, fmt, quality, out_template)
    except Exception as e:
        _job_error(job_id, str(e))
        shutil.rmtree(tempdir, ignore_errors=True)
        return

    creation = 0
    if os.name == 'nt':
        creation = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', bufsize=1,
            creationflags=creation,
        )
    except Exception as e:
        _job_error(job_id, f'起動失敗: {e}')
        shutil.rmtree(tempdir, ignore_errors=True)
        return

    last_lines = []
    dest_count = 0
    phase = 'download'

    for line in proc.stdout:
        line = line.rstrip()
        if line:
            last_lines.append(line)
            if len(last_lines) > 30:
                last_lines.pop(0)

        if '[download] Destination:' in line:
            dest_count += 1
            phase = 'video' if (fmt == 'mp4' and dest_count == 1) else ('audio' if fmt == 'mp4' else 'download')
        elif '[Merger]' in line or 'Merging formats' in line:
            phase = 'merging'
            _job_set(job_id, progress=97.0, phase=phase)
            continue
        elif '[ExtractAudio]' in line:
            phase = 'extracting'
            _job_set(job_id, progress=97.0, phase=phase)
            continue

        m = PROGRESS_RE.search(line)
        if m:
            pct = float(m.group(1))
            if fmt == 'mp4':
                overall = pct * 0.5 if dest_count <= 1 else 50 + pct * 0.45
            else:
                overall = pct * 0.95
            _job_set(job_id, progress=overall, phase=phase)

    proc.wait()
    if proc.returncode != 0:
        err = '\n'.join(last_lines[-6:]) or 'yt-dlp failed'
        _job_error(job_id, err[-600:])
        shutil.rmtree(tempdir, ignore_errors=True)
        return

    files = [p for p in Path(tempdir).iterdir() if p.is_file()]
    if not files:
        _job_error(job_id, '出力ファイルが見つかりません')
        shutil.rmtree(tempdir, ignore_errors=True)
        return

    fp = max(files, key=lambda p: p.stat().st_size)
    with jobs_lock:
        jobs[job_id]['filepath'] = str(fp)
        jobs[job_id]['tempdir'] = tempdir
        jobs[job_id]['progress'] = 100.0
        jobs[job_id]['phase'] = 'done'
        jobs[job_id]['status'] = 'done'


def _job_set(job_id, **kw):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kw)


def _job_error(job_id, message):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['error'] = message


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, fmt, *args):
        msg = fmt % args
        if '/api/ping' in msg or '/api/progress/' in msg:
            return
        print('[server]', msg)

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path in ('/', '/api/health'):
            self._send_json(200, {
                'ok': True,
                'service': 'youtube-dl-backend',
                'public_mode': PUBLIC_MODE,
                'cookies': bool(get_cookies_file()),
            })
            return
        if self.path == '/api/ping':
            global last_ping
            with ping_lock:
                last_ping = time.monotonic()
            self._send_json(200, {'ok': True})
            return
        if self.path == '/api/config':
            self._send_json(200, {
                'ok': True,
                'public_mode': PUBLIC_MODE,
                'shutdown_on_close': not PUBLIC_MODE,
            })
            return
        if self.path.startswith('/api/progress/'):
            self._handle_progress(self.path.rsplit('/', 1)[-1])
            return
        if self.path.startswith('/api/file/'):
            self._handle_file(self.path.rsplit('/', 1)[-1])
            return
        super().do_GET()

    def do_POST(self):
        if self.path == '/api/shutdown':
            self.send_response(204)
            self.end_headers()
            if not PUBLIC_MODE:
                threading.Thread(target=_shutdown, daemon=True).start()
            return
        if self.path == '/api/start':
            self._handle_start()
            return
        self.send_error(404)

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_start(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length).decode('utf-8'))
            url = data.get('url', '').strip()
            fmt = data.get('format', 'mp4')
            quality = data.get('quality', '')
            if not url:
                self._send_json(400, {'error': 'URLが空です'})
                return
            cancel_pending_shutdown()  # 新規DL開始 → 終了予約を取り消し
            job_id = uuid.uuid4().hex
            with jobs_lock:
                jobs[job_id] = {
                    'progress': 0.0, 'status': 'running', 'phase': 'starting',
                    'filepath': None, 'tempdir': None, 'error': None,
                    'fmt': fmt,
                }
            threading.Thread(target=run_job, args=(job_id, url, fmt, quality), daemon=True).start()
            self._send_json(200, {'job_id': job_id})
        except Exception as e:
            self._send_json(500, {'error': str(e)})

    def _handle_progress(self, job_id):
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        try:
            while True:
                with jobs_lock:
                    job = jobs.get(job_id)
                if job is None:
                    payload = json.dumps({'status': 'error', 'error': 'job not found'})
                    self.wfile.write(f'data: {payload}\n\n'.encode('utf-8'))
                    self.wfile.flush()
                    return
                payload = json.dumps({
                    'progress': round(job['progress'], 1),
                    'status': job['status'],
                    'phase': job.get('phase', ''),
                    'error': job.get('error'),
                })
                self.wfile.write(f'data: {payload}\n\n'.encode('utf-8'))
                self.wfile.flush()
                if job['status'] in ('done', 'error'):
                    return
                time.sleep(0.3)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _handle_file(self, job_id):
        with jobs_lock:
            job = jobs.get(job_id)
        if not job or job.get('status') != 'done' or not job.get('filepath'):
            self.send_error(404)
            return
        fp = Path(job['filepath'])
        if not fp.exists():
            self.send_error(404)
            return
        filename = sanitize_filename(fp.name)
        size = fp.stat().st_size
        mime = {'mp4': 'video/mp4', 'mp3': 'audio/mpeg', 'wav': 'audio/wav'}.get(
            job.get('fmt', ''), 'application/octet-stream')
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(size))
        quoted = urllib.parse.quote(filename)
        self.send_header('Content-Disposition', f"attachment; filename*=UTF-8''{quoted}")
        self.end_headers()
        try:
            with open(fp, 'rb') as f:
                shutil.copyfileobj(f, self.wfile)
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            tempdir = job.get('tempdir')
            with jobs_lock:
                jobs.pop(job_id, None)
            if tempdir:
                shutil.rmtree(tempdir, ignore_errors=True)
            # ダウンロード完了 → 拡張利用時はサーバーを自動終了
            schedule_idle_shutdown()


def _shutdown():
    server = httpd_ref.get('server')
    if server is not None:
        print('\nブラウザが閉じられました。サーバーを終了します。')
        threading.Thread(target=server.shutdown, daemon=True).start()


def _watchdog():
    if PUBLIC_MODE:
        return

    time.sleep(15)
    while True:
        time.sleep(2)
        with ping_lock:
            lp = last_ping
        # Web UI を一度も開いていない (拡張のみ利用) 場合は終了しない
        if lp is None:
            continue
        if time.monotonic() - lp > HEARTBEAT_TIMEOUT:
            _shutdown()
            return


def check_deps():
    try:
        subprocess.run([sys.executable, '-m', 'yt_dlp', '--version'],
                       capture_output=True, check=True)
    except Exception:
        print('[警告] yt-dlp 未インストール:  pip install -U yt-dlp')
    if shutil.which('ffmpeg') is None:
        print('[警告] ffmpeg が見つかりません (MP3/WAV/高画質マージに必須)')
        print('       https://www.gyan.dev/ffmpeg/builds/ から DL し PATH に追加してください')


if __name__ == '__main__':
    check_deps()
    os.chdir(ROOT)
    with socketserver.ThreadingTCPServer((HOST, PORT), Handler) as httpd:
        httpd.daemon_threads = True
        httpd_ref['server'] = httpd
        threading.Thread(target=_watchdog, daemon=True).start()
        print(f'YouTube Downloader: http://{HOST}:{PORT}')
        if PUBLIC_MODE:
            print('公開モード: /api/shutdown と自動終了を無効化しています')
        else:
            print('Ctrl+C またはブラウザを閉じると終了します')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\n終了します')
