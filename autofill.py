"""新北市校務系統 — 親師訪談紀錄自動填報機器人

用法：
  python autofill.py fill records.csv           # 逐筆填入，每筆停下來讓你檢查，按 Enter 才儲存
  python autofill.py fill records.csv --slow    # 慢速模式：看得到滑鼠移動、逐字打字
  python autofill.py inspect                    # 列出目前畫面上的欄位（欄位對不上時用）

流程：機器人開瀏覽器 → 你自己登入，點進任一位學生的「輔導資料」頁（有上一位／下一位那頁）
→ 回黑色視窗按 Enter → 機器人用座號下拉選單切換學生、核對姓名、按「新增紀錄」、填表。

這個系統登入後網址列不會變，功能頁是嵌在頁面裡的，所以機器人是在「目前畫面」上找欄位，
而不是用網址開頁。帳號密碼不會被讀取或儲存；登入狀態存在本機 browser-profile/。
"""

import argparse
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / "browser-profile"
LOG_DIR = HERE / "logs"
SLOW = False  # --slow：放慢動作並顯示滑鼠游標，方便看機器人在做什麼

# 在頁面上畫出紅點游標，跟著機器人的滑鼠移動
CURSOR_JS = """
addEventListener('DOMContentLoaded', () => {
  const d = document.createElement('div');
  d.style.cssText = 'position:fixed;z-index:2147483647;width:18px;height:18px;margin:-9px 0 0 -9px;'
    + 'border-radius:50%;background:rgba(230,40,40,.75);box-shadow:0 0 0 4px rgba(230,40,40,.25);'
    + 'pointer-events:none;left:-50px;top:-50px;transition:left .15s,top .15s';
  document.body.appendChild(d);
  addEventListener('mousemove', e => { d.style.left = e.clientX + 'px'; d.style.top = e.clientY + 'px'; }, true);
});
"""

# 學生下拉選單的選項長這樣：「01 王小明」
STUDENT_OPTION = re.compile(r"^\s*\d{1,2}\s+\S")
FIND_ERRORS = []  # 找學生頁時遇到的錯誤，診斷時印出


class StopRow(Exception):
    """這一筆不能填（找不到學生、姓名不符…），跳過並記錄。"""


def load_config(path):
    p = Path(path)
    if not p.exists():
        sys.exit(f"找不到 {p.name}，請先執行：copy config.example.json config.json")
    return json.loads(p.read_text(encoding="utf-8"))


