#!/usr/bin/env python3
"""a11yscan - a zero-dependency static accessibility checker for HTML.

    python a11yscan.py [PATH ...]

It parses HTML with the standard library only; nothing is rendered or executed.
It catches the common, machine-detectable WCAG problems - it is not a substitute
for testing with real assistive technology.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

__version__ = "1.0.0"

# rule id -> (default severity, title, hint)
RULES: Dict[str, Tuple[str, str, str]] = {
    "A11Y001": ("error", "Image without alt text", 'Add alt="..." (use alt="" for purely decorative images).'),
    "A11Y002": ("warning", "Suspicious alt text", "Describe what the image conveys, not its file name or type."),
    "A11Y003": ("error", "<html> has no lang attribute", 'Add lang="en" (or your language) to <html>.'),
    "A11Y004": ("error", "Page title missing or empty", "Add a short, unique <title> inside <head>."),
    "A11Y005": ("error", "Form control without a label",
                "Use <label for=ID>, wrap the control in <label>, or add aria-label."),
    "A11Y006": ("error", "Button without an accessible name", "Give the button text or an aria-label."),
    "A11Y007": ("error", "Link without an accessible name", "Give the link text, an img alt, or an aria-label."),
    "A11Y008": ("warning", "Generic link text", 'Replace "click here"/"read more" with text that makes sense alone.'),
    "A11Y009": ("warning", "Heading level skipped", "Do not jump levels (h2 -> h4); keep headings nested."),
    "A11Y010": ("warning", "Page has no <h1>", "Add one <h1> describing the page."),
    "A11Y011": ("info", "More than one <h1>", "Prefer a single <h1> per page."),
    "A11Y012": ("error", "Empty heading", "Remove the heading or give it text."),
    "A11Y013": ("error", "Duplicate id", "ids must be unique; labels and ARIA rely on them."),
    "A11Y014": ("warning", "Positive tabindex", "Use tabindex=0 or -1 and rely on DOM order instead."),
    "A11Y015": ("error", "iframe without a title", "Add title=\"...\" describing the embedded content."),
    "A11Y016": ("warning", "Viewport blocks zoom", "Remove user-scalable=no / maximum-scale=1."),
    "A11Y017": ("warning", "Data table without header cells", "Use <th> (with scope) for headers, or role=presentation for layout."),
    "A11Y018": ("warning", "Media autoplays", "Do not autoplay audio/video with sound; let users start it."),
    "A11Y019": ("warning", "Click handler on a non-interactive element",
                "Use a <button>/<a>, or add role and tabindex plus keyboard handlers."),
    "A11Y020": ("warning", "Moving content (marquee/blink)", "Remove it; users must be able to stop moving text."),
    "A11Y021": ("error", "Reference to a missing id", "Point aria-labelledby/for to an id that exists."),
    "A11Y022": ("warning", "Low colour contrast", "Raise contrast to at least 4.5:1 (3:1 for large text)."),
}

SEV_RANK = {"info": 0, "warning": 1, "error": 2}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr"}
TEXT_TAGS = {"a", "button", "label", "title", "h1", "h2", "h3", "h4", "h5", "h6", "th", "summary",
             "option", "legend", "caption"}
NON_INTERACTIVE = {"div", "span", "p", "li", "td", "section", "article", "img", "ul", "header",
                   "footer", "main", "h1", "h2", "h3", "h4", "h5", "h6"}
GENERIC_LINKS = {"click here", "here", "read more", "more", "learn more", "link", "this", "details",
                 "click", "this link"}
SKIP_INPUT = {"hidden"}
BUTTON_INPUT = {"submit", "button", "reset"}
IGNORE_RE = re.compile(r"a11yscan-ignore:\s*([A-Za-z0-9_, ]+)")

NAMED_COLORS = {
    "white": (255, 255, 255), "black": (0, 0, 0), "red": (255, 0, 0), "gray": (128, 128, 128),
    "grey": (128, 128, 128), "silver": (192, 192, 192), "yellow": (255, 255, 0), "blue": (0, 0, 255),
    "green": (0, 128, 0), "lime": (0, 255, 0), "orange": (255, 165, 0), "navy": (0, 0, 128),
    "purple": (128, 0, 128), "teal": (0, 128, 128), "maroon": (128, 0, 0),
}


@dataclass
class Finding:
    rule: str
    severity: str
    line: int
    message: str
    hint: str
    file: str = ""


def parse_color(value: str) -> Optional[Tuple[int, int, int]]:
    v = value.strip().lower()
    if v in NAMED_COLORS:
        return NAMED_COLORS[v]
    m = re.fullmatch(r"#([0-9a-f]{3})", v)
    if m:
        return tuple(int(c * 2, 16) for c in m.group(1))  # type: ignore[return-value]
    m = re.fullmatch(r"#([0-9a-f]{6})", v)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    m = re.fullmatch(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", v)
    if m:
        return tuple(min(255, int(x)) for x in m.groups())  # type: ignore[return-value]
    return None


def luminance(rgb: Tuple[int, int, int]) -> float:
    def chan(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: Tuple[int, int, int], b: Tuple[int, int, int]) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def parse_style(style: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in style.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


class Scanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.raw: List[Tuple[str, int, str, Optional[str]]] = []
        self.stack: List[dict] = []
        self.ids: Dict[str, int] = {}
        self.label_for: List[Tuple[int, str]] = []
        self.refs: List[Tuple[int, str, str]] = []
        self.controls: List[Tuple[int, str, dict, bool]] = []
        self.headings: List[Tuple[int, int]] = []
        self.saw_html = False
        self.saw_title = False
        self.svg_depth = 0
        self.pending_ignore: Set[str] = set()
        self.ignored: Dict[int, Set[str]] = {}

    # -- helpers ---------------------------------------------------------
    def add(self, rule: str, line: int, message: str, sev: Optional[str] = None) -> None:
        self.raw.append((rule, line, message, sev))

    def add_text(self, text: str) -> None:
        for e in self.stack:
            if e["tag"] in TEXT_TAGS:
                e["text"].append(text)

    # -- parser callbacks --------------------------------------------------
    def handle_comment(self, data: str) -> None:
        m = IGNORE_RE.search(data)
        if m:
            self.pending_ignore |= {r.strip().upper() for r in m.group(1).split(",") if r.strip()}

    def handle_data(self, data: str) -> None:
        if self.stack:
            self.add_text(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        self._start(tag, attrs)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._start(tag, attrs)
        if tag not in VOID:
            self._end(tag)

    def handle_endtag(self, tag: str) -> None:
        self._end(tag)

    # -- start tag ---------------------------------------------------------
    def _start(self, tag: str, attrs) -> None:
        line = self.getpos()[0]
        d = {k.lower(): (v or "") for k, v in attrs}
        if self.pending_ignore:
            self.ignored.setdefault(line, set()).update(self.pending_ignore)
            self.pending_ignore = set()
        self._global_checks(tag, d, line)

        if tag == "html":
            self.saw_html = True
            if not d.get("lang", "").strip():
                self.add("A11Y003", line, "<html> has no lang attribute")
        elif tag == "meta" and d.get("name", "").lower() == "viewport":
            content = d.get("content", "").lower().replace(" ", "")
            settings = dict(p.split("=", 1) for p in content.split(",") if "=" in p)
            if settings.get("user-scalable") in ("no", "0"):
                self.add("A11Y016", line, "Viewport sets user-scalable=no")
            try:
                if float(settings.get("maximum-scale", "10")) < 2:
                    self.add("A11Y016", line, f"Viewport sets maximum-scale={settings['maximum-scale']}")
            except ValueError:
                pass
        elif tag == "img":
            alt = d.get("alt")
            hidden = (d.get("role", "").lower() in ("presentation", "none")
                      or d.get("aria-hidden", "").lower() == "true")
            if alt is None and not hidden and not d.get("aria-label") and not d.get("aria-labelledby"):
                self.add("A11Y001", line, "<img> has no alt attribute")
            elif alt and (re.search(r"\.(png|jpe?g|gif|svg|webp|bmp)$", alt.strip(), re.I)
                          or alt.strip().lower() in ("image", "photo", "picture", "img", "graphic")):
                self.add("A11Y002", line, f'alt text "{alt.strip()}" does not describe the image')
            if alt or d.get("aria-label"):
                self.add_text(alt or d["aria-label"])
        elif tag == "input":
            kind = d.get("type", "text").lower()
            if kind in SKIP_INPUT:
                return
            if kind == "image":
                if not d.get("alt") and not d.get("aria-label"):
                    self.add("A11Y001", line, '<input type="image"> has no alt text')
            elif kind in BUTTON_INPUT:
                if kind == "button" and not (d.get("value") or d.get("aria-label") or d.get("title")):
                    self.add("A11Y006", line, '<input type="button"> has no value or label')
            else:
                self.controls.append((line, "input", d, any(e["tag"] == "label" for e in self.stack)))
        elif tag in ("select", "textarea"):
            self.controls.append((line, tag, d, any(e["tag"] == "label" for e in self.stack)))
        elif tag == "label" and d.get("for"):
            self.label_for.append((line, d["for"]))
        elif tag == "iframe":
            if not (d.get("title", "").strip() or d.get("aria-label", "").strip()):
                self.add("A11Y015", line, "<iframe> has no title")
        elif tag in ("video", "audio"):
            if "autoplay" in d and not (tag == "video" and "muted" in d):
                self.add("A11Y018", line, f"<{tag}> autoplays")
        elif tag in ("marquee", "blink"):
            self.add("A11Y020", line, f"<{tag}> creates moving content")
        elif tag == "svg":
            self.svg_depth += 1
        elif tag == "th":
            for e in reversed(self.stack):
                if e["tag"] == "table":
                    e["has_th"] = True
                    break
        elif len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
            self.headings.append((line, int(tag[1])))

        if tag not in VOID:
            self.stack.append({"tag": tag, "attrs": d, "line": line, "text": [],
                               "has_th": False, "svg": self.svg_depth > 0})

    def _global_checks(self, tag: str, d: dict, line: int) -> None:
        ident = d.get("id", "")
        if ident:
            if ident in self.ids:
                self.add("A11Y013", line, f"Duplicate id '{ident}' (first used on line {self.ids[ident]})")
            else:
                self.ids[ident] = line
        ti = d.get("tabindex")
        if ti is not None:
            try:
                if int(ti.strip()) > 0:
                    self.add("A11Y014", line, f"tabindex={ti.strip()} disrupts the natural tab order")
            except ValueError:
                pass
        for attr in ("aria-labelledby", "aria-describedby"):
            for ref in d.get(attr, "").split():
                self.refs.append((line, attr, ref))
        if "onclick" in d and tag in NON_INTERACTIVE and not ("role" in d and "tabindex" in d):
            self.add("A11Y019", line, f"<{tag}> has onclick but is not keyboard accessible")
        if "style" in d:
            style = parse_style(d["style"])
            fg = parse_color(style.get("color", ""))
            bg_raw = style.get("background-color") or (style.get("background", "").split() or [""])[0]
            bg = parse_color(bg_raw)
            if fg and bg:
                size_m = re.match(r"([\d.]+)px", style.get("font-size", ""))
                size = float(size_m.group(1)) if size_m else 16.0
                bold = style.get("font-weight", "") in ("bold", "600", "700", "800", "900")
                need = 3.0 if (size >= 24 or (size >= 18.66 and bold)) else 4.5
                ratio = contrast_ratio(fg, bg)
                if ratio < need:
                    self.add("A11Y022", line, f"Contrast {ratio:.2f}:1 is below {need}:1")

    # -- end tag -----------------------------------------------------------
    def _end(self, tag: str) -> None:
        if tag in VOID:
            return
        idx = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i]["tag"] == tag), None)
        if idx is None:
            return
        e = self.stack[idx]
        del self.stack[idx:]
        if tag == "svg":
            self.svg_depth = max(0, self.svg_depth - 1)
        self._close(e)

    def _close(self, e: dict) -> None:
        tag, d, line = e["tag"], e["attrs"], e["line"]
        text = " ".join("".join(e["text"]).split())
        named = bool(d.get("aria-label", "").strip() or d.get("title", "").strip()
                     or d.get("aria-labelledby", "").strip())
        if tag == "a" and "href" in d:
            if not text and not named:
                self.add("A11Y007", line, "Link has no text or accessible name")
            elif not d.get("aria-label") and text.lower().strip(".!:> ") in GENERIC_LINKS:
                self.add("A11Y008", line, f'Link text "{text}" is not descriptive')
        elif tag == "button":
            if not text and not named:
                self.add("A11Y006", line, "Button has no text or accessible name")
        elif len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
            if not text and not named:
                self.add("A11Y012", line, f"<{tag}> is empty")
        elif tag == "title" and not e["svg"]:
            self.saw_title = True
            if not text:
                self.add("A11Y004", line, "<title> is empty")
        elif tag == "table":
            if not e["has_th"] and d.get("role", "").lower() not in ("presentation", "none"):
                self.add("A11Y017", line, "Table has no <th> header cells")

    # -- end of document -----------------------------------------------------
    def finish(self) -> None:
        label_ids = {i for _, i in self.label_for}
        for line, attr, ref in self.refs:
            if ref not in self.ids:
                self.add("A11Y021", line, f"{attr} points to missing id '{ref}'")
        for line, ref in self.label_for:
            if ref not in self.ids:
                self.add("A11Y021", line, f"<label for='{ref}'> points to a missing id")
        for line, tag, d, inside in self.controls:
            ok = (inside or d.get("aria-label", "").strip() or d.get("aria-labelledby", "").strip()
                  or d.get("title", "").strip() or (d.get("id") and d["id"] in label_ids))
            if ok:
                continue
            if d.get("placeholder"):
                self.add("A11Y005", line, f"<{tag}> relies on placeholder text instead of a label", "warning")
            else:
                self.add("A11Y005", line, f"<{tag}> has no label")
        if self.saw_html and not self.saw_title:
            self.add("A11Y004", 1, "Page has no <title>")
        prev = 0
        for line, level in self.headings:
            if prev and level > prev + 1:
                self.add("A11Y009", line, f"Heading jumps from h{prev} to h{level}")
            prev = level
        if self.saw_html:
            h1s = [ln for ln, lv in self.headings if lv == 1]
            if not h1s:
                self.add("A11Y010", self.headings[0][0] if self.headings else 1, "Page has no <h1>")
            elif len(h1s) > 1:
                self.add("A11Y011", h1s[1], f"Page has {len(h1s)} <h1> elements")


def scan(html: str, filename: str = "<html>") -> List[Finding]:
    sc = Scanner()
    sc.feed(html)
    sc.close()
    sc.finish()
    out: List[Finding] = []
    for rule, line, message, sev in sc.raw:
        if rule in sc.ignored.get(line, ()):
            continue
        default_sev, _title, hint = RULES[rule]
        out.append(Finding(rule, sev or default_sev, line, message, hint, filename))
    return sorted(out, key=lambda f: (f.line, f.rule))


def find_html(path: Path) -> List[Path]:
    if not path.is_dir():
        return [path]
    return [p for p in sorted(path.rglob("*"))
            if p.is_file() and p.suffix.lower() in (".html", ".htm", ".xhtml")
            and ".git" not in p.parts and "node_modules" not in p.parts]


COLORS = {"error": "\033[31m", "warning": "\033[33m", "info": "\033[36m"}


def render_text(results: List[Tuple[str, List[Finding]]], color: bool) -> str:
    lines: List[str] = []
    counts = {"error": 0, "warning": 0, "info": 0}
    for name, findings in results:
        for f in findings:
            counts[f.severity] += 1
            tag = f.severity.upper().ljust(7)
            if color:
                tag = f"{COLORS[f.severity]}{tag}\033[0m"
            lines.append(f"{name}:{f.line}: {tag} {f.rule} {f.message}")
            lines.append(f"    fix: {f.hint}")
    if sum(counts.values()) == 0:
        lines.append(f"{len(results)} file(s) scanned: no issues found")
    else:
        lines.append(f"{len(results)} file(s) scanned: {counts['error']} error(s), "
                     f"{counts['warning']} warning(s), {counts['info']} info")
    return "\n".join(lines)


def render_github(results: List[Tuple[str, List[Finding]]]) -> str:
    level = {"error": "error", "warning": "warning", "info": "notice"}
    return "\n".join(f"::{level[f.severity]} file={n},line={f.line},title={f.rule}::{f.message}"
                     for n, fs in results for f in fs)


def render_json(results: List[Tuple[str, List[Finding]]]) -> str:
    summary = {"error": 0, "warning": 0, "info": 0}
    files = []
    for name, findings in results:
        for f in findings:
            summary[f.severity] += 1
        files.append({"file": name, "findings": [asdict(f) for f in findings]})
    return json.dumps({"version": __version__, "files": files, "summary": summary}, indent=2)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="a11yscan", description="Static accessibility checker for HTML.")
    p.add_argument("paths", nargs="*", help="HTML files or folders ('-' reads stdin). Default: current folder.")
    p.add_argument("-f", "--format", choices=("text", "json", "github"), default="text")
    p.add_argument("-i", "--ignore", action="append", default=[], metavar="RULE",
                   help="Rule id(s) to skip, comma separated. Repeatable.")
    p.add_argument("--fail-on", choices=("info", "warning", "error", "never"), default="error",
                   help="Lowest severity that makes the exit code 1 (default: error).")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--list-rules", action="store_true", help="Show every rule and exit.")
    p.add_argument("-V", "--version", action="version", version=f"a11yscan {__version__}")
    a = p.parse_args(argv)

    if a.list_rules:
        for rid, (sev, title, _h) in RULES.items():
            print(f"{rid}  {sev.ljust(7)}  {title}")
        return 0

    ignore = {r.strip().upper() for chunk in a.ignore for r in chunk.split(",") if r.strip()}
    files: List[str] = []
    for raw in a.paths or ["."]:
        if raw == "-":
            files.append("-")
            continue
        path = Path(raw)
        if not path.exists():
            print(f"a11yscan: no such file or folder: {raw}", file=sys.stderr)
            return 2
        files.extend(str(x) for x in find_html(path))
    if not files:
        print("a11yscan: no HTML files found", file=sys.stderr)
        return 2

    results: List[Tuple[str, List[Finding]]] = []
    for f in files:
        if f == "-":
            text, name = sys.stdin.read(), "<stdin>"
        else:
            text, name = Path(f).read_text(encoding="utf-8", errors="replace"), f
        results.append((name, [x for x in scan(text, name) if x.rule not in ignore]))

    color = (not a.no_color) and sys.stdout.isatty() and "NO_COLOR" not in os.environ
    if a.format == "json":
        print(render_json(results))
    elif a.format == "github":
        out = render_github(results)
        if out:
            print(out)
    else:
        print(render_text(results, color))

    if a.fail_on == "never":
        return 0
    worst = max((SEV_RANK[f.severity] for _, fs in results for f in fs), default=-1)
    return 1 if worst >= SEV_RANK[a.fail_on] else 0


if __name__ == "__main__":
    sys.exit(main())
