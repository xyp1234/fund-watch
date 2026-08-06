import urllib.request, urllib.parse, json, re, os, datetime

# ===== 配置区 =====
FUNDS = [
    {"code": "005844", "name": "东方人工智能A", "buy_price": 0, "buy_date": "2026-07-30", "type": "trade"},
    {"code": "481015", "name": "工银战略性A", "buy_price": 0, "buy_date": "2026-07-30", "type": "trade"},
    {"code": "006479", "name": "广发纳指100联接A", "buy_price": 7.5087, "buy_date": "", "type": "watch"},
    {"code": "000218", "name": "国泰黄金联接A", "buy_price": 3.5427, "buy_date": "", "type": "watch"},
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


def fetch_index_kline(days=60):
    url = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=sh000300&scale=240&datalen=" + str(days))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=15).read()
    data = json.loads(raw)
    if not isinstance(data, list):
        return None
    result = []
    for d in data:
        result.append({
            "date": d["day"][:10],
            "close": float(d["close"]),
            "ma5": float(d.get("ma_price5", 0)),
            "ma10": float(d.get("ma_price10", 0)),
            "ma30": float(d.get("ma_price30", 0)),
        })
    return result


def analyze_sentiment():
    kline = fetch_index_kline(60)
    if not kline or len(kline) < 5:
        return "中性", 0, None

    latest = kline[-1]
    current = latest["close"]

    closes = [k["close"] for k in kline]
    ma10 = latest.get("ma10", sum(closes[-10:]) / 10) if len(closes) >= 10 else sum(closes) / len(closes)
    ma30 = latest.get("ma30", sum(closes[-30:]) / 30) if len(closes) >= 30 else sum(closes) / len(closes)

    chg5 = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
    chg20 = (closes[-1] - closes[-20]) / closes[-20] * 100 if len(closes) >= 20 else chg5

    score = 0
    if current > ma10:
        score += 2
    if current > ma30:
        score += 2
    if chg5 > 2:
        score += 2
    elif chg5 > 0:
        score += 1
    elif chg5 < -2:
        score -= 2
    elif chg5 < 0:
        score -= 1
    if chg20 > 5:
        score += 2
    elif chg20 > 2:
        score += 1
    elif chg20 < -5:
        score -= 2
    elif chg20 < -2:
        score -= 1

    if score >= 4:
        label = "强势"
    elif score <= -3:
        label = "弱势"
    else:
        label = "中性"

    return label, score, {"current": current, "ma10": ma10, "ma30": ma30, "chg5": chg5, "chg20": chg20}


def get_dynamic_rules(sentiment):
    rules = dict(RULES)
    if sentiment == "强势":
        rules["take_profit"] = 30
        rules["stop_loss"] = -15
        rules["max_drawdown"] = 15
    elif sentiment == "弱势":
        rules["take_profit"] = 15
        rules["stop_loss"] = -8
        rules["max_drawdown"] = 8
    return rules


def analyze_fund(fund, rules):
    code = fund["code"]
    buy_price = fund["buy_price"]
    name = fund["name"]
    buy_date = fund.get("buy_date", "")
    fund_type = fund.get("type", "trade")
    history = fetch_nav(code)
    if not history:
        return None

    if buy_price == 0 and buy_date:
        for h in history:
            if h["date"] == buy_date:
                buy_price = h["nav"]
                fund["buy_price"] = buy_price
                break
        if buy_price == 0:
            buy_price = history[0]["nav"]
            fund["buy_price"] = buy_price
            buy_date = history[0]["date"]

    latest = history[0]
    current_nav = latest["nav"]
    today = latest["date"]
    gain = (current_nav - buy_price) / buy_price * 100

    peak = buy_price
    for h in history:
        if buy_date and h["date"] >= buy_date:
            if h["nav"] > peak:
                peak = h["nav"]

    drawdown = 0
    if peak and peak > current_nav:
        drawdown = (peak - current_nav) / peak * 100

    actions = []
    if fund_type == "watch":
        if gain < 0:
            actions.append(f"[观察] 亏损{gain:.1f}% (定投/避险品种，按计划继续)")
        else:
            actions.append(f"[观察] 收益+{gain:.1f}% (定投/避险品种，按计划继续)")
    elif gain >= rules["take_profit"]:
        actions.append(f"[止盈] 收益+{gain:.1f}%, 超过目标+{rules['take_profit']}%, 建议卖出")
    elif gain <= rules["stop_loss"]:
        actions.append(f"[止损] 亏损{gain:.1f}%, 触及止损{rules['stop_loss']}%, 建议卖出")
    elif drawdown >= rules["max_drawdown"]:
        actions.append(f"[回撤] 从高点回撤{drawdown:.1f}%, 超过{rules['max_drawdown']}%, 建议卖出")
    elif gain >= rules["take_profit"] * 0.75:
        actions.append(f"[接近止盈] 收益+{gain:.1f}%, 接近目标+{rules['take_profit']}%")
    elif gain < 0:
        actions.append(f"[持有] 亏损{gain:.1f}%, 距止损还有{gain - rules['stop_loss']:.1f}%")
    else:
        actions.append(f"[持有] 收益+{gain:.1f}%")

    return {
        "name": name, "code": code, "today": today,
        "current_nav": current_nav, "buy_price": buy_price,
        "buy_date": buy_date, "gain": gain, "peak": peak,
        "drawdown": drawdown, "actions": actions, "type": fund_type,
    }


def main():
    lines = []
    lines.append(f"基金日报 - {datetime.date.today()}")
    lines.append("=" * 40)

    sentiment_label, sentiment_score, sent_data = analyze_sentiment()
    lines.append(f"\n市场情绪: {sentiment_label}")
    if sent_data:
        lines.append(f"  沪深300: {sent_data['current']:.0f}")
        lines.append(f"  10日均线: {sent_data['ma10']:.0f}  30日均线: {sent_data['ma30']:.0f}")
        lines.append(f"  近5日: {sent_data['chg5']:+.2f}%  近20日: {sent_data['chg20']:+.2f}%")
    lines.append("")

    rules = get_dynamic_rules(sentiment_label)
    lines.append(f"当前规则: 止盈+{rules['take_profit']}% | 止损{rules['stop_loss']}% | 回撤>{rules['max_drawdown']}%卖出")

    need_alert = False
    for fund in FUNDS:
        r = analyze_fund(fund, rules)
        if not r:
            lines.append(f"\n{f['name']} - 获取净值失败")
            continue
        lines.append(f"\n{r['name']} ({r['code']})")
        if r['type'] == 'watch':
            lines.append(f"  成本价: {r['buy_price']:.4f}")
        else:
            lines.append(f"  买入价: {r['buy_price']:.4f} ({r['buy_date']})")
        lines.append(f"  最新: {r['current_nav']:.4f} ({r['today']})")
        lines.append(f"  收益: {'+' if r['gain']>=0 else ''}{r['gain']:.2f}%")
        if r['peak']:
            lines.append(f"  回撤: {r['drawdown']:.2f}%")
        for a in r["actions"]:
            lines.append(f"  >> {a}")
            if a.startswith("[止盈]") or a.startswith("[止损]") or a.startswith("[回撤]"):
                need_alert = True

    output = "\n".join(lines)
    print(output)

    if not SENDKEY:
        print("\n[SCT_SENDKEY 未设置，跳过推送]")
        return

    title = f"{'[ALERT]' if need_alert else '[DAILY]'} 基金日报 - {sentiment_label} - {datetime.date.today()}"
    url = f"https://sctapi.ftqq.com/{SENDKEY}.send"
    data = urllib.parse.urlencode({"title": title, "desp": output}).encode()
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
        print(f"\n[推送结果] {resp.read().decode()}")
    except Exception as e:
        print(f"\n[推送失败] {e}")


if __name__ == "__main__":
    main()
