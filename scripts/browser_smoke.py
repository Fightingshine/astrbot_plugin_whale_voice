"""隔离协议桩浏览器回归：不会连接线上服务或上传真实歌曲。"""

import base64
import io
import json
import threading
import wave
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "browser_artifacts"
OUT.mkdir(exist_ok=True)
SOURCE = ROOT / "pages" / "songs"
raw = io.BytesIO()
with wave.open(raw, "wb") as f:
    f.setnchannels(1)
    f.setsampwidth(2)
    f.setframerate(16000)
    f.writeframes(b"\0\0" * 3200)
AUDIO = "data:audio/wav;base64," + base64.b64encode(raw.getvalue()).decode()
rows = []
for i, (name, enabled, aliases) in enumerate(
    [
        ("人是猫完整版", True, ["猫猫"]),
        ("O'Clock", False, ["英文歌"]),
        ("<img src=x onerror=window.__xss=1>", True, []),
    ]
):
    rows.append(
        {
            "name": name,
            "filename": name + ".mp3",
            "extension": ".mp3",
            "size_str": "1.0 MB",
            "size_bytes": 1000000,
            "mtime_str": "2026-10-23 00:00",
            "revision": "r" + str(i),
            "enabled": enabled,
            "aliases": aliases,
            "description": "隔离测试数据",
        }
    )
FIXTURE = r"""
window.__calls=[];window.__downloads=[];window.__rows=INITIAL;window.__failNext='';
function listing(){return {songs:window.__rows,total_count:window.__rows.length,enabled_count:window.__rows.filter(x=>x.enabled).length,total_size_str:'3.0 MB'};}
window.AstrBotPluginPage={ready:async()=>{},onContext:callback=>callback({isDark:false}),
 apiGet:async(path,params)=>{window.__calls.push({path,params});if(window.__failNext==='list'){window.__failNext='';throw new Error('隔离测试：临时加载失败');}if(path.endsWith('/list'))return listing();if(path.endsWith('/audio'))return {audio_url:AUDIO,preview_seconds:.2};throw new Error('未知读取接口');},
 apiPost:async(path,body)=>{window.__calls.push({path,body});const row=window.__rows.find(x=>x.filename===body.filename);if(!row||row.revision!==body.revision)return {status:'error',message:'版本已变化，请刷新'};if(path.endsWith('/update')){Object.assign(row,{aliases:body.aliases,description:body.description,enabled:body.enabled,revision:row.revision+'x'});return row;}if(path.endsWith('/rename')){row.name=body.new_name;row.filename=body.new_name+row.extension;row.revision+='x';return row;}if(path.endsWith('/delete')){window.__rows=window.__rows.filter(x=>x!==row);return {deleted:true};}throw new Error('未知修改接口');},
 upload:async(path,file)=>{window.__calls.push({path,filename:file.name});if(window.__duplicateNext){const old=window.__duplicateNext;window.__duplicateNext=null;return {status:'error',message:'曲库已有相同原音，未保存重复文件。',data:{code:'duplicate_audio',saved:false,duplicates:[{name:old.name,filename:old.filename,enabled:old.enabled}],match_count:1}};}if(window.__rows.some(x=>x.filename===file.name))return {status:'error',message:'同名歌曲已存在，原文件未被覆盖'};const row={name:file.name.replace(/\.[^.]+$/,''),filename:file.name,extension:'.wav',size_str:'6 KB',size_bytes:file.size,mtime_str:'2026-10-23 00:00',revision:'new',enabled:false,aliases:[],description:''};window.__rows.push(row);return row;},
 download:async(path,params,name)=>window.__downloads.push({path,params,name})};
""".replace("INITIAL", json.dumps(rows, ensure_ascii=False)).replace("AUDIO", json.dumps(AUDIO))


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(SOURCE)))
threading.Thread(target=server.serve_forever, daemon=True).start()
checks = {}
errors = []


def check(name, condition):
    assert condition, name
    checks[name] = True


