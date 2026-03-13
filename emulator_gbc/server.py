#!/usr/bin/env python3
"""GBC Emulator Web Server for WiFi Pineapple Pager"""

import json
import os
import socketserver
import sys
import threading
import time
import glob
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

PAYLOAD_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8080

# ----------------------------------------------------------------
# Globals
# ----------------------------------------------------------------
_buttons      = 0
_buttons_lock = threading.Lock()
_game_name    = None
_game_lock    = threading.Lock()
_pager        = None
_log_file     = None


def _log(*args):
    msg = ' '.join(str(a) for a in args)
    print(msg, flush=True)
    if _log_file:
        _log_file.write(msg + '\n')
        _log_file.flush()


def get_local_ip():
    import socket
    for target in ('10.255.255.255', '8.8.8.8'):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((target, 1))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith('127.'):
                return ip
        except Exception:
            pass
    try:
        import socket
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return '127.0.0.1'


# ----------------------------------------------------------------
# Pager display helpers
# ----------------------------------------------------------------
def _show_splash(pager, url):
    pager.clear(pager.BLACK)
    pager.draw_text_centered(55,  'GBC EMULATOR',   pager.GREEN,  2)
    pager.draw_text_centered(90,  'Open in browser:', pager.WHITE, 1)
    pager.draw_text_centered(108, url,               pager.CYAN,   2)
    pager.draw_text_centered(158, 'Hold A+B to quit', pager.GRAY,  1)
    pager.flip()


def _show_game(pager, name):
    # Truncate long names to fit screen
    if len(name) > 28:
        name = name[:25] + '...'
    pager.clear(pager.BLACK)
    pager.draw_text_centered(30,  'NOW PLAYING',    pager.GREEN, 2)
    pager.draw_text_centered(75,  name,             pager.WHITE, 1)
    pager.draw_text_centered(115, 'Z=A  X=B',       pager.CYAN,  1)
    pager.draw_text_centered(133, 'Enter=START',    pager.CYAN,  1)
    pager.draw_text_centered(151, 'Shift=SELECT',   pager.CYAN,  1)
    pager.draw_text_centered(185, 'Hold A+B to quit', pager.GRAY, 1)
    pager.flip()


def do_quit(pager=None):
    if pager:
        try:
            pager.clear(pager.BLACK)
            pager.draw_text_centered(100, 'Goodbye!', pager.GREEN, 2)
            pager.flip()
            time.sleep(0.5)
            pager.cleanup()
        except Exception:
            pass
    os._exit(0)


# ----------------------------------------------------------------
# Pager thread: info display + button polling
# ----------------------------------------------------------------
def pager_thread():
    global _buttons, _pager
    try:
        sys.path.insert(0, PAYLOAD_DIR)
        from pagerctl import Pager
        _pager = Pager()
        result = _pager.init()
        _log(f'pager.init={result}')
        if result != 0:
            _log('pager init failed')
            return

        _pager.set_rotation(270)

        ip  = get_local_ip()
        url = f'http://{ip}:{PORT}'
        _show_splash(_pager, url)
        _log(f'splash: {url}')

        current_game = None
        red_held_since = None

        while True:
            time.sleep(0.05)

            # Refresh display if game changed
            with _game_lock:
                new_game = _game_name
            if new_game != current_game:
                current_game = new_game
                if current_game:
                    _show_game(_pager, current_game)
                else:
                    _show_splash(_pager, url)

            # Button polling
            try:
                current = _pager.peek_buttons()
            except Exception:
                current = 0
            with _buttons_lock:
                _buttons = current

            # POWER button = instant quit
            if current & 0x40:
                _log('POWER — exiting')
                do_quit(_pager)

            # Hold RED (B) for 2 seconds = quit
            if current & 0x20:
                if red_held_since is None:
                    red_held_since = time.time()
                elif time.time() - red_held_since >= 1.0:
                    _log('RED held — exiting')
                    do_quit(_pager)
            else:
                red_held_since = None

    except Exception as e:
        _log(f'pager_thread: {e}')
    finally:
        if _pager:
            try:
                _pager.cleanup()
            except Exception:
                pass


