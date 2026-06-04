#!/usr/bin/env python3

import os
import re
import json
import time
import sqlite3
import hashlib
import requests
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import praw


# ====== CHANGE SUBREDDIT/LIMIT WHEN APPLICABLE OR IF APPLICABLE ======
SUBREDDIT = ""
LIMIT = 100

REDDIT_CLIENT_ID = os.environ["REDDIT_CLIENT_ID"]
REDDIT_CLIENT_SECRET = os.environ["REDDIT_CLIENT_SECRET"]
REDDIT_USERNAME = os.environ["REDDIT_USERNAME"]
REDDIT_PASSWORD = os.environ["REDDIT_PASSWORD"]
REDDIT_USER_AGENT = "personal-subreddit-archiver"
# ========================


ARCHIVE_ROOT = Path.home() / "RedditArchive"
DB_PATH = ARCHIVE_ROOT / "archive.sqlite"
MEDIA_ROOT = ARCHIVE_ROOT / "media" / SUBREDDIT
JSON_ROOT = ARCHIVE_ROOT / "json" / SUBREDDIT


def safe_name(text, max_len=80):
    text = re.sub(r"[^\w\-. ]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len] or "untitled"


def init_db():
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    JSON_ROOT.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            post_id TEXT PRIMARY KEY,
            subreddit TEXT,
            title TEXT,
            author TEXT,
            created_utc INTEGER,
            permalink TEXT,
            url TEXT,
            archived_at INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS files (
            sha256 TEXT PRIMARY KEY,
            post_id TEXT,
            url TEXT,
            path TEXT,
            downloaded_at INTEGER
        )
    """)

    conn.commit()
    return conn


def already_archived(conn, post_id):
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM posts WHERE post_id = ?", (post_id,))
    return cur.fetchone() is not None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def file_hash_exists(conn, file_hash):
    cur = conn.cursor()
    cur.execute("SELECT path FROM files WHERE sha256 = ?", (file_hash,))
    return cur.fetchone()


def record_file(conn, file_hash, post_id, url, path):
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO files
        (sha256, post_id, url, path, downloaded_at)
        VALUES (?, ?, ?, ?, ?)
    """, (file_hash, post_id, url, str(path), int(time.time())))
    conn.commit()


def record_post(conn, post):
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO posts
        (post_id, subreddit, title, author, created_utc, permalink, url, archived_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        post.id,
        str(post.subreddit),
        post.title,
        str(post.author) if post.author else None,
        int(post.created_utc),
        "https://www.reddit.com" + post.permalink,
        post.url,
        int(time.time())
    ))
    conn.commit()


def download_direct_file(conn, post_id, url, dest_dir):
    parsed = urlparse(url)
    ext = os.path.splitext(parsed.path)[1]

    if not ext:
        ext = ".bin"

    temp_path = dest_dir / f"temp_{int(time.time())}{ext}"

    try:
        with requests.get(url, stream=True, timeout=45) as r:
            r.raise_for_status()
            with open(temp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

        file_hash = sha256_file(temp_path)
        existing = file_hash_exists(conn, file_hash)

        if existing:
            print(f"    Duplicate skipped: {url}")
            temp_path.unlink(missing_ok=True)
            return

        final_path = dest_dir / f"{file_hash[:16]}{ext}"
        temp_path.rename(final_path)
        record_file(conn, file_hash, post_id, url, final_path)
        print(f"    Saved file: {final_path}")

    except Exception as e:
        print(f"    Direct download failed: {url} — {e}")
        temp_path.unlink(missing_ok=True)


def download_with_ytdlp(conn, post_id, url, dest_dir):
    before = set(dest_dir.glob("*"))

    cmd = [
        "yt-dlp",
        "--no-overwrites",
        "--restrict-filenames",
        "-o", str(dest_dir / "%(id)s_%(title).80s.%(ext)s"),
        url
    ]

    try:
        subprocess.run(cmd, check=False)
    except Exception as e:
        print(f"    yt-dlp failed: {url} — {e}")
        return

    after = set(dest_dir.glob("*"))
    new_files = after - before

    for path in new_files:
        if path.is_file():
            file_hash = sha256_file(path)
            existing = file_hash_exists(conn, file_hash)

            if existing:
                print(f"    Duplicate yt-dlp file skipped: {path.name}")
                path.unlink(missing_ok=True)
            else:
                record_file(conn, file_hash, post_id, url, path)
                print(f"    Saved media: {path}")


def extract_media_urls(post):
    urls = []

    if post.url:
        urls.append(post.url)

    if hasattr(post, "media_metadata") and post.media_metadata:
        for item in post.media_metadata.values():
            if "s" in item and "u" in item["s"]:
                urls.append(item["s"]["u"].replace("&amp;", "&"))

    media = getattr(post, "media", None)
    if media and "reddit_video" in media:
        rv = media["reddit_video"]
        if "fallback_url" in rv:
            urls.append(rv["fallback_url"])

    return list(dict.fromkeys(urls))


def archive_post(conn, post):
    post_dir_name = f"{post.id}_{safe_name(post.title)}"
    post_media_dir = MEDIA_ROOT / post_dir_name
    post_media_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nArchiving: {post.title}")

    post.comments.replace_more(limit=0)

    comments = []
    for c in post.comments.list():
        comments.append({
            "id": c.id,
            "author": str(c.author) if c.author else None,
            "body": c.body,
            "score": c.score,
            "created_utc": int(c.created_utc),
            "permalink": "https://www.reddit.com" + c.permalink
        })

    data = {
        "id": post.id,
        "subreddit": str(post.subreddit),
        "title": post.title,
        "author": str(post.author) if post.author else None,
        "selftext": post.selftext,
        "score": post.score,
        "upvote_ratio": post.upvote_ratio,
        "created_utc": int(post.created_utc),
        "permalink": "https://www.reddit.com" + post.permalink,
        "url": post.url,
        "is_self": post.is_self,
        "is_video": post.is_video,
        "media_urls": extract_media_urls(post),
        "comments": comments
    }

    json_path = JSON_ROOT / f"{post.id}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"  Saved text/comments: {json_path}")

    for url in data["media_urls"]:
        lower = url.lower()

        if any(lower.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov"]):
            download_direct_file(conn, post.id, url, post_media_dir)
        else:
            download_with_ytdlp(conn, post.id, url, post_media_dir)

    record_post(conn, post)


def main():
    conn = init_db()

    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        username=REDDIT_USERNAME,
        password=REDDIT_PASSWORD,
        user_agent=REDDIT_USER_AGENT,
    )

    subreddit = reddit.subreddit(SUBREDDIT)

    for post in subreddit.new(limit=LIMIT):
        if already_archived(conn, post.id):
            print(f"Skipping already archived post: {post.id}")
            continue

        try:
            archive_post(conn, post)
            time.sleep(2)
        except Exception as e:
            print(f"Failed on post {post.id}: {e}")

    conn.close()


if __name__ == "__main__":
    main()