try:
    with sync_playwright() as p:
        candidates = [
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ]
        edge = next((path for path in candidates if path.is_file()), None)
        browser = (
            p.chromium.launch(executable_path=str(edge), headless=True)
            if edge
            else p.chromium.launch(headless=True)
        )
        ctx = browser.new_context(viewport={"width": 1440, "height": 1050})
        ctx.add_init_script(FIXTURE)
        ctx.route(
            "**/api/plugin/page/bridge-sdk.js", lambda route: route.fulfill(status=200, body="")
        )
        page = ctx.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
        page.wait_for_function("document.querySelectorAll('#songTableBody tr').length===3")
        check("page_identity", "预置原音曲库管理" in page.title())
        check("not_blank", page.locator("#statCount").inner_text() == "3 首 / 启用 2")
        check(
            "unsafe_title_not_html",
            page.locator("img[src=x]").count() == 0 and page.evaluate("window.__xss") is None,
        )
        page.evaluate("showToast('<img src=x onerror=window.__xss=1>')")
        check("toast_not_html", page.locator("#toastContainer img").count() == 0)
        for index, state in ((1, "未开放"), (2, "已启用")):
            candidate = page.evaluate(f"window.__rows[{index}]")
            before_count = page.evaluate("window.__rows.length")
            page.evaluate(f"window.__duplicateNext=window.__rows[{index}]")
            page.locator("#fileInput").set_input_files(
                {"name": "重复上传.wav", "mimeType": "audio/wav", "buffer": raw.getvalue()}
            )
            page.locator("#btnUploadSubmit").click()
            page.locator("#duplicateNotice").wait_for(state="visible")
            check(
                f"duplicate_{index}_does_not_create_row",
                page.evaluate("window.__rows.length") == before_count,
            )
            check(
                f"duplicate_{index}_status_visible",
                state in page.locator("#duplicateChoices").inner_text(),
            )
            check(
                f"duplicate_{index}_name_is_text",
                page.locator("#duplicateChoices img").count() == 0
                and page.evaluate("window.__xss") is None,
            )
            check(
                f"duplicate_{index}_selection_cleared",
                page.evaluate("selectedFile===null")
                and not page.locator("#uploadForm").is_visible(),
            )
            if index == 1:
                page.screenshot(path=str(OUT / "duplicate-desktop.png"), full_page=True)
            if index == 2:
                page.set_viewport_size({"width": 430, "height": 1000})
                page.locator("#duplicateNotice").scroll_into_view_if_needed()
                page.screenshot(path=str(OUT / "duplicate-mobile.png"), full_page=True)
                check(
                    "duplicate_mobile_no_document_overflow",
                    page.evaluate("document.documentElement.scrollWidth<=window.innerWidth"),
                )
                check(
                    "duplicate_mobile_choice_visible",
                    page.locator("#duplicateChoices button").is_visible(),
                )
                page.set_viewport_size({"width": 1440, "height": 1050})
            page.get_by_role(
                "button", name="定位已有原音：" + candidate["filename"], exact=True
            ).click()
            page.wait_for_function("document.querySelectorAll('#songTableBody tr').length===1")
            check(
                f"duplicate_{index}_locates_original",
                page.locator("#songTableBody tr").get_attribute("data-filename")
                == candidate["filename"],
            )
            check(
                f"duplicate_{index}_does_not_open_editor",
                not page.locator("#editDialog").is_visible(),
            )
            page.locator("#searchInput").fill("")
        page.evaluate("hideDuplicate()")
        page.locator("#searchInput").fill("猫猫")
        check("alias_search", page.locator("#songTableBody tr").count() == 1)
        page.locator("#searchInput").fill("没有这首歌")
        check("filtered_empty_message", "筛选" in page.locator("#emptyTitle").inner_text())
        page.locator("#searchInput").fill("")
        page.locator("#statusFilter").select_option("draft")
        check("draft_filter", page.locator("#songTableBody tr").count() == 1)
        page.get_by_role("button", name="编辑别名与点播状态：O'Clock", exact=True).click()
        check("quoted_title_editor", page.locator("#editDialog").is_visible())
        check(
            "dialog_opaque_background",
            page.locator("#editDialog").evaluate("x=>getComputedStyle(x).backgroundColor")
            != "rgba(0, 0, 0, 0)",
        )
        page.locator("#editAliases").fill("时钟，英文小曲")
        page.locator("#editDescription").fill("新版说明")
        page.locator("#editEnabled").check()
        page.locator("#editSave").click()
        page.wait_for_function("!document.querySelector('#editDialog').open")
        check(
            "metadata_saved",
            page.evaluate("window.__rows.find(x=>x.name===\"O'Clock\").aliases.includes('时钟')"),
        )
        check("enabled_filter_updates", page.locator("#songTableBody tr").count() == 0)
        page.locator("#statusFilter").select_option("all")
        page.locator("#searchInput").fill("O'Clock")
        page.get_by_role("button", name="重命名：O'Clock", exact=True).click()
        page.locator("#actionName").fill("时钟新名字")
        page.locator("#actionConfirm").click()
        page.wait_for_function("window.__rows.some(x=>x.name==='时钟新名字')")
        check(
            "rename_revision_submitted",
            page.evaluate("window.__calls.some(x=>x.path.endsWith('/rename')&&x.body.revision)"),
        )
        page.locator("#searchInput").fill("时钟新名字")
        page.get_by_role("button", name="试听前30秒：时钟新名字", exact=True).click()
        page.wait_for_function(
            "document.querySelector('#audioPlayer').src.startsWith('data:audio/')"
        )
        check(
            "preview_player",
            page.locator("#playerBar").evaluate("x=>x.classList.contains('active')"),
        )
        page.locator("#btnClosePlayer").click()
        check("close_releases_audio", page.locator("#audioPlayer").get_attribute("src") is None)
        page.get_by_role("button", name="下载原音：时钟新名字", exact=True).click()
        check("download_protocol", page.evaluate("window.__downloads.length") == 1)
        page.locator("#searchInput").fill("")
        page.locator("#fileInput").set_input_files(
            {"name": "界面验收曲.wav", "mimeType": "audio/wav", "buffer": raw.getvalue()}
        )
        page.locator("#btnUploadSubmit").click()
        page.wait_for_function("document.querySelector('#editDialog').open")
        check("upload_defaults_to_draft", not page.locator("#editEnabled").is_checked())
        page.locator("#editAliases").fill("新曲别名")
        page.locator("#editSave").click()
        page.wait_for_function("!document.querySelector('#editDialog').open")
        page.locator("#searchInput").fill("新曲别名")
        check("uploaded_alias_search", page.locator("#songTableBody tr").count() == 1)
        page.get_by_role("button", name="删除：界面验收曲", exact=True).click()
        page.locator("#actionConfirm").click()
        page.wait_for_function("!window.__rows.some(x=>x.name==='界面验收曲')")
        check(
            "delete_revision_submitted",
            page.evaluate("window.__calls.some(x=>x.path.endsWith('/delete')&&x.body.revision)"),
        )
        before = page.evaluate("window.__calls.length")
        page.evaluate("handleFileSelect({name:'bad.exe',size:100})")
        check(
            "bad_extension_rejected_client_side", page.evaluate("window.__calls.length") == before
        )
        page.locator("#searchInput").fill("")
        page.evaluate("window.__failNext='list'")
        page.locator("#btnRefresh").click()
        page.wait_for_function(
            "document.querySelector('#emptyTitle').textContent==='曲库暂时无法加载'"
        )
        check("load_error_visible", page.locator("#emptyState").is_visible())
        page.locator("#btnRefresh").click()
        page.wait_for_function("document.querySelectorAll('#songTableBody tr').length===3")
        check("load_retry_recovers", not page.locator("#btnRefresh").is_disabled())
        page.evaluate(
            "window.__rows=window.__rows.filter(x=>!x.name.startsWith('<img'));document.querySelector('#toastContainer').replaceChildren()"
        )
        page.locator("#btnRefresh").click()
        page.wait_for_function("document.querySelectorAll('#songTableBody tr').length===2")
        page.mouse.move(1200, 900)
        page.screenshot(path=str(OUT / "fixture-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 430, "height": 1000})
        page.screenshot(path=str(OUT / "fixture-mobile.png"), full_page=True)
        check(
            "mobile_no_document_overflow",
            page.evaluate("document.documentElement.scrollWidth<=window.innerWidth"),
        )
        check(
            "mobile_actions_visible",
            page.get_by_role(
                "button", name="编辑别名与点播状态：时钟新名字", exact=True
            ).is_visible(),
        )
        check("closed_player_not_visible", not page.locator("#playerBar").is_visible())
        page.set_viewport_size({"width": 1440, "height": 1050})
        page.evaluate("document.documentElement.dataset.theme='dark'")
        page.wait_for_function(
            "getComputedStyle(document.body).backgroundColor==='rgb(15, 23, 42)'"
        )
        page.screenshot(path=str(OUT / "fixture-dark.png"), full_page=True)
        check("console_no_page_errors", not errors)
        sandbox = ctx.new_page()
        sandbox.on("pageerror", lambda error: errors.append(str(error)))
        sandbox.goto(f"http://127.0.0.1:{server.server_port}/index.html")
        sandbox.set_content(
            '<iframe id="sandboxFrame" sandbox="allow-scripts" src="/index.html" style="width:100%;height:1000px;border:0"></iframe>'
        )
        inner = sandbox.frame_locator("#sandboxFrame")
        inner.locator("#songTableBody tr").first.wait_for()
        inner.get_by_role("button", name="重命名：O'Clock", exact=True).click()
        inner.locator("#actionName").fill("沙箱新歌名")
        inner.locator("#actionConfirm").click()
        inner.get_by_role("button", name="删除：沙箱新歌名", exact=True).wait_for()
        check("sandbox_rename_dialog_works", True)
        inner.get_by_role("button", name="删除：沙箱新歌名", exact=True).click()
        inner.locator("#actionConfirm").click()
        inner.get_by_role("button", name="删除：沙箱新歌名", exact=True).wait_for(state="detached")
        check("sandbox_delete_confirmation_works", True)
        sandbox.close()
        report = {
            "checks": checks,
            "check_count": len(checks),
            "page_errors": errors,
            "native_backend": False,
            "cloud_synthesis_calls": 0,
            "qq_send_calls": 0,
        }
        (OUT / "browser-fixture-checks.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False))
        browser.close()
finally:
    server.shutdown()
