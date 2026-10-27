# noinspection PyTypeChecker, SpellCheckingInspection, PyBroadException
import flet as ft
import re
import asyncio
import json
import os
import threading
import time
import io
import queue
import edge_tts
import pygame
from charset_normalizer import from_path

os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "1"

CHAPTER_PATTERN = re.compile(
    r"^\s*(第[零一二三四五六七八九十百千万0-9]+[卷章节回].{0,30})\s*$",
    re.MULTILINE,
)


def parse_txt(file_path: str):
    result = from_path(file_path).best()
    if result is None:
        return [], []
    text = str(result)
    matches = list(CHAPTER_PATTERN.finditer(text))
    if not matches:
        return text, [{"title": "全文", "body": text, "is_volume": False}]

    raw_chapters = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        title = m.group(1).strip()
        is_volume = "卷" in title and not any(k in title for k in ["章", "节", "回"])
        body = text[start:end]
        if body.startswith(title):
            body = body[len(title):].strip()
        raw_chapters.append({"title": title, "body": body, "is_volume": is_volume})

    cleaned_chapters = []
    for ch in raw_chapters:
        if ch["is_volume"] and not ch["body"].strip():
            if cleaned_chapters and cleaned_chapters[-1]["is_volume"]:
                cleaned_chapters[-1] = ch
            else:
                cleaned_chapters.append(ch)
        else:
            cleaned_chapters.append(ch)

    return text, cleaned_chapters


