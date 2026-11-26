"""Content-addressed files, query history and favorite pins."""

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import asdict
from itertools import count
from pathlib import Path

from .google_query import cache_key as google_page_key
from .model import Item, Page, provider_page_key
from .providers import COMMONS, GOOGLE
from .topics import recurring_topics

MEDIA_TTL = 3 * 86400


class FolderNameConflict(ValueError):
    def __init__(self, folder_id, folder_name):
        super().__init__("A folder with that name already exists.")
        self.folder_id = folder_id
        self.folder_name = folder_name


def _file_stamp(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISREG(info.st_mode):
        return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino
    return None


class Store:
    def __init__(self, root: Path, budget=512 * 1024**2):
        self.files = root / "media"
        self.files.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.verified = {}
        self.db = sqlite3.connect(root / "index.sqlite3", check_same_thread=False, timeout=8)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS blobs(
                digest TEXT PRIMARY KEY, ext TEXT NOT NULL, size INTEGER NOT NULL, used REAL NOT NULL, lease REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS aliases(
                name TEXT PRIMARY KEY, digest TEXT NOT NULL REFERENCES blobs(digest) ON DELETE CASCADE);
            CREATE INDEX IF NOT EXISTS by_digest ON aliases(digest);
            CREATE INDEX IF NOT EXISTS by_last_use ON blobs(used);
            CREATE TABLE IF NOT EXISTS favorites(
                id TEXT PRIMARY KEY, info TEXT NOT NULL, words TEXT NOT NULL, full TEXT NOT NULL, poster TEXT NOT NULL, added REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS searches(
                id TEXT PRIMARY KEY, page TEXT NOT NULL, stamp REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS gif_topic_queries(
                query TEXT PRIMARY KEY, uses INTEGER NOT NULL, last_used REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS gif_search_covers(
                query TEXT PRIMARY KEY REFERENCES gif_topic_queries(query) ON DELETE CASCADE,
                info TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS gif_usage(
                id TEXT PRIMARY KEY, info TEXT NOT NULL, uses INTEGER NOT NULL, last_used INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS by_gif_usage ON gif_usage(uses DESC,last_used DESC,id);
            UPDATE favorites SET poster='gif-cover:' || id WHERE full LIKE 'gif:%' AND poster LIKE 'poster:%';
        """)
        self._ensure_folders()
        self._favorite_topics = {}
        self._query_topics = None
        self._session_covers = OrderedDict()
        self.budget = budget
        self.next_age_sweep = 0
        # Recover interrupted temporary writes and content writes before an index commit.
        known = {h + "." + e for h, e in self.db.execute("SELECT digest,ext FROM blobs")}
        for file in self.files.iterdir():
            owned = file.name.startswith("pending-") or re.fullmatch(
                r"[0-9a-f]{64}\.(gif|png)", file.name
            )
            if owned and file.is_file() and file.name not in known:
                file.unlink(missing_ok=True)
        self.prune()

    def _ensure_folders(self):
        # Preserve existing IDs and filing decisions. New IDs must never reuse a
        # deleted folder's identity, including while a confirmation dialog is open.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            schema = self.db.execute("SELECT sql FROM sqlite_master WHERE name='favorite_folders'").fetchone()
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(favorite_folders)")}
            legacy = bool(columns) and "kind" not in columns
            rebuild = legacy or (schema is not None and "AUTOINCREMENT" not in schema[0].upper())
            if rebuild:
                self.db.execute("ALTER TABLE favorite_filing RENAME TO old_filing")
                self.db.execute("ALTER TABLE favorite_folders RENAME TO old_folders")
            if legacy:
                self.db.execute("ALTER TABLE favorite_preferences RENAME TO old_folder_preferences")
            self.db.execute("""CREATE TABLE IF NOT EXISTS favorite_folders(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL CHECK(kind IN ('gif','clip')),
                name TEXT NOT NULL, name_key TEXT NOT NULL, topic TEXT,
                UNIQUE(kind,name_key), UNIQUE(kind,topic))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS favorite_filing(
                favorite_id TEXT PRIMARY KEY REFERENCES favorites(id) ON DELETE CASCADE,
                folder_id INTEGER REFERENCES favorite_folders(id) ON DELETE SET NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS favorite_preferences(
                kind TEXT PRIMARY KEY CHECK(kind IN ('gif','clip')), auto_categorize INTEGER NOT NULL)""")
            if rebuild:
                old_kind = "'gif'" if legacy else "kind"
                self.db.execute(f"""INSERT INTO favorite_folders(id,kind,name,name_key,topic)
                    SELECT id,{old_kind},name,name_key,topic FROM old_folders""")
                self.db.execute("INSERT INTO favorite_filing SELECT favorite_id,folder_id FROM old_filing")
                self.db.execute("DROP TABLE old_filing")
                self.db.execute("DROP TABLE old_folders")
            if legacy:
                self.db.execute("INSERT INTO favorite_preferences SELECT 'gif',auto_categorize FROM old_folder_preferences")
                self.db.execute("DROP TABLE old_folder_preferences")
            self.db.execute("CREATE INDEX IF NOT EXISTS by_folder ON favorite_filing(folder_id)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS favorite_folder_merges(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                source_id INTEGER NOT NULL UNIQUE, source_name TEXT NOT NULL, source_key TEXT NOT NULL,
                source_topic TEXT, destination_id INTEGER, destination_name TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS favorite_merge_members(
                merge_id INTEGER NOT NULL REFERENCES favorite_folder_merges(id) ON DELETE CASCADE,
                favorite_id TEXT NOT NULL REFERENCES favorites(id) ON DELETE CASCADE,
                PRIMARY KEY(merge_id, favorite_id))""")
            self.db.execute("CREATE INDEX IF NOT EXISTS by_merged_favorite ON favorite_merge_members(favorite_id)")

    @staticmethod
    def valid_name(digest, ext):
        return (
            ext in ("gif", "png")
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        )

    def intact(self, path, digest, size):
        stamp = _file_stamp(path)
        if not stamp or stamp[0] != size:
            return False
        if self.verified.get(digest) != stamp:
            with path.open("rb") as fp:
                if hashlib.file_digest(fp, "sha256").hexdigest() != digest:
                    return False
            self.verified[digest] = stamp
        return True

    def still_verified(self, path):
        """Fast playback check for a previously verified file. Never hashes on the UI thread."""
        if path.parent != self.files:
            return False
        try:
            stamp = _file_stamp(path)
        except OSError:
            return False
        return stamp is not None and self.verified.get(path.stem) == stamp

    def lookup(self, alias, lease=0, *, touch=True):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT b.digest,ext,size FROM aliases a JOIN blobs b ON b.digest=a.digest WHERE name=?",
                (alias,),
            ).fetchone()
            if not row:
                return None
            digest, ext, expected = row
            path = self.files / (digest + "." + ext)
            if not self.valid_name(digest, ext) or not self.intact(path, digest, expected):
                self.db.execute("DELETE FROM blobs WHERE digest=?", (digest,))
                self.verified.pop(digest, None)
                return None
            if touch or lease:
                now = time.time()
                self.db.execute(
                    "UPDATE blobs SET used=?,lease=max(lease,?) WHERE digest=?",
                    (now, now + lease if lease else 0, digest),
                )
            return path

    def put(self, alias, data, ext, lease=0):
        if ext not in ("gif", "png"):
            raise ValueError("Unsupported cache format")
        digest = hashlib.sha256(data).hexdigest()
        path = self.files / (digest + "." + ext)
        with self.lock:
            if not self.intact(path, digest, len(data)):
                with tempfile.NamedTemporaryFile(
                    dir=self.files, delete=False, prefix="pending-"
                ) as fp:
                    temp = Path(fp.name)
                    try:
                        fp.write(data)
                    except BaseException:
                        fp.close()
                        temp.unlink(missing_ok=True)
                        raise
                try:
                    os.replace(temp, path)
                finally:
                    temp.unlink(missing_ok=True)
                # Verify once on the worker before offering the path to a player.
                self.verified.pop(digest, None)
                if not self.intact(path, digest, len(data)):
                    raise OSError("The cached file failed verification after writing.")
            with self.db:
                now = time.time()
                self.db.execute(
                    """INSERT INTO blobs(digest,ext,size,used,lease) VALUES(?,?,?,?,?)
                                ON CONFLICT(digest) DO UPDATE SET used=excluded.used,lease=max(blobs.lease,excluded.lease)""",
                    (digest, ext, len(data), now, now + lease if lease else 0),
                )
                self.db.execute("INSERT OR REPLACE INTO aliases VALUES(?,?)", (alias, digest))
            self.prune(exempt=digest)
            return path

    def prune(self, exempt=""):
        with self.lock, self.db:
            now = time.time()
            cutoff = now - MEDIA_TTL
            total = self.db.execute("SELECT coalesce(sum(size),0) FROM blobs").fetchone()[0]
            # Writes always enforce the byte budget. With room available, time
            # the age sweep so a large set of old pins isn't scanned per poster.
            if exempt and total <= self.budget and time.monotonic() < self.next_age_sweep:
                return
            self.next_age_sweep = time.monotonic() + 3600
            if (
                total <= self.budget
                and not self.db.execute(
                    "SELECT 1 FROM blobs WHERE used<=? AND lease<=? LIMIT 1", (cutoff, now)
                ).fetchone()
            ):
                return
            rows = self.db.execute(
                """SELECT digest,ext,size,used FROM blobs WHERE lease<=? AND digest!=? AND digest NOT IN
                (SELECT a.digest FROM aliases a JOIN favorites f ON a.name=f.full OR a.name=f.poster
                 OR (f.full!='' AND a.name IN ('poster:' || substr(f.full, 5), 'frame0:' || substr(f.full, 5))))
                ORDER BY used""",
                (now, exempt),
            ).fetchall()
            for digest, ext, count, used in rows:
                if total <= self.budget and used > cutoff:
                    break
                try:
                    if self.valid_name(digest, ext):
                        (self.files / (digest + "." + ext)).unlink(missing_ok=True)
                    self.verified.pop(digest, None)
                except OSError:
                    continue
                self.db.execute("DELETE FROM blobs WHERE digest=?", (digest,))
                total -= count

    def usage(self):
        with self.lock:
            return self.db.execute("SELECT coalesce(sum(size),0) FROM blobs").fetchone()[0]

    def remember(self, item, words):
        with self.lock:
            topics = None
            with self.db:
                self.db.execute(
                    """INSERT INTO favorites(id,info,words,full,poster,added) VALUES(?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET info=excluded.info,
                       words=coalesce(nullif(excluded.words,''), favorites.words),
                       full=excluded.full, poster=excluded.poster, added=excluded.added""",
                    (
                        item.key, json.dumps(asdict(item)), words,
                        item.full_alias if not item.link_only else "",
                        item.poster_alias if not item.service_media else "", time.time(),
                    ),
                )
                if item.kind in ("gif", "clip") and self.auto_categorize_enabled(item.kind):
                    topics = self._read_topics(item.kind)
                    self._file_recurring_topics(topics[0], item.kind)
            self._favorite_topics.clear()
            if topics is not None:
                self._favorite_topics[item.kind] = topics

    def remove(self, key):
        with self.lock, self.db:
            self.db.execute("DELETE FROM favorites WHERE id=?", (key,))
            self._favorite_topics.clear()
        self.prune()

    def favorites(self, query="", *, kind=None, folder_id=None):
        return [item for item, _ in self.favorite_entries(query, kind=kind, folder_id=folder_id)]

    def favorite_entries(self, query="", *, kind=None, folder_id=None):
        terms = query.casefold().split()
        conditions, params = [], []
        if kind:
            conditions.append("coalesce(json_extract(f.info,'$.kind'), 'gif')=?")
            params.append(kind)
        if folder_id is not None:
            conditions.append("g.folder_id IS ?")
            params.append(folder_id or None)  # 0 selects the Unfiled view.
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.lock:
            rows = self.db.execute(
                "SELECT f.info,f.words,d.name FROM favorites f "
                "LEFT JOIN favorite_filing g ON f.id=g.favorite_id "
                "LEFT JOIN favorite_folders d ON d.id=g.folder_id" + where + " ORDER BY f.added DESC,f.rowid DESC", params
            ).fetchall()
        found = []
        for info, words, folder in rows:
            item = Item(**json.loads(info))
            text = f"{item.title} {item.source} {item.kind} {words}".casefold()
            if all(word in text for word in terms):
                found.append((item, folder))
        return found

    def favorite_ids(self):
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT id FROM favorites")}

    def favorite_words(self, key):
        with self.lock:
            row = self.db.execute("SELECT words FROM favorites WHERE id=?", (key,)).fetchone()
            return row[0] if row else ""

    def folders(self, kind="gif"):
        with self.lock:
            return self.db.execute("SELECT id,name FROM favorite_folders WHERE kind=? ORDER BY name_key",
                                   (kind,)).fetchall()

    @staticmethod
    def _folder_name(name):
        name = " ".join(name.split())
        if not name or len(name) > 64 or not name.isprintable():
            raise ValueError("Enter a folder name between 1 and 64 printable characters.")
        if name.casefold() in {"all folders", "unfiled"}:
            raise ValueError("That name is reserved for a folder view. Choose another name.")
        return name

    def _insert_folder(self, name, kind):
        name = self._folder_name(name)
        try:
            return self.db.execute(
                "INSERT INTO favorite_folders(kind,name,name_key) VALUES(?,?,?)", (kind, name, name.casefold())
            ).lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("A folder with that name already exists.") from None

    def create_folder(self, name, *, kind="gif", favorite=None):
        with self.lock, self.db:
            folder_id = self._insert_folder(name, kind)
            if favorite is not None:
                self._set_folder(favorite, folder_id)
            return folder_id

    def rename_folder(self, folder_id, name, *, kind="gif"):
        name = self._folder_name(name)
        with self.lock, self.db:
            try:
                changed = self.db.execute("UPDATE favorite_folders SET name=?,name_key=? WHERE kind=? AND id=?",
                                          (name, name.casefold(), kind, folder_id))
            except sqlite3.IntegrityError:
                existing = self.db.execute("SELECT id,name FROM favorite_folders WHERE kind=? AND name_key=?",
                                           (kind, name.casefold())).fetchone()
                if existing:
                    raise FolderNameConflict(*existing) from None
                raise
            if not changed.rowcount:
                raise ValueError("This folder no longer exists.")

    def merge_folders(self, source_id, destination_id, *, kind="gif", source_name=None, destination_name=None):
        """Journal exactly the original members, then move them and remove the source atomically."""
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            source = self.db.execute("SELECT name,name_key,topic FROM favorite_folders WHERE id=? AND kind=?",
                                     (source_id, kind)).fetchone()
            dest = self.db.execute("SELECT name FROM favorite_folders WHERE id=? AND kind=?",
                                   (destination_id, kind)).fetchone()
            if not source or not dest or source_id == destination_id:
                raise ValueError("Choose two existing folders in the same collection.")
            if ((source_name is not None and source_name != source[0]) or
                    (destination_name is not None and destination_name != dest[0])):
                raise ValueError("A folder changed while the confirmation was open. Choose the folders again.")
            merge_id = self.db.execute("""INSERT INTO favorite_folder_merges
                (kind,source_id,source_name,source_key,source_topic,destination_id,destination_name)
                VALUES(?,?,?,?,?,?,?)""", (kind, source_id, *source, destination_id, dest[0])).lastrowid
            self.db.execute("""INSERT INTO favorite_merge_members(merge_id,favorite_id)
                SELECT ?,favorite_id FROM favorite_filing WHERE folder_id=?""", (merge_id, source_id))
            self.db.execute("UPDATE favorite_filing SET folder_id=? WHERE folder_id=?", (destination_id, source_id))
            self.db.execute("DELETE FROM favorite_folders WHERE id=?", (source_id,))
            return destination_id

    def last_folder_merge(self, kind="gif"):
        with self.lock:
            return self.db.execute("""SELECT id,source_name,destination_name FROM favorite_folder_merges
                WHERE kind=? ORDER BY id DESC LIMIT 1""", (kind,)).fetchone()

    def undo_folder_merge(self, merge_id, *, kind="gif"):
        """Restore the latest source and its members, preserving subsequent explicit filing decisions."""
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            record = self.db.execute("""SELECT id,source_id,source_name,source_key,source_topic,destination_id
                FROM favorite_folder_merges WHERE kind=? ORDER BY id DESC LIMIT 1""", (kind,)).fetchone()
            if not record or record[0] != merge_id:
                raise ValueError("This is no longer the latest merge. Open Manage folders again.")
            _, source_id, name, name_key, topic, destination_id = record
            if self.db.execute("SELECT 1 FROM favorite_folders WHERE kind=? AND name_key=?",
                               (kind, name_key)).fetchone():
                raise ValueError(f"Rename the current “{name}” folder before undoing the merge.")
            # If the destination was deleted, auto-filing may since have adopted the same
            # subject elsewhere. Preserve that newer assignment, and restore this folder manually.
            if topic and self.db.execute("SELECT 1 FROM favorite_folders WHERE kind=? AND topic=?",
                                         (kind, topic)).fetchone():
                topic = None
            self.db.execute("INSERT INTO favorite_folders(id,kind,name,name_key,topic) VALUES(?,?,?,?,?)",
                            (source_id, kind, name, name_key, topic))
            self.db.execute("""UPDATE favorite_filing SET folder_id=? WHERE folder_id IS ? AND favorite_id IN
                (SELECT favorite_id FROM favorite_merge_members WHERE merge_id=?)""",
                            (source_id, destination_id, merge_id))
            self.db.execute("DELETE FROM favorite_folder_merges WHERE id=?", (merge_id,))
            return source_id

    def delete_folder(self, folder_id, *, kind="gif", if_empty_named=None):
        """An empty-folder confirmation applies only while its name and contents still match."""
        with self.lock, self.db:
            deleted = self.db.execute("""DELETE FROM favorite_folders WHERE kind=? AND id=?
                AND (? IS NULL OR (name=? AND NOT EXISTS (
                    SELECT 1 FROM favorite_filing WHERE folder_id=favorite_folders.id)))""",
                                      (kind, folder_id, if_empty_named, if_empty_named)).rowcount
            if not deleted:
                return False
            # Explicit deletion puts members in Unfiled; a later reuse of this ID is unrelated.
            self.db.execute("UPDATE favorite_folder_merges SET destination_id=NULL WHERE kind=? AND destination_id=?",
                            (kind, folder_id))
            return True

    def folder_for(self, key):
        with self.lock:
            row = self.db.execute("SELECT folder_id FROM favorite_filing WHERE favorite_id=?",
                                  (key,)).fetchone()
            return row[0] if row else None

    def _set_folder(self, key, folder_id):
        # An explicit move to Unfiled is a filing decision too. Auto-filing only
        # considers items without a row, so a later save cannot undo that choice.
        cursor = self.db.execute(
            """INSERT INTO favorite_filing(favorite_id,folder_id)
               SELECT id,:folder FROM favorites WHERE id=:key AND
                 coalesce(json_extract(info,'$.kind'),'gif') IN ('gif','clip') AND
                 (:folder IS NULL OR coalesce(json_extract(info,'$.kind'),'gif') =
                    (SELECT kind FROM favorite_folders WHERE id=:folder))
               ON CONFLICT(favorite_id) DO UPDATE SET folder_id=excluded.folder_id""",
            {"folder": folder_id, "key": key},
        )
        if not cursor.rowcount:
            raise ValueError("Choose a folder from this favorite’s collection.")
        # A later manual move (including out and back) takes precedence over any past merge.
        self.db.execute("DELETE FROM favorite_merge_members WHERE favorite_id=?", (key,))

    def move_favorite(self, key, folder_id, *, new_name=None, kind="gif"):
        """Move a favorite and return the named folder this move emptied, if any."""
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            source_id = self.folder_for(key)
            if new_name is not None:
                folder_id = self._insert_folder(new_name, kind)
            if source_id is not None and source_id == folder_id:
                return None
            self._set_folder(key, folder_id)
            if source_id is None:
                return None
            return self.db.execute("""SELECT id,name,kind FROM favorite_folders WHERE id=? AND NOT EXISTS
                (SELECT 1 FROM favorite_filing WHERE folder_id=favorite_folders.id)""", (source_id,)).fetchone()

    def auto_categorize_enabled(self, kind="gif"):
        with self.lock:
            row = self.db.execute("SELECT auto_categorize FROM favorite_preferences WHERE kind=?", (kind,)).fetchone()
            return bool(row and row[0])

    def set_auto_categorize(self, enabled, kind="gif"):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO favorite_preferences VALUES(?,?)", (kind, int(enabled)))
            return self._file_recurring_topics(self._saved_topics(kind)[0], kind) if enabled else 0

    def _read_topics(self, kind):
        return recurring_topics(self.db.execute(
            """SELECT id,json_extract(info,'$.title'),words,1 FROM favorites
               WHERE coalesce(json_extract(info,'$.kind'),'gif')=?""", (kind,)
        ))

    def _saved_topics(self, kind):
        if kind not in self._favorite_topics:
            self._favorite_topics[kind] = self._read_topics(kind)
        return self._favorite_topics[kind]

    def _folder_for_topic(self, topic, kind):
        row = self.db.execute("SELECT id FROM favorite_folders WHERE kind=? AND topic=?", (kind, topic)).fetchone()
        if row:
            return row[0]  # User renames also apply to future matches.
        # A source can itself be the destination of an earlier merge. Follow the chain
        # to a live folder so auto-filing continues to respect the user's grouping.
        merged = self.db.execute("""WITH RECURSIVE routes(merge_id,folder_id) AS (
            SELECT id,destination_id FROM favorite_folder_merges
            WHERE kind=? AND (source_topic=? OR (source_topic IS NULL AND source_key=?))
            UNION ALL SELECT r.merge_id,m.destination_id FROM routes r
            JOIN favorite_folder_merges m ON m.source_id=r.folder_id
        ) SELECT f.id FROM routes r JOIN favorite_folders f ON f.id=r.folder_id
          ORDER BY r.merge_id DESC LIMIT 1""", (kind, topic, topic.casefold())).fetchone()
        if merged:
            return merged[0]
        base = topic.title()
        for number in count(1):
            suffix = "" if number == 1 else f" ({number})"
            name = base[:64 - len(suffix)] + suffix
            # Adopt a same-named manual folder. If the name belongs to another
            # automatic topic, choose an available name without mixing the two.
            row = self.db.execute(
                """INSERT INTO favorite_folders(kind,name,name_key,topic) VALUES(?,?,?,?)
                   ON CONFLICT(kind,name_key) DO UPDATE SET topic=excluded.topic
                   WHERE favorite_folders.topic IS NULL RETURNING id""",
                (kind, name, name.casefold(), topic),
            ).fetchone()
            if row:
                return row[0]

    def _file_recurring_topics(self, assignments, kind):
        filed = {key for key, in self.db.execute("SELECT favorite_id FROM favorite_filing")}
        topic_folders = {}
        moved = []
        for key, topic in assignments.items():
            if key in filed:
                continue
            if topic not in topic_folders:
                topic_folders[topic] = self._folder_for_topic(topic, kind)
            moved.append((key, topic_folders[topic]))
        self.db.executemany("INSERT INTO favorite_filing(favorite_id,folder_id) VALUES(?,?)", moved)
        return len(moved)

    def record_gif_search(self, query):
        query = " ".join(query.casefold().split())
        if not query:
            return
        with self.lock, self.db:
            self.db.execute(
                """INSERT INTO gif_topic_queries VALUES(?,1,?) ON CONFLICT(query)
                   DO UPDATE SET uses=uses+1, last_used=excluded.last_used""", (query, time.time())
            )
            self.db.execute("""DELETE FROM gif_topic_queries WHERE query IN
                (SELECT query FROM gif_topic_queries ORDER BY last_used DESC LIMIT -1 OFFSET 128)""")
            self._query_topics = None

    def _search_topics(self):
        if self._query_topics is None:
            rows = self.db.execute("SELECT query,'',query,uses FROM gif_topic_queries")
            self._query_topics = recurring_topics(rows)
        return self._query_topics

    def search_categories(self):
        with self.lock:
            _, favorites = self._saved_topics("gif")
            _, searches = self._search_topics()
            counts = favorites + searches
            return [term.title() for term in sorted(counts, key=lambda term: (-counts[term], term))]

    def remember_search_cover(self, query, item):
        query = " ".join(query.casefold().split())
        with self.lock, self.db:
            self._session_covers.pop(query, None)
            if item.service_media:
                self._session_covers[query] = item
                if len(self._session_covers) > 128:
                    self._session_covers.popitem(last=False)
            else:
                self.db.execute("""INSERT OR REPLACE INTO gif_search_covers(query,info)
                    SELECT query,? FROM gif_topic_queries WHERE query=?""", (json.dumps(asdict(item)), query))

    def category_covers(self, titles):
        """Prefer a filed or matching favorite, then an example from the user's actual searches."""
        wanted = {name.casefold() for name in titles}
        covers, mentions = {}, {}

        def match_words(text, item):
            words = re.findall(r"[^\W_]+", text.casefold(), re.UNICODE)
            for i in range(len(words)):
                for size in range(1, min(3, len(words) - i) + 1):
                    term = " ".join(words[i:i + size])
                    if term in wanted:
                        mentions.setdefault(term, item)

        with self.lock:
            assigned, _ = self._saved_topics("gif")
            favorites = self.db.execute("""SELECT f.id,f.info,f.words,d.name FROM favorites f
                LEFT JOIN favorite_filing g ON f.id=g.favorite_id LEFT JOIN favorite_folders d ON d.id=g.folder_id
                WHERE coalesce(json_extract(f.info,'$.kind'),'gif')='gif' ORDER BY f.added DESC,f.rowid DESC""").fetchall()
            by_query, _ = self._search_topics()
            searches = self.db.execute("""SELECT q.query,c.info FROM gif_topic_queries q
                LEFT JOIN gif_search_covers c ON c.query=q.query ORDER BY q.last_used DESC""").fetchall()
            session = self._session_covers.copy()
        for key, info, words, folder in favorites:
            item = Item(**json.loads(info))
            covers.setdefault("favorites", item)
            for term in (assigned.get(key), (folder or "").casefold()):
                if term in wanted:
                    covers.setdefault(term, item)
            match_words(item.title, item)
            match_words(words, item)
        for query, info in searches:
            item = session.get(query) or (Item(**json.loads(info)) if info else None)
            if item:
                for term in (query, by_query.get(query)):
                    if term in wanted:
                        mentions.setdefault(term, item)
                match_words(query, item)
        # Older installs already hold result pages but did not store a cover per query.
        missing = (set(by_query.values()) & wanted) - (covers.keys() | mentions.keys())
        if missing:
            old_keys = {}
            for query, topic in by_query.items():
                if topic in missing:
                    for spelling in {query, query.title(), query.capitalize(), query.upper()}:
                        old_keys[google_page_key(spelling)] = topic
                        for provider, mode in ((GOOGLE, "api"), (GOOGLE, "public-v2"), (COMMONS, "direct")):
                            old_keys[provider_page_key(provider, mode, spelling)] = topic
            with self.lock:
                rows = self.db.execute("SELECT id,json_extract(page,'$.items[0]') FROM searches ORDER BY stamp DESC").fetchall()
            for key, info in rows:
                if info:
                    item = Item(**json.loads(info))
                    if item.kind == "gif":
                        if key in old_keys:
                            covers.setdefault(old_keys[key], item)
                        match_words(item.title, item)
        return mentions | covers

    def record_gif_use(self, item):
        if item.kind == "gif":
            with self.lock, self.db:
                self.db.execute("""INSERT INTO gif_usage(id,info,uses,last_used) VALUES(?,?,1,?)
                    ON CONFLICT(id) DO UPDATE SET uses=uses+1,info=excluded.info,last_used=excluded.last_used""",
                    (item.key, json.dumps(asdict(item)), time.time_ns()))

    def frequent_gifs(self, limit):
        with self.lock:
            rows = self.db.execute("SELECT info FROM gif_usage ORDER BY uses DESC,last_used DESC,id LIMIT ?",
                                   (limit,)).fetchall()
        return [Item(**json.loads(info)) for info, in rows]

    def read_page(self, key):
        with self.lock:
            row = self.db.execute("SELECT page,stamp FROM searches WHERE id=?", (key,)).fetchone()
        if row:
            return Page.unpack(json.loads(row[0])), time.time() - row[1]
        return None, None

    def save_page(self, key, page):
        with self.lock, self.db:
            now = time.time()
            self.db.execute(
                "INSERT OR REPLACE INTO searches VALUES(?,?,?)", (key, json.dumps(page.pack()), now)
            )
            self.db.execute("DELETE FROM searches WHERE stamp<?", (now - 30 * 86400,))
            self.db.execute(
                "DELETE FROM searches WHERE id IN (SELECT id FROM searches ORDER BY stamp DESC LIMIT -1 OFFSET 192)"
            )
