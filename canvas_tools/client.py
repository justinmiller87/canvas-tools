import os
import time
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

load_dotenv()


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
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            time.sleep(2)
            resp = self.session.request(method, url, **kwargs)
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

    def download_file(self, url, dest_path):
        """Stream a Canvas file-attachment URL straight to disk. Attachment
        URLs already carry their own signed `verifier` query param, so the
        session's Bearer token isn't required — but sending it too is
        harmless and keeps this on the same authenticated session/retry
        path as every other request."""
        resp = self.session.get(url, stream=True)
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
        negotiate = self.post(path, json={"name": os.path.basename(file_path), "size": os.path.getsize(file_path)})
        with open(file_path, "rb") as f:
            resp = self.session.post(
                negotiate["upload_url"], data=negotiate.get("upload_params") or {}, files={"file": f}, allow_redirects=False
            )
        if resp.status_code in (301, 302, 303, 307, 308):
            resp = self.session.get(resp.headers["Location"])
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