def read_records(path):
    raw = Path(path).read_bytes()
    # Excel 存的 CSV 可能是 UTF-8（含 BOM）或 Big5
    for enc in ("utf-8-sig", "cp950"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        sys.exit("無法辨識 CSV 編碼，請在 Excel 另存為「CSV UTF-8」。")
    return [r for r in csv.DictReader(text.splitlines()) if any((v or "").strip() for v in r.values())]


def open_browser(p):
    ctx = p.chromium.launch_persistent_context(
        str(PROFILE_DIR), headless=False, viewport={"width": 1366, "height": 860},
        slow_mo=300 if SLOW else 0,
    )
    if SLOW:
        ctx.add_init_script(CURSOR_JS)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def wait_enter(msg):
    try:
        return input(msg).strip().lower()
    except EOFError:
        return "q"


def all_frames(ctx):
    for pg in reversed(ctx.pages):  # 新開的分頁優先
        for fr in pg.frames:
            yield pg, fr


def option_texts(select):
    # 用 textContent：被系統美化而隱藏起來的下拉選單也讀得到
    return select.evaluate("s => [...s.options].map(o => o.textContent.trim())")


def find_student_frame(ctx):
    """找出學生頁（可能在 iframe 或新分頁裡）。

    優先用座號下拉選單；沒有標準下拉選單時，改用「上一位／下一位」按鈕切換（select 回傳 None）。
    """
    FIND_ERRORS.clear()
    for attempt in range(5):  # 頁面可能還在載入，多試幾次
        fallback = (None, None, None)
        for pg, fr in list(all_frames(ctx)):
            try:
                for sel in fr.locator("select").all():
                    if sum(bool(STUDENT_OPTION.match(o)) for o in option_texts(sel)) >= 2:
                        return pg, fr, sel
                if fallback[1] is None and fr.get_by_text("下一位").count() and fr.get_by_text("上一位").count():
                    fallback = (pg, fr, None)
            except Exception as e:
                FIND_ERRORS.append(f"{fr.url[:60]}：{type(e).__name__} {str(e).splitlines()[0][:100]}")
        if fallback[1] is not None:
            return fallback
        ctx.pages[-1].wait_for_timeout(1000)
    return fallback


def diagnose(ctx):
    """找不到學生頁時，印出每個分頁／框架的概況（姓名遮蔽）。"""
    mask = lambda t: re.sub(r"[\u4e00-\u9fff]", "○", t)
    print("\n--- 診斷資訊（請截圖給 Claude）---")
    for i, (pg, fr) in enumerate(all_frames(ctx)):
        try:
            sels = fr.locator("select").all()
            first = [mask(" / ".join(option_texts(x)[:2]))[:40] for x in sels[:3]]
            hits = [sum(bool(STUDENT_OPTION.match(o)) for o in option_texts(x)) for x in sels]
            first.append(f"符合座號格式：{hits}")
            first.append(repr(option_texts(sels[0])[0][:4]) if sels and option_texts(sels[0]) else "")
            nav = fr.get_by_text("下一位").count()
            add = fr.get_by_text("新增紀錄").count()
            print(f"[{i}] {fr.url[:90]}\n     下拉選單 {len(sels)} 個 {first}｜下一位 {nav}｜新增紀錄 {add}")
        except Exception as e:
            print(f"[{i}] {fr.url[:90]} 讀取失敗：{str(e).splitlines()[0][:60]}")
    for err in FIND_ERRORS[-5:]:
        print("  錯誤：" + err)
    print("---------------------------------\n")


# ---------- 選學生 ----------

SEAT_ON_PAGE = re.compile(r"(\d{1,2})號")


def current_seat(frame):
    m = SEAT_ON_PAGE.search(frame.locator("body").inner_text())
    return int(m.group(1)) if m else None


def step_to_seat(page, frame, seat_n):
    """沒有下拉選單時：一直按「下一位／上一位」直到畫面上是目標座號。"""
    for _ in range(80):
        cur = current_seat(frame)
        if cur is None:
            raise StopRow("畫面上讀不到目前的座號")
        if cur == seat_n:
            return
        frame.get_by_text("下一位" if cur < seat_n else "上一位").first.click()
        page.wait_for_load_state("networkidle")
        for _ in range(40):  # 等座號變了
            page.wait_for_timeout(250)
            if current_seat(frame) != cur:
                break
        else:
            raise StopRow(f"按了{'下一位' if cur < seat_n else '上一位'}，座號沒有改變（可能已到第一位或最後一位）")
    raise StopRow(f"找不到座號 {seat_n}")


def go_to_student(page, frame, select, seat, name):
    seat_n = int(seat)
    if select is None:
        step_to_seat(page, frame, seat_n)
        return verify_student(frame, seat_n, name)
    opts = option_texts(select)
    target = next((o for o in opts if re.match(rf"^\s*0*{seat_n}\s", o)), None)
    if target is None:
        raise StopRow(f"下拉選單裡找不到座號 {seat_n}")
    if name and name not in target:
        raise StopRow(f"座號 {seat_n} 在系統是「{target.strip()}」，和 Excel 的「{name}」不符")
    current = select.evaluate("s => s.options[s.selectedIndex] && s.options[s.selectedIndex].text")
    if (current or "").strip() != target.strip():
        try:
            select.select_option(label=target, timeout=3000)
        except Exception:
            # 下拉選單被美化隱藏了：直接設定值並通知頁面
            select.evaluate(
                "(s, t) => { const o = [...s.options].find(o => o.textContent.trim() === t);"
                " s.value = o.value; s.dispatchEvent(new Event('change', {bubbles: true})); }",
                target.strip(),
            )
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(800)
    verify_student(frame, seat_n, name)


def verify_student(frame, seat_n, name):
    # 切換後畫面上要看到「05號 王小明」才算到對的人
    header = f"{seat_n:02d}號"
    try:
        frame.get_by_text(re.compile(rf"0*{seat_n}號")).first.wait_for(timeout=8000)
    except Exception:
        raise StopRow(f"切換後畫面上沒有出現「{header}」")
    if name:
        body = frame.locator("body").inner_text()
        if not re.search(rf"0*{seat_n}號\s*{re.escape(name)}", body):
            raise StopRow(f"畫面上不是「{header} {name}」，停止填寫")


# ---------- 填欄位 ----------

def dialog_scope(frame, title):
    """新增紀錄的跳出視窗；找不到就用整頁。"""
    for css in (".modal.show", ".modal.in", ".modal[style*='block']", "[role=dialog]"):
        loc = frame.locator(css).filter(has_text=title)
        if loc.count():
            return loc.last
    return frame.locator("body")


def locate(scope, field):
    if field.get("selector"):
        return scope.locator(field["selector"]).first
    if field.get("placeholder"):
        return scope.get_by_placeholder(field["placeholder"]).first
    # 「訪談日期：」這類標題後面的第一個輸入框
    label = field.get("near") or field["column"]
    return scope.locator(
        f"xpath=.//*[starts-with(normalize-space(text()), '{label}')]/following::*[self::input or self::textarea][1]"
    ).first


def type_into(el, value):
    if SLOW:
        el.click()
        el.fill("")
        el.press_sequentially(value, delay=40)  # 一個字一個字打
    else:
        el.fill(value)


def fill_field(scope, field, value):
    kind = field.get("type", "text")
    if not value:
        return
    if kind == "text":
        el = locate(scope, field)
        type_into(el, value)
        el.dispatch_event("change")
    elif kind == "date":
        el = locate(scope, field)
        el.click()
        if el.get_attribute("readonly") is not None:
            # 只能用小日曆選的欄位：直接寫入值
            el.evaluate(
                "(e, v) => { e.value = v; ['input','change','blur'].forEach(t => e.dispatchEvent(new Event(t, {bubbles:true}))); }",
                value,
            )
        else:
            el.fill(value)
            el.dispatch_event("change")
        el.press("Escape")  # 關掉小日曆
    elif kind == "radio":
        options = field.get("options", [])
        other = field.get("other_option")
        if options and value not in options and other:
            # 不在選項裡的，選「其他」再把文字填進「其他」欄
            pick, extra = other, value
        else:
            pick, extra = value, None
        try:
            scope.get_by_role("radio", name=pick, exact=True).first.check(timeout=2000)
        except Exception:
            scope.get_by_text(pick, exact=True).first.click()
        if extra and field.get("other_placeholder"):
            type_into(scope.get_by_placeholder(field["other_placeholder"]).first, extra)
    elif kind == "select":
        locate(scope, field).select_option(label=value)
    else:
        raise ValueError(f"未知欄位類型：{kind}")


# ---------- 儲存 ----------

SUBMIT_NAMES = ["儲存", "存檔", "確定", "送出", "新增", "確認"]


def click_submit(frame, scope, cfg):
    """按下儲存鈕，回傳按到的按鈕文字；都找不到回傳 None。"""
    first = cfg.get("submit_text")
    names = ([first] if isinstance(first, str) else list(first or [])) + SUBMIT_NAMES
    for where in (scope, frame):  # 先找跳出視窗裡，再找整頁
        for name in dict.fromkeys(names):
            for loc in (
                where.get_by_role("button", name=name, exact=True),
                where.locator("a, .btn, input[type=button], input[type=submit]").filter(has_text=re.compile(rf"^\s*{name}\s*$")),
                where.locator(f"input[type=button][value='{name}'], input[type=submit][value='{name}']"),
            ):
                try:
                    vis = [b for b in loc.all() if b.is_visible()]
                except Exception:
                    continue
                if vis:
                    vis[-1].click()
                    return name
    return None


# ---------- fill ----------

def cmd_fill(cfg, csv_path, auto_submit):
    records = read_records(csv_path)
    if not records:
        sys.exit("CSV 沒有資料。")
    need = [cfg["seat_column"]] + [f["column"] for f in cfg["fields"]]
    missing = [c for c in need if c not in records[0]]
    if missing:
        sys.exit(f"CSV 缺少欄位：{', '.join(missing)}（第一列標題要和 records.example.csv 一樣）")

    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"fill-{dt.datetime.now():%Y%m%d-%H%M%S}.csv"
    seat_col, name_col = cfg["seat_column"], cfg.get("name_column", "")

    with sync_playwright() as p, open(log_path, "w", newline="", encoding="utf-8-sig") as logf:
        log = csv.writer(logf)
        log.writerow(["列", "座號", "結果", "訊息"])
        ctx, page = open_browser(p)
        page.goto(cfg["login_url"])
        print("1. 在跳出的瀏覽器登入（帳號密碼請自己輸入）。")
        print("2. 點進任一位學生的「輔導資料」頁（上方有 上一位／座號選單／下一位）。")
        if wait_enter("3. 好了之後回到這裡按 Enter（輸入 q 離開）… ") == "q":
            ctx.close()
            return
        while True:
            page, frame, select = find_student_frame(ctx)
            if frame is not None:
                break
            print("\n找不到學生頁（座號選單或上一位／下一位按鈕）。")
            diagnose(ctx)
            print("請確認瀏覽器畫面停在某位學生的頁面（看得到「上一位」「下一位」「新增紀錄」），")
            if wait_enter("再按 Enter 重試，輸入 q 離開… ") == "q":
                ctx.close()
                return
        how = "座號選單" if select is not None else "上一位／下一位按鈕"
        print(f"找到學生頁（用{how}切換學生），共 {len(records)} 筆要填。\n")

        for n, row in enumerate(records, start=2):  # 第 1 列是標題
            seat = (row.get(seat_col) or "").strip()
            name = (row.get(name_col) or "").strip() if name_col else ""
            tag = f"[第 {n} 列 {seat}號 {name}]"
            try:
                if not seat.isdigit():
                    raise StopRow("座號不是數字")
                # 每筆重新找一次：存檔後頁面可能重新載入
                page, frame, select = find_student_frame(ctx)
                if frame is None:
                    raise StopRow("畫面上找不到座號選單（是否被登出或跳離學生頁？）")
                go_to_student(page, frame, select, seat, name)
                frame.get_by_text(cfg["add_button_text"]).first.click()
                scope = dialog_scope(frame, cfg["dialog_title"])
                scope.get_by_text(cfg["dialog_title"]).first.wait_for(timeout=8000)
                for field in cfg["fields"]:
                    fill_field(scope, field, (row.get(field["column"]) or "").strip())
            except Exception as e:
                msg = str(e).splitlines()[0]
                print(f"{tag} 沒有填：{msg}")
                log.writerow([n, seat, "未填", msg])
                if wait_enter("請在瀏覽器關掉跳出視窗（若有），按 Enter 繼續下一筆，輸入 q 結束… ") == "q":
                    break
                continue

            if auto_submit:
                ans = ""
            else:
                ans = wait_enter(f"{tag} 已填好，請在瀏覽器檢查。Enter=按「{cfg['submit_text']}」，s=跳過，q=結束：")
            if ans == "q":
                log.writerow([n, seat, "未送出", "使用者結束"])
                break
            if ans == "s":
                log.writerow([n, seat, "跳過", "請手動關掉跳出視窗"])
                wait_enter("請在瀏覽器關掉跳出視窗，再按 Enter 繼續… ")
                continue
            clicked = click_submit(frame, scope, cfg)
            if clicked:
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(1500)
                print(f"{tag} 已按「{clicked}」")
                log.writerow([n, seat, "已儲存", clicked])
            else:
                print(f"{tag} 找不到儲存按鈕。")
                wait_enter("請你在瀏覽器自己按儲存，存好後回到這裡按 Enter 繼續下一筆… ")
                log.writerow([n, seat, "手動儲存", ""])
        ctx.close()
    print(f"\n完成，紀錄檔：{log_path}")


# ---------- inspect ----------

INSPECT_JS = """
() => [...document.querySelectorAll('input, select, textarea, button')]
  .filter(e => e.type !== 'hidden' && e.offsetParent !== null)
  .map(e => {
    const opts = e.tagName === 'SELECT' ? [...e.options].map(o => o.text.trim()) : [];
    // 學生名單的選項含姓名，不列出來
    const isStudents = opts.filter(t => /^\\d{1,2}\\s+\\S/.test(t)).length >= 2;
    return {
      tag: e.tagName.toLowerCase(), type: e.type || '', name: e.name || '', id: e.id || '',
      cls: (e.className || '').toString().slice(0, 60), placeholder: e.placeholder || '',
      readonly: e.readOnly || false,
      text: e.tagName === 'BUTTON' ? e.innerText.trim().slice(0, 20) : '',
      radio_label: e.type === 'radio' ? ((e.closest('label') || e.parentElement).innerText.trim().slice(0, 20)) : '',
      options: isStudents ? '(學生名單，略)' : opts.join(' | ').slice(0, 200),
    };
  })
"""


def cmd_inspect(cfg):
    with sync_playwright() as p:
        ctx, page = open_browser(p)
        page.goto(cfg["login_url"])
        print("請登入，點進學生頁並按「新增紀錄」，讓填寫視窗開著。")
        wait_enter("準備好後回到這裡按 Enter… ")
        lines = []
        for i, (pg, fr) in enumerate(all_frames(ctx)):
            try:
                items = fr.evaluate(INSPECT_JS)
            except Exception:
                continue
            if items:
                lines.append(f"\n== 分頁/框架 {i}: {fr.url}")
                lines += [json.dumps(it, ensure_ascii=False) for it in items]
        (HERE / "fields.txt").write_text("\n".join(lines), encoding="utf-8")
        print("已存成 fields.txt（不含學生姓名），請用記事本打開，把內容貼給 Claude。")
        ctx.close()


def main():
    ap = argparse.ArgumentParser(description="新北市校務系統親師訪談紀錄自動填報")
    ap.add_argument("command", choices=["fill", "inspect"])
    ap.add_argument("csv", nargs="?", help="fill 用的資料檔")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--auto-submit", action="store_true", help="不逐筆確認，直接儲存")
    ap.add_argument("--slow", action="store_true", help="慢速模式：看得到滑鼠移動與逐字輸入")
    args = ap.parse_args()
    global SLOW
    SLOW = args.slow

    cfg = load_config(args.config)
    if args.command == "inspect":
        cmd_inspect(cfg)
    else:
        if not args.csv:
            sys.exit("請指定資料檔，例如：python autofill.py fill records.csv")
        cmd_fill(cfg, args.csv, args.auto_submit)


if __name__ == "__main__":
    main()
