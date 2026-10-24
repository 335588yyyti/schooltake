"""新北市校務系統 — 輔導紀錄自動填報

用法：
  python autofill.py login              # 開瀏覽器，自己手動登入（含驗證碼/OTP），登入狀態會保留
  python autofill.py inspect            # 列出 form_url 頁面上所有欄位，存成 fields.txt，用來設定 config.json
  python autofill.py fill records.csv   # 逐筆填入，每筆停下來讓你檢查，再由你決定是否送出
  python autofill.py fill records.csv --auto-submit   # 檢查過幾筆、確認無誤後才用：自動按儲存

帳號密碼不會被這支程式讀取或儲存；登入狀態存在本機 browser-profile/ 資料夾。
"""

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / "browser-profile"
LOG_DIR = HERE / "logs"


def load_config(path):
    p = Path(path)
    if not p.exists():
        sys.exit(f"找不到 {p.name}，請先複製 config.example.json 為 config.json 再修改。")
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
    return list(csv.DictReader(text.splitlines()))


def open_browser(p):
    ctx = p.chromium.launch_persistent_context(
        str(PROFILE_DIR), headless=False, viewport={"width": 1366, "height": 860}
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def wait_enter(msg):
    try:
        return input(msg).strip().lower()
    except EOFError:
        return "q"


# ---------- login ----------

def cmd_login(cfg):
    with sync_playwright() as p:
        ctx, page = open_browser(p)
        page.goto(cfg["login_url"])
        print("請在開啟的瀏覽器中手動登入，並點到「新增輔導紀錄」的頁面。")
        wait_enter("完成後回到這裡按 Enter… ")
        print(f"\n目前網址：{page.url}")
        print("若這就是填報頁，請把它貼到 config.json 的 form_url。")
        ctx.close()


# ---------- inspect ----------

INSPECT_JS = """
() => {
  const labelOf = el => {
    if (el.id) { const l = document.querySelector(`label[for="${el.id}"]`); if (l) return l.innerText.trim(); }
    const wrap = el.closest('label'); if (wrap) return wrap.innerText.trim();
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    // 表格式表單：同一列的 th / 前一格
    const td = el.closest('td'); if (td) {
      const prev = td.previousElementSibling; if (prev) return prev.innerText.trim();
      const th = td.parentElement && td.parentElement.querySelector('th'); if (th) return th.innerText.trim();
    }
    return '';
  };
  return [...document.querySelectorAll('input, select, textarea, [role=combobox], mat-select')]
    .filter(e => e.type !== 'hidden')
    .map(e => ({
      tag: e.tagName.toLowerCase(), type: e.type || '', name: e.name || '', id: e.id || '',
      formcontrolname: e.getAttribute('formcontrolname') || '', label: labelOf(e),
      placeholder: e.placeholder || '', value_text: e.tagName === 'SELECT' ? [...e.options].map(o => o.text).join(' | ') : '',
    }));
}
"""


def cmd_inspect(cfg):
    with sync_playwright() as p:
        ctx, page = open_browser(p)
        page.goto(cfg["form_url"])
        print("請在瀏覽器中把畫面停在要填的表單（必要時先按「新增」）。")
        wait_enter("準備好後按 Enter… ")
        lines = [f"URL: {page.url}"]
        frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
        for i, fr in enumerate(frames):
            try:
                items = fr.evaluate(INSPECT_JS)
            except Exception:
                continue
            if not items:
                continue
            lines.append(f"\n== frame {i}: {fr.url}")
            for it in items:
                lines.append(json.dumps(it, ensure_ascii=False))
        buttons = page.locator("button, input[type=submit], input[type=button]").all_inner_texts()
        lines.append("\n== buttons: " + " | ".join(b.strip() for b in buttons if b.strip()))
        (HERE / "fields.txt").write_text("\n".join(lines), encoding="utf-8")
        print("已存成 fields.txt（只有欄位名稱，沒有學生資料），可依此修改 config.json。")
        ctx.close()


# ---------- fill ----------

def locate(page, field):
    if field.get("selector"):
        return page.locator(field["selector"]).first
    return page.get_by_label(field["label"], exact=False).first


def fill_field(page, field, value):
    kind = field.get("type", "text")
    if value is None or value == "":
        return
    if kind in ("text", "date"):
        el = locate(page, field)
        el.fill(value)
        el.dispatch_event("change")
        el.press("Tab")  # 讓 Angular 表單觸發驗證
    elif kind == "select":
        el = locate(page, field)
        try:
            el.select_option(label=value, timeout=2000)
        except Exception:
            # 非原生下拉（如 Angular Material）：點開再點選項
            el.click()
            page.get_by_role("option", name=value).first.click()
    elif kind == "radio":
        page.get_by_role("radio", name=value).first.check()
    elif kind == "checkbox":
        for v in [s.strip() for s in value.replace("、", ",").split(",") if s.strip()]:
            page.get_by_role("checkbox", name=v).first.check()
    else:
        raise ValueError(f"未知欄位類型：{kind}")


def run_steps(page, steps):
    for s in steps or []:
        if s["action"] == "click":
            target = page.locator(s["selector"]) if s.get("selector") else page.get_by_text(s["text"], exact=True)
            target.first.click()
            page.wait_for_load_state("networkidle")


def cmd_fill(cfg, csv_path, auto_submit):
    records = read_records(csv_path)
    if not records:
        sys.exit("CSV 沒有資料。")
    missing = [f["column"] for f in cfg["fields"] if f["column"] not in records[0]]
    if missing:
        sys.exit(f"CSV 缺少欄位：{', '.join(missing)}")

    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"fill-{dt.datetime.now():%Y%m%d-%H%M%S}.csv"
    key_col = cfg["fields"][0]["column"]

    with sync_playwright() as p, open(log_path, "w", newline="", encoding="utf-8-sig") as logf:
        log = csv.writer(logf)
        log.writerow(["列", key_col, "結果", "訊息"])
        ctx, page = open_browser(p)
        for n, row in enumerate(records, start=2):  # 第 1 列是標題
            tag = f"[第 {n} 列 {row.get(key_col, '')}]"
            try:
                page.goto(cfg["form_url"])
                page.wait_for_load_state("networkidle")
                if "sso" in page.url and "login" in page.url:
                    print("登入已失效，請先執行：python autofill.py login")
                    break
                run_steps(page, cfg.get("before_fill"))
                for field in cfg["fields"]:
                    fill_field(page, field, row.get(field["column"], "").strip())
            except (PWTimeout, Exception) as e:
                print(f"{tag} 填寫失敗：{e}")
                log.writerow([n, row.get(key_col, ""), "失敗", str(e).splitlines()[0]])
                if wait_enter("按 Enter 跳過這筆，輸入 q 結束… ") == "q":
                    break
                continue

            if auto_submit:
                page.get_by_role("button", name=cfg["submit_text"]).first.click()
                page.wait_for_load_state("networkidle")
                print(f"{tag} 已送出")
                log.writerow([n, row.get(key_col, ""), "已送出", ""])
                continue

            ans = wait_enter(f"{tag} 已填好，請在瀏覽器檢查。Enter=按「{cfg['submit_text']}」送出，s=跳過，q=結束：")
            if ans == "q":
                log.writerow([n, row.get(key_col, ""), "未送出", "使用者結束"])
                break
            if ans == "s":
                log.writerow([n, row.get(key_col, ""), "跳過", ""])
                continue
            page.get_by_role("button", name=cfg["submit_text"]).first.click()
            page.wait_for_load_state("networkidle")
            log.writerow([n, row.get(key_col, ""), "已送出", ""])
        ctx.close()
    print(f"完成，紀錄檔：{log_path}")


def main():
    ap = argparse.ArgumentParser(description="新北市校務系統輔導紀錄自動填報")
    ap.add_argument("command", choices=["login", "inspect", "fill"])
    ap.add_argument("csv", nargs="?", help="fill 用的資料檔")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--auto-submit", action="store_true", help="不逐筆確認，直接送出")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.command == "login":
        cmd_login(cfg)
    elif args.command == "inspect":
        cmd_inspect(cfg)
    else:
        if not args.csv:
            sys.exit("請指定資料檔，例如：python autofill.py fill records.csv")
        cmd_fill(cfg, args.csv, args.auto_submit)


if __name__ == "__main__":
    main()
