"""IIBの既存DBと認証を使う、画像優先ライブラリAPI。課金APIは呼ばない。"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import uuid
from pathlib import Path
import sqlite3
import threading
import time
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from PIL import Image, ImageOps

from scripts.iib.db.datamodel import DataBase, ExtraPath
from scripts.iib.media_library_metadata import (
    IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, PARSER_REVISION, read_metadata, searchable_text,
)

WEB_ROOT = Path(__file__).resolve().parents[2] / 'media-library'
# File Explorer compares a run of decimal digits as a number when it falls
# back to a name comparison (for example, ``image2`` comes before ``image10``).
# Keep the key text based so SQLite can use it for both ordering and cursors.
_NATURAL_NAME_PARTS = re.compile(r'(\d+)')

try:
    _SHLWAPI = ctypes.WinDLL('shlwapi', use_last_error=True) if os.name == 'nt' else None
    _STRCMP_LOGICAL_W = _SHLWAPI.StrCmpLogicalW if _SHLWAPI is not None else None
    if _STRCMP_LOGICAL_W is not None:
        _STRCMP_LOGICAL_W.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        _STRCMP_LOGICAL_W.restype = ctypes.c_int
except (AttributeError, OSError):
    _STRCMP_LOGICAL_W = None


def _natural_name_key(value: str | None) -> str:
    """Return a deterministic, case-insensitive natural filename key.

    Decimal runs are left-padded to the Windows filename component limit, so
    their lexical order is their numeric order while punctuation and letters
    retain their normal relative order. Leading zeroes are retained in a final
    tie-break, so the key is deterministic for names such as ``frame1`` and
    ``frame01``.
    """
    name = Path(value).name if value else ''
    parts = _NATURAL_NAME_PARTS.split(name.casefold())
    encoded = []
    for index, part in enumerate(parts):
        if index % 2 and part.isdigit():
            number = part.lstrip('0') or '0'
            # Windows limits a filename component to 255 characters. Padding
            # to that width keeps the key bounded and avoids integer parsing.
            encoded.append(number.rjust(255, '0'))
        else:
            encoded.append(part)
    return ''.join(encoded) + '\x01' + name.casefold()


def _raw_basename(value: str | None) -> str:
    """Return the original basename so cursor keys retain case exactly."""
    return Path(value).name if value else ''


def _natural_name_compare(left: str, right: str) -> int:
    """Compare basenames using Windows shell semantics where available."""
    if _STRCMP_LOGICAL_W is not None:
        return int(_STRCMP_LOGICAL_W(left or '', right or ''))
    left_key, right_key = _natural_name_key(left), _natural_name_key(right)
    return (left_key > right_key) - (left_key < right_key)


# ロックは画像単位の重複生成と同時デコードのメモリ使用を制限する。
_THUMB_SLOTS = threading.BoundedSemaphore(3)
_THUMB_LOCKS = [threading.Lock() for _ in range(32)]


class SearchRequest(BaseModel):
    q: str = Field(default='', max_length=1000)
    folder: str = Field(default='', max_length=2048)
    source: str = Field(default='', max_length=512)
    model: str = Field(default='', max_length=512)
    lora: str = Field(default='', max_length=512)
    tags: list[int] = Field(default_factory=list, max_length=32)
    kind: Literal['all', 'image', 'video'] = 'all'
    sort: Literal['newest', 'oldest', 'name', 'name_desc'] = 'newest'
    cursor: str = Field(default='', max_length=2048)
    limit: int = Field(default=120, ge=1, le=240)


class RootRequest(BaseModel):
    path: str = Field(min_length=1, max_length=2048)
    action: Literal['add', 'remove'] = 'add'


class DesktopSession(BaseModel):
    ticket: str = Field(min_length=32, max_length=128)


class TagRequest(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=128)
    action: Literal['add', 'remove'] = 'add'


class RatingRequest(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    rating: int = Field(ge=0, le=10)


class TagMembershipRequest(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)


class MoveItemsRequest(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    dest: str = Field(min_length=1, max_length=4096)


class ChatContextRequest(BaseModel):
    image_path: str = Field(min_length=1, max_length=4096)
    instruction: str = Field(min_length=1, max_length=50000)
    character_name: str = Field(default='', max_length=1000)
    context_summary: str = Field(default='', max_length=10000)
    search_terms: list[str] = Field(default_factory=list, max_length=100)
    source: str = Field(min_length=1, max_length=200)


def canonical(path: str) -> str:
    return os.path.normpath(os.path.abspath(path))


def inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))]) == os.path.normcase(os.path.realpath(root))
    except (OSError, ValueError):
        return False


def escaped(value: str) -> str:
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


class MediaLibrary:
    def __init__(self, db_path: str, cli_roots: list[str], root_guard: Callable[[str], None] | None = None, root_enrollment_guard=None, roots_changed=None):
        self.db_path = db_path
        self.cli_roots = list(cli_roots)
        self.root_guard = root_guard or (lambda path: None)
        self.root_enrollment_guard = root_enrollment_guard or self.root_guard
        self.roots_changed = roots_changed or (lambda: None)
        self.lock = threading.RLock()
        self.write_lock = threading.RLock()
        self.cancel = threading.Event()
        self.progress = {'running': False, 'seen': 0, 'updated': 0, 'errors': 0, 'finished_at': None, 'failure': None}
        self.cache_root = Path(db_path).parent / '.media-library-cache'
        self._thread = None
        self.rescan_requested = False
        self.fts = False
        with self.connection() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS media_library (
                    image_id INTEGER PRIMARY KEY, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
                    model TEXT NOT NULL, metadata TEXT NOT NULL, search_text TEXT NOT NULL,
                    parser_rev INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ml_date ON media_library(mtime_ns, image_id);
                CREATE INDEX IF NOT EXISTS ml_source ON media_library(source);
                CREATE INDEX IF NOT EXISTS ml_model ON media_library(model);
                CREATE TABLE IF NOT EXISTS media_library_excluded_root (path TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS media_library_rating (
                    image_id INTEGER PRIMARY KEY,
                    image_path TEXT NOT NULL,
                    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 10),
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS media_library_chat_context (
                    path TEXT PRIMARY KEY,
                    instruction TEXT NOT NULL,
                    character_name TEXT NOT NULL DEFAULT '',
                    context_summary TEXT NOT NULL DEFAULT '',
                    search_terms TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS ml_chat_context_source ON media_library_chat_context(source);
                CREATE INDEX IF NOT EXISTS ml_tag_lookup ON tag(type, name);
            ''')
            columns = {row[1] for row in conn.execute('PRAGMA table_info(media_library)')}
            if 'image_path' not in columns:
                conn.execute("ALTER TABLE media_library ADD COLUMN image_path TEXT NOT NULL DEFAULT ''")
                conn.execute("UPDATE media_library SET image_path=COALESCE((SELECT path FROM image WHERE image.id=media_library.image_id),'')")
            try:
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS media_library_fts USING fts5(search_text, tokenize='trigram')")
                self.fts = True
            except sqlite3.OperationalError:
                pass

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.create_function('iib_basename', 1, lambda value: Path(value).name.casefold() if value else '')
        conn.create_function('iib_raw_basename', 1, _raw_basename)
        conn.create_collation('IIB_NATURAL', _natural_name_compare)
        conn.execute('PRAGMA busy_timeout=30000')
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def roots(self) -> list[dict]:
        with self.connection() as conn:
            saved = conn.execute('SELECT path, alias FROM extra_path').fetchall()
            excluded = {os.path.normcase(r[0]) for r in conn.execute('SELECT path FROM media_library_excluded_root')}
        paths = [(p, '') for p in self.cli_roots] + [(r['path'], r['alias']) for r in saved]
        unique = {}
        for value, alias in paths:
            path = canonical(value)
            if os.path.normcase(path) in excluded:
                continue
            try:
                self.root_guard(os.path.realpath(path))
            except HTTPException:
                continue
            unique[os.path.normcase(path)] = {'path': path, 'label': alias or Path(path).name or path, 'available': os.path.isdir(path)}
        return list(unique.values())

    def checked_path(self, path: str) -> str:
        path = canonical(path)
        if not any(inside(path, r['path']) for r in self.roots()):
            raise HTTPException(403, '登録フォルダ外のファイルです')
        if Path(path).suffix.lower() not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
            raise HTTPException(415, '画像・動画以外は表示できません')
        if not os.path.isfile(path):
            raise HTTPException(404, 'ファイルが見つかりません')
        return path

    def checked_folder(self, path: str) -> str:
        path = canonical(path)
        if not any(inside(path, r['path']) for r in self.roots()):
            raise HTTPException(403, '登録フォルダ外です')
        if not os.path.isdir(path):
            raise HTTPException(404, 'フォルダが見つかりません')
        return path

    def unique_destination(self, dest: str, filename: str, sidecar: bool = False) -> str:
        dest = self.checked_folder(dest)
        safe_name = Path(filename).name.strip()
        if not safe_name or safe_name in ('.', '..'):
            raise HTTPException(400, 'ファイル名が無効です')
        stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
        index = 0
        while True:
            name = safe_name if index == 0 else f'{stem} ({index}){suffix}'
            candidate = os.path.join(dest, name)
            sidecar_path = str(Path(candidate).with_suffix('.txt'))
            if not os.path.exists(candidate) and (not sidecar or not os.path.exists(sidecar_path)):
                return candidate
            index += 1

    def resolve_id(self, image_id: int) -> tuple[str, dict]:
        with self.connection() as conn:
            row = conn.execute('SELECT i.path, m.* FROM image i JOIN media_library m ON m.image_id=i.id AND m.image_path=i.path WHERE i.id=?', (image_id,)).fetchone()
        if row is None:
            raise HTTPException(404, '画像が見つかりません')
        return self.checked_path(row['path']), dict(row)

    def change_root(self, req: RootRequest):
        if not os.path.isabs(req.path) or req.path.startswith(('\\\\', '//')):
            raise HTTPException(400, 'このPCのフォルダを絶対パスで指定してください')
        path = canonical(req.path)
        self.root_enrollment_guard(os.path.realpath(path))
        if req.action == 'add' and not os.path.isdir(path):
            raise HTTPException(400, 'フォルダが見つかりません')
        if req.action == 'add' and Path(path).parent == Path(path):
            raise HTTPException(400, 'ドライブ全体ではなく画像のフォルダを指定してください')
        with self.write_lock, self.connection() as conn:
            if req.action == 'remove':
                # ファイルも既存索引/タグも削除しない。表示対象から外すだけ。
                conn.execute('INSERT OR IGNORE INTO media_library_excluded_root(path) VALUES (?)', (path,))
            else:
                conn.execute('DELETE FROM media_library_excluded_root WHERE path=?', (path,))
                existing = conn.execute('SELECT type,alias FROM extra_path WHERE path=?', (path,)).fetchone()
                types = list(dict.fromkeys((existing['type'].split('+') if existing else []) + ['scanned']))
                ExtraPath(path, types, existing['alias'] if existing else '').save(conn)
        self.roots_changed()
        return self.roots()

    def status(self) -> dict:
        with self.lock:
            progress = dict(self.progress)
        with self.connection() as conn:
            count = conn.execute('SELECT COUNT(*) FROM media_library m JOIN image i ON i.id=m.image_id AND m.image_path=i.path').fetchone()[0]
        return {**progress, 'indexed': count}

    def start_scan(self):
        with self.lock:
            if self.progress['running']:
                self.rescan_requested = True
                return dict(self.progress)
            roots = [r['path'] for r in self.roots() if r['available']]
            self.cancel.clear()
            self.progress = {'running': True, 'seen': 0, 'updated': 0, 'errors': 0, 'finished_at': None, 'failure': None}
            self._thread = threading.Thread(target=self._scan, args=(roots,), daemon=True, name='iib-media-library-index')
            self._thread.start()
            return dict(self.progress)

    def _scan(self, roots: list[str]):
        try:
            seen_paths = set()
            with self.connection() as conn:
                known = {os.path.normcase(r['path']): (r['id'], r['mtime_ns'], r['parser_rev'])
                         for r in conn.execute('SELECT i.id,i.path,m.mtime_ns,m.parser_rev FROM image i LEFT JOIN media_library m ON m.image_id=i.id AND m.image_path=i.path')}
                tag_ids = {(r['type'], r['name']): r['id'] for r in conn.execute('SELECT id,type,name FROM tag')}
            for root in roots:
                for folder, directories, files in os.walk(root, followlinks=False):
                    if self.cancel.is_set():
                        return
                    directories[:] = [name for name in directories if not name.startswith('.') and not os.path.islink(os.path.join(folder, name)) and inside(os.path.join(folder, name), root)]
                    # 新しい生成物が初期索引中でも先に見える順。
                    candidates = []
                    for name in files:
                        path = canonical(os.path.join(folder, name))
                        if Path(name).suffix.lower() not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS or os.path.islink(path):
                            continue
                        key = os.path.normcase(path)
                        if key in seen_paths or not inside(path, root):
                            continue
                        seen_paths.add(key)
                        try:
                            stat = os.stat(path)
                            candidates.append((stat.st_mtime_ns, path, stat))
                        except OSError:
                            continue
                    candidates.sort(reverse=True)
                    batch = []
                    for mtime, path, stat in candidates:
                        if self.cancel.is_set():
                            return
                        with self.lock:
                            self.progress['seen'] += 1
                        previous = known.get(os.path.normcase(path))
                        if previous and previous[1:] == (mtime, PARSER_REVISION):
                            continue
                        try:
                            meta = read_metadata(path)
                            if meta['width'] <= 0 or meta['height'] <= 0:
                                raise ValueError('invalid dimensions')
                            # 書き込み中のファイルは次のrefreshで再試行。
                            now = os.stat(path)
                            if (now.st_mtime_ns, now.st_size) != (mtime, stat.st_size):
                                continue
                            batch.append((path, stat, meta))
                        except Exception:
                            with self.lock:
                                self.progress['errors'] += 1
                        if len(batch) >= 48:
                            self._save_batch(batch, tag_ids)
                            batch.clear()
                    if batch:
                        self._save_batch(batch, tag_ids)
        except Exception:
            with self.lock:
                self.progress['failure'] = '索引の更新に失敗しました。再読み込みで再試行できます。'
        finally:
            with self.lock:
                self.progress['running'] = False
                self.progress['finished_at'] = time.time()
                rescan = self.rescan_requested
                self.rescan_requested = False
            if rescan and not self.cancel.is_set():
                self.start_scan()

    def _save_batch(self, batch, tag_ids):
        with self.write_lock, self.connection() as conn:
            for path, stat, meta in batch:
                raw = meta['positive'] + '\nNegative prompt: ' + meta['negative'] + '\nModel: ' + meta['model'] + ', Source Identifier: ' + meta['source']
                date = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                conn.execute('INSERT OR IGNORE INTO image(path,exif,size,date,exif_edited) VALUES (?,?,?,?,0)', (path, raw, stat.st_size, date))
                row = conn.execute('SELECT id,exif_edited FROM image WHERE path=?', (path,)).fetchone()
                image_id = row['id']
                if not row['exif_edited']:
                    conn.execute('UPDATE image SET exif=?,size=?,date=? WHERE id=?', (raw, stat.st_size, date, image_id))
                kind = 'video' if Path(path).suffix.lower() in VIDEO_EXTENSIONS else 'image'
                search = searchable_text(meta)
                conn.execute('INSERT OR REPLACE INTO media_library(image_id,width,height,mtime_ns,kind,source,model,metadata,search_text,parser_rev,image_path) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                             (image_id, meta['width'], meta['height'], stat.st_mtime_ns, kind, meta['source'], meta['model'], json.dumps(meta, ensure_ascii=False), search, PARSER_REVISION, path))
                if self.fts:
                    conn.execute('DELETE FROM media_library_fts WHERE rowid=?', (image_id,))
                    conn.execute('INSERT INTO media_library_fts(rowid,search_text) VALUES (?,?)', (image_id, search))
                # 明示編集済みEXIFのタグを上書きしない。customタグは常に保持。
                if not row['exif_edited']:
                    old = conn.execute("SELECT it.tag_id FROM image_tag it JOIN tag t ON t.id=it.tag_id WHERE it.image_id=? AND t.type IN ('Model','Source Identifier','lora','pos','size','Media Type')", (image_id,)).fetchall()
                    for item in old:
                        conn.execute('UPDATE tag SET count=MAX(0,count-1) WHERE id=?', (item[0],))
                        conn.execute('DELETE FROM image_tag WHERE image_id=? AND tag_id=?', (image_id, item[0]))
                    tags = [('Source Identifier', meta['source']), ('Model', meta['model']), ('Media Type', kind.title()), ('size', f"{meta['width']} × {meta['height']}")]
                    tags += [('lora', item['name']) for item in meta['loras']]
                    tags += [('pos', value) for value in meta['prompt_tags'] if len(value) <= 160]
                    for tag_type, name in dict.fromkeys(tags):
                        if not name:
                            continue
                        key = (tag_type, name)
                        tag_id = tag_ids.get(key)
                        if tag_id is None:
                            stored = conn.execute('SELECT id FROM tag WHERE type=? AND name=?', key).fetchone()
                            tag_id = stored[0] if stored else conn.execute('INSERT INTO tag(name,score,type,count,color) VALUES (?,0,?,0,?)', (name, tag_type, '')).lastrowid
                            tag_ids[key] = tag_id
                        changed = conn.execute('INSERT OR IGNORE INTO image_tag(image_id,tag_id,created_at) VALUES (?,?,CURRENT_TIMESTAMP)', (image_id, tag_id)).rowcount
                        if changed:
                            conn.execute('UPDATE tag SET count=count+1 WHERE id=?', (tag_id,))
            with self.lock:
                self.progress['updated'] += len(batch)

    def _where(self, req: SearchRequest):
        roots = [r['path'] for r in self.roots()]
        if req.folder:
            selected = canonical(req.folder)
            if not any(inside(selected, root) for root in roots):
                raise HTTPException(403, '登録フォルダ外です')
            roots = [selected]
        if not roots:
            return ['0'], []
        clauses = ['(' + ' OR '.join("i.path LIKE ? ESCAPE '\\'" for _ in roots) + ')']
        params = [escaped(root.rstrip('\\/') + os.sep) + '%' for root in roots]
        for value in req.q.casefold().split():
            like = '%' + escaped(value) + '%'
            if self.fts and len(value) >= 3:
                # trigram FTSは候補を絞り、LIKEで記号を含めた実際の部分一致を確認。
                phrase = '"' + value.replace('"', '""') + '"'
                clauses.append("(i.id IN (SELECT rowid FROM media_library_fts WHERE media_library_fts MATCH ?) OR EXISTS (SELECT 1 FROM image_tag it JOIN tag t ON t.id=it.tag_id WHERE it.image_id=i.id AND t.type='custom' AND lower(t.name) LIKE ? ESCAPE '\\') OR EXISTS (SELECT 1 FROM media_library_chat_context c WHERE c.path=i.path AND lower(c.instruction || char(10) || c.character_name || char(10) || c.context_summary || char(10) || c.search_terms || char(10) || c.source) LIKE ? ESCAPE '\\'))")
                params.extend([phrase, like, like])
            else:
                clauses.append("(m.search_text LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM image_tag it JOIN tag t ON t.id=it.tag_id WHERE it.image_id=i.id AND t.type='custom' AND lower(t.name) LIKE ? ESCAPE '\\') OR EXISTS (SELECT 1 FROM media_library_chat_context c WHERE c.path=i.path AND lower(c.instruction || char(10) || c.character_name || char(10) || c.context_summary || char(10) || c.search_terms || char(10) || c.source) LIKE ? ESCAPE '\\'))")
                params.extend([like, like, like])
        if req.source:
            clauses.append('(m.source=? OR EXISTS (SELECT 1 FROM media_library_chat_context c WHERE c.path=i.path AND c.source=?))'); params.extend([req.source, req.source])
        if req.model:
            clauses.append("m.model LIKE ? ESCAPE '\\'"); params.append('%' + escaped(req.model) + '%')
        if req.kind != 'all':
            clauses.append('m.kind=?'); params.append(req.kind)
        if req.lora:
            clauses.append("EXISTS (SELECT 1 FROM image_tag it JOIN tag t ON t.id=it.tag_id WHERE it.image_id=i.id AND t.type='lora' AND t.name LIKE ? ESCAPE '\\')")
            params.append('%' + escaped(req.lora) + '%')
        for tag_id in req.tags:
            clauses.append('EXISTS (SELECT 1 FROM image_tag it WHERE it.image_id=i.id AND it.tag_id=?)'); params.append(tag_id)
        return clauses, params

    def search(self, req: SearchRequest):
        clauses, params = self._where(req)
        signature = hashlib.sha256(json.dumps(req.model_dump(exclude={'cursor', 'limit'}), sort_keys=True).encode()).hexdigest()[:16]
        name_expr = 'iib_raw_basename(i.path) COLLATE IIB_NATURAL'
        raw_name_expr = 'iib_raw_basename(i.path)'
        if req.sort in ('name', 'name_desc'):
            sort_expr = name_expr
            sort_order = 'DESC' if req.sort == 'name_desc' else 'ASC'
            tie_order = 'DESC' if req.sort == 'name_desc' else 'ASC'
            order_terms = [(sort_expr, sort_order), (raw_name_expr, tie_order), ('i.id', tie_order)]
        else:
            sort_expr = 'm.mtime_ns'
            sort_order = 'DESC' if req.sort == 'newest' else 'ASC'
            # Explorer's fallback for equal timestamps is a name comparison
            # and that fallback remains ascending when the date column is
            # descending. Raw name and image_id resolve case-only duplicates.
            order_terms = [(sort_expr, sort_order), (name_expr, 'ASC'), (raw_name_expr, 'ASC'), ('i.id', 'ASC')]

        def cursor_clause(values):
            pieces = []
            seek_params = []
            for index, (expression, direction) in enumerate(order_terms):
                prefix = ' AND '.join(f'{prior}=?' for prior, _ in order_terms[:index])
                operator = '<' if direction == 'DESC' else '>'
                pieces.append((prefix + ' AND ' if prefix else '') + f'{expression}{operator}?')
                seek_params.extend(values[:index])
                seek_params.append(values[index])
            return '(' + ' OR '.join(pieces) + ')', seek_params

        with self.connection() as conn:
            total = conn.execute('SELECT COUNT(*) FROM image i JOIN media_library m ON m.image_id=i.id AND m.image_path=i.path WHERE ' + ' AND '.join(clauses), params).fetchone()[0]
            if req.cursor:
                try:
                    values = json.loads(base64.urlsafe_b64decode(req.cursor + '=' * (-len(req.cursor) % 4)))
                    if req.sort in ('name', 'name_desc'):
                        if not isinstance(values, list) or len(values) != 3:
                            raise ValueError()
                        basename, image_id, sig = values
                        if sig != signature or not isinstance(basename, str) or not isinstance(image_id, int):
                            raise ValueError()
                        sort_key = name_key = basename
                    else:
                        if not isinstance(values, list) or len(values) != 4:
                            raise ValueError()
                        sort_key, basename, image_id, sig = values
                        if (sig != signature or not isinstance(sort_key, int)
                                or not isinstance(basename, str) or not isinstance(image_id, int)):
                            raise ValueError()
                        name_key = basename
                except Exception:
                    raise HTTPException(400, '検索位置が無効です。検索し直してください')
                seek_values = ([sort_key, name_key, image_id] if req.sort in ('name', 'name_desc')
                               else [sort_key, name_key, name_key, image_id])
                seek, seek_params = cursor_clause(seek_values)
                clauses.append(seek); params.extend(seek_params)
            rows = conn.execute(
                'SELECT i.id,m.width,m.height,m.kind,m.mtime_ns,COALESCE(r.rating,0) AS rating,'
                f'{sort_expr} AS sort_key,iib_raw_basename(i.path) AS basename_sort_key,'
                "EXISTS (SELECT 1 FROM image_tag lit JOIN tag lt ON lt.id=lit.tag_id "
                "WHERE lit.image_id=i.id AND lt.type='custom' AND lower(lt.name)='like') AS liked "
                'FROM image i JOIN media_library m ON m.image_id=i.id AND m.image_path=i.path '
                'LEFT JOIN media_library_rating r ON r.image_id=i.id AND r.image_path=i.path WHERE '
                + ' AND '.join(clauses)
                + ' ORDER BY ' + ','.join(f'{expression} {direction}' for expression, direction in order_terms) + ' LIMIT ?',
                [*params, req.limit + 1],
            ).fetchall()
        more = len(rows) > req.limit
        rows = rows[:req.limit]
        cursor = ''
        if more and rows:
            if req.sort in ('name', 'name_desc'):
                cursor_values = [rows[-1]['basename_sort_key'], rows[-1]['id'], signature]
            else:
                cursor_values = [rows[-1]['sort_key'], rows[-1]['basename_sort_key'], rows[-1]['id'], signature]
            cursor = base64.urlsafe_b64encode(json.dumps(cursor_values, ensure_ascii=False).encode()).decode().rstrip('=')
        items = []
        for row in rows:
            item = dict(row)
            item.pop('sort_key', None)
            item.pop('basename_sort_key', None)
            item['liked'] = bool(item.get('liked'))
            items.append(item)
        return {'items': items, 'total': total, 'cursor': cursor, 'index': self.status()}

    def facets(self):
        with self.connection() as conn:
            rows = conn.execute("SELECT id,name,type,count FROM tag WHERE type IN ('custom','lora','Model','Source Identifier') AND (count>0 OR type='custom') ORDER BY count DESC,name LIMIT 4000").fetchall()
        return {'tags': [dict(r) for r in rows]}

    def detail(self, image_id: int):
        path, row = self.resolve_id(image_id)
        data = json.loads(row['metadata'])
        stat = os.stat(path)
        with self.connection() as conn:
            tags = conn.execute("SELECT t.id,t.name FROM tag t JOIN image_tag it ON it.tag_id=t.id WHERE it.image_id=? AND t.type='custom' ORDER BY t.name", (image_id,)).fetchall()
            context = conn.execute("SELECT instruction,character_name,context_summary,search_terms,source,updated_at FROM media_library_chat_context WHERE path=?", (path,)).fetchone()
            rating_row = conn.execute("SELECT rating FROM media_library_rating WHERE image_id=? AND image_path=?", (image_id, path)).fetchone()
        rating = rating_row['rating'] if rating_row is not None else 0
        chat_context = None
        if context is not None:
            chat_context = dict(context)
            try:
                chat_context['search_terms'] = json.loads(chat_context['search_terms'])
            except (ValueError, TypeError):
                chat_context['search_terms'] = []
        return {**data, 'id': image_id, 'path': path, 'name': Path(path).name, 'kind': row['kind'],
                'bytes': stat.st_size, 'modified': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                'tags': [dict(t) for t in tags], 'rating': rating, 'chat_context': chat_context,
                'stale': stat.st_mtime_ns != row['mtime_ns']}

    def move_items(self, req: MoveItemsRequest):
        dest = self.checked_folder(req.dest)
        moved = []
        for image_id in dict.fromkeys(req.ids):
            source, _ = self.resolve_id(image_id)
            if os.path.normcase(os.path.dirname(source)) == os.path.normcase(dest):
                moved.append({'id': image_id, 'path': source})
                continue
            source_sidecar = str(Path(source).with_suffix('.txt'))
            has_sidecar = os.path.isfile(source_sidecar)
            target = self.unique_destination(dest, Path(source).name, has_sidecar)
            target_sidecar = str(Path(target).with_suffix('.txt'))
            try:
                shutil.move(source, target)
                if has_sidecar:
                    shutil.move(source_sidecar, target_sidecar)
                with self.write_lock, self.connection() as conn:
                    conn.execute('UPDATE image SET path=? WHERE id=? AND path=?', (target, image_id, source))
                    conn.execute('UPDATE media_library SET image_path=?,parser_rev=-1 WHERE image_id=?', (target, image_id))
                    conn.execute('UPDATE media_library_rating SET image_path=? WHERE image_id=?', (target, image_id))
                    conn.execute('UPDATE media_library_chat_context SET path=? WHERE path=?', (target, source))
                moved.append({'id': image_id, 'path': target})
            except Exception:
                # Best-effort filesystem rollback keeps the database and image together.
                try:
                    if os.path.exists(target) and not os.path.exists(source):
                        shutil.move(target, source)
                    if has_sidecar and os.path.exists(target_sidecar) and not os.path.exists(source_sidecar):
                        shutil.move(target_sidecar, source_sidecar)
                except OSError:
                    pass
                raise
        if moved:
            self.start_scan()
        return {'ok': True, 'moved': moved}

    def tag_membership(self, req: TagMembershipRequest):
        ids = list(dict.fromkeys(req.ids))
        for image_id in ids:
            self.resolve_id(image_id)
        placeholders = ','.join('?' for _ in ids)
        with self.connection() as conn:
            rows = conn.execute(
                f'''SELECT t.id,t.name,COUNT(it.image_id) AS applied
                    FROM tag t
                    LEFT JOIN image_tag it
                      ON it.tag_id=t.id AND it.image_id IN ({placeholders})
                    WHERE t.type='custom'
                    GROUP BY t.id,t.name
                    ORDER BY lower(t.name),t.id''',
                ids,
            ).fetchall()
        return {'total': len(ids), 'tags': [dict(row) for row in rows]}

    def set_rating(self, req: RatingRequest):
        targets = []
        for image_id in dict.fromkeys(req.ids):
            path, _ = self.resolve_id(image_id)
            targets.append((image_id, path))
        with self.write_lock, self.connection() as conn:
            for image_id, path in targets:
                if req.rating == 0:
                    conn.execute('DELETE FROM media_library_rating WHERE image_id=? AND image_path=?', (image_id, path))
                else:
                    conn.execute('''INSERT INTO media_library_rating(image_id,image_path,rating,updated_at)
                                    VALUES (?,?,?,CURRENT_TIMESTAMP)
                                    ON CONFLICT(image_id) DO UPDATE SET image_path=excluded.image_path,
                                      rating=excluded.rating, updated_at=CURRENT_TIMESTAMP''',
                                 (image_id, path, req.rating))
        return {'ok': True, 'rating': req.rating, 'ids': [item[0] for item in targets]}

    def save_chat_context(self, req: ChatContextRequest):
        path = self.checked_path(req.image_path)
        terms = list(dict.fromkeys(x.strip() for x in req.search_terms if isinstance(x, str) and x.strip()))[:100]
        values = (path, req.instruction.strip(), req.character_name.strip(), req.context_summary.strip(),
                  json.dumps(terms, ensure_ascii=False), req.source.strip())
        if not values[1] or not values[5]:
            raise HTTPException(400, 'instruction/sourceは必須です')
        with self.write_lock, self.connection() as conn:
            conn.execute('''INSERT INTO media_library_chat_context(path,instruction,character_name,context_summary,search_terms,source,updated_at)
                            VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)
                            ON CONFLICT(path) DO UPDATE SET instruction=excluded.instruction, character_name=excluded.character_name,
                              context_summary=excluded.context_summary, search_terms=excluded.search_terms, source=excluded.source,
                              updated_at=CURRENT_TIMESTAMP''', values)
        return {'ok': True, 'path': path, 'source': values[5], 'search_terms': terms}

    def tag(self, req: TagRequest):
        name = req.name.strip()
        if not name or any(ord(ch) < 32 for ch in name):
            raise HTTPException(400, 'タグ名が無効です')
        for image_id in req.ids:
            self.resolve_id(image_id)
        with self.write_lock, self.connection() as conn:
            row = conn.execute("SELECT id FROM tag WHERE type='custom' AND name=?", (name,)).fetchone()
            tag_id = row[0] if row else conn.execute("INSERT INTO tag(name,score,type,count,color) VALUES (?,0,'custom',0,'')", (name,)).lastrowid
            for image_id in dict.fromkeys(req.ids):
                if req.action == 'add':
                    changed = conn.execute('INSERT OR IGNORE INTO image_tag(image_id,tag_id,created_at) VALUES (?,?,CURRENT_TIMESTAMP)', (image_id, tag_id)).rowcount
                    conn.execute('UPDATE tag SET count=count+? WHERE id=?', (changed, tag_id))
                else:
                    changed = conn.execute('DELETE FROM image_tag WHERE image_id=? AND tag_id=?', (image_id, tag_id)).rowcount
                    conn.execute('UPDATE tag SET count=MAX(0,count-?) WHERE id=?', (changed, tag_id))
        return {'ok': True, 'tag_id': tag_id}

    def thumbnail(self, image_id: int, size: int):
        path, row = self.resolve_id(image_id)
        stat = os.stat(path)
        fingerprint = hashlib.sha256(f'{path}|{stat.st_mtime_ns}|{stat.st_size}|{size}'.encode()).hexdigest()
        destination = self.cache_root / fingerprint[:2] / (fingerprint + '.webp')
        with _THUMB_SLOTS, _THUMB_LOCKS[int(fingerprint[:4], 16) % len(_THUMB_LOCKS)]:
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix('.tmp')
                try:
                    if row['kind'] == 'video':
                        import av
                        with av.open(path) as container:
                            frame = next(container.decode(video=0))
                            image = frame.to_image()
                    else:
                        with Image.open(path) as original:
                            original.draft('RGB', (size, size))
                            image = ImageOps.exif_transpose(original).copy()
                    with image:
                        image.thumbnail((size, size))
                        image.save(temporary, format='WEBP', quality=85, method=1)
                    os.replace(temporary, destination)
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise HTTPException(422, 'プレビューを生成できません')
        return destination


def mount_media_library(app, base: str, verify_secret, write_permission_required, cli_roots: list[str], root_guard=None, root_enrollment_guard=None, roots_changed=None):
    # DataBase側の正規migrationを完了してから追加テーブルを作る。
    DataBase.get_conn()
    if roots_changed:
        roots_changed()
    library = MediaLibrary(DataBase.get_db_file_path(), cli_roots, root_guard, root_enrollment_guard, roots_changed)
    app.state.media_library = library
    router = APIRouter(prefix=base + '/library-api', dependencies=[Depends(verify_secret)])

    async def same_origin(request: Request):
        origin = request.headers.get('origin')
        if origin:
            from urllib.parse import urlsplit
            if urlsplit(origin).netloc.lower() != request.headers.get('host', '').lower():
                raise HTTPException(403, 'Cross-origin write denied')

    write_dependencies = [Depends(write_permission_required), Depends(same_origin)]

    @router.get('/config')
    def config():
        return {'roots': library.roots(), 'index': library.status(), 'writeable': os.getenv('IIB_ACCESS_CONTROL_PERMISSION', 'read-write') in ('read-write', 'write-only')}

    @router.get('/status')
    def status():
        return library.status()

    @router.post('/index', dependencies=write_dependencies)
    def start_index():
        return library.start_scan()

    @router.post('/roots', dependencies=write_dependencies)
    def roots(req: RootRequest):
        result = library.change_root(req)
        library.start_scan()
        return {'roots': result}

    @router.get('/folders')
    def folders(path: str = ''):
        if not path:
            return {'folders': library.roots()}
        parent = canonical(path)
        if not any(inside(parent, r['path']) for r in library.roots()):
            raise HTTPException(403, '登録フォルダ外です')
        try:
            values = [{'path': entry.path, 'label': entry.name} for entry in os.scandir(parent)
                      if entry.is_dir(follow_symlinks=False) and not entry.name.startswith('.') and inside(entry.path, parent)]
        except OSError:
            raise HTTPException(404, 'フォルダが見つかりません')
        return {'folders': sorted(values, key=lambda x: x['label'].casefold())[:1000]}

    @router.post('/search')
    def search(req: SearchRequest):
        return library.search(req)

    @router.get('/facets')
    def facets():
        return library.facets()

    @router.get('/items/{image_id}')
    def detail(image_id: int):
        return library.detail(image_id)

    @router.get('/items/{image_id}/thumbnail')
    def thumbnail(image_id: int, size: int = 768):
        if size not in (384, 768, 1280):
            raise HTTPException(400, 'Invalid thumbnail size')
        return FileResponse(library.thumbnail(image_id, size), media_type='image/webp', headers={'Cache-Control': 'private, max-age=86400'})

    @router.get('/items/{image_id}/file')
    def original(image_id: int, download: bool = False):
        path, row = library.resolve_id(image_id)
        return FileResponse(path, filename=Path(path).name if download else None, headers={'Cache-Control': 'private, max-age=60'})

    @router.post('/tags', dependencies=write_dependencies)
    def tags(req: TagRequest):
        return library.tag(req)

    @router.post('/move-items', dependencies=write_dependencies)
    def move_items(req: MoveItemsRequest):
        return library.move_items(req)

    @router.post('/upload-image', dependencies=write_dependencies)
    async def upload_image(request: Request, dest: str, filename: str):
        dest = library.checked_folder(dest)
        safe_name = Path(filename.replace(chr(92), '/')).name.strip()
        if Path(safe_name).suffix.lower() not in IMAGE_EXTENSIONS:
            raise HTTPException(415, '画像ファイルだけ追加できます')
        target = library.unique_destination(dest, safe_name)
        temporary = os.path.join(dest, f'.{Path(target).name}.{uuid.uuid4().hex}.iib-upload')
        try:
            with open(temporary, 'xb') as handle:
                async for chunk in request.stream():
                    if chunk:
                        handle.write(chunk)
            os.replace(temporary, target)
        except Exception:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise
        library.start_scan()
        return {'ok': True, 'path': target, 'name': Path(target).name}

    @router.post('/tag-membership')
    def tag_membership(req: TagMembershipRequest):
        return library.tag_membership(req)

    @router.post('/ratings', dependencies=write_dependencies)
    def ratings(req: RatingRequest):
        return library.set_rating(req)

    @router.post('/chat-context', dependencies=write_dependencies)
    def chat_context(req: ChatContextRequest):
        return library.save_chat_context(req)

    desktop_tickets = {}
    desktop_ticket_lock = threading.Lock()

    @router.post('/desktop-ticket', dependencies=write_dependencies)
    def desktop_ticket(request: Request):
        if request.client.host not in ('127.0.0.1', '::1'):
            raise HTTPException(403, 'Desktop launch is loopback-only')
        if not os.getenv('IIB_SECRET_KEY'):
            raise HTTPException(503, 'Desktop authentication is unavailable')
        ticket = secrets.token_urlsafe(32)
        with desktop_ticket_lock:
            now = time.monotonic()
            for key, expiry in list(desktop_tickets.items()):
                if expiry < now: desktop_tickets.pop(key, None)
            if len(desktop_tickets) >= 32: desktop_tickets.pop(next(iter(desktop_tickets)))
            desktop_tickets[hashlib.sha256(ticket.encode()).hexdigest()] = now + 120
        return {'ticket': ticket}

    @router.post('/desktop-stop', dependencies=write_dependencies)
    def desktop_stop(request: Request):
        callback = getattr(app.state, 'media_library_desktop_stop', None)
        if request.client.host not in ('127.0.0.1', '::1') or callback is None:
            raise HTTPException(403, 'This process is not owned by the desktop launcher')
        library.cancel.set()
        callback()
        return {'stopping': True}

    @app.post(base + '/desktop-session', dependencies=[Depends(same_origin)])
    def desktop_session(req: DesktopSession, request: Request, response: Response):
        if request.client.host not in ('127.0.0.1', '::1'):
            raise HTTPException(403)
        with desktop_ticket_lock:
            expiry = desktop_tickets.pop(hashlib.sha256(req.ticket.encode()).hexdigest(), 0)
        secret = os.getenv('IIB_SECRET_KEY')
        if not secret or expiry < time.monotonic():
            raise HTTPException(401, '起動リンクの有効期限が切れました。run.batを開き直してください。')
        cookie = hashlib.sha256((secret + '_ciallo').encode()).hexdigest()
        response.set_cookie('IIB_S', cookie, httponly=True, secure=request.url.scheme == 'https', samesite='strict', path=base)
        return {'path': base + '/library'}

    @app.get(base + '/desktop')
    def desktop_page():
        content = (WEB_ROOT / 'desktop.html').read_text(encoding='utf-8').replace('__IIB_BASE__', base)
        return HTMLResponse(content, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'"})

    app.include_router(router)

    @app.get(base + '/library', dependencies=[Depends(verify_secret)])
    def media_library_page():
        page = (WEB_ROOT / 'index.html').read_text(encoding='utf-8').replace('__IIB_BASE__', base)
        return HTMLResponse(page, headers={'Cache-Control': 'no-store', 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'"})

    @app.get(base + '/library-ui/{filename}')
    def media_library_static(filename: str):
        if filename not in ('library.js', 'library.css', 'layout.js', 'rating-shortcuts.js', 'desktop.js'):
            raise HTTPException(404)
        return FileResponse(WEB_ROOT / filename, headers={'Cache-Control': 'no-cache'})
    return library

