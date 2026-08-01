# 每日市場半年線 Email 任務

每天平行抓取：

- S&P 500：`^GSPC`
- NASDAQ Composite：`^IXIC`
- 台灣加權指數：`^TWII`

計算開盤、收盤、120 個交易日均線、與半年線差距，以及向上突破／向下跌破訊號，接著將結果寄到 Gmail。

> 若你所說的 NASDAQ 是 NASDAQ-100，請把 `^IXIC` 改成 `^NDX`。

## Email 內容

- 主旨會顯示：每日更新、出現半年線穿越、部分成功或抓取失敗。
- 信件正文包含三個市場的開盤、收盤、半年線、差距與穿越判斷。
- 附件包含 `market_history.csv` 與 `latest_report.json`。
- 預設寄到 `ab123ab456g@gmail.com`。
- 非交易日若沒有新資料且沒有錯誤，預設不重複寄信。

## 產出檔案

- `data/market_history.csv`：歷史紀錄，同一市場同一交易日不重複新增。
- `data/latest_report.json`：最新結構化結果。
- `data/latest_report.txt`：最新文字報告。
- `data/market_daily.log`：執行紀錄。

## Gmail 準備

程式不能使用一般 Gmail 登入密碼，請使用 Google 應用程式密碼：

1. Gmail 帳戶開啟「兩步驟驗證」。
2. 到 Google 帳戶的「應用程式密碼」。
3. 建立一組 16 位數應用程式密碼。
4. 將它放到 GitHub Secret 或 PythonAnywhere 環境變數，不要寫進程式碼。

## 免費排程：GitHub Actions（建議）

### 1. 上傳專案

建立 GitHub repository，將本專案全部上傳。

### 2. 新增 Secrets

進入：

`Repository → Settings → Secrets and variables → Actions → Secrets`

新增兩個 Repository secrets：

| 名稱 | 值 |
|---|---|
| `SMTP_USERNAME` | 你的 Gmail，例如 `ab123ab456g@gmail.com` |
| `SMTP_APP_PASSWORD` | Google 產生的 16 位數應用程式密碼 |

不要把一般 Gmail 密碼放進去。

### 3. 收件人設定

預設已寄到 `ab123ab456g@gmail.com`，不必另外設定。

若要修改，進入：

`Repository → Settings → Secrets and variables → Actions → Variables`

新增 Repository variable：

| 名稱 | 值 |
|---|---|
| `EMAIL_TO` | 新的收件信箱；多個信箱可用逗號分隔 |

### 4. 測試

進入 GitHub `Actions` 頁面，選擇：

`Daily market MA120 email report → Run workflow`

成功後應收到測試信，之後每天台北時間約 06:30 自動執行。

排程每天都跑，而不是只跑週一至週五，原因是美股週五收盤在台北時間週六清晨。程式會利用市場日期避免 CSV 與 Email 重複。

## PythonAnywhere

先安裝套件：

```bash
python3.12 -m pip install --user -r /home/你的帳號/market_daily_task/requirements.txt
```

在 `~/.bashrc` 加入：

```bash
export SMTP_USERNAME="ab123ab456g@gmail.com"
export SMTP_APP_PASSWORD="你的16位數應用程式密碼"
export EMAIL_TO="ab123ab456g@gmail.com"
export TZ="Asia/Taipei"
```

重新載入：

```bash
source ~/.bashrc
```

手動測試：

```bash
python3.12 /home/你的帳號/market_daily_task/market_daily.py
```

若帳號有 Scheduled Tasks，在 `Tasks` 頁面加入：

```bash
python3.12 /home/你的帳號/market_daily_task/market_daily.py
```

## 可調整的環境變數

| 變數 | 預設 | 說明 |
|---|---:|---|
| `EMAIL_ENABLED` | `true` | 是否寄信 |
| `EMAIL_SEND_ONLY_WHEN_NEW` | `true` | 沒有新交易日資料時不寄重複信 |
| `EMAIL_ATTACH_CSV` | `true` | 附加 CSV |
| `EMAIL_ATTACH_JSON` | `true` | 附加 JSON |
| `EMAIL_TO` | 預設信箱 | 多個收件人以逗號分隔 |
| `MA_DAYS` | `120` | 半年線交易日數 |

若希望即使沒有新資料也每天寄信：

```bash
export EMAIL_SEND_ONLY_WHEN_NEW="false"
```

## 退出碼

- `0`：三個市場都成功。
- `1`：全部失敗。
- `2`：部分市場成功。

## 資料用途

`yfinance` 是非官方套件，適合個人研究與自動化監看，不應視為交易所正式報價或直接交易依據。