# ----------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Filename, X-Game-Name')

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            self._serve_file('index.html', 'text/html; charset=utf-8')
        elif path == '/roms':
            self._serve_roms()
        elif path.startswith('/rom/'):
            self._serve_rom(path[5:])
        elif path == '/buttons':
            self._serve_buttons()
        else:
            fname = os.path.basename(path)
            fpath = os.path.join(PAYLOAD_DIR, fname)
            if os.path.isfile(fpath):
                ct = ('text/html' if fname.endswith('.html') else
                      'application/javascript' if fname.endswith('.js') else
                      'application/octet-stream')
                self._serve_file(fname, ct)
            else:
                self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/game':
            self._handle_game()
        elif path == '/upload':
            self._handle_upload()
        elif path == '/quit':
            self._handle_quit()
        else:
            self.send_error(404)

    def _serve_file(self, filename, content_type):
        try:
            with open(os.path.join(PAYLOAD_DIR, filename), 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(data))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def _serve_roms(self):
        roms = sorted(set(
            os.path.basename(p)
            for ext in ('*.gb', '*.gbc', '*.GB', '*.GBC')
            for p in glob.glob(os.path.join(PAYLOAD_DIR, ext))
        ))
        data = json.dumps(roms).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(data))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _serve_rom(self, name):
        name = os.path.basename(name)
        if not name.lower().endswith(('.gb', '.gbc')):
            self.send_error(404)
            return
        fpath = os.path.join(PAYLOAD_DIR, name)
        if not os.path.isfile(fpath):
            self.send_error(404)
            return
        with open(fpath, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', len(data))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _serve_buttons(self):
        with _buttons_lock:
            b = _buttons
        data = json.dumps({'buttons': b}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(data))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _handle_game(self):
        global _game_name
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8', errors='replace').strip()
        try:
            name = json.loads(body).get('name', body)
        except Exception:
            name = body
        with _game_lock:
            _game_name = name if name else None
        _log(f'game: {_game_name}')
        self.send_response(200)
        self._cors()
        self.end_headers()

    def _handle_upload(self):
        length = int(self.headers.get('Content-Length', 0))
        if length <= 0 or length > 16 * 1024 * 1024:
            self.send_error(400, 'Bad size')
            return
        name = os.path.basename(self.headers.get('X-Filename', 'rom.gbc'))
        if not name.lower().endswith(('.gb', '.gbc')):
            self.send_error(400, 'Only .gb/.gbc files accepted')
            return
        data = self.rfile.read(length)
        fpath = os.path.join(PAYLOAD_DIR, name)
        with open(fpath, 'wb') as f:
            f.write(data)
        _log(f'uploaded {name} ({len(data)}B)')
        resp = json.dumps({'ok': True, 'name': name}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(resp))
        self._cors()
        self.end_headers()
        self.wfile.write(resp)

    def _handle_quit(self):
        self.send_response(200)
        self._cors()
        self.end_headers()
        _log('quit from browser')
        do_quit(_pager)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------
def main():
    global _log_file
    _log_file = open(os.path.join(PAYLOAD_DIR, 'server.log'), 'w', buffering=1)
    sys.stderr = _log_file
    _log('[server] starting')

    t = threading.Thread(target=pager_thread, daemon=True)
    t.start()

    ip = get_local_ip()
    _log(f'[server] http://{ip}:{PORT}')

    try:
        server = ThreadedHTTPServer(('', PORT), Handler)
        server.serve_forever()
    except KeyboardInterrupt:
        _log('[server] interrupted')
    finally:
        if _log_file:
            _log_file.close()


if __name__ == '__main__':
    main()
