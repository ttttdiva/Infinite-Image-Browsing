"""run.bat用。生成ツールを起動せず、画像ライブラリだけを直接開く。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local' / 'desktop'
BASE = '/iib'


def request(port: int, secret: str, path: str, body=None):
    cookie = hashlib.sha256((secret + '_ciallo').encode()).hexdigest()
    headers = {'Cookie': 'IIB_S=' + cookie}
    if body is not None: headers['Content-Type'] = 'application/json'
    req = Request(f'http://127.0.0.1:{port}{BASE}{path}',
                  data=json.dumps(body).encode() if body is not None else None, headers=headers)
    with urlopen(req, timeout=4) as response:
        return json.load(response)


def studio_instance():
    """既存73の同じIIBなら再利用。資格情報は表示・URL・引数へ出さない。"""
    config_path = ROOT.parent / '73_ComfyUI-Workbench' / '.local' / 'generation-studio' / 'config.json'
    try:
        config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        if os.path.normcase(os.path.realpath(config.get('iib_root', ''))) != os.path.normcase(str(ROOT)):
            return None
        port, secret = int(config['iib_port']), str(config['iib_secret_key'])
        if config.get('iib_host') != '127.0.0.1' or not 1 <= port <= 65535 or not secret:
            return None
        request(port, secret, '/library-api/config')
        return port, secret
    except (OSError, ValueError, KeyError, URLError, HTTPError):
        return None


def serve(port: int):
    if not os.getenv('IIB_SECRET_KEY'):
        raise RuntimeError('Desktop authentication is required')
    sys.path.insert(0, str(ROOT)); os.chdir(ROOT)
    from app import AppUtils
    import uvicorn
    roots = json.loads(os.getenv('IIB_DESKTOP_ROOTS', '[]'))
    app = AppUtils(base=BASE, extra_paths=roots, export_fe_fn=True).get_root_browser_app()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, access_log=False))
    app.state.media_library_desktop_stop = lambda: setattr(server, 'should_exit', True)
    try:
        server.run()
    finally:
        library = getattr(app.state, 'media_library', None)
        if library:
            library.cancel.set()
            if library._thread: library._thread.join(30)


def port_is_listening(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(('127.0.0.1', port)) == 0


def stop_saved_instance(port: int, state_path: Path, saved: dict):
    # A visible run must not silently reuse a server started by the old hidden
    # launcher.  Stop only the authenticated standalone instance recorded in
    # this port's state file; an unrelated listener is never adopted.
    result = request(port, saved['secret'], '/library-api/desktop-stop', {})
    if not result.get('stopping'):
        raise RuntimeError('既存の単独画像ライブラリを安全に停止できませんでした。')
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not port_is_listening(port):
            state_path.unlink(missing_ok=True)
            return
        time.sleep(.2)
    raise RuntimeError('既存の単独画像ライブラリが停止する前に時間切れになりました。')


def start(port: int, state_path: Path, visible_console: bool = False):
    if state_path.exists():
        try:
            saved = json.loads(state_path.read_text(encoding='utf-8'))
            if not isinstance(saved, dict):
                raise ValueError('standalone state is not an object')
            if not isinstance(saved.get('secret'), str) or not saved['secret']:
                raise ValueError('standalone state has no secret')
            request(port, saved['secret'], '/library-api/config')
            if not visible_console or saved.get('visible_console') is True:
                return port, saved['secret']
            stop_saved_instance(port, state_path, saved)
        except (OSError, TypeError, ValueError, KeyError, URLError, HTTPError):
            pass
    # 未所有の待受を停止・乗っ取りしない。
    if port_is_listening(port):
        raise RuntimeError(f'画像ライブラリ用ポート {port} は別のプロセスが使用中です。')
    safe_names = {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','USERPROFILE','APPDATA','LOCALAPPDATA',
                  'HOMEDRIVE','HOMEPATH','PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMDATA',
                  'COMSPEC','PATHEXT','PROCESSOR_ARCHITECTURE','NUMBER_OF_PROCESSORS'}
    environment = {key: value for key, value in os.environ.items() if key.upper() in safe_names}
    secret = secrets.token_urlsafe(48)
    # Standalone startup must not infer media roots from the repository drive.
    # Persisted roots are loaded from the existing IIB DB by MediaLibrary;
    # folders are added there only after the user explicitly registers them.
    environment.update(IIB_SECRET_KEY=secret, IIB_ACCESS_CONTROL='enable', IIB_ACCESS_CONTROL_PERMISSION='read-write',
                       IIB_DESKTOP_ROOTS='[]',
                       PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    LOCAL.mkdir(parents=True, exist_ok=True)
    log_path = LOCAL / f'desktop-{port}.log'
    command = [sys.executable, str(Path(__file__).resolve()), '--server', '--port', str(port)]
    actual_visible_console = bool(visible_console and os.name == 'nt')
    if actual_visible_console:
        # run.bat is the operator-facing standalone launcher. Give its server
        # an ordinary console so it is obvious that IIB is still running.
        process = subprocess.Popen(command, cwd=ROOT, env=environment,
                                   creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
    else:
        with log_path.open('ab') as log:
            process = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    temporary = state_path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'port': port, 'secret': secret, 'pid': process.pid,
                                     'visible_console': actual_visible_console}), encoding='utf-8')
    temporary.replace(state_path)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('画像ライブラリを起動できませんでした。.local/desktopのログを確認してください。')
        try:
            request(port, secret, '/library-api/config')
            return port, secret
        except (OSError, ValueError, URLError, HTTPError):
            time.sleep(.3)
    raise RuntimeError('起動確認が時間内に完了しませんでした。run.batを再実行してください。')



@contextmanager
def desktop_lock(port):
    LOCAL.mkdir(parents=True, exist_ok=True)
    with (LOCAL / f'launcher-{port}.lock').open('a+b') as handle:
        if handle.tell() == 0: handle.write(b'0'); handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt': msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', action='store_true')
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--standalone', action='store_true')
    parser.add_argument('--visible-console', action='store_true')
    parser.add_argument('--port', type=int, default=17860)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535: raise RuntimeError('ポート番号が無効です')
    if args.server: serve(args.port); return
    with desktop_lock(args.port):
        state_path = LOCAL / f'desktop-{args.port}.json'
        if args.stop:
            if not state_path.exists():
                print('単独起動した画像ライブラリはありません。73のIIBには変更しません。'); return
            try:
                saved = json.loads(state_path.read_text(encoding='utf-8'))
                if not isinstance(saved, dict):
                    raise ValueError('standalone state is not an object')
                if not isinstance(saved.get('secret'), str) or not saved['secret']:
                    raise ValueError('standalone state has no secret')
            except (OSError, TypeError, ValueError, KeyError):
                print('単独ライブラリのstateが壊れているため停止確認を行いません。別プロセスには触れていません。'); return
            try:
                result = request(args.port, saved['secret'], '/library-api/desktop-stop', {})
                if result.get('stopping'): state_path.unlink(missing_ok=True)
            except (URLError, HTTPError):
                print('単独ライブラリの停止を確認できません。別プロセスには触れていません。'); return
            print('単独起動した画像ライブラリを停止しました。73・ComfyUIは変更していません。'); return
        instance = None if args.standalone else studio_instance()
        reused = instance is not None
        port, secret = instance or start(args.port, state_path, visible_console=args.visible_console)
        ticket = request(port, secret, '/library-api/desktop-ticket', {})['ticket']
        # fragmentはHTTP access logやRefererへ送られない。交換後ブラウザ履歴から即座に除去。
        if not args.no_browser:
            webbrowser.open(f'http://127.0.0.1:{port}{BASE}/desktop#{ticket}')
        print(json.dumps({'ready': True, 'reused_studio_iib': reused, 'port': port}, ensure_ascii=False))



if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # HTTP bodyやsecret付きRequestはログへ投影しない。
        print(str(error) if isinstance(error, RuntimeError) else '画像ライブラリを起動できませんでした。設定または接続を確認してください。', file=sys.stderr)
        sys.exit(1)
