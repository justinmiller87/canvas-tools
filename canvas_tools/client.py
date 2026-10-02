import os
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv

load_dotenv()


def _debug(msg):
    if os.environ.get("CANVAS_DEBUG"):
        print(f"[DEBUG] {msg}", file=sys.stderr)


class CanvasError(RuntimeError):
    pass


class CanvasClient:
    def __init__(self, base_url=None, token=None):
        self.base_url = (base_url or os.environ["CANVAS_API_URL"]).rstrip("/")
        self.token = token or os.environ["CANVAS_API_TOKEN"]
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    def _url(self, path):
        if path.startswith("http"):
            return path
        if not path.startswith("/api/"):
            path = "/api/v1/" + path.lstrip("/")
        return urljoin(self.base_url, path)

    def _request(self, method, path, **kwargs):
        url = self._url(path)
        _debug(f"{method} {url}")
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            _debug("rate limited, sleeping 2s...")
            time.sleep(2)
            resp = self.session.request(method, url, **kwargs)
        _debug(f"{method} {url} -> {resp.status_code}")
        if not resp.ok:
            raise CanvasError(f"{method} {url} -> {resp.status_code}: {resp.text[:500]}")
        return resp

    def get(self, path, params=None):
        """GET with automatic pagination. Returns a list of all items merged,
        or the raw dict if the response isn't a list."""
        results = None
        url = self._url(path)
        params = dict(params or {})
        while url:
            resp = self._request("GET", url, params=params)
            params = None  # only needed on first request; next link has them baked in
            data = resp.json()
            if isinstance(data, list):
                results = (results or []) + data
            else:
                return data
            url = resp.links.get("next", {}).get("url")
        return results if results is not None else []

    def post(self, path, json=None):
        return self._request("POST", path, json=json).json()

    def put(self, path, json=None):
        return self._request("PUT", path, json=json).json()

    def delete(self, path):
        return self._request("DELETE", path).json()

    def _is_canvas_host(self, url):
        netloc = urlparse(url).netloc
        return not netloc or netloc == urlparse(self.base_url).netloc

    def download_file(self, url, dest_path):
        """Stream a Canvas file-attachment URL straight to disk. Attachment
        URLs already carry their own signed `verifier` query param. Avoid
        sending the Canvas Authorization Bearer token to external storage/CDN hosts."""
        is_canvas = self._is_canvas_host(url)
        auth_desc = "Bearer token" if is_canvas else "none (pre-signed)"
        _debug(f"download_file: GET {url} [target={urlparse(url).netloc}, auth={auth_desc}]")
        getter = self.session.get if is_canvas else requests.get
        resp = getter(url, stream=True)
        _debug(f"download_file: -> {resp.status_code}")
        if not resp.ok:
            raise CanvasError(f"GET {url} -> {resp.status_code}: {resp.text[:500]}")
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)

    def upload_file(self, path, file_path):
        """Canvas's 3-step file upload handshake, scoped to whatever `path`
        is (e.g. a submission comment's `.../comments/files` endpoint):
        negotiate an upload target (returns `upload_url` + `upload_params`),
        stream the file to it, then follow the redirect Canvas returns to
        finalize. Returns the resulting file object (with `id`)."""
        _debug(f"upload_file step 1: negotiating target for {file_path} via {path}")
        negotiate = self.post(path, json={"name": os.path.basename(file_path), "size": os.path.getsize(file_path)})
        upload_url = negotiate["upload_url"]
        is_canvas = self._is_canvas_host(upload_url)
        auth_desc = "Bearer token" if is_canvas else "none (pre-signed S3)"
        _debug(f"upload_file step 2: POST {upload_url} [target={urlparse(upload_url).netloc}, auth={auth_desc}]")
        post_headers = {"Authorization": f"Bearer {self.token}"} if is_canvas else {}
        with open(file_path, "rb") as f:
            resp = requests.post(
                upload_url, data=negotiate.get("upload_params") or {}, files={"file": f}, headers=post_headers, allow_redirects=False
            )
        _debug(f"upload_file step 2: -> {resp.status_code}")
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers["Location"]
            is_canvas_loc = self._is_canvas_host(loc)
            loc_auth = "Bearer token" if is_canvas_loc else "none"
            _debug(f"upload_file step 3: redirect to {loc} [target={urlparse(loc).netloc}, auth={loc_auth}]")
            resp = self.session.get(loc) if is_canvas_loc else requests.get(loc)
            _debug(f"upload_file step 3: -> {resp.status_code}")
        if not resp.ok:
            raise CanvasError(f"upload {file_path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def graphql(self, query, variables=None):
        """Canvas's GraphQL endpoint. Needed for things the REST API doesn't
        expose at all — e.g. discussion checkpoint due dates, which only exist
        via the mutations the redesigned Discussions UI uses internally."""
        resp = self._request("POST", "/api/graphql", json={"query": query, "variables": variables or {}})
        data = resp.json()
        if data.get("errors"):
            raise CanvasError(f"GraphQL error: {data['errors']}")
        return data["data"]
