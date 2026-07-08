"""
scripts/wordpress_client.py
----------------------------
Thin wrapper around the WordPress REST API for pushing generated content
(live scoring posts, weekly recaps) to https://zachcrockett.com.

Idempotent by slug: callers never track WordPress post IDs themselves —
upsert() looks a post/page up by its slug and updates it in place if found,
creates it otherwise. WordPress's own slug field is the source of truth,
so state stays in sync even if content is edited manually in WP admin.

Note: this host's ModSecurity WAF returns 406 for the default
`python-requests/x.x` User-Agent on some endpoints (e.g. GET /wp/v2/posts)
even with otherwise-valid auth — confirmed by hand before writing this
client. A custom User-Agent avoids it; don't remove the header below.

Usage:
    from scripts.wordpress_client import WordPressClient
    client = WordPressClient()
    client.upsert('posts', slug='2026-week-1-status',
                   title='2026 Week 1 Status', content='<p>...</p>')
"""

import os
import sys
import tomllib
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

_SECRETS_PATH = os.path.join(os.path.dirname(__file__), '..', 'secrets', 'wordpress.toml')


class WordPressError(Exception):
    pass


class WordPressClient:
    def __init__(self):
        cfg = self._load_config()
        self._base_url = cfg['url'].rstrip('/')
        self._session = requests.Session()
        self._session.auth = (cfg['username'], cfg['password'])
        self._session.headers.update({
            'User-Agent': 'CFB-Fantasy-App/1.0',
        })

    def _load_config(self):
        with open(_SECRETS_PATH, 'rb') as f:
            return tomllib.load(f)['wordpress_creds']

    def _request(self, method, path, **kwargs):
        url = f"{self._base_url}/wp-json/wp/v2/{path.lstrip('/')}"
        response = self._session.request(method, url, timeout=15, **kwargs)
        if not response.ok:
            raise WordPressError(
                f"WordPress API error {response.status_code} on {method} {url}: "
                f"{response.text[:300]}"
            )
        return response.json()

    def get_by_slug(self, post_type, slug):
        """Return the post/page dict for this slug, or None if it doesn't exist."""
        results = self._request('GET', post_type, params={
            'slug': slug,
            'status': 'any',
            'context': 'edit',
        })
        return results[0] if results else None

    def create(self, post_type, title, slug, content, status='publish'):
        """Create a new post/page. Returns the created resource dict."""
        return self._request('POST', post_type, json={
            'title': title,
            'slug': slug,
            'content': content,
            'status': status,
        })

    def update(self, post_type, post_id, content, title=None, status=None):
        """Update an existing post/page by id. Returns the updated resource dict."""
        payload = {'content': content}
        if title is not None:
            payload['title'] = title
        if status is not None:
            payload['status'] = status
        return self._request('POST', f'{post_type}/{post_id}', json=payload)

    def upsert(self, post_type, slug, title, content, status='publish'):
        """
        Create the post/page if no post with this slug exists yet, otherwise
        update it in place. This is the main entry point — callers never
        need to track WordPress post IDs themselves.
        """
        existing = self.get_by_slug(post_type, slug)
        if existing is None:
            return self.create(post_type, title, slug, content, status=status)
        return self.update(post_type, existing['id'], content, title=title, status=status)

    def delete(self, post_type, post_id, force=False):
        """Delete a post/page (moves to trash unless force=True)."""
        return self._request('DELETE', f'{post_type}/{post_id}', params={'force': force})
