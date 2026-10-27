"""智能阅读器 Pro — Flet 1.0 桌面阅读器（单文件版）。"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

# 必须在导入 pygame 之前设置
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import edge_tts
import flet as ft
import pygame
from charset_normalizer import from_path

APP_NAME = "智能阅读器 Pro"
MAX_BOOK_BYTES = 128 * 1024 * 1024
MAX_SEARCH_RESULTS = 100
SNIPPET_PADDING = 20

THEMES: dict[str, str] = {
    "羊皮纸": "#F6F0DF",
    "豆沙绿": "#CCE8CF",
    "纯白": "#FFFFFF",
    "夜间": "#1E1E1E",
}
DEFAULT_THEME = THEMES["羊皮纸"]

VOICES: dict[str, str] = {
    "晓晓（温柔女）": "zh-CN-XiaoxiaoNeural",
    "云希（阳光男）": "zh-CN-YunxiNeural",
    "云扬（新闻男）": "zh-CN-YunyangNeural",
    "晓伊（活泼女）": "zh-CN-XiaoyiNeural",
}
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

CHAPTER_PATTERN = re.compile(
    r"^[ \t\u3000]*"
    r"(第[0-9零一二三四五六七八九十百千万两]+[卷章节回]"
    r"(?:[ \t\u3000]*(?:[:：、.．·\-—][ \t\u3000]*)?"
    r"[^\n。！？!?，,；;]{0,40})?)"
    r"[ \t\u3000]*$",
    re.MULTILINE,
)


@dataclass
class Chapter:
    title: str
    body: str
    is_volume: bool = False
    _paragraphs: Optional[list[str]] = field(default=None, repr=False, compare=False)

    @property
    def paragraphs(self) -> list[str]:
        if self._paragraphs is None:
            self._paragraphs = list(iter_paragraphs(self.body))
        return self._paragraphs

    @property
    def first_paragraph(self) -> str:
        paras = self.paragraphs
        return paras[0] if paras else ""


@dataclass
class Book:
    path: str
    title: str
    last_chapter: int = 0
    scroll_offset: float = 0.0
    last_chapter_title: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Optional["Book"]:
        path = data.get("path")
        if not isinstance(path, str) or not path.strip(): return None
        title = data.get("title")
        if not isinstance(title, str) or not title.strip(): title = Path(path).stem
        try:
            last = int(data.get("last_chapter", 0))
        except:
            last = 0
        try:
            offset = float(data.get("scroll_offset", 0.0))
        except:
            offset = 0.0
        last_title = data.get("last_chapter_title")
        return cls(path=path, title=title.strip(), last_chapter=max(0, last), scroll_offset=max(0.0, offset),
                   last_chapter_title=last_title if isinstance(last_title, str) else "")

    def to_dict(self) -> dict:
        return {"path": self.path, "title": self.title, "last_chapter": self.last_chapter,
                "scroll_offset": self.scroll_offset, "last_chapter_title": self.last_chapter_title}


@dataclass
class Bookmark:
    book_path: str
    chapter_idx: int
    offset: float = 0.0
    para_idx: int = -1
    text: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Optional["Bookmark"]:
        path = data.get("book_path")
        if not isinstance(path, str) or not path: return None
        raw_idx = data.get("chapter_idx", data.get("chapter", -1))
        try:
            idx = int(raw_idx)
        except:
            return None
        if idx < 0: return None
        try:
            offset = float(data.get("offset", 0.0))
        except:
            offset = 0.0
        try:
            para = int(data.get("para_idx", -1))
        except:
            para = -1
        text = data.get("text")
        return cls(book_path=path, chapter_idx=idx, offset=max(0.0, offset), para_idx=para,
                   text=text if isinstance(text, str) else "")

    def to_dict(self) -> dict:
        return {"book_path": self.book_path, "chapter_idx": self.chapter_idx, "offset": self.offset,
                "para_idx": self.para_idx, "text": self.text}


@dataclass
class SearchHit:
    chapter_idx: int
    para_idx: int
    chapter_title: str
    snippet: str


def iter_paragraphs(text: str) -> Iterator[str]:
    for line in text.splitlines():
        line = line.strip()
        if line: yield line


def is_volume_title(title: str) -> bool:
    return "卷" in title and not any(k in title for k in "章节回")


def read_book_text(path: str) -> str:
    file_path = Path(path)
    if not file_path.is_file(): raise FileNotFoundError(f"文件不存在：{file_path}")
    size = file_path.stat().st_size
    if size == 0: raise ValueError("文件为空")
    if size > MAX_BOOK_BYTES: raise ValueError(
        f"文件过大（{size / 1024 / 1024:.0f} MB），上限为 {MAX_BOOK_BYTES // 1024 // 1024} MB")
    best = from_path(str(file_path)).best()
    if best is None: raise ValueError("无法识别文本编码")
    return str(best).lstrip("\ufeff")


def parse_book(path: str) -> list[Chapter]:
    text = read_book_text(path)
    matches = list(CHAPTER_PATTERN.finditer(text))
    chapters: list[Chapter] = []
    first_start = matches[0].start() if matches else len(text)
    preface = text[:first_start].strip()
    if preface: chapters.append(Chapter(title="前言", body=preface))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        title = match.group(1).strip()
        body = text[match.end():end].strip()
        chapters.append(Chapter(title=title, body=body, is_volume=is_volume_title(title)))
    if not chapters: chapters.append(Chapter(title="全文", body=text.strip()))
    return chapters


def search_chapters(chapters: list[Chapter], query: str, limit: int = MAX_SEARCH_RESULTS) -> list[SearchHit]:
    if not query: return []
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    hits: list[SearchHit] = []
    for chapter_idx, chapter in enumerate(chapters):
        for para_idx, paragraph in enumerate(iter_paragraphs(chapter.body)):
            match = pattern.search(paragraph)
            if match is None: continue
            start = max(0, match.start() - SNIPPET_PADDING)
            end = min(len(paragraph), match.end() + SNIPPET_PADDING)
            snippet = f"{'…' if start > 0 else ''}{paragraph[start:end]}{'…' if end < len(paragraph) else ''}"
            hits.append(SearchHit(chapter_idx, para_idx, chapter.title, snippet))
            if len(hits) >= limit: return hits
    return hits


def highlight_spans(text: str, pattern: re.Pattern) -> list[ft.TextSpan]:
    spans: list[ft.TextSpan] = []
    position = 0
    for match in pattern.finditer(text):
        if match.start() > position: spans.append(ft.TextSpan(text=text[position:match.start()]))
        spans.append(
            ft.TextSpan(text=match.group(0), style=ft.TextStyle(color=ft.Colors.RED, weight=ft.FontWeight.BOLD)))
        position = match.end()
    if position < len(text): spans.append(ft.TextSpan(text=text[position:]))
    return spans or [ft.TextSpan(text=text)]


def chapter_neighbor(chapters: list[Chapter], idx: int, step: int) -> Optional[int]:
    i = idx + step
    while 0 <= i < len(chapters):
        if not chapters[i].is_volume: return i
        i += step
    return None


def error_log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "IntelligentReader" / "error.log"


def log_exception(exc: BaseException) -> str:
    try:
        path = error_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {exc!r}\n")
            handle.write(traceback.format_exc())
        return str(path)
    except Exception:
        traceback.print_exc()
        return ""


class ReaderApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.prefs = ft.SharedPreferences()
        self.picker = ft.FilePicker()

        self.bookshelf: list[Book] = []
        self.bookmarks: list[Bookmark] = []
        self.book: Optional[Book] = None
        self.chapters: list[Chapter] = []
        self.current_index = 0
        self.scroll_offset = 0.0
        self.view = "shelf"

        self.font_size = 22
        self.bg_color = DEFAULT_THEME
        self.voice = DEFAULT_VOICE
        self.auto_restore = True
        self.is_immersive = False

        self._audio_ready = False
        self._mixer_lock = threading.Lock()
        self._tts_running = False
        self._tts_gen = 0
        self._tts_chapter = -1
        self._tts_para = 0
        self._tts_task = None

        self._auto_scroll_running = False
        self._auto_gen = 0
        self._auto_task = None

        self._col: Optional[ft.Column] = None
        self._paragraph_controls: list[ft.Text] = []
        self._highlight_idx = -1
        self._key_prefix = ""
        self._dialogs: list[ft.Control] = []
        self._dialog_cleanup: dict[int, object] = {}
        self._input_focused = False
        self._nav_busy = False
        self._load_token = 0
        self._search_token = 0
        self._search_open = False
        self._last_progress_save = 0.0
        self._max_scroll_extent = 0.0
        self._at_scroll_end = False

        self._dirty_bookshelf = False
        self._dirty_bookmarks = False
        self._dirty_settings = False
        self._save_task = None
        self._save_error_shown = False
        self._startup_warning = ""
        self._parse_cache: dict[str, tuple[float, list[Chapter]]] = {}

        self.main_area = ft.Container(expand=True)
        self.bookshelf_list = ft.ListView(expand=True, spacing=8, padding=20)
        self.reader_container = ft.Container(expand=True)

    async def start(self) -> None:
        await self._load_prefs()
        self.page.title = APP_NAME
        self.page.padding = 0
        self.page.theme_mode = ft.ThemeMode.DARK if self.bg_color == "#1E1E1E" else ft.ThemeMode.LIGHT
        self.page.on_keyboard_event = self._on_keyboard

        # 🌟 修复：移除复杂的 prevent_close 拦截，直接使用 Flet 原生关闭
        # 同时设置窗口关闭时停止音频的钩子，绝对不卡死
        self.page.on_disconnect = self._on_disconnect

        self.page.add(self.main_area)
        self.show_bookshelf()
        if self._startup_warning: self.notify(self._startup_warning, error=True)

    def _on_disconnect(self, e=None):
        try:
            pygame.mixer.music.stop()
            pygame.mixer.quit()
        except:
            pass

    async def _load_prefs(self) -> None:
        try:
            raw = await self.prefs.get("bookshelf")
            self.bookshelf = self._decode_books(raw)
            raw = await self.prefs.get("bookmarks")
            self.bookmarks = self._decode_bookmarks(raw)
            raw = await self.prefs.get("auto_restore")
            if isinstance(raw, str): self.auto_restore = raw.strip().lower() == "true"
            raw = await self.prefs.get("settings")
            self._apply_settings(raw)
        except Exception as exc:
            self._startup_warning = f"本地数据读取失败：{exc}"
            log_exception(exc)

    @staticmethod
    def _decode_json(raw) -> object:
        if raw is None: return None
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except:
                return None
        return raw

    def _decode_books(self, raw) -> list[Book]:
        data = self._decode_json(raw)
        if not isinstance(data, list): return []
        books: list[Book] = []
        seen: set[str] = set()
        for item in data:
            if not isinstance(item, dict): continue
            book = Book.from_dict(item)
            if book is None: continue
            key = os.path.normcase(book.path)
            if key in seen: continue
            seen.add(key)
            books.append(book)
        return books

    def _decode_bookmarks(self, raw) -> list[Bookmark]:
        data = self._decode_json(raw)
        if not isinstance(data, list): return []
        bookmarks: list[Bookmark] = []
        for item in data:
            if not isinstance(item, dict): continue
            bookmark = Bookmark.from_dict(item)
            if bookmark is not None: bookmarks.append(bookmark)
        return bookmarks

    def _apply_settings(self, raw) -> None:
        data = self._decode_json(raw)
        if not isinstance(data, dict): return
        try:
            size = int(data.get("font_size", self.font_size))
        except:
            size = self.font_size
        self.font_size = max(14, min(40, size))
        color = data.get("bg_color")
        if color in THEMES.values(): self.bg_color = color
        voice = data.get("voice")
        if voice in VOICES.values(): self.voice = voice

    def _settings_dict(self) -> dict:
        return {"font_size": self.font_size, "bg_color": self.bg_color, "voice": self.voice}

    def _has_dirty(self) -> bool:
        return self._dirty_bookshelf or self._dirty_bookmarks or self._dirty_settings

    def _mark_dirty(self, kind: str) -> None:
        setattr(self, f"_dirty_{kind}", True)
        if self._save_task is None or self._save_task.done():
            self._save_task = self.page.run_task(self._persist_loop)

    async def _persist_loop(self) -> None:
        try:
            while self._has_dirty():
                if self._dirty_bookshelf:
                    self._dirty_bookshelf = False
                    payload = json.dumps([b.to_dict() for b in self.bookshelf], ensure_ascii=False)
                    try:
                        await self.prefs.set("bookshelf", payload)
                    except:
                        self._dirty_bookshelf = True; raise
                if self._dirty_bookmarks:
                    self._dirty_bookmarks = False
                    payload = json.dumps([b.to_dict() for b in self.bookmarks], ensure_ascii=False)
                    try:
                        await self.prefs.set("bookmarks", payload)
                    except:
                        self._dirty_bookmarks = True; raise
                if self._dirty_settings:
                    self._dirty_settings = False
                    try:
                        await self.prefs.set("settings", json.dumps(self._settings_dict(), ensure_ascii=False))
                    except:
                        self._dirty_settings = True; raise

                await asyncio.sleep(0.1)
        except Exception as exc:
            log_exception(exc)
            if not self._save_error_shown:
                self._save_error_shown = True
                self.notify("本地数据保存失败，请检查存储权限", error=True)

    async def _save_auto_restore(self) -> None:
        try:
            await self.prefs.set("auto_restore", "True" if self.auto_restore else "False")
        except:
            pass

    @property
    def text_color(self) -> str:
        return ft.Colors.WHITE if self.bg_color == "#1E1E1E" else ft.Colors.BLACK

    def _show_dialog(self, dialog: ft.Control, cleanup=None) -> None:
        if cleanup is not None: self._dialog_cleanup[id(dialog)] = cleanup

        def on_dismiss(_e): self._forget_dialog(dialog)

        dialog.on_dismiss = on_dismiss
        self._dialogs.append(dialog)
        self.page.show_dialog(dialog)

    def _forget_dialog(self, dialog: ft.Control) -> None:
        self._dialogs = [d for d in self._dialogs if d is not dialog]
        cleanup = self._dialog_cleanup.pop(id(dialog), None)
        if cleanup is not None: cleanup()

    def _close_dialog(self, dialog: ft.Control) -> None:
        self._forget_dialog(dialog)
        if dialog.open:
            dialog.open = False
            try:
                dialog.update()
            except:
                pass

    def notify(self, message: str, error: bool = False) -> None:
        try:
            text = ft.Text(message, color=ft.Colors.WHITE if error else None)
            self.page.show_dialog(ft.SnackBar(content=text))
        except:
            pass

    def _refresh_appbar(self) -> None:
        try:
            if self.view == "reader":
                self.page.appbar = None if self.is_immersive else self._reader_appbar()
            elif self.view == "catalog":
                self.page.appbar = self._catalog_appbar()
            else:
                self.page.appbar = self._shelf_appbar()
            self.page.update()
        except:
            pass

    def _shelf_appbar(self) -> ft.AppBar:
        return ft.AppBar(
            title=ft.TextButton(
                content=ft.Row([ft.Icon(ft.Icons.HELP_OUTLINE, color=ft.Colors.BLUE),
                                ft.Text("使用教程 / 提示", color=ft.Colors.BLUE, weight=ft.FontWeight.BOLD)],
                               tight=True, spacing=6),
                on_click=self._show_tutorial,
            ),
            actions=[ft.IconButton(ft.Icons.ADD, tooltip="导入 TXT 小说", on_click=self._import_book)],
        )

    def _catalog_appbar(self) -> ft.AppBar:
        title = self.book.title if self.book else "目录"
        return ft.AppBar(
            leading=ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回书架", on_click=lambda _: self.show_bookshelf()),
            title=ft.Text(title, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            actions=[
                ft.IconButton(ft.Icons.SEARCH, tooltip="全书搜索", on_click=self._open_search),
                ft.IconButton(ft.Icons.BOOKMARK, tooltip="书签列表", on_click=self._open_bookmarks),
            ],
        )

    def _reader_appbar(self) -> ft.AppBar:
        if not self.book or not self.chapters: return ft.AppBar(title=ft.Text("阅读"))
        chapter = self.chapters[self.current_index]
        marked = any(b.book_path == self.book.path and b.chapter_idx == self.current_index for b in self.bookmarks)
        return ft.AppBar(
            leading=ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回目录", on_click=lambda _: self.leave_reader()),
            title=ft.Text(chapter.title, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            actions=[
                ft.IconButton(ft.Icons.STOP_CIRCLE if self._tts_running else ft.Icons.RECORD_VOICE_OVER,
                              tooltip="停止朗读" if self._tts_running else "AI 朗读", on_click=self._toggle_tts),
                ft.IconButton(ft.Icons.PAUSE_CIRCLE if self._auto_scroll_running else ft.Icons.PLAY_CIRCLE,
                              tooltip="暂停自动滚动" if self._auto_scroll_running else "自动滚动",
                              on_click=self._toggle_auto_scroll),
                ft.IconButton(ft.Icons.BOOKMARK if marked else ft.Icons.BOOKMARK_BORDER, tooltip="添加/取消书签",
                              on_click=self._toggle_bookmark),
                ft.IconButton(ft.Icons.SETTINGS, tooltip="阅读设置", on_click=self._open_settings),
            ],
        )

    def show_bookshelf(self) -> None:
        self._stop_tts(update_ui=False)
        self._stop_auto_scroll(update_ui=False)
        self._save_progress()
        self.view = "shelf"
        self.is_immersive = False
        self.book = None
        self.chapters = []
        self._parse_cache.clear()
        self._col = None
        self._paragraph_controls = []
        self._highlight_idx = -1
        self.refresh_bookshelf()
        self.page.appbar = self._shelf_appbar()
        self.main_area.content = self.bookshelf_list
        self.page.update()

    def refresh_bookshelf(self) -> None:
        self.bookshelf_list.controls.clear()
        if not self.bookshelf:
            self.bookshelf_list.controls.append(
                ft.Container(content=ft.Text("书架空空如也，点击右上角 + 导入 TXT 小说", color=ft.Colors.GREY),
                             alignment=ft.Alignment.CENTER, padding=40))
        else:
            for book in self.bookshelf:
                subtitle = f"读到：{book.last_chapter_title}" if book.last_chapter_title else "未开始"
                self.bookshelf_list.controls.append(
                    ft.ListTile(
                        leading=ft.Icon(ft.Icons.MENU_BOOK, color=ft.Colors.BLUE),
                        title=ft.Text(book.title, weight=ft.FontWeight.BOLD, max_lines=1,
                                      overflow=ft.TextOverflow.ELLIPSIS),
                        subtitle=ft.Text(subtitle, size=12, color=ft.Colors.GREY),
                        trailing=ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_color=ft.Colors.RED_400,
                                               tooltip="从书架移除",
                                               on_click=lambda _, b=book: self._ask_delete_book(b)),
                        on_click=lambda _, b=book: self.page.run_task(self._open_book, b),
                    )
                )
        try:
            self.page.update()
        except:
            pass

    async def _import_book(self, _e=None) -> None:
        try:
            files = await self.picker.pick_files(dialog_title="选择 TXT 小说", file_type=ft.FilePickerFileType.CUSTOM,
                                                 allowed_extensions=["txt", "TXT"])
        except Exception as exc:
            log_exception(exc)
            self.notify(f"打开文件选择器失败：{exc}", error=True)
            return
        if not files: return
        path = files[0].path
        if not path:
            self.notify("无法获取文件路径", error=True)
            return
        path = os.path.abspath(path)
        key = os.path.normcase(path)
        if any(os.path.normcase(b.path) == key for b in self.bookshelf):
            self.notify("这本书已经在书架里了")
            return
        self.notify("正在解析文件…")
        try:
            chapters = await asyncio.to_thread(parse_book, path)
            mtime = os.path.getmtime(path)
        except Exception as exc:
            log_exception(exc)
            self.notify(f"文件解析失败：{exc}", error=True)
            return
        book = Book(path=path, title=Path(path).stem)
        self.bookshelf.append(book)
        self._parse_cache = {path: (mtime, chapters)}
        self._mark_dirty("bookshelf")
        self.refresh_bookshelf()
        self.notify(f"导入成功：{book.title}（{len(chapters)} 章）")

    def _ask_delete_book(self, book: Book) -> None:
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("确认删除"),
            content=ft.Text(f"确定要将《{book.title}》从书架中移除吗？\n（不会删除本地文件）"),
            actions=[
                ft.TextButton("取消", on_click=lambda _: self._close_dialog(dialog)),
                ft.TextButton("删除", style=ft.ButtonStyle(color=ft.Colors.RED),
                              on_click=lambda _: self._delete_book(book, dialog)),
            ],
        )
        self._show_dialog(dialog)

    def _delete_book(self, book: Book, dialog: ft.Control) -> None:
        self._close_dialog(dialog)
        self.bookshelf = [b for b in self.bookshelf if b is not book]
        if any(b.book_path == book.path for b in self.bookmarks):
            self.bookmarks = [b for b in self.bookmarks if b.book_path != book.path]
            self._mark_dirty("bookmarks")
        self._parse_cache.pop(book.path, None)
        self._mark_dirty("bookshelf")
        self.refresh_bookshelf()
        self.notify(f"已移除《{book.title}》")

    def _show_tutorial(self, _e=None) -> None:
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                [ft.Icon(ft.Icons.MENU_BOOK, color=ft.Colors.BLUE), ft.Text("操作教程", weight=ft.FontWeight.BOLD)]),
            content=ft.Container(content=ft.Column([
                ft.Text("🖱️ 双击正文进入/退出沉浸模式"),
                ft.Text("⌨️ 空格/→ 下一章，← 上一章，Esc 退出沉浸"),
                ft.Text("🎧 点击右上角朗读按钮，文字高亮并自动跟随滚动"),
                ft.Text("🔍 目录页可全书搜索，结果直接定位到段落"),
                ft.Text("⚠️ 朗读依赖云端语音服务，需要联网", color=ft.Colors.RED_400, weight=ft.FontWeight.BOLD),
            ], spacing=10), width=380, height=240),
            actions=[ft.TextButton("知道了", on_click=lambda _: self._close_dialog(dialog))],
        )
        self._show_dialog(dialog)

    def show_catalog(self) -> None:
        if not self.book or not self.chapters:
            self.show_bookshelf()
            return
        self._stop_tts(update_ui=False)
        self._stop_auto_scroll(update_ui=False)
        self._save_progress()
        self.view = "catalog"
        self.is_immersive = False
        self.page.appbar = self._catalog_appbar()
        self.main_area.content = self._build_catalog()
        self.page.update()

    def _build_catalog(self) -> ft.ListView:
        assert self.book is not None
        marked = {b.chapter_idx for b in self.bookmarks if b.book_path == self.book.path}
        view = ft.ListView(expand=True, spacing=0, padding=10)
        current = self.current_index
        for idx, chapter in enumerate(self.chapters):
            if chapter.is_volume:
                view.controls.append(ft.Container(
                    content=ft.Text(chapter.title, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_GREY, size=16),
                    padding=ft.Padding(left=10, top=14, bottom=8),
                    border=ft.Border.only(bottom=ft.BorderSide(1, ft.Colors.BLUE_GREY))))
                continue
            is_current = idx == current
            row: list[ft.Control] = [
                ft.Text(chapter.title, color=ft.Colors.BLUE if is_current else (
                    ft.Colors.WHITE if self.bg_color == "#1E1E1E" else ft.Colors.BLACK),
                        weight=ft.FontWeight.BOLD if is_current or idx > current else ft.FontWeight.NORMAL, max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS, size=15, expand=True)
            ]
            if idx in marked: row.append(ft.Icon(ft.Icons.BOOKMARK, color=ft.Colors.ORANGE, size=18))
            view.controls.append(ft.Container(content=ft.Row(row, alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                                              padding=ft.Padding(left=20, right=20, top=11, bottom=11),
                                              bgcolor=ft.Colors.with_opacity(0.08,
                                                                             ft.Colors.BLUE) if is_current else None,
                                              border_radius=6, on_click=lambda _, i=idx: self.page.run_task(
                    self._open_chapter_from_catalog, i)))
        return view

    async def _open_chapter_from_catalog(self, idx: int) -> None:
        self._stop_tts()
        self._stop_auto_scroll()
        await self.read_chapter(idx)

    async def _open_book(self, book: Book) -> None:
        self._load_token += 1
        token = self._load_token
        self._stop_tts(update_ui=False)
        self._stop_auto_scroll(update_ui=False)
        self.main_area.content = ft.Container(
            content=ft.Column([ft.ProgressRing(), ft.Text(f"正在解析《{book.title}》…", color=ft.Colors.GREY)],
                              alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            expand=True, alignment=ft.Alignment.CENTER)
        self.page.update()
        try:
            mtime = os.path.getmtime(book.path)
            cached = self._parse_cache.get(book.path)
            if cached and cached[0] == mtime:
                chapters = cached[1]
            else:
                chapters = await asyncio.to_thread(parse_book, book.path)
                self._parse_cache = {book.path: (mtime, chapters)}
        except Exception as exc:
            log_exception(exc)
            if token == self._load_token: self._show_open_error(book, exc)
            return
        if token != self._load_token: return
        if not chapters:
            self._show_open_error(book, ValueError("没有解析到任何章节"))
            return
        self.book = book
        self.chapters = chapters
        last = min(max(0, book.last_chapter), len(chapters) - 1)
        index = self._normalize_chapter(last) or 0
        self.current_index = index
        self.scroll_offset = max(0.0, book.scroll_offset)
        if self.auto_restore and (book.last_chapter > 0 or book.scroll_offset > 0):
            await self.read_chapter(index, offset=book.scroll_offset)
            self._show_recovered(book)
        else:
            self.show_catalog()

    def _show_open_error(self, book: Book, exc: BaseException) -> None:
        self.view = "shelf"
        self.page.appbar = self._shelf_appbar()
        self.main_area.content = ft.Container(content=ft.Column(
            [ft.Icon(ft.Icons.ERROR_OUTLINE, color=ft.Colors.RED_400, size=48),
             ft.Text(f"无法打开《{book.title}》", size=18, weight=ft.FontWeight.BOLD),
             ft.Text(str(exc), color=ft.Colors.GREY, text_align=ft.TextAlign.CENTER),
             ft.Button("返回书架", on_click=lambda _: self.show_bookshelf())], alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=14, tight=True), expand=True,
                                              alignment=ft.Alignment.CENTER)
        self.page.update()

    def _show_recovered(self, book: Book) -> None:
        dialog = ft.AlertDialog(modal=True, title=ft.Row(
            [ft.Icon(ft.Icons.HISTORY, color=ft.Colors.BLUE), ft.Text("进度已恢复", weight=ft.FontWeight.BOLD)]),
                                content=ft.Text(
                                    f"已跳转到上次阅读位置：\n\n📖 {self.chapters[self.current_index].title if self.chapters else book.last_chapter_title}",
                                    size=16),
                                actions=[ft.TextButton("继续阅读", on_click=lambda _: self._close_dialog(dialog))])
        self._show_dialog(dialog)

    def _normalize_chapter(self, idx: int) -> Optional[int]:
        if not self.chapters: return None
        idx = max(0, min(int(idx), len(self.chapters) - 1))
        if self.chapters[idx].is_volume: return chapter_neighbor(self.chapters, idx, 1)
        return idx

    def _chapter_key(self, para_idx: int) -> str:
        return f"{self._key_prefix}{para_idx}"

    def _render_chapter(self, chapter: Chapter) -> None:
        self._clear_highlight(update=False)
        for other in self.chapters:
            if other is not chapter: other._paragraphs = None
        self._key_prefix = f"c{self.current_index}_"
        self._paragraph_controls = []
        controls: list[ft.Control] = []
        for para_idx, paragraph in enumerate(chapter.paragraphs):
            text = ft.Text(paragraph, size=self.font_size, selectable=True, font_family="SimSun", color=self.text_color,
                           style=ft.TextStyle(height=1.75), key=ft.ScrollKey(self._chapter_key(para_idx)))
            self._paragraph_controls.append(text)
            controls.append(text)
        controls.append(self._build_nav())
        self._col = ft.Column(controls=controls, scroll=ft.ScrollMode.AUTO, spacing=16, expand=True,
                              on_scroll=self._on_scroll)
        self.reader_container.content = ft.GestureDetector(content=ft.Container(content=self._col, expand=True,
                                                                                padding=ft.Padding(left=34, right=34,
                                                                                                   top=20, bottom=20)),
                                                           on_double_tap=self._toggle_immersive, expand=True)
        self.reader_container.bgcolor = self.bg_color
        self.main_area.content = self.reader_container

    def _build_nav(self) -> ft.Control:
        prev_idx = chapter_neighbor(self.chapters, self.current_index, -1)
        next_idx = chapter_neighbor(self.chapters, self.current_index, 1)
        return ft.Container(content=ft.Row([ft.Button(content=ft.Text("上一章"), disabled=prev_idx is None,
                                                      on_click=lambda _: self.page.run_task(self._go_chapter, -1)),
                                            ft.Button(content=ft.Text("下一章"), disabled=next_idx is None,
                                                      on_click=lambda _: self.page.run_task(self._go_chapter, 1))],
                                           alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                            padding=ft.Padding(top=28, bottom=60))

    async def read_chapter(self, idx: int, *, offset: float = 0.0, para_idx: int = -1) -> None:
        if not self.chapters: return
        idx = self._normalize_chapter(idx)
        if idx is None: return
        if self.view == "reader" and idx == self.current_index and self._col is not None:
            if para_idx >= 0:
                await self._focus_paragraph(para_idx, idx, duration=150, color=ft.Colors.YELLOW_200)
            else:
                try:
                    await self._col.scroll_to(offset=max(0.0, offset), duration=0)
                except:
                    pass
            return
        self.view = "reader"
        self.current_index = idx
        chapter = self.chapters[idx]
        self._at_scroll_end = False
        self._max_scroll_extent = 0.0
        self._render_chapter(chapter)
        if not self.is_immersive: self.page.appbar = self._reader_appbar()
        self.page.update()
        self.scroll_offset = max(0.0, offset)
        if self.book:
            self.book.last_chapter = idx
            self.book.scroll_offset = self.scroll_offset
            self.book.last_chapter_title = chapter.title
            self._mark_dirty("bookshelf")
        await asyncio.sleep(0.05)
        if para_idx >= 0:
            await self._focus_paragraph(para_idx, idx, duration=120, color=ft.Colors.YELLOW_200)
        else:
            try:
                await self._col.scroll_to(offset=self.scroll_offset, duration=0)
            except:
                pass
        if self._tts_running: self._start_tts(reset=True)

    def leave_reader(self) -> None:
        self._stop_tts(update_ui=False)
        self._stop_auto_scroll(update_ui=False)
        self._save_progress()
        self.show_catalog()

    def _save_progress(self) -> None:
        if not self.book or not self.chapters: return
        if not 0 <= self.current_index < len(self.chapters): return
        self.book.last_chapter = self.current_index
        self.book.scroll_offset = max(0.0, self.scroll_offset)
        self.book.last_chapter_title = self.chapters[self.current_index].title
        self._mark_dirty("bookshelf")

    def _on_scroll(self, event) -> None:
        try:
            self.scroll_offset = max(0.0, float(event.pixels or 0))
            self._max_scroll_extent = max(0.0, float(event.max_scroll_extent or 0))
            self._at_scroll_end = self._max_scroll_extent > 0 and self.scroll_offset >= self._max_scroll_extent - 4
        except:
            return
        if not self.book or not self.chapters: return
        self.book.scroll_offset = self.scroll_offset
        now = time.monotonic()
        if now - self._last_progress_save >= 5.0:
            self._last_progress_save = now
            if 0 <= self.current_index < len(self.chapters):
                self.book.last_chapter = self.current_index
                self.book.last_chapter_title = self.chapters[self.current_index].title
            self._mark_dirty("bookshelf")

    async def _go_chapter(self, step: int) -> None:
        if self._nav_busy or self.view != "reader": return
        target = chapter_neighbor(self.chapters, self.current_index, step)
        if target is None: return
        self._nav_busy = True
        try:
            await self.read_chapter(target)
        finally:
            self._nav_busy = False

    def _on_keyboard(self, event: ft.KeyboardEvent) -> None:
        if self.view != "reader" or self._dialogs or self._input_focused: return
        if event.ctrl or event.alt or event.meta: return
        raw = event.key or ""
        if raw == " " or raw.strip().lower() in ("space", "spacebar"):
            self.page.run_task(self._go_chapter, 1)
            return
        key = raw.strip().lower().replace(" ", "")
        if key == "arrowleft":
            self.page.run_task(self._go_chapter, -1)
        elif key == "arrowright":
            self.page.run_task(self._go_chapter, 1)
        elif key == "escape" and self.is_immersive:
            self._toggle_immersive()

    def _toggle_immersive(self, _e=None) -> None:
        if self.view != "reader": return
        self.is_immersive = not self.is_immersive
        self.page.appbar = None if self.is_immersive else self._reader_appbar()
        self.page.update()

    def _set_highlight(self, para_idx: int, color: Optional[str]) -> bool:
        if not 0 <= para_idx < len(self._paragraph_controls): return False
        previous = self._highlight_idx
        if 0 <= previous < len(self._paragraph_controls) and previous != para_idx: self._paragraph_controls[
            previous].bgcolor = None
        control = self._paragraph_controls[para_idx]
        control.bgcolor = color or ft.Colors.BLUE_100
        self._highlight_idx = para_idx
        if self._col is not None:
            try:
                self._col.update()
            except:
                pass
        return True

    def _clear_highlight(self, update: bool = True) -> None:
        idx = self._highlight_idx
        self._highlight_idx = -1
        if 0 <= idx < len(self._paragraph_controls):
            self._paragraph_controls[idx].bgcolor = None
            if update and self._col is not None:
                try:
                    self._col.update()
                except:
                    pass

    async def _focus_paragraph(self, para_idx: int, chapter_idx: int, duration: int = 200,
                               color: Optional[str] = None) -> None:
        if chapter_idx != self.current_index: return
        if not self._set_highlight(para_idx, color): return
        if self._col is None: return
        try:
            await self._col.scroll_to(scroll_key=self._chapter_key(para_idx), duration=duration)
        except:
            pass

    def _ensure_audio(self) -> bool:
        if self._audio_ready: return True
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
            self._audio_ready = True
            return True
        except Exception as exc:
            log_exception(exc)
            self.notify("音频设备初始化失败，无法朗读", error=True)
            return False

    def _toggle_tts(self, _e=None) -> None:
        if self._tts_running:
            self._stop_tts()
            return
        if not self.chapters or not self._ensure_audio(): return
        self._stop_auto_scroll(update_ui=False)
        self._start_tts(reset=False)
        self.notify("正在请求云端语音，首句请稍候…")
        self._refresh_appbar()

    def _start_tts(self, reset: bool = False) -> None:
        if not self.chapters: return
        if not self._ensure_audio(): return
        self._stop_tts(update_ui=False)
        chapter = self.chapters[self.current_index]
        paragraphs = chapter.paragraphs
        if not paragraphs:
            self.notify("本章没有可朗读的内容")
            return
        if reset or self._tts_chapter != self.current_index:
            start = 0
        else:
            start = self._tts_para
        start = max(0, min(start, len(paragraphs) - 1))
        self._tts_chapter = self.current_index
        self._tts_para = start
        self._tts_running = True
        self._tts_gen += 1
        generation = self._tts_gen
        self._tts_task = self.page.run_task(self._tts_loop, generation, self.current_index, list(paragraphs),
                                            self.voice, start)

    def _stop_tts(self, update_ui: bool = True) -> None:
        task = self._tts_task
        self._tts_task = None
        self._tts_running = False
        self._tts_gen += 1
        if task is not None and not task.done(): task.cancel()
        self._clear_highlight()
        if update_ui: self._refresh_appbar()

    async def _tts_loop(self, generation: int, chapter_idx: int, paragraphs: list[str], voice: str, start: int) -> None:
        next_fetch = None
        try:
            next_fetch = asyncio.create_task(self._fetch_audio(paragraphs[start], voice))
            for idx in range(start, len(paragraphs)):
                if generation != self._tts_gen: return
                try:
                    audio = await next_fetch
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_exception(exc)
                    self.notify("语音获取失败，已停止朗读（请检查网络）", error=True)
                    return
                next_fetch = None
                if idx + 1 < len(paragraphs):
                    next_fetch = asyncio.create_task(self._fetch_audio(paragraphs[idx + 1], voice))
                if generation != self._tts_gen: return

                self._tts_para = idx

                await self._focus_paragraph(idx, chapter_idx, duration=220, color=ft.Colors.BLUE_100)
                if generation != self._tts_gen: return

                await asyncio.to_thread(self._play_audio, audio, generation)

                if generation == self._tts_gen:
                    self._tts_para = idx + 1

            if generation == self._tts_gen: self._tts_para = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_exception(exc)
            self.notify("朗读出错，已停止", error=True)
        finally:
            if next_fetch is not None: next_fetch.cancel()
            if generation == self._tts_gen:
                self._tts_running = False
                self._refresh_appbar()

    async def _fetch_audio(self, text: str, voice: str) -> bytes:
        async def pump(buffer: io.BytesIO) -> None:
            communicate = edge_tts.Communicate(text, voice)
            async for chunk in communicate.stream():
                if chunk["type"] == "audio": buffer.write(chunk["data"])

        last_error = None
        for attempt in range(3):
            if not self._tts_running: raise asyncio.CancelledError
            try:
                buffer = io.BytesIO()
                await asyncio.wait_for(pump(buffer), timeout=20)
                if buffer.tell() == 0: raise RuntimeError("语音数据为空")
                return buffer.getvalue()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < 2: await asyncio.sleep(0.8)
        raise RuntimeError(f"语音获取失败：{last_error}")

    def _play_audio(self, audio: bytes, generation: int) -> None:
        with self._mixer_lock:
            if generation != self._tts_gen or not self._tts_running: return
            try:
                stream = io.BytesIO(audio)
                pygame.mixer.music.load(stream)
                pygame.mixer.music.play()
            except:
                return
            try:
                while pygame.mixer.music.get_busy():
                    if generation != self._tts_gen or not self._tts_running: break
                    time.sleep(0.05)
            finally:
                try:
                    pygame.mixer.music.stop()
                    pygame.mixer.music.unload()
                except:
                    pass

    def _toggle_auto_scroll(self, _e=None) -> None:
        if self._auto_scroll_running:
            self._stop_auto_scroll()
            return
        if self._col is None: return
        if self._tts_running: self._stop_tts(update_ui=False)
        self._at_scroll_end = False
        self._max_scroll_extent = 0.0
        self._auto_scroll_running = True
        self._auto_gen += 1
        self._auto_task = self.page.run_task(self._auto_scroll_loop, self._auto_gen)
        self._refresh_appbar()

    def _stop_auto_scroll(self, update_ui: bool = True) -> None:
        task = self._auto_task
        self._auto_task = None
        self._auto_scroll_running = False
        self._auto_gen += 1
        if task is not None and not task.done(): task.cancel()
        if update_ui: self._refresh_appbar()

    async def _auto_scroll_loop(self, generation: int) -> None:
        try:
            while generation == self._auto_gen and self._auto_scroll_running and self.view == "reader":
                if self._at_scroll_end:
                    self.notify("已滚动到本章末尾")
                    break
                column = self._col
                if column is None: break
                try:
                    await column.scroll_to(delta=40, duration=800)
                except:
                    break
                await asyncio.sleep(0.7)
                if self._max_scroll_extent <= 0:
                    self.notify("当前章节不足一屏，无需自动滚动")
                    break
        finally:
            if generation == self._auto_gen:
                self._auto_scroll_running = False
                self._refresh_appbar()

    def _open_search(self, _e=None) -> None:
        if self.view not in ("reader", "catalog") or not self.chapters: return
        if self._search_open: return
        self._search_open = True
        self._search_token += 1
        token = self._search_token
        search_input = ft.TextField(hint_text="输入关键词…", expand=True, autofocus=True,
                                    on_submit=lambda _: self.page.run_task(run_search),
                                    on_focus=lambda _: setattr(self, "_input_focused", True),
                                    on_blur=lambda _: setattr(self, "_input_focused", False))
        search_btn = ft.IconButton(ft.Icons.SEARCH, icon_color=ft.Colors.BLUE, tooltip="搜索")
        spinner = ft.ProgressRing(visible=False, width=18, height=18, stroke_width=2)
        status = ft.Text("", size=12, color=ft.Colors.GREY)
        results = ft.ListView(expand=True, spacing=8, padding=0)
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Row([ft.Text("全书搜索", weight=ft.FontWeight.BOLD), spinner, status], spacing=10,
                         alignment=ft.MainAxisAlignment.START),
            content=ft.Container(
                content=ft.Column([ft.Row([search_input, search_btn], spacing=4), ft.Divider(height=1), results],
                                  spacing=10), width=620, height=420),
            actions=[ft.TextButton("关闭", on_click=lambda _: self._close_dialog(dialog))],
        )
        busy = False

        async def run_search() -> None:
            nonlocal busy
            if busy: return
            query = (search_input.value or "").strip()
            if not query: return
            pattern = re.compile(re.escape(query), re.IGNORECASE)
            busy = True
            search_btn.disabled = True
            spinner.visible = True
            status.value = "搜索中…"
            results.controls.clear()
            try:
                dialog.update()
            except:
                pass
            try:
                hits = await asyncio.to_thread(search_chapters, list(self.chapters), query)
            except Exception as exc:
                log_exception(exc)
                hits = []
            finally:
                busy = False
                search_btn.disabled = False
                spinner.visible = False
            if token != self._search_token: return
            status.value = f"找到 {len(hits)} 条" if hits else "没有找到相关结果"
            if len(hits) >= MAX_SEARCH_RESULTS: status.value += f"（仅显示前 {MAX_SEARCH_RESULTS} 条）"
            if not hits:
                results.controls.append(ft.Text("没有找到相关结果", color=ft.Colors.GREY))
            else:
                for hit in hits:
                    results.controls.append(
                        ft.Row(
                            [
                                ft.Column([
                                    ft.Text(hit.chapter_title, color=ft.Colors.BLUE, weight=ft.FontWeight.BOLD,
                                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                                    ft.Text(spans=highlight_spans(hit.snippet, pattern), size=13, max_lines=2,
                                            overflow=ft.TextOverflow.ELLIPSIS),
                                ], expand=True, spacing=2, tight=True),
                                ft.Button(content=ft.Text("前往", max_lines=1), width=80, height=36,
                                          on_click=lambda _, h=hit: self.page.run_task(self._jump_to_hit, h, dialog)),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        )
                    )
            try:
                dialog.update()
            except:
                pass

        search_btn.on_click = lambda _: self.page.run_task(run_search)
        self._show_dialog(dialog, cleanup=self._search_closed)

    def _search_closed(self) -> None:
        self._search_open = False
        self._search_token += 1
        self._input_focused = False

    async def _jump_to_hit(self, hit: SearchHit, dialog: ft.Control) -> None:
        self._close_dialog(dialog)
        self._stop_tts()
        self._stop_auto_scroll()
        await self.read_chapter(hit.chapter_idx, para_idx=hit.para_idx)

    def _toggle_bookmark(self, _e=None) -> None:
        if not self.book or not self.chapters: return
        path = self.book.path
        idx = self.current_index
        existing = next((b for b in self.bookmarks if b.book_path == path and b.chapter_idx == idx), None)
        if existing is not None:
            self.bookmarks = [b for b in self.bookmarks if b is not existing]
            self.notify("已取消本书签")
        else:
            chapter = self.chapters[idx]
            text = chapter.first_paragraph[:24] + "…" if len(chapter.first_paragraph) > 24 else chapter.first_paragraph
            self.bookmarks.append(
                Bookmark(book_path=path, chapter_idx=idx, offset=float(self.scroll_offset), para_idx=-1, text=text))
            self.notify("书签添加成功")
        self._mark_dirty("bookmarks")
        self._refresh_appbar()

    def _open_bookmarks(self, _e=None) -> None:
        if not self.book: return
        marks = [b for b in self.bookmarks if b.book_path == self.book.path and 0 <= b.chapter_idx < len(self.chapters)]
        if not marks:
            self.notify("这本书还没有书签")
            return
        marks.sort(key=lambda b: b.chapter_idx)
        dialog = ft.AlertDialog(modal=True, title=ft.Text("书签列表"), actions=[])
        list_view = ft.ListView(expand=True, spacing=6)
        for bookmark in marks:
            tile = ft.ListTile(leading=ft.Icon(ft.Icons.BOOKMARK, color=ft.Colors.ORANGE),
                               title=ft.Text(self.chapters[bookmark.chapter_idx].title, max_lines=1,
                                             overflow=ft.TextOverflow.ELLIPSIS),
                               subtitle=ft.Text(bookmark.text or "", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                               on_click=lambda _, b=bookmark: self._jump_bookmark(b, dialog))
            tile.trailing = ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_color=ft.Colors.RED_400, tooltip="删除书签",
                                          on_click=lambda _, b=bookmark, t=tile: self._delete_bookmark(b, t, list_view,
                                                                                                       dialog))
            list_view.controls.append(tile)
        dialog.content = ft.Container(content=list_view, width=460, height=360)
        dialog.actions = [ft.TextButton("关闭", on_click=lambda _: self._close_dialog(dialog))]
        self._show_dialog(dialog)

    def _jump_bookmark(self, bookmark: Bookmark, dialog: ft.Control) -> None:
        self._close_dialog(dialog)
        self.page.run_task(self._jump_bookmark_async, bookmark)

    async def _jump_bookmark_async(self, bookmark: Bookmark) -> None:
        if not 0 <= bookmark.chapter_idx < len(self.chapters):
            self.notify("书签已失效（章节数量发生变化）", error=True)
            return
        self._stop_tts()
        self._stop_auto_scroll()
        chapter = self.chapters[bookmark.chapter_idx]
        if 0 <= bookmark.para_idx < len(chapter.paragraphs):
            await self.read_chapter(bookmark.chapter_idx, para_idx=bookmark.para_idx)
        else:
            await self.read_chapter(bookmark.chapter_idx, offset=bookmark.offset)

    def _delete_bookmark(self, bookmark: Bookmark, tile: ft.Control, list_view: ft.ListView,
                         dialog: ft.Control) -> None:
        self.bookmarks = [b for b in self.bookmarks if b is not bookmark]
        self._mark_dirty("bookmarks")
        try:
            list_view.controls.remove(tile)
            dialog.update()
        except:
            pass

    def _open_settings(self, _e=None) -> None:
        if not self.chapters: return

        color_buttons = []
        voice_buttons = []
        sheet = None

        def update_theme_ui(selected_color):
            for btn in color_buttons:
                btn.border = ft.Border.all(2, ft.Colors.BLUE if btn.data == selected_color else ft.Colors.TRANSPARENT)

        def update_voice_ui(selected_code):
            for btn in voice_buttons:
                btn.bgcolor = ft.Colors.BLUE if btn.data == selected_code else ft.Colors.GREY_200
                btn.color = ft.Colors.WHITE if btn.data == selected_code else ft.Colors.BLACK

        def make_color_handler(hex_code):
            def handler(_):
                self._apply_theme(hex_code)
                update_theme_ui(hex_code)
                if sheet: sheet.update()

            return handler

        def make_voice_handler(code, name):
            def handler(_):
                self.voice = code
                self._mark_dirty("settings")
                update_voice_ui(code)
                if self._tts_running: self._start_tts(reset=False)
                self.notify(f"音色已切换为：{name}")
                if sheet: sheet.update()

            return handler

        for name, hex_code in THEMES.items():
            is_current = (self.bg_color == hex_code)
            color_buttons.append(
                ft.Container(
                    width=44, height=44, bgcolor=hex_code, border_radius=22,
                    border=ft.Border.all(2, ft.Colors.BLUE if is_current else ft.Colors.TRANSPARENT),
                    tooltip=name, data=hex_code,
                    on_click=make_color_handler(hex_code)
                )
            )

        for name, code in VOICES.items():
            is_selected = (self.voice == code)
            voice_buttons.append(
                ft.Button(
                    content=ft.Text(name), data=code,
                    bgcolor=ft.Colors.BLUE if is_selected else ft.Colors.GREY_200,
                    color=ft.Colors.WHITE if is_selected else ft.Colors.BLACK,
                    on_click=make_voice_handler(code, name)
                )
            )

        sheet = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column([
                    ft.Text("阅读设置", size=20, weight=ft.FontWeight.BOLD),
                    ft.Divider(height=1),
                    ft.Row([ft.Text("字号"), ft.Slider(min=14, max=40, divisions=26, value=self.font_size, expand=True,
                                                       on_change_end=self._on_font_change)]),
                    ft.Text("背景颜色"),
                    ft.Row(color_buttons, wrap=True, spacing=14, run_spacing=10),
                    ft.Divider(height=1),
                    ft.Text("AI 朗读音色"),
                    ft.Row(voice_buttons, wrap=True, spacing=8, run_spacing=8),
                    ft.Divider(height=1),
                    ft.Row([ft.Text("打开时自动恢复阅读进度"),
                            ft.Switch(value=self.auto_restore, on_change=self._on_auto_restore)],
                           alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    ft.Button("关闭", on_click=lambda _: self._close_dialog(sheet)),
                ], spacing=14), padding=24, width=560),
            dismissible=True,
            scrollable=True,
        )
        self._show_dialog(sheet)

    def _on_font_change(self, event) -> None:
        try:
            size = int(float(event.control.value))
        except:
            return
        self._apply_font(size)

    def _apply_font(self, size: int) -> None:
        self.font_size = max(14, min(40, size))
        changed = False
        for control in self._paragraph_controls:
            if control.size != self.font_size:
                control.size = self.font_size
                changed = True
        if changed and self._col is not None:
            try:
                self._col.update()
            except:
                pass
        self._mark_dirty("settings")

    def _apply_theme(self, color_hex: str) -> None:
        self.bg_color = color_hex
        self.page.theme_mode = ft.ThemeMode.DARK if color_hex == "#1E1E1E" else ft.ThemeMode.LIGHT
        self.reader_container.bgcolor = color_hex
        for control in self._paragraph_controls: control.color = self.text_color
        self._mark_dirty("settings")
        try:
            self.page.update()
        except:
            pass

    def _on_auto_restore(self, event) -> None:
        self.auto_restore = bool(event.control.value)
        self.page.run_task(self._save_auto_restore)


async def main(page: ft.Page) -> None:
    app = ReaderApp(page)
    await app.start()


if __name__ == "__main__":
    try:
        ft.run(main)
    except Exception as exc:
        path = log_exception(exc)
        print(f"程序遭遇致命错误，日志已写入：{path or '（日志写入失败，见控制台）'}")