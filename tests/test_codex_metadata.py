"""Display-name import checks with synthetic Codex metadata only."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_stats.codex_metadata import project_name, read_project_metadata, read_session_titles
from agent_stats.core import Store


class SessionNameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.source = self.home / 'sessions' / '2026' / '11' / '08' / 'rollout.jsonl'
        self.source.parent.mkdir(parents=True)
        events = [
            {'type': 'session_meta', 'payload': {'id': 'session-a', 'cwd': '/synthetic'}},
            {'type': 'turn_context', 'payload': {'turn_id': 'turn-a', 'model': 'synthetic'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'total_token_usage': {'input_tokens': 20, 'output_tokens': 10, 'total_tokens': 30}}}},
        ]
        for row in events:
            row['timestamp'] = '2026-11-08T00:00:00Z'
        self.source.write_text(''.join(json.dumps(row) + '\n' for row in events), encoding='utf-8')
        self.store = Store(self.home / 'stats.sqlite3')
        self.addCleanup(self.store.db.close)

    def write_index(self, title):
        with (self.home / 'session_index.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'id': 'session-a', 'thread_name': title,
                                     'updated_at': '2026-11-08T00:00:00Z'}) + '\n')

    def state(self, filename='state_5.sqlite', with_name=True):
        db = sqlite3.connect(self.home / filename)
        self.addCleanup(db.close)
        db.execute('CREATE TABLE threads (id TEXT, title TEXT%s)' % (', name TEXT' if with_name else ''))
        return db

    def test_name_matches_app_and_never_imports_first_prompt(self):
        self.write_index('Old indexed name')
        db = self.state()
        db.execute('INSERT INTO threads VALUES(?,?,?)',
                   ('session-a', 'PRIVATE_FIRST_PROMPT', 'Renamed in Codex'))
        db.commit()
        self.store.import_paths([self.source])
        result = self.store.query()
        session = result['sessions'][0]
        self.assertEqual(session['title'], 'Renamed in Codex')
        self.assertEqual(session['native_id'], 'session-a')
        self.assertEqual(result['turns'][0]['session_title'], 'Renamed in Codex')
        self.assertEqual(self.store.detail(session['id'])['session']['title'], 'Renamed in Codex')
        self.assertNotIn(b'PRIVATE_FIRST_PROMPT', self.store.path.read_bytes())
        self.assertEqual(result['summary']['token_total'], 30)

    def test_index_only_rename_updates_without_usage_revision(self):
        self.write_index('Original name')
        self.store.import_paths([self.source])
        self.assertEqual(self.store.query()['sessions'][0]['title'], 'Original name')
        self.write_index('Updated name')
        report = self.store.import_paths([self.source])
        self.assertEqual(report['imported'], 1)
        self.assertEqual(self.store.query()['sessions'][0]['title'], 'Updated name')
        self.assertEqual(self.store.query()['summary']['token_total'], 30)
        self.assertEqual(self.store.import_paths([self.source])['skipped'], 1)

    def test_database_rename_in_wal_and_archived_source(self):
        db = self.state()
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('INSERT INTO threads VALUES(?,?,?)', ('session-a', 'PRIVATE_FIRST_PROMPT', 'Before'))
        db.commit()
        self.store.import_paths([self.source])
        archived = self.home / 'archived_sessions' / 'rollout.jsonl'
        archived.parent.mkdir()
        archived.write_bytes(self.source.read_bytes())
        db.execute('UPDATE threads SET name=?', ('After',))
        db.commit()
        self.store.import_paths([archived])
        result = self.store.query()
        self.assertEqual(result['sessions'][0]['title'], 'After')
        self.assertEqual(result['summary']['sessions'], 1)
        self.assertEqual(result['summary']['token_total'], 30)

    def test_missing_name_does_not_use_title_or_another_home(self):
        db = self.state()
        db.execute('INSERT INTO threads VALUES(?,?,?)', ('session-a', 'PRIVATE_FIRST_PROMPT', None))
        db.commit()
        self.store.import_paths([self.source])
        self.assertIsNone(self.store.query()['sessions'][0]['title'])
        self.write_index('Local name')
        other = self.home / 'export' / 'rollout.jsonl'
        other.parent.mkdir()
        other.write_bytes(self.source.read_bytes())
        self.store.import_paths([other], instance='other-device')
        other_session = next(s for s in self.store.query()['sessions'] if s['instance'] == 'other-device')
        self.assertIsNone(other_session['title'])

    def test_older_database_schema_falls_back_to_index(self):
        self.write_index('Indexed name')
        db = self.state(with_name=False)
        db.execute('INSERT INTO threads VALUES(?,?)', ('session-a', 'PRIVATE_FIRST_PROMPT'))
        db.commit()
        self.assertEqual(read_session_titles(self.home), ({'session-a': 'Indexed name'}, []))

    def test_corrupt_metadata_does_not_block_usage(self):
        (self.home / 'state_5.sqlite').write_bytes(b'broken')
        self.write_index('Indexed name')
        with (self.home / 'session_index.jsonl').open('a') as stream:
            stream.write('{broken\n')
        report = self.store.import_paths([self.source])
        self.assertEqual(report['errors'], [])
        self.assertEqual(len(report['warnings']), 2)
        self.assertEqual(self.store.query()['sessions'][0]['title'], 'Indexed name')
        self.assertEqual(self.store.query()['summary']['token_total'], 30)

    def test_newest_database_schema_wins(self):
        for version, name in ((5, 'Old name'), (10, 'Current name')):
            db = self.state('state_%d.sqlite' % version)
            db.execute('INSERT INTO threads VALUES(?,?,?)', ('session-a', 'PRIVATE_FIRST_PROMPT', name))
            db.commit()
        self.assertEqual(read_session_titles(self.home)[0]['session-a'], 'Current name')

    def test_project_name_is_read_from_codex_and_updates_after_rename(self):
        db = self.state()
        db.execute('CREATE TABLE projects (id TEXT, name TEXT)')
        db.execute('CREATE TABLE project_roots (project_id TEXT, position INTEGER, path TEXT)')
        db.executemany('INSERT INTO projects VALUES (?, ?)', [('one', '父项目'), ('two', '自定义名称')])
        db.executemany('INSERT INTO project_roots VALUES (?, ?, ?)', [
            ('one', 0, '/'), ('two', 0, '/synthetic'),
        ])
        db.commit()
        self.store.import_paths([self.source])
        result = self.store.query({'project': '/synthetic'})
        session, turn = result['sessions'][0], result['turns'][0]
        self.assertEqual(session['project'], '/synthetic')
        self.assertEqual(session['project_name'], '自定义名称')
        self.assertEqual(turn['project_name'], '自定义名称')
        self.assertEqual(self.store.detail(session['id'])['session']['project_name'], '自定义名称')
        db.execute('UPDATE projects SET name=? WHERE id=?', ('重命名后', 'two'))
        db.commit()
        self.assertEqual(self.store.import_paths([self.source])['imported'], 1)
        self.assertEqual(self.store.query()['sessions'][0]['project_name'], '重命名后')
        self.assertEqual(self.store.query()['summary']['token_total'], 30)
        self.assertEqual(self.store.import_paths([self.source])['skipped'], 1)

    def test_project_root_precedence_and_explicit_thread_assignment(self):
        db = self.state()
        db.execute('ALTER TABLE threads ADD COLUMN project_id TEXT')
        db.execute('CREATE TABLE projects (id TEXT, name TEXT)')
        db.execute('CREATE TABLE project_roots (project_id TEXT, position INTEGER, path TEXT)')
        db.executemany('INSERT INTO projects VALUES (?, ?)', [
            ('one', '父项目'), ('two', '子项目'), ('three', '另一个项目'),
        ])
        db.executemany('INSERT INTO project_roots VALUES (?, ?, ?)', [
            ('one', 0, '/synthetic'), ('two', 0, '/synthetic/sub'),
            ('three', 1, '/synthetic/sub'),
        ])
        db.execute('INSERT INTO threads VALUES (?, ?, ?, ?)', ('session-a', '', None, 'three'))
        db.commit()
        metadata = read_project_metadata(self.home)
        self.assertEqual(project_name('session-a', '/synthetic/sub/dir', metadata), '另一个项目')
        self.assertEqual(project_name('other', '/synthetic/sub/dir', metadata), '子项目')
        self.assertEqual(project_name('other', '/synthetic2', metadata), None)
        self.store.import_paths([self.source])
        self.assertEqual(self.store.query()['sessions'][0]['project_name'], '另一个项目')

    def test_linked_worktree_and_ambiguous_root_names(self):
        root = self.home / 'repository'
        worktree = self.home / 'worktree'
        gitdir = root / '.git' / 'worktrees' / 'branch'
        gitdir.mkdir(parents=True)
        worktree.mkdir()
        (worktree / '.git').write_text('gitdir: ' + str(gitdir) + '\n')
        (gitdir / 'commondir').write_text('../..\n')
        metadata = ({}, [(str(root), 0, '工作区名称')])
        self.assertEqual(project_name('other', str(worktree / 'subdir'), metadata), '工作区名称')
        ambiguous = ({}, [(str(root), 0, '甲'), (str(root), 0, '乙')])
        self.assertIsNone(project_name('other', str(root), ambiguous))
        self.assertIsNone(project_name('other', str(worktree), ambiguous))

    def test_project_metadata_is_source_scoped_and_optional(self):
        db = self.state()
        db.execute('CREATE TABLE projects (id TEXT, name TEXT)')
        db.execute('CREATE TABLE project_roots (project_id TEXT, position INTEGER, path TEXT)')
        db.execute('INSERT INTO projects VALUES (?, ?)', ('one', '本机项目'))
        db.execute('INSERT INTO project_roots VALUES (?, ?, ?)', ('one', 0, '/synthetic'))
        db.commit()
        self.store.import_paths([self.source])
        other = self.home / 'export' / 'rollout.jsonl'
        other.parent.mkdir()
        other.write_bytes(self.source.read_bytes())
        self.store.import_paths([other], instance='other-device')
        sessions = {s['instance']: s for s in self.store.query()['sessions']}
        self.assertEqual(sessions['local']['project_name'], '本机项目')
        self.assertIsNone(sessions['other-device']['project_name'])

    def test_rename_with_older_source_keeps_newer_usage(self):
        db = self.state()
        db.execute('CREATE TABLE projects (id TEXT, name TEXT)')
        db.execute('CREATE TABLE project_roots (project_id TEXT, position INTEGER, path TEXT)')
        db.execute('INSERT INTO projects VALUES (?, ?)', ('one', '原名称'))
        db.execute('INSERT INTO project_roots VALUES (?, ?, ?)', ('one', 0, '/synthetic'))
        db.commit()
        old = self.source.read_text()
        event = {'type': 'event_msg', 'timestamp': '2026-11-08T00:01:00Z',
                 'payload': {'type': 'token_count', 'info': {'total_token_usage': {
                     'input_tokens': 30, 'output_tokens': 10, 'total_tokens': 40}}}}
        with self.source.open('a') as stream:
            stream.write(json.dumps(event) + '\n')
        self.store.import_paths([self.source])
        self.source.write_text(old)
        db.execute('UPDATE projects SET name=? WHERE id=?', ('新名称', 'one'))
        db.commit()
        self.assertEqual(self.store.import_paths([self.source])['imported'], 1)
        result = self.store.query()
        self.assertEqual(result['sessions'][0]['project_name'], '新名称')
        self.assertEqual(result['summary']['token_total'], 40)


if __name__ == '__main__':
    unittest.main()
