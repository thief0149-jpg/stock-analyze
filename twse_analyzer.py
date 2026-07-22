# -*- coding: utf-8 -*-
"""
twse_analyzer.py — 台股抓價 + 技術指標分析（雲端/本機通用版）
- 抓證交所日收盤，計算 MA/RSI/KD 給進出場訊號
- 輸出 prices_data.js 供 stock.html 載入
- 訊號改變時透過 LINE Messaging API 推播（需設環境變數）

環境變數（GitHub Actions 用 Secrets 設定）：
    STOCK_NO        股票代號，預設 2330
    LINE_TOKEN      LINE Messaging API 的 Channel access token（可留空 = 不推播）
    LINE_USER_ID    你的 LINE user id（可留空 = 不推播）

本機用法：
    py twse_analyzer.py 2330
    py twse_analyzer.py 2330 --months 4 --csv
"""
import os
import sys
import csv
import json
import time
import argparse
from datetime import date

import requests

HEADERS = {"User-Agent": "Mozilla/5.0"}
STATE_FILE = "signal_state.json"   # 記錄上次訊號，用來偵測變化


# ---------------- 抓資料 ----------------
def fetch_month(stock_no, yyyymm):
    url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
    params = {"response": "json", "date": yyyymm + "01", "stockNo": stock_no}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        j = r.json()
    except Exception:
        return []
    out = []
    if j.get("stat") != "OK":
        return out
    for row in j.get("data", []):
        close = row[6].replace(",", "").strip()
        if close in ("--", ""):
            continue
        out.append((row[0].strip(), float(close)))
    return out


def fetch_history(stock_no, months):
    today = date.today()
    y, m = today.year, today.month
    ym_list = []
    for _ in range(months):
        ym_list.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    result = []
    for ym in reversed(ym_list):
        result += fetch_month(stock_no, ym)
        time.sleep(1.5)
    return result


def fetch_realtime(stock_no):
    url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
    params = {"ex_ch": f"tse_{stock_no}.tw", "json": "1", "delay": "0"}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        arr = r.json().get("msgArray", [])
        if arr:
            name = arr[0].get("n", "")
            z = arr[0].get("z", "-")
            if z not in ("-", ""):
                return name, float(z)
            y = arr[0].get("y", "-")
            if y not in ("-", ""):
                return name, float(y)
    except Exception:
        pass
    return None


# ---------------- 指標 ----------------
def sma(a, n, i):
    if i < n - 1:
        return None
    return sum(a[i - n + 1:i + 1]) / n


def rsi(a, n=14):
    if len(a) < n + 1:
        return None
    g = l = 0.0
    for i in range(len(a) - n, len(a)):
        d = a[i] - a[i - 1]
        if d > 0:
            g += d
        else:
            l -= d
    if l == 0:
        return 100.0
    rs = (g / n) / (l / n)
    return 100 - 100 / (1 + rs)


def kd(a, n=9):
    K = D = 50.0
    for i in range(n - 1, len(a)):
        win = a[i - n + 1:i + 1]
        hi, lo = max(win), min(win)
        rsv = 50.0 if hi == lo else (a[i] - lo) / (hi - lo) * 100
        K = K * 2 / 3 + rsv / 3
        D = D * 2 / 3 + K / 3
    return K, D


