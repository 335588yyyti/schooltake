# 新北市校務系統｜輔導紀錄自動填報

把整理在 Excel／CSV 裡的輔導紀錄，自動逐筆填進新北市校務系統
（<https://esa.ntpc.edu.tw/module/sso2/module/sso2/login-page25/init>）。

- **登入由你自己手動完成**（帳號 → 繼續 → 密碼／驗證），程式不讀取也不儲存帳密。
  登入狀態保存在本機 `browser-profile/` 資料夾，下次通常不用再登入。
- 預設**每一筆都會停下來讓你在瀏覽器檢查**，按 Enter 才送出。
- 學生資料（`records.csv`）、設定（`config.json`）、登入狀態、執行紀錄都已列入 `.gitignore`，不會被上傳。

## 一、安裝（只需一次）

需要 Python 3.9 以上（Windows 可從 <https://www.python.org/downloads/> 安裝，記得勾選 *Add to PATH*）。

```bash
cd ntpc-counseling-autofill
pip install -r requirements.txt
python -m playwright install chromium
copy config.example.json config.json      # macOS / Linux 用 cp
```

## 二、登入並找到填報頁

```bash
python autofill.py login
```

1. 在跳出的瀏覽器中登入。
2. 點進「輔導紀錄」→「新增」的那一頁。
3. 回到終端機按 Enter，畫面會印出目前網址 → 貼到 `config.json` 的 `form_url`。

## 三、讀取表單欄位、調整 config.json

```bash
python autofill.py inspect
```

會產生 `fields.txt`，列出頁面上每個欄位的名稱、類型、下拉選項（**不含學生資料**）。
依此修改 `config.json` 的 `fields`：

| 欄位 | 說明 |
|---|---|
| `column` | CSV 的欄名 |
| `label` | 畫面上欄位旁邊的文字（程式用它找欄位） |
| `selector` | 若用 `label` 找不到，改填 CSS 選擇器，例如 `input[formcontrolname=studentNo]` |
| `type` | `text`、`date`、`select`（下拉）、`radio`（單選）、`checkbox`（複選，CSV 內用「、」分隔） |

- `before_fill`：每筆開始前要先點的按鈕，例如「新增」；不需要就設成 `[]`。
- `submit_text`：儲存按鈕上的文字。

> 不確定怎麼改？把 `fields.txt` 貼給 Claude 就能幫你產生 config.json。

## 四、準備資料並填報

照 `records.example.csv` 的格式建立 `records.csv`（Excel 另存為「CSV UTF-8」）。
欄名需與 `config.json` 的 `column` 一致；選項文字要和系統畫面完全相同。

```bash
python autofill.py fill records.csv
```

每筆填好後：**Enter** = 送出、**s** = 跳過、**q** = 結束。
執行結果會記錄在 `logs/`（只記列號、第一個欄位、結果）。

確認流程都正確後，才考慮用 `--auto-submit` 不逐筆確認直接送出。

## 常見問題

- **出現「登入已失效」**：重新執行 `python autofill.py login`。
- **某欄位填寫失敗**：該欄的 `label` 對不到，改用 `selector`（從 `fields.txt` 的 `name`／`id`／`formcontrolname` 組出來）。
- **表單在 iframe 裡**：`fields.txt` 會顯示多個 frame；請告訴 Claude，需要調整程式。
