"""MkDocs build hook for the teaching-ladder site.

Three jobs:

1. **README.md as directory index.** GitHub renders `README.md` automatically
   when you browse a folder, so we keep the filename. MkDocs by default would
   serve it at `/00-hello-audio/README/`; we rewrite the URL/dest so it lives
   at `/00-hello-audio/` instead. The top-level `docs/teaching/README.md` is
   served at `/`.

2. **Cross-link rewriting.** The source markdown links to sibling chapters
   with `[..](../04-vad-preroll/)` (folder-style, works on GitHub). MkDocs
   needs an explicit file reference. We rewrite those at build time to
   `[..](../04-vad-preroll/README.md)`, which — combined with job 1 — lands
   at the correct `/04-vad-preroll/` URL.

3. **Source-file deep links.** Chapter prose links to sibling Python files
   with `[..](./main.py#L83-L92)` so the line anchors work on GitHub. MkDocs
   doesn't render `.py` files, so we rewrite those at build time to point at
   `repo_url`'s blob URL on the configured edit branch.

No source files are modified.
"""

from __future__ import annotations

import re
from pathlib import Path

_CROSS_LINK = re.compile(r"\((\.{1,2}(?:/\d{2}-[^/)]+)?)/\)")
_PY_DEEP_LINK = re.compile(r"\((?P<rel>(?:\./|\.\./)[^)\s]+?\.py(?:#L\d+(?:-L\d+)?)?)\)")
_EDIT_BRANCH = "main"


def on_files(files, config):
    site_dir = Path(config["site_dir"])
    for f in files:
        if not f.src_path.endswith("README.md"):
            continue
        if f.src_path == "README.md":
            f.url = ""
            f.dest_path = "index.html"
        else:
            parent = f.src_path.rsplit("/", 1)[0]
            f.url = f"{parent}/"
            f.dest_path = f"{parent}/index.html"
        f.abs_dest_path = str(site_dir / f.dest_path)
    return files


def _rewrite_py_links(markdown: str, page, config) -> str:
    """Rewrite ``[..](./main.py#L..)`` → absolute repo blob URL."""
    repo_url = (config.get("repo_url") or "").rstrip("/")
    if not repo_url or page.file.src_path == "README.md":
        return markdown
    docs_root = Path(config["docs_dir"]).resolve()
    repo_root = Path(config["config_file_path"]).resolve().parent
    page_dir = (docs_root / page.file.src_path).parent

    def _sub(m: re.Match[str]) -> str:
        rel = m.group("rel")
        path_part, _, hash_part = rel.partition("#")
        try:
            resolved = (page_dir / path_part).resolve()
            repo_rel = resolved.relative_to(repo_root)
        except ValueError:
            return m.group(0)
        url = f"{repo_url}/blob/{_EDIT_BRANCH}/{repo_rel.as_posix()}"
        if hash_part:
            url += f"#{hash_part}"
        return f"({url})"

    return _PY_DEEP_LINK.sub(_sub, markdown)


def on_page_markdown(markdown, page, config, **kwargs):
    markdown = _CROSS_LINK.sub(r"(\1/README.md)", markdown)
    markdown = _rewrite_py_links(markdown, page, config)
    return markdown