def evaluate(prices):
    """回傳 (訊號字串, 評分, 理由list, 指標dict)"""
    i = len(prices) - 1
    last = prices[i]
    ma5, ma20 = sma(prices, 5, i), sma(prices, 20, i)
    ma5p, ma20p = sma(prices, 5, i - 1), sma(prices, 20, i - 1)
    R = rsi(prices)
    K, D = kd(prices)

    score = 0
    reasons = []
    if ma5p <= ma20p and ma5 > ma20:
        score += 2; reasons.append("MA5 黃金交叉 MA20")
    if ma5p >= ma20p and ma5 < ma20:
        score -= 2; reasons.append("MA5 死亡交叉 MA20")
    if ma5 > ma20:
        score += 1
    else:
        score -= 1
    if R is not None:
        if R < 30:
            score += 2; reasons.append(f"RSI={R:.0f} 超賣")
        elif R > 70:
            score -= 2; reasons.append(f"RSI={R:.0f} 超買")
    if K > D and K < 80:
        score += 1; reasons.append("KD 金叉")
    if K < D and K > 20:
        score -= 1; reasons.append("KD 死叉")
    if last > ma20 * 1.1:
        score -= 1; reasons.append("乖離 MA20 逾10%")

    if score >= 3:
        sig = "偏多"
    elif score <= -3:
        sig = "偏空"
    else:
        sig = "中性"
    ind = {"last": last, "ma5": ma5, "ma20": ma20, "rsi": R, "k": K, "d": D}
    return sig, score, reasons, ind


# ---------------- LINE 推播 ----------------
def line_push(text):
    token = os.environ.get("LINE_TOKEN", "").strip()
    uid = os.environ.get("LINE_USER_ID", "").strip()
    if not token or not uid:
        print("（未設定 LINE，略過推播）")
        return
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"to": uid, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        )
        print("LINE 推播狀態：", r.status_code)
    except Exception as e:
        print("LINE 推播失敗：", e)


def load_last_signal():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f).get("signal")
    except Exception:
        return None


def save_last_signal(sig):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"signal": sig, "time": time.strftime("%Y-%m-%d %H:%M")}, f,
                  ensure_ascii=False)


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stock", nargs="?", default=os.environ.get("STOCK_NO", "2330"))
    ap.add_argument("--months", type=int, default=4)
    ap.add_argument("--csv", action="store_true")
    args = ap.parse_args()

    stock = args.stock
    print(f"抓取 {stock} 近 {args.months} 個月…")
    hist = fetch_history(stock, args.months)
    if not hist:
        print("查無資料（僅支援上市股票，或代號錯誤）")
        sys.exit(1)

    rt = fetch_realtime(stock)
    prices = [p for _, p in hist]
    stock_name = stock
    if rt:
        stock_name, px = rt
        if abs(px - prices[-1]) > 1e-9:
            prices.append(px)

    # 輸出 prices_data.js
    payload = {
        "stock": f"{stock} {stock_name}",
        "updated": time.strftime("%Y-%m-%d %H:%M"),
        "prices": prices,
    }
    with open("prices_data.js", "w", encoding="utf-8") as f:
        f.write("window.TWSE_DATA = " + json.dumps(payload, ensure_ascii=False) + ";")
    print(f"已輸出 prices_data.js（{len(prices)} 筆）")

    if args.csv:
        with open(f"prices_{stock}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["日期", "收盤價"])
            w.writerows(hist)

    if len(prices) < 25:
        print("筆數不足，略過訊號判斷")
        return

    sig, score, reasons, ind = evaluate(prices)
    print(f"訊號：{sig}（評分 {score}） 最新 {ind['last']:.1f} "
          f"MA5 {ind['ma5']:.1f} MA20 {ind['ma20']:.1f} RSI {ind['rsi']:.0f}")

    # 訊號變化才推播
    last_sig = load_last_signal()
    if sig != last_sig and sig != "中性":
        msg = (f"📊 {stock} {stock_name}\n"
               f"訊號：{last_sig or '—'} → {sig}\n"
               f"最新價 {ind['last']:.1f}｜MA20 {ind['ma20']:.1f}｜RSI {ind['rsi']:.0f}\n"
               f"理由：{'、'.join(reasons) or '趨勢延續'}\n"
               f"（{payload['updated']} 技術面參考，非投資建議）")
        line_push(msg)
    elif sig != last_sig:
        print(f"訊號轉為中性（{last_sig} → 中性），不推播")
    else:
        print("訊號未變，不推播")
    save_last_signal(sig)


if __name__ == "__main__":
    main()
