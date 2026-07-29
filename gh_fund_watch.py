import urllib.request, urllib.parse, json, re, os, datetime

# ===== 配置区 =====
FUNDS = [
    {"code": "005844", "name": "东方人工智能A", "buy_price": 3.3056},
    {"code": "481015", "name": "工银战略性A", "buy_price": 9.2570},
    {"code": "021179", "name": "易方达产业机遇A", "buy_price": 3.0661},
]
RULES = {"take_profit": 20, "stop_loss": -10, "max_drawdown": 10}
SENDKEY = os.environ.get("SCT_SENDKEY", "")
# =================


def fetch_nav(code):
    url = "https://api.fund.eastmoney.com/f10/lsjz?" + urllib.parse.urlencode({
        "callback": "jQ", "fundCode": code, "pageIndex": 1, "pageSize": 90,
    })
    req = urllib.request.Request(url, headers={
        "Referer": "https://fundf10.eastmoney.com/",
        "User-Agent": "Mozilla/5.0",
    })
    raw = urllib.request.urlopen(req, timeout=15).read()
    text = raw.decode("gbk", errors="replace")
    m = re.search(r'"LSJZList":(\[.*?\]),', text)
    if not m:
        return None
    items = json.loads(m.group(1))
    result = []
    for item in items:
        nav = item.get("DWJZ", "")
        date = item.get("FSRQ", "")
        if nav and date:
            result.append({"date": date, "nav": float(nav)})
    return result


def analyze_fund(fund):
    code = fund["code"]
    buy_price = fund["buy_price"]
    name = fund["name"]
    history = fetch_nav(code)
    if not history:
        return None

    latest = history[0]
    current_nav = latest["nav"]
    today = latest["date"]
    gain = (current_nav - buy_price) / buy_price * 100

    peak = None
    for h in history:
        if h["date"] >= "2026-07-29":
            if peak is None or h["nav"] > peak:
                peak = h["nav"]

    drawdown = 0
    if peak and peak > current_nav:
        drawdown = (peak - current_nav) / peak * 100

    actions = []
    if gain >= RULES["take_profit"]:
        actions.append(f"[止盈] 达到目标 +{RULES['take_profit']}%, 建议卖出")
    elif gain <= RULES["stop_loss"]:
        actions.append(f"[止损] 触及止损 {RULES['stop_loss']}%, 建议卖出")
    elif drawdown >= RULES["max_drawdown"]:
        actions.append(f"[回撤] 从高点回撤 {drawdown:.1f}%, 建议卖出")
    elif gain >= 15:
        actions.append(f"[接近止盈] 收益+{gain:.1f}%, 接近目标")
    elif gain < 0:
        actions.append(f"[持有] 亏损{gain:.1f}%, 距止损还有{gain-RULES['stop_loss']:.1f}%")
    else:
        actions.append(f"[持有] 收益+{gain:.1f}%")

    return {
        "name": name, "code": code, "today": today,
        "current_nav": current_nav, "buy_price": buy_price,
        "gain": gain, "peak": peak, "drawdown": drawdown, "actions": actions,
    }


def main():
    lines = []
    lines.append(f"基金卖出提醒 - {datetime.date.today()}")
    lines.append("=" * 40)

    need_alert = False
    for fund in FUNDS:
        r = analyze_fund(fund)
        if not r:
            lines.append(f"\n{f['name']} - 获取净值失败")
            continue
        lines.append(f"\n{r['name']} ({r['code']})")
        lines.append(f"  买入: {r['buy_price']:.4f}")
        lines.append(f"  最新: {r['current_nav']:.4f} ({r['today']})")
        lines.append(f"  收益: {'+' if r['gain']>=0 else ''}{r['gain']:.2f}%")
        if r['peak']:
            lines.append(f"  回撤: {r['drawdown']:.2f}%")
        for a in r["actions"]:
            lines.append(f"  >> {a}")
            if a.startswith("[止盈]") or a.startswith("[止损]") or a.startswith("[回撤]"):
                need_alert = True

    lines.append(f"\n{'=' * 40}")
    lines.append("止盈+20% | 止损-10% | 回撤超10%卖出")

    output = "\n".join(lines)
    print(output)

    if need_alert and SENDKEY:
        url = f"https://sct.ftqq.com/{SENDKEY}.send"
        data = urllib.parse.urlencode({
            "title": f"基金提醒 - {datetime.date.today()}",
            "desp": output,
        }).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)


if __name__ == "__main__":
    main()
