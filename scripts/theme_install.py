#!/usr/bin/env python3
"""Install or remove the Dragons Den Mainsail sidebar entry.

Mainsail reads two files we use from <config>/.theme/:
  navi.json   extra sidebar links (a JSON list)
  custom.css  extra styles
Both may already hold the user's own content, so this script merges:
our navi entry is added (or replaced, matched by href) and our CSS lives
between marker comments that are replaced on every install.

Usage:
  theme_install.py install   <theme_dir> <assets_dir>
  theme_install.py uninstall <theme_dir> <assets_dir>
"""
import json
import os
import sys

CSS_BEGIN = "/* >>> dragons_den (managed by dd-plugin install.sh) >>> */"
CSS_END = "/* <<< dragons_den <<< */"


class ThemeError(Exception):
    pass


def _read(path):
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def _load_navi(path):
    text = _read(path)
    if text is None or not text.strip():
        return []
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ThemeError("%s is not valid JSON (%s); fix it by hand, it was"
                         " left unchanged" % (path, e))
    if not isinstance(data, list):
        raise ThemeError("%s must contain a JSON list; it was left unchanged"
                         % (path,))
    return data


def merge_navi(path, entries):
    """Add entries to navi.json, replacing any with the same href."""
    data = _load_navi(path)
    hrefs = {e.get('href') for e in entries}
    data = [e for e in data
            if not (isinstance(e, dict) and e.get('href') in hrefs)]
    data.extend(entries)
    _write(path, json.dumps(data, indent=2) + "\n")


def remove_navi(path, entries):
    if not os.path.exists(path):
        return
    data = _load_navi(path)
    hrefs = {e.get('href') for e in entries}
    data = [e for e in data
            if not (isinstance(e, dict) and e.get('href') in hrefs)]
    if data:
        _write(path, json.dumps(data, indent=2) + "\n")
    else:
        os.remove(path)


def _strip_block(text):
    start = text.find(CSS_BEGIN)
    if start < 0:
        return text
    end = text.find(CSS_END, start)
    end = len(text) if end < 0 else end + len(CSS_END)
    return (text[:start].rstrip("\n") + "\n" + text[end:].lstrip("\n")) \
        .strip("\n")


def upsert_css(path, block):
    """Append our block to custom.css, replacing a previous copy."""
    text = _strip_block(_read(path) or "")
    parts = [text] if text.strip() else []
    parts.append("%s\n%s\n%s" % (CSS_BEGIN, block.strip("\n"), CSS_END))
    _write(path, "\n\n".join(parts) + "\n")


def remove_css(path):
    text = _read(path)
    if text is None:
        return
    text = _strip_block(text)
    if text.strip():
        _write(path, text + "\n")
    else:
        os.remove(path)


def install(theme_dir, assets_dir):
    os.makedirs(theme_dir, exist_ok=True)
    with open(os.path.join(assets_dir, "navi.json"), encoding='utf-8') as f:
        entries = json.load(f)
    merge_navi(os.path.join(theme_dir, "navi.json"), entries)
    upsert_css(os.path.join(theme_dir, "custom.css"),
               _read(os.path.join(assets_dir, "custom.css")))


def uninstall(theme_dir, assets_dir):
    with open(os.path.join(assets_dir, "navi.json"), encoding='utf-8') as f:
        entries = json.load(f)
    remove_navi(os.path.join(theme_dir, "navi.json"), entries)
    remove_css(os.path.join(theme_dir, "custom.css"))


def main(argv):
    if len(argv) != 4 or argv[1] not in ('install', 'uninstall'):
        sys.stderr.write(__doc__)
        return 2
    try:
        (install if argv[1] == 'install' else uninstall)(argv[2], argv[3])
    except ThemeError as e:
        sys.stderr.write("theme_install: %s\n" % (e,))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