async def main(page: ft.Page):
    pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)

    event_loop = asyncio.get_running_loop()
    event_loop.set_exception_handler(lambda loop, ctx: None if isinstance(ctx.get("exception"),
                                                                          ConnectionResetError) else loop.default_exception_handler(
        ctx))

    page.title = "凡人阅读器 Pro"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.padding = 0

    prefs = ft.SharedPreferences()

    bookshelf = []
    current_book = {}
    chapters = []
    current_idx = {"value": 0}
    current_scroll = {"value": 0.0}
    bookmarks = []

    font_size = {"value": 22}
    bg_color = {"value": "#F6F0DF"}
    is_immersive = {"value": False}
    load_token = {"id": 0}

    auto_scroll_state = {"running": False}
    tts_state = {"running": False, "session_id": 0, "book_path": "", "chapter_idx": -1, "para_idx": 0}

    themes = {
        "羊皮纸": "#F6F0DF",
        "豆沙绿": "#CCE8CF",
        "纯白": "#FFFFFF",
        "夜间": "#1E1E1E"
    }

    async def load_data():
        nonlocal bookshelf, bookmarks
        b_str = await prefs.get("bookshelf")
        if b_str: bookshelf = json.loads(b_str)
        m_str = await prefs.get("bookmarks")
        if m_str: bookmarks = json.loads(m_str)

    async def save_bookshelf():
        await prefs.set("bookshelf", json.dumps(bookshelf, ensure_ascii=False))

    async def save_bookmarks():
        await prefs.set("bookmarks", json.dumps(bookmarks, ensure_ascii=False))

    def save_current_progress():
        if not current_book: return
        for b in bookshelf:
            if b["path"] == current_book["path"]:
                b["last_chapter"] = current_idx["value"]
                b["scroll_offset"] = current_scroll["value"]
                break
        page.run_task(save_bookshelf)

    picker = ft.FilePicker()
    page.services.append(picker)

    bookshelf_list = ft.ListView(expand=True, spacing=10, padding=20)
    reader_container = ft.Container(expand=True)
    main_area = ft.Container(expand=True)

    def show_tutorial(_):
        dlg = ft.AlertDialog(
            title=ft.Row([
                ft.Icon(ft.Icons.MENU_BOOK, color=ft.Colors.BLUE),
                ft.Text("阅读器操作教程", weight=ft.FontWeight.BOLD)
            ]),
            content=ft.Container(
                content=ft.Column([
                    ft.Text("🖱️ 鼠标操作", weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE),
                    ft.Text("• 双击正文：进入/退出沉浸模式"),
                    ft.Text("• 点击右上角图标：可添加书签、开启阅读功能"),
                    ft.Divider(),
                    ft.Text("⌨️ 键盘快捷键", weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE),
                    ft.Text("• 按 空格键：下一页 / 向下滚动"),
                    ft.Text("• 按 ← 左方向键：上一章"),
                    ft.Text("• 按 → 右方向键：下一章"),
                    ft.Text("• 按 Esc 键：退出沉浸模式"),
                    ft.Divider(),
                    ft.Text("🎧 特色功能", weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE),
                    ft.Text("• AI朗读：点击小人图标，丝滑无缝发声"),
                    ft.Text("⚠️ 注意：AI朗读依赖云端引擎，请确保电脑已联网！", color=ft.Colors.RED_400,
                            weight=ft.FontWeight.BOLD),
                    ft.Text("• 自动阅读：点击播放键，屏幕自己滚"),
                ], spacing=8),
                width=380, height=400, padding=10
            ),
            actions=[ft.TextButton("我知道了", on_click=lambda _: (setattr(dlg, 'open', False), page.update()))]
        )
        page.overlay.append(dlg)
        dlg.open = True
        page.update()

    def handle_keyboard(e: ft.KeyboardEvent):
        if not reader_container.visible: return

        if e.key == " ":
            page.run_task(read_chapter, current_idx["value"] + 1, 0)
        elif e.key == "Arrow Left":
            page.run_task(read_chapter, current_idx["value"] - 1, 0)
        elif e.key == "Arrow Right":
            page.run_task(read_chapter, current_idx["value"] + 1, 0)
        elif e.key == "Escape":
            if is_immersive["value"]:
                toggle_immersive()

    page.on_keyboard_event = handle_keyboard

    def get_appbar(title_text, is_reading=False, is_catalog=False):
        actions = []

        if is_reading:
            has_mark = any(
                b["book_path"] == current_book.get("path") and b["chapter_idx"] == current_idx["value"] for b in
                bookmarks)
            mark_icon = ft.Icons.BOOKMARK if has_mark else ft.Icons.BOOKMARK_BORDER
            actions = [
                ft.IconButton(ft.Icons.RECORD_VOICE_OVER if not tts_state["running"] else ft.Icons.STOP_CIRCLE,
                              tooltip="AI 朗读 (需联网)", on_click=toggle_tts),
                ft.IconButton(ft.Icons.PLAY_CIRCLE, tooltip="开启自动滚动", on_click=toggle_auto_scroll),
                ft.IconButton(mark_icon, tooltip="切换书签", on_click=add_bookmark),
                ft.IconButton(ft.Icons.SETTINGS, on_click=open_settings)
            ]

        if is_catalog:
            actions.insert(0, ft.IconButton(ft.Icons.BOOKMARK, tooltip="查看书签列表", on_click=show_bookmarks_dialog))

        if not is_reading:
            title_widget = ft.TextButton(
                content=ft.Row([
                    ft.Icon(ft.Icons.HELP_OUTLINE, color=ft.Colors.BLUE),
                    ft.Text(title_text, color=ft.Colors.BLUE, weight=ft.FontWeight.BOLD, max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS)
                ]),
                on_click=show_tutorial,
                tooltip="点击查看操作教程"
            )
        else:
            title_widget = ft.Text(title_text, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)

        return ft.AppBar(
            leading=ft.IconButton(ft.Icons.ARROW_BACK,
                                  on_click=lambda _: go_back() if is_reading else show_bookshelf()) if (
                        is_reading or current_book) else None,
            title=title_widget,
            actions=actions
        )

    async def auto_scroll_task():
        while auto_scroll_state["running"] and reader_container.visible:
            current_scroll["value"] += 30
            if reader_container.content and isinstance(reader_container.content, ft.GestureDetector):
                list_view = reader_container.content.content
                if isinstance(list_view, ft.ListView):
                    await list_view.scroll_to(offset=current_scroll["value"], duration=500)
            await asyncio.sleep(1)

    def toggle_auto_scroll(e):
        auto_scroll_state["running"] = not auto_scroll_state["running"]
        if auto_scroll_state["running"]:
            e.control.icon = ft.Icons.PAUSE_CIRCLE
            page.run_task(auto_scroll_task)
        else:
            e.control.icon = ft.Icons.PLAY_CIRCLE
        page.update()

    # ==========================================
    # 🌟 修复死锁的核心逻辑
    # ==========================================
    def stop_tts():
        # 主线程只发指令，绝不碰 pygame，防止和后台线程抢锁导致死机
        tts_state["running"] = False
        tts_state["session_id"] += 1

    def start_tts():
        stop_tts()
        tts_state["running"] = True
        current_session = tts_state["session_id"]

        if tts_state["book_path"] != current_book.get("path") or tts_state["chapter_idx"] != current_idx["value"]:
            tts_state["book_path"] = current_book.get("path")
            tts_state["chapter_idx"] = current_idx["value"]
            tts_state["para_idx"] = 0

        text_body = chapters[current_idx["value"]]["body"]
        paragraphs = [p.strip() for p in text_body.split("\n") if p.strip()]
        start_idx = tts_state["para_idx"]

        def tts_manager():
            audio_queue = queue.Queue(maxsize=5)

            def producer():
                for idx in range(start_idx, len(paragraphs)):
                    if not tts_state["running"] or tts_state["session_id"] != current_session:
                        break

                    text_to_read = paragraphs[idx]
                    audio_data = io.BytesIO()

                    success = False
                    for attempt in range(3):
                        if not tts_state["running"] or tts_state["session_id"] != current_session: break
                        try:
                            async def fetch():
                                communicate = edge_tts.Communicate(text_to_read, "zh-CN-XiaoxiaoNeural")
                                async for chunk in communicate.stream():
                                    if chunk["type"] == "audio":
                                        audio_data.write(chunk["data"])
                                audio_data.seek(0)

                            asyncio.run(fetch())
                            audio_queue.put((idx, audio_data))
                            success = True
                            break
                        except:
                            time.sleep(1)

                    if not success:
                        audio_queue.put((-1, None))
                        break

                audio_queue.put((-1, None))

            def consumer():
                try:
                    while True:
                        if not tts_state["running"] or tts_state["session_id"] != current_session:
                            break

                        idx, stream = audio_queue.get()
                        if idx == -1: break

                        if not tts_state["running"] or tts_state["session_id"] != current_session:
                            break

                        try:
                            pygame.mixer.music.load(stream)
                            pygame.mixer.music.play()
                            while pygame.mixer.music.get_busy():
                                if not tts_state["running"] or tts_state["session_id"] != current_session:
                                    break  # 发现被中止，立刻跳出循环
                                time.sleep(0.05)
                            pygame.mixer.music.unload()
                        except Exception as e:
                            pass

                        if tts_state["running"] and tts_state["session_id"] == current_session:
                            tts_state["para_idx"] = idx + 1

                finally:
                    # 🌟 在后台线程里彻底清空音频，无论发生什么都不会卡死主线程
                    try:
                        pygame.mixer.music.stop()
                        pygame.mixer.music.unload()
                    except:
                        pass

                    if tts_state["session_id"] == current_session:
                        tts_state["running"] = False
                        page.run_task(restore_ui)

            threading.Thread(target=producer, daemon=True).start()
            threading.Thread(target=consumer, daemon=True).start()

        threading.Thread(target=tts_manager, daemon=True).start()

    def restore_ui():
        if not is_immersive["value"] and reader_container.visible and current_book.get("path"):
            page.appbar = get_appbar(chapters[current_idx["value"]]["title"], is_reading=True)
        page.update()

    def toggle_tts(e):
        if tts_state["running"]:
            stop_tts()
            e.control.icon = ft.Icons.RECORD_VOICE_OVER
        else:
            page.snack_bar = ft.SnackBar(ft.Text("正在请求云端AI语音（需联网），首句请稍候..."))
            page.snack_bar.open = True
            page.update()

            start_tts()
            e.control.icon = ft.Icons.STOP_CIRCLE
        page.update()

    # ==========================================
    # 书架界面
    # ==========================================
    def refresh_bookshelf():
        bookshelf_list.controls.clear()
        if not bookshelf:
            bookshelf_list.controls.append(ft.Text("书架空空如也，点击右上角 + 导入小说", color=ft.Colors.GREY))

        for book in bookshelf:
            title = book.get("title", os.path.basename(book["path"]))
            progress = f"读到: 第 {book.get('last_chapter', 0) + 1} 章"

            def delete_book(_, b=book):
                bookshelf.remove(b)
                page.run_task(save_bookshelf)
                refresh_bookshelf()
                page.update()

            def open_book(_, b=book):
                page.run_task(load_book, b)

            bookshelf_list.controls.append(
                ft.ListTile(
                    leading=ft.Icon(ft.Icons.BOOK, color=ft.Colors.BLUE),
                    title=ft.Text(title, weight=ft.FontWeight.BOLD),
                    subtitle=ft.Text(progress, size=12, color=ft.Colors.GREY),
                    trailing=ft.IconButton(ft.Icons.DELETE, icon_color=ft.Colors.RED_400, on_click=delete_book),
                    on_click=open_book,
                )
            )
        page.update()

    async def import_book(_):
        files = await picker.pick_files(file_type=ft.FilePickerFileType.CUSTOM, allowed_extensions=["txt"])
        if not files: return
        path = files[0].path

        for b in bookshelf:
            if b["path"] == path:
                page.snack_bar = ft.SnackBar(ft.Text("这本书已经在书架里了！"))
                page.snack_bar.open = True
                page.update()
                return

        page.snack_bar = ft.SnackBar(ft.Text("正在解析大文件，请稍候…"))
        page.snack_bar.open = True
        page.update()

        _full, _chapters = await asyncio.to_thread(parse_txt, path)

        new_book = {"path": path, "title": os.path.basename(path).replace(".txt", ""), "last_chapter": 0,
                    "scroll_offset": 0.0}
        bookshelf.append(new_book)
        await save_bookshelf()
        refresh_bookshelf()

        page.snack_bar = ft.SnackBar(ft.Text("导入成功！"))
        page.snack_bar.open = True
        page.update()

    # ==========================================
    # 目录界面
    # ==========================================
    def show_catalog():
        catalog_list = ft.ListView(expand=True, spacing=0, padding=10)
        last_read = current_book.get("last_chapter", 0)
        marked_chapters = {b["chapter_idx"] for b in bookmarks if b["book_path"] == current_book["path"]}

        for idx, ch in enumerate(chapters):
            if ch["is_volume"]:
                catalog_list.controls.append(
                    ft.Container(
                        content=ft.Text(ch["title"], weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_GREY, size=16),
                        padding=ft.Padding(left=10, top=15, bottom=10),
                        border=ft.Border.only(bottom=ft.BorderSide(1, ft.Colors.BLUE_GREY))
                    )
                )
            else:
                text_color = ft.Colors.GREY if idx <= last_read else ft.Colors.BLACK
                is_marked = idx in marked_chapters

                row_content: list[ft.Control] = [
                    ft.Text(ch["title"], color=text_color, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, size=15,
                            expand=True)
                ]
                if is_marked:
                    row_content.append(ft.Icon(ft.Icons.BOOKMARK, color=ft.Colors.ORANGE, size=18))

                catalog_list.controls.append(
                    ft.Container(
                        content=ft.Row(row_content, alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                        padding=ft.Padding(left=20, right=20, top=12, bottom=12),
                        on_click=lambda _, i=idx: page.run_task(read_chapter, i, 0),
                    )
                )

        page.appbar = get_appbar(current_book["title"], is_reading=False, is_catalog=True)
        main_area.content = catalog_list
        page.update()

    # ==========================================
    # 阅读界面
    # ==========================================
    def toggle_immersive(_=None):
        is_immersive["value"] = not is_immersive["value"]
        if is_immersive["value"]:
            page.appbar = None
        else:
            page.appbar = get_appbar(chapters[current_idx["value"]]["title"], is_reading=True)
        page.update()

    def go_back():
        auto_scroll_state["running"] = False
        stop_tts()  # 这里只是发指令，不会阻塞
        save_current_progress()
        show_catalog()

    def on_scroll(e):
        current_scroll["value"] = e.pixels

    async def read_chapter(idx: int, target_offset: float = 0.0):
        if idx < 0 or idx >= len(chapters): return
        if chapters[idx]["is_volume"]:
            if idx + 1 < len(chapters) and not chapters[idx + 1]["is_volume"]:
                idx += 1
            else:
                return

        current_idx["value"] = idx
        title, body = chapters[idx]["title"], chapters[idx]["body"]

        if not is_immersive["value"]:
            page.appbar = get_appbar(title, is_reading=True)

        paragraphs = [p.strip() for p in body.split("\n") if p.strip()]
        new_list = ft.ListView(expand=True, spacing=16, padding=30, on_scroll=on_scroll)
        text_color = ft.Colors.WHITE if bg_color["value"] == "#1E1E1E" else ft.Colors.BLACK

        for p in paragraphs:
            new_list.controls.append(
                ft.Text(p, size=font_size["value"], selectable=True, font_family="SimSun", color=text_color,
                        style=ft.TextStyle(height=1.8))
            )

        nav_row = ft.Row(
            [
                ft.Button(content=ft.Text("上一章"), disabled=(idx == 0),
                          on_click=lambda _: page.run_task(read_chapter, idx - 1, 0)),
                ft.Button(content=ft.Text("下一章"), disabled=(idx == len(chapters) - 1),
                          on_click=lambda _: page.run_task(read_chapter, idx + 1, 0))
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        )
        new_list.controls.append(ft.Container(content=nav_row, padding=ft.Padding(top=40, bottom=80)))

        reader_container.content = ft.GestureDetector(content=new_list, on_double_tap=toggle_immersive, expand=True)
        reader_container.bgcolor = bg_color["value"]

        main_area.content = reader_container
        page.update()

        if tts_state["running"]:
            start_tts()

        if target_offset > 0:
            await asyncio.sleep(0.15)
            await new_list.scroll_to(offset=target_offset, duration=0)
            current_scroll["value"] = target_offset
        else:
            current_scroll["value"] = 0

    # ==========================================
    # 设置与书签弹窗
    # ==========================================
    def change_font(e):
        font_size["value"] = int(e.control.value)
        if reader_container.content and isinstance(reader_container.content, ft.GestureDetector):
            list_view = reader_container.content.content
            if isinstance(list_view, ft.ListView):
                for control in list_view.controls:
                    if isinstance(control, ft.Text):
                        control.size = font_size["value"]
        page.update()

    def change_color(color_hex):
        bg_color["value"] = color_hex
        page.theme_mode = ft.ThemeMode.DARK if color_hex == "#1E1E1E" else ft.ThemeMode.LIGHT
        page.run_task(read_chapter, current_idx["value"], current_scroll["value"])

    def open_settings(_):
        def close_sheet(_):
            settings_panel.open = False
            page.update()

        color_buttons = [
            ft.Container(
                width=40, height=40, bgcolor=hex_code, border_radius=20,
                border=ft.Border.all(2, ft.Colors.BLUE if bg_color["value"] == hex_code else ft.Colors.TRANSPARENT),
                on_click=lambda _, c=hex_code: change_color(c)
            ) for name, hex_code in themes.items()
        ]

        settings_panel = ft.BottomSheet(
            ft.Container(
                ft.Column([
                    ft.Text("阅读设置", size=20, weight=ft.FontWeight.BOLD),
                    ft.Divider(),
                    ft.Row([ft.Text("字号"),
                            ft.Slider(min=14, max=40, value=font_size["value"], divisions=26, on_change=change_font,
                                      expand=True)]),
                    ft.Text("背景颜色"),
                    ft.Row(color_buttons, alignment=ft.MainAxisAlignment.SPACE_AROUND),
                    ft.Button("关闭", on_click=close_sheet),
                ]),
                padding=30,
            )
        )
        page.overlay.append(settings_panel)
        settings_panel.open = True
        page.update()

    def add_bookmark(_):
        nonlocal bookmarks
        original_len = len(bookmarks)
        bookmarks = [b for b in bookmarks if
                     not (b["book_path"] == current_book["path"] and b["chapter_idx"] == current_idx["value"])]

        if len(bookmarks) == original_len:
            text_snippet = chapters[current_idx["value"]]["body"][:20].strip() + "..."
            bookmarks.append({
                "book_path": current_book["path"], "chapter_idx": current_idx["value"],
                "offset": current_scroll["value"], "text": text_snippet
            })
            page.snack_bar = ft.SnackBar(ft.Text("书签添加成功！"))
        else:
            page.snack_bar = ft.SnackBar(ft.Text("已取消本书签"))

        page.run_task(save_bookmarks)
        page.snack_bar.open = True

        if not is_immersive["value"]:
            page.appbar = get_appbar(chapters[current_idx["value"]]["title"], is_reading=True)
        page.update()

    def show_bookmarks_dialog(_):
        current_book_marks = [b for b in bookmarks if b["book_path"] == current_book["path"]]
        if not current_book_marks:
            page.snack_bar = ft.SnackBar(ft.Text("这本书还没有书签哦~"))
            page.snack_bar.open = True
            page.update()
            return

        dlg = ft.AlertDialog(title=ft.Text("书签列表"), actions=[])

        def close_dlg(_):
            dlg.open = False
            page.update()

        def jump_to_bookmark(bookmark):
            dlg.open = False
            page.update()
            page.run_task(read_chapter, int(bookmark["chapter_idx"]), float(bookmark["offset"]))

        marks_list = ft.ListView(expand=True, spacing=10)
        for b in current_book_marks:
            c_idx = int(b['chapter_idx'])
            marks_list.controls.append(
                ft.ListTile(
                    leading=ft.Icon(ft.Icons.BOOKMARK, color=ft.Colors.ORANGE),
                    title=ft.Text(f"{chapters[c_idx]['title']}"),
                    subtitle=ft.Text(b["text"]),
                    on_click=lambda _, m=b: jump_to_bookmark(m)
                )
            )

        dlg.content = ft.Container(content=marks_list, height=300, width=400)
        dlg.actions = [ft.TextButton("关闭", on_click=close_dlg)]
        page.overlay.append(dlg)
        dlg.open = True
        page.update()

    # ==========================================
    # 页面调度
    # ==========================================
    async def load_book(book):
        nonlocal current_book, chapters
        load_token["id"] += 1
        my_id = load_token["id"]

        loading_overlay = ft.Container(
            content=ft.Column([
                ft.ProgressRing(),
                ft.Text(f"正在解析《{book['title']}》...", color=ft.Colors.GREY)
            ], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            expand=True,
            alignment=ft.Alignment.CENTER
        )
        main_area.content = loading_overlay
        page.update()

        _full, new_chapters = await asyncio.to_thread(parse_txt, book["path"])

        if my_id != load_token["id"]: return

        current_book = book
        chapters = new_chapters
        show_catalog()

    def show_bookshelf():
        nonlocal current_book
        load_token["id"] += 1
        current_book = {}
        page.appbar = get_appbar("点击查看【阅读器操作教程】", is_reading=False)
        page.appbar.actions.append(ft.IconButton(ft.Icons.ADD, on_click=import_book))

        refresh_bookshelf()
        main_area.content = bookshelf_list
        page.update()

    await load_data()
    show_bookshelf()
    page.add(main_area)


ft.run(main)