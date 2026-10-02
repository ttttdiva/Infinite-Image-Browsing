"""画像優先UIのメタデータ・既存DB保護・認証・ページングを実DBで検証。"""
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch
import tempfile
import threading
import unittest

from fastapi import FastAPI, HTTPException, Request
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from scripts.iib.db.datamodel import DataBase
from scripts.iib.media_library import ChatContextRequest, MediaLibrary, MoveItemsRequest, RatingRequest, RootRequest, SearchRequest, TagMembershipRequest, TagRequest, mount_media_library
from scripts.iib.media_library_metadata import normalize_image



class ASGITestClient:
    """追加HTTPクライアント依存なしで実ASGI境界を検証する。"""
    def __init__(self, app):
        self.app=app
        class Cookies(dict):
            def set(self,k,v): self[k]=v
        self.cookies=Cookies()
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def get(self,path,headers=None): return self.request('GET',path,headers=headers)
    def post(self,path,json=None,headers=None): return self.request('POST',path,json,headers)
    def request(self,method,path,body=None,headers=None):
        import asyncio
        from types import SimpleNamespace
        raw=json.dumps(body).encode() if body is not None else b''
        hs={'host':'testserver',**(headers or {})}
        if body is not None: hs['content-type']='application/json'
        if self.cookies: hs['cookie']='; '.join(k+'='+v for k,v in self.cookies.items())
        scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':method,'scheme':'http','path':path,'raw_path':path.encode(),'query_string':b'','root_path':'','server':('testserver',80),'client':('127.0.0.1',1),'headers':[(k.lower().encode(),v.encode()) for k,v in hs.items()]}
        messages=[]
        async def call():
            async def receive(): return {'type':'http.request','body':raw,'more_body':False}
            async def send(message): messages.append(message)
            await self.app(scope,receive,send)
        asyncio.run(call())
        start=next(m for m in messages if m['type']=='http.response.start')
        return SimpleNamespace(status_code=start['status'],headers={k.decode().lower():v.decode() for k,v in start.get('headers',[])},text=b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body').decode())


def nai_file(path, name='violet coat', width=96, height=144):
    data = {'prompt': 'base sunset', 'uc': 'base negative', 'steps': 28, 'sampler': 'k_euler', 'seed': 123,
            'v4_prompt': {'caption': {'base_caption': 'base sunset', 'char_captions': [
                {'char_caption': name, 'centers': [{'x': 0.25, 'y': 0.5}]},
                {'char_caption': 'blue coat', 'centers': [{'x': 0.75, 'y': 0.5}]}]}, 'use_coords': True},
            'v4_negative_prompt': {'caption': {'base_caption': 'base negative', 'char_captions': [
                {'char_caption': 'wrong violet'}, {'char_caption': 'wrong blue'}]}}}
    info = PngInfo(); info.add_text('Software', 'NovelAI'); info.add_text('Source', 'NovelAI Diffusion V4.5 TESTHASH'); info.add_text('Comment', json.dumps(data))
    Image.new('RGB', (width, height), '#769ba9').save(path, pnginfo=info)


class MediaLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.images = self.root / 'images'; self.images.mkdir()
        self.previous_path, self.previous_local = DataBase.path, DataBase.local
        DataBase.path = str(self.root / 'iib.db'); DataBase.local = threading.local(); DataBase.get_conn()
        self.library = MediaLibrary(DataBase.path, [str(self.images)])

    def tearDown(self):
        if self.library._thread: self.library._thread.join(30)
        if hasattr(DataBase.local, 'conn'): DataBase.local.conn.close()
        DataBase.path, DataBase.local = self.previous_path, self.previous_local
        self.tmp.cleanup()

    def scan(self):
        self.library.start_scan(); self.library._thread.join(30)
        self.assertFalse(self.library.status()['running'])
        self.assertIsNone(self.library.status()['failure'])

    def test_novelai_model_and_character_pairs_are_preserved(self):
        p = self.images / 'a.png'; nai_file(p)
        with Image.open(p) as image: value = normalize_image(image, str(p))
        self.assertEqual(value['model'], 'NovelAI Diffusion V4.5 TESTHASH')
        self.assertEqual(value['positive'], 'base sunset')
        self.assertEqual(value['negative'], 'base negative')
        self.assertEqual(len(value['characters']), 2)
        self.assertEqual(value['characters'][0]['positive'], 'violet coat')
        self.assertEqual(value['characters'][0]['negative'], 'wrong violet')
        self.assertEqual(value['characters'][1]['negative'], 'wrong blue')
        self.assertEqual(value['characters'][0]['centers'], [{'x': 0.25, 'y': 0.5}])
        self.assertNotIn('blue coat', value['positive'])

    def test_character_text_and_source_model_are_searchable(self):
        nai_file(self.images / 'a.png', '星空の人物')
        self.scan()
        self.assertEqual(self.library.search(SearchRequest(q='星空'))['total'], 1)
        self.assertEqual(self.library.search(SearchRequest(q='星空の人物', source='NovelAI', model='V4.5'))['total'], 1)
        self.assertEqual(self.library.search(SearchRequest(q='unrelated'))['total'], 0)
        self.assertEqual(self.library.search(SearchRequest(source='ComfyUI'))['total'], 0)

    def test_chat_context_is_saved_shown_and_searchable_without_modifying_image(self):
        p = self.images / 'a.png'; nai_file(p); before = p.read_bytes(); self.scan()
        saved = self.library.save_chat_context(ChatContextRequest(
            image_path=str(p), instruction='kimono at dusk', character_name='meika hime',
            context_summary='採用した夕景版', search_terms=['kimono', '夕景'], source='ChatGPT'))
        self.assertTrue(saved['ok'])
        self.assertEqual(p.read_bytes(), before)
        self.assertEqual(self.library.search(SearchRequest(q='meika hime'))['total'], 1)
        self.assertEqual(self.library.search(SearchRequest(q='夕景'))['total'], 1)
        self.assertEqual(self.library.search(SearchRequest(source='ChatGPT'))['total'], 1)
        image_id = self.library.search(SearchRequest(q='kimono'))['items'][0]['id']
        context = self.library.detail(image_id)['chat_context']
        self.assertEqual(context['instruction'], 'kimono at dusk')
        self.assertEqual(context['character_name'], 'meika hime')
        self.assertEqual(context['search_terms'], ['kimono', '夕景'])

    def test_paging_has_no_duplicates_and_stops_at_end(self):
        for i in range(19): nai_file(self.images / f'{i}.png')
        self.scan(); ids=[]; cursor=''
        for _ in range(10):
            result=self.library.search(SearchRequest(limit=4,cursor=cursor))
            ids.extend(item['id'] for item in result['items']);cursor=result['cursor']
            if not cursor: break
        self.assertEqual(len(ids),19);self.assertEqual(len(set(ids)),19);self.assertEqual(cursor,'')

    def test_update_keeps_image_identity_custom_tags_and_original_bytes(self):
        p=self.images/'a.png';nai_file(p);original=hashlib.sha256(p.read_bytes()).hexdigest();self.scan()
        image_id=self.library.search(SearchRequest())['items'][0]['id']
        self.library.tag(TagRequest(ids=[image_id],name='採用候補'))
        self.scan()
        self.assertEqual(self.library.search(SearchRequest(q='採用候補'))['total'],1)
        self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),original)
        nai_file(p,'updated caption');self.scan()
        result=self.library.search(SearchRequest())['items'][0]
        self.assertEqual(result['id'],image_id)
        self.assertEqual(self.library.detail(image_id)['tags'][0]['name'],'採用候補')

    def test_removed_root_hides_without_deleting_files_or_tags(self):
        p=self.images/'a.png';nai_file(p);self.scan()
        image_id=self.library.search(SearchRequest())['items'][0]['id']
        self.library.tag(TagRequest(ids=[image_id],name='keep'))
        self.library.change_root(RootRequest(path=str(self.images),action='remove'))
        self.assertEqual(self.library.search(SearchRequest())['total'],0);self.assertTrue(p.exists())
        with self.assertRaises(HTTPException):self.library.detail(image_id)
        self.library.change_root(RootRequest(path=str(self.images),action='add'))
        self.assertEqual(self.library.detail(image_id)['tags'][0]['name'],'keep')


    def test_rating_is_persisted_returned_and_clearable(self):
        p=self.images/'a.png'; nai_file(p); self.scan()
        image_id=self.library.search(SearchRequest())['items'][0]['id']
        self.assertEqual(self.library.detail(image_id)['rating'],0)
        self.assertEqual(self.library.set_rating(RatingRequest(ids=[image_id],rating=4))['rating'],4)
        self.assertEqual(self.library.detail(image_id)['rating'],4)
        self.assertEqual(self.library.search(SearchRequest())['items'][0]['rating'],4)
        self.scan()
        self.assertEqual(self.library.detail(image_id)['rating'],4)
        self.library.set_rating(RatingRequest(ids=[image_id],rating=0))
        self.assertEqual(self.library.detail(image_id)['rating'],0)

    def test_rating_does_not_follow_reused_image_id_to_another_path(self):
        a=self.images/'a.png'; b=self.images/'b.png'
        nai_file(a,'alpha'); nai_file(b,'beta'); self.scan()
        with self.library.connection() as conn:
            aid=conn.execute('SELECT id FROM image WHERE path=?',(str(a),)).fetchone()[0]
            bid=conn.execute('SELECT id FROM image WHERE path=?',(str(b),)).fetchone()[0]
        self.library.set_rating(RatingRequest(ids=[aid],rating=5))
        with self.library.connection() as conn:
            conn.execute('UPDATE image SET path=? WHERE id=?',(str(self.images/'placeholder'),aid))
            conn.execute('UPDATE image SET path=? WHERE id=?',(str(a),bid))
            conn.execute('UPDATE image SET path=? WHERE id=?',(str(b),aid))
        self.scan()
        self.assertEqual(self.library.detail(bid)['rating'],0)

    def test_rating_request_range_is_validated(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError): RatingRequest(ids=[1],rating=11)
        with self.assertRaises(ValidationError): RatingRequest(ids=[1],rating=-1)

    def test_name_sort_like_and_tag_membership(self):
        for name in ['zeta.png', 'Alpha.png', 'middle.png']:
            nai_file(self.images / name, name)
        self.scan()
        by_name = self.library.search(SearchRequest(sort='name', limit=2))
        first_names = [self.library.detail(item['id'])['name'] for item in by_name['items']]
        self.assertEqual(first_names, ['Alpha.png', 'middle.png'])
        self.assertTrue(by_name['cursor'])
        second = self.library.search(SearchRequest(sort='name', limit=2, cursor=by_name['cursor']))
        self.assertEqual([self.library.detail(item['id'])['name'] for item in second['items']], ['zeta.png'])

        ids = [item['id'] for item in self.library.search(SearchRequest(sort='name'))['items']]
        self.library.tag(TagRequest(ids=[ids[0]], name='like'))
        self.library.tag(TagRequest(ids=[ids[0], ids[1]], name='mixed'))
        projected = self.library.search(SearchRequest(sort='name'))['items']
        self.assertTrue(projected[0]['liked'])
        self.assertFalse(projected[1]['liked'])
        membership = self.library.tag_membership(TagMembershipRequest(ids=ids[:2]))
        counts = {tag['name']: tag['applied'] for tag in membership['tags']}
        self.assertEqual(counts['like'], 1)
        self.assertEqual(counts['mixed'], 2)

    def test_updated_sort_uses_exact_mtime_and_natural_filename_ties(self):
        base = 1_700_000_000_000_000_000
        files = {
            'new.png': base + 900,
            'old.png': base + 100,
            'frame10.png': base + 500,
            'frame2.png': base + 500,
            'frame1.png': base + 500,
        }
        # Deliberately create the tied files in an order different from their
        # expected display order so an image-id tie-break cannot pass this test.
        for name in ['frame10.png', 'frame2.png', 'new.png', 'frame1.png', 'old.png']:
            nai_file(self.images / name, name)
            os.utime(self.images / name, ns=(files[name], files[name]))
        self.scan()

        def names(result):
            return [Path(self.library.detail(item['id'])['path']).name for item in result['items']]

        newest = self.library.search(SearchRequest(sort='newest', limit=20))
        self.assertEqual(names(newest), ['new.png', 'frame1.png', 'frame2.png', 'frame10.png', 'old.png'])
        self.assertEqual([item['mtime_ns'] for item in newest['items']], [files[n] for n in names(newest)])

        oldest = self.library.search(SearchRequest(sort='oldest', limit=20))
        self.assertEqual(names(oldest), ['old.png', 'frame1.png', 'frame2.png', 'frame10.png', 'new.png'])
        self.assertEqual([item['mtime_ns'] for item in oldest['items']], [files[n] for n in names(oldest)])

        descending_name = self.library.search(SearchRequest(sort='name_desc', limit=20))
        self.assertEqual(names(descending_name), ['old.png', 'new.png', 'frame10.png', 'frame2.png', 'frame1.png'])

        # Every cursor page must use the same composite key as the first page,
        # including the three files sharing exactly one nanosecond timestamp.
        for sort, expected in {
            'newest': ['new.png', 'frame1.png', 'frame2.png', 'frame10.png', 'old.png'],
            'oldest': ['old.png', 'frame1.png', 'frame2.png', 'frame10.png', 'new.png'],
            'name': ['frame1.png', 'frame2.png', 'frame10.png', 'new.png', 'old.png'],
            'name_desc': ['old.png', 'new.png', 'frame10.png', 'frame2.png', 'frame1.png'],
        }.items():
            cursor = ''
            actual = []
            for _ in range(10):
                page = self.library.search(SearchRequest(sort=sort, limit=1, cursor=cursor))
                actual.extend(names(page))
                cursor = page['cursor']
                if not cursor:
                    break
            self.assertEqual(actual, expected, sort)

    def test_name_cursor_stays_compact_for_long_unicode_filename(self):
        long_name = ('segment1-' * 18) + '終.png'
        nai_file(self.images / long_name, long_name)
        nai_file(self.images / 'short.png', 'short')
        self.scan()
        first = self.library.search(SearchRequest(sort='name', limit=1))
        self.assertLess(len(first['cursor']), 2048)
        second = self.library.search(SearchRequest(sort='name', limit=1, cursor=first['cursor']))
        self.assertEqual(len(second['items']), 1)

    def test_cursor_preserves_case_for_natural_name_ties_across_folders(self):
        stamp = 1_700_000_000_000_000_000
        entries = [('one', 'B.png'), ('two', 'b.PnG'), ('three', 'b.png')]
        for folder_name, name in entries:
            folder = self.images / folder_name
            folder.mkdir()
            path = folder / name
            nai_file(path, name)
            os.utime(path, ns=(stamp, stamp))
        self.scan()

        def names(result):
            return [Path(self.library.detail(item['id'])['path']).name for item in result['items']]

        expected = {
            'newest': ['B.png', 'b.PnG', 'b.png'],
            'oldest': ['B.png', 'b.PnG', 'b.png'],
            'name': ['B.png', 'b.PnG', 'b.png'],
            # Name descending reverses both the shell comparison tie and its
            # raw case-sensitive fallback.
            'name_desc': ['b.png', 'b.PnG', 'B.png'],
        }
        for sort, wanted in expected.items():
            cursor = ''
            actual = []
            for _ in range(5):
                page = self.library.search(SearchRequest(sort=sort, limit=1, cursor=cursor))
                actual.extend(names(page))
                cursor = page['cursor']
                if not cursor:
                    break
            self.assertEqual(actual, wanted, sort)

    def test_move_items_preserves_identity_tags_rating_and_chat_context(self):
        source = self.images / 'move-me.png'
        destination = self.images / 'destination'
        destination.mkdir()
        nai_file(source, 'move me')
        self.scan()
        image_id = self.library.search(SearchRequest())['items'][0]['id']
        self.library.tag(TagRequest(ids=[image_id], name='like'))
        self.library.set_rating(RatingRequest(ids=[image_id], rating=4))
        self.library.save_chat_context(ChatContextRequest(
            image_path=str(source), instruction='keep context', source='ChatGPT',
            context_summary='move-safe', search_terms=['moving']
        ))
        result = self.library.move_items(MoveItemsRequest(ids=[image_id], dest=str(destination)))
        moved_path = Path(result['moved'][0]['path'])
        self.assertFalse(source.exists())
        self.assertTrue(moved_path.exists())
        detail = self.library.detail(image_id)
        self.assertEqual(detail['id'], image_id)
        self.assertEqual(Path(detail['path']), moved_path)
        self.assertEqual(detail['rating'], 4)
        self.assertEqual([tag['name'] for tag in detail['tags']], ['like'])
        self.assertEqual(detail['chat_context']['instruction'], 'keep context')

    def test_thumbnail_preserves_aspect_and_does_not_modify_original(self):
        p=self.images/'a.png';nai_file(p,width=100,height=225);before=p.read_bytes();self.scan()
        image_id=self.library.search(SearchRequest())['items'][0]['id']
        with Image.open(self.library.thumbnail(image_id,384)) as thumb:self.assertAlmostEqual(thumb.width/thumb.height,100/225,places=3)
        self.assertEqual(p.read_bytes(),before)

    def test_path_escape_and_invalid_cursor_are_rejected(self):
        outside=self.root/'outside.png';nai_file(outside)
        with self.assertRaises(HTTPException):self.library.checked_path(str(outside))
        with self.assertRaises(HTTPException):self.library.search(SearchRequest(cursor='broken'))

    def test_api_requires_cookie_and_rejects_cross_origin_mutation(self):
        async def auth(request:Request):
            if request.cookies.get('IIB_S')!='test':raise HTTPException(401)
        async def write(): pass
        app=FastAPI();mount_media_library(app,'/iib',auth,write,[str(self.images)])
        with ASGITestClient(app) as client:
            self.assertEqual(client.get('/iib/library-api/config').status_code,401)
            self.assertEqual(client.get('/iib/library').status_code,401)
            client.cookies.set('IIB_S','test')
            self.assertEqual(client.get('/iib/library-api/config').status_code,200)
            page=client.get('/iib/library');self.assertEqual(page.status_code,200)
            self.assertIn('content-security-policy',page.headers)
            self.assertEqual(client.get('/iib/library-ui/rating-shortcuts.js').status_code,200)
            self.assertEqual(client.post('/iib/library-api/index',json={},headers={'Origin':'https://evil.example'}).status_code,403)

            rating=client.post('/iib/library-api/ratings',json={'ids':[1],'rating':4},headers={'Origin':'https://evil.example'})
            self.assertEqual(rating.status_code,403)
            self.assertEqual(client.get('/iib/library-ui/not-allowed.txt').status_code,404)


    def test_rating_api_respects_write_permission(self):
        async def auth(request: Request): pass
        async def readonly(): raise HTTPException(403, 'Read only')
        app=FastAPI(); mount_media_library(app,'/iib',auth,readonly,[str(self.images)])
        with ASGITestClient(app) as client:
            self.assertEqual(client.post('/iib/library-api/ratings',json={'ids':[1],'rating':4}).status_code,403)


    def test_legacy_index_rebuild_cannot_attach_metadata_to_a_different_file(self):
        a=self.images/'a.png'; b=self.images/'b.png'
        nai_file(a,'alpha caption');nai_file(b,'beta caption');self.scan()
        with self.library.connection() as conn:
            aid=conn.execute('SELECT id FROM image WHERE path=?',(str(a),)).fetchone()[0]
            bid=conn.execute('SELECT id FROM image WHERE path=?',(str(b),)).fetchone()[0]
            # 従来IIBのrebuild/moveがimage IDとpathの対応を変更した状態を再現。
            conn.execute('UPDATE image SET path=? WHERE id=?',(str(self.images/'placeholder'),aid))
            conn.execute('UPDATE image SET path=? WHERE id=?',(str(a),bid))
            conn.execute('UPDATE image SET path=? WHERE id=?',(str(b),aid))
        self.assertEqual(self.library.search(SearchRequest())['total'],0)
        self.scan()
        result=self.library.search(SearchRequest(q='alpha'))
        self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['id'],bid)
        self.assertEqual(self.library.detail(bid)['path'],str(a))

    def test_backup_includes_committed_wal_data(self):
        import sqlite3
        from scripts.iib import tool
        source=sqlite3.connect(self.root/'wal-source.db')
        try:
            source.execute('PRAGMA journal_mode=WAL')
            source.execute('PRAGMA wal_autocheckpoint=0')
            source.execute('CREATE TABLE proof(value TEXT)')
            source.execute('INSERT INTO proof VALUES (?)',('committed but not checkpointed',));source.commit()
            with patch.object(tool,'cwd',str(self.root)), patch.dict(os.environ,{'IIB_DB_FILE_BACKUP_MAX':'4'}):
                tool.backup_db_file(str(self.root/'wal-source.db'))
            files=list((self.root/'iib_db_backup').glob('iib.db_*'))
            self.assertEqual(len(files),1)
            with sqlite3.connect(files[0]) as backup:
                self.assertEqual(backup.execute('SELECT value FROM proof').fetchone()[0],'committed but not checkpointed')
        finally:
            source.close()

    def test_desktop_tickets_are_authenticated_single_use_and_do_not_stop_studio(self):
        async def auth(request: Request):
            if request.cookies.get('IIB_S') != 'test': raise HTTPException(401)
        async def write(): pass
        with patch.dict(os.environ, {'IIB_SECRET_KEY': 'desktop-test-secret'}):
            app=FastAPI();mount_media_library(app,'/iib',auth,write,[str(self.images)])
            with ASGITestClient(app) as client:
                self.assertEqual(client.post('/iib/library-api/desktop-ticket',json={}).status_code,401)
                client.cookies.set('IIB_S','test')
                issued=client.post('/iib/library-api/desktop-ticket',json={})
                self.assertEqual(issued.status_code,200)
                ticket=json.loads(issued.text)['ticket']
                session=client.post('/iib/desktop-session',json={'ticket':ticket})
                self.assertEqual(session.status_code,200)
                self.assertIn('HttpOnly',session.headers['set-cookie'])
                self.assertIn('SameSite=strict',session.headers['set-cookie'])
                self.assertNotIn('desktop-test-secret',session.text)
                self.assertEqual(client.post('/iib/desktop-session',json={'ticket':ticket}).status_code,401)
                self.assertEqual(client.post('/iib/library-api/desktop-stop',json={}).status_code,403)
                called=[];app.state.media_library_desktop_stop=lambda:called.append(True)
                self.assertEqual(client.post('/iib/library-api/desktop-stop',json={}).status_code,200)
                self.assertEqual(called,[True])


if __name__=='__main__': unittest.main()
