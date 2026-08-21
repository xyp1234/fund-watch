import urllib.request, urllib.parse, json, re, os, time, datetime, statistics

# ===== 配置区 =====
FUNDS = [
    {"code": "005844", "name": "东方人工智能A", "buy_price": 0, "buy_date": "2026-07-30", "type": "trade", "amount": 164.92},
    {"code": "481015", "name": "工银战略性A", "buy_price": 0, "buy_date": "2026-07-30", "type": "trade", "amount": 123.61},
    {"code": "006479", "name": "广发纳指100联接A", "buy_price": 7.5087, "buy_date": "", "type": "watch", "amount": 317.64},
    {"code": "000218", "name": "国泰黄金联接A", "buy_price": 3.5427, "buy_date": "", "type": "watch", "amount": 335.00},
    {"code": "022364", "name": "华盈科技精选混合A", "buy_price": 3.6179, "buy_date": "", "type": "trade", "amount": 0,
     "staged": []},
]
RULES = {"take_profit": 18, "stop_loss": -10, "max_drawdown": 10}
STOP_LOSS_ALERT_THRESHOLD = -8  # 接近止损线时提醒（-8%）
SENDKEY = os.environ.get("SCT_SENDKEY", "")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# 事件日历: 已知风险节点 (start~end 窗口内预警)
EVENTS = [
    {"start": "2026-08-10", "end": "2026-08-31", "label": "中美PNTR贸易调查结论窗口", "risk": "high"},
    {"start": "2026-09-09", "end": "2026-09-18", "label": "美联储利率决议(9/17 02:00, 鹰派加息风险)", "risk": "high"},
    {"start": "2026-11-01", "end": "2026-11-12", "label": "中美临时关税停火协议到期", "risk": "high"},
]
# =================


def check_stop_loss_alert(fund_results, rules):
    """检查止损预警，返回需要提醒的基金列表"""
    alerts = []
    for r in fund_results:
        gain = r.get("gain", 0)
        code = r.get("code", "")
        name = r.get("name", "")
        current_nav = r.get("current_nav", 0)
        buy_price = r.get("buy_price", 0)
        
        # 计算止损价格
        stop_loss_price = buy_price * (1 + rules["stop_loss"] / 100)
        alert_price = buy_price * (1 + STOP_LOSS_ALERT_THRESHOLD / 100)
        
        # 检查是否接近止损线
        if gain <= STOP_LOSS_ALERT_THRESHOLD and gain > rules["stop_loss"]:
            alerts.append({
                "code": code,
                "name": name,
                "gain": gain,
                "current_nav": current_nav,
                "stop_loss_price": stop_loss_price,
                "alert_price": alert_price,
                "distance_to_stop": gain - rules["stop_loss"],
                "level": "warning"  # 接近止损
            })
        elif gain <= rules["stop_loss"]:
            alerts.append({
                "code": code,
                "name": name,
                "gain": gain,
                "current_nav": current_nav,
                "stop_loss_price": stop_loss_price,
                "alert_price": alert_price,
                "distance_to_stop": 0,
                "level": "critical"  # 已触发止损
            })
    
    return alerts


def send_stop_loss_alert(alerts):
    """发送止损预警通知"""
    if not alerts or not SENDKEY:
        return
    
    title = f"⚠️ 止损预警 - {len(alerts)}只基金接近止损线"
    
    content_parts = []
    content_parts.append("## 止损预警")
    content_parts.append("")
    
    for alert in alerts:
        level_emoji = "🔴" if alert["level"] == "critical" else "🟡"
        content_parts.append(f"### {level_emoji} {alert['code']} {alert['name']}")
        content_parts.append(f"- 当前收益: {alert['gain']:+.2f}%")
        content_parts.append(f"- 当前净值: {alert['current_nav']:.4f}")
        content_parts.append(f"- 止损价格: {alert['stop_loss_price']:.4f}")
        content_parts.append(f"- 距止损线: {alert['distance_to_stop']:.2f}%")
        
        if alert["level"] == "critical":
            content_parts.append("- **已触发止损线，建议立即清仓！**")
        else:
            content_parts.append(f"- 接近止损线，密切关注！")
        content_parts.append("")
    
    content_parts.append("---")
    content_parts.append("*此消息由止损预警系统自动发送*")
    
    content = "\n".join(content_parts)
    
    url = f"https://sctapi.ftqq.com/{SENDKEY}.send"
    data = urllib.parse.urlencode({"title": title, "desp": content}).encode()
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
        print(f"\n[止损预警推送] {resp.read().decode()}")
    except Exception as e:
        print(f"\n[止损预警推送失败] {e}")


def fetch_nav(code):
    url = "https://api.fund.eastmoney.com/f10/lsjz?" + urllib.parse.urlencode({
        "callback": "jQ", "fundCode": code, "pageIndex": 1, "pageSize": 90,
    })
    req = urllib.request.Request(url, headers={
        "Referer": "https://fundf10.eastmoney.com/",
        "User-Agent": "Mozilla/5.0",
    })
    raw = http_get(req, retries=3)
    if raw is None:
        return None
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


def http_get(req, retries=3, timeout=20):
    last_err = None
    for attempt in range(retries):
        try:
            raw = urllib.request.urlopen(req, timeout=timeout).read()
            return raw
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    print("http_get 失败: %s" % last_err)
    return None


def fetch_index_kline(symbol, days=60):
    url = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=" + symbol + "&scale=240&datalen=" + str(days))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = http_get(req, retries=3)
    if raw is None:
        return None
    data = json.loads(raw.decode("utf-8", errors="replace"))
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


def analyze_sentiment(kline):
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


def calc_momentum(history):
    if not history or len(history) < 2:
        return None, None, None, None
    closes = [h["nav"] for h in history]
    chg5 = (closes[0] - closes[4]) / closes[4] * 100 if len(closes) >= 5 else None
    chg20 = (closes[0] - closes[19]) / closes[19] * 100 if len(closes) >= 20 else None
    rets = []
    for i in range(min(20, len(closes) - 1)):
        rets.append((closes[i] - closes[i + 1]) / closes[i + 1])
    vol20 = statistics.pstdev(rets) * 100 if len(rets) >= 3 else None
    rets5 = rets[:5]
    vol5 = statistics.pstdev(rets5) * 100 if len(rets5) >= 3 else None
    vol_ratio = (vol5 / vol20) if (vol5 is not None and vol20) else None
    return chg5, chg20, vol20, vol_ratio


def _d(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d").date()


def check_events(today):
    t = _d(today)
    hits = []
    for ev in EVENTS:
        s = _d(ev["start"])
        e = _d(ev["end"])
        if s <= t <= e:
            days_left = (e - t).days
            hits.append({"label": ev["label"], "risk": ev.get("risk", "medium"), "days_left": days_left})
    return hits


def llm_analyze(prompt):
    body = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content":
                "你是专业的中国公募基金投资顾问，结合用户持有的基金和当前市场数据给出每日操作建议。"
                "必须只输出严格JSON，不要任何其他文字，格式如下：\n"
                '{"summary":"一句话总结今日总体判断","funds":{"基金代码":{"advice":"持有或卖出","ratio":0-1的小数表示卖出比例,持有则为0,"reason":"≤50字的中文理由"}}}'
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + DEEPSEEK_KEY,
        },
    )
    raw = urllib.request.urlopen(req, timeout=60).read()
    resp = json.loads(raw)
    return resp["choices"][0]["message"]["content"]


def build_llm_prompt(rules, sent_label, sent_data, tech_label, tech_data, events, fund_results, quant_recs):
    lines = ["以下是今天的基金与市场数据，请据此给出每日操作建议。"]
    lines.append(f"日期: {datetime.date.today().isoformat()}")
    lines.append(f"沪深300市场情绪: {sent_label}")
    if sent_data:
        lines.append(f"  沪深300={sent_data['current']:.0f}, 近5日{sent_data['chg5']:+.1f}%, 近20日{sent_data['chg20']:+.1f}%")
    lines.append(f"科创50科技板块情绪: {tech_label}")
    if tech_data:
        lines.append(f"  科创50={tech_data['current']:.0f}, 近5日{tech_data['chg5']:+.1f}%, 近20日{tech_data['chg20']:+.1f}%")
    if events:
        lines.append("近期风险事件: " + "; ".join(f"{e['label']}(剩{e['days_left']}天)" for e in events))
    lines.append(f"量化规则: 止盈+{rules['take_profit']}%, 止损{rules['stop_loss']}%, 回撤>{rules['max_drawdown']}%清仓")
    lines.append("持仓明细:")
    for r in fund_results:
        staged = r.get("staged")
        if staged:
            stages = sorted(staged, key=lambda s: s["at"])
            reached = [s for s in stages if r["gain"] >= s["at"]]
            nxt = next((s for s in stages if r["gain"] < s["at"]), None)
            stage_desc = "分批止盈档:" + ",".join(f"+{s['at']}%({s['action']})" for s in stages)
            if reached:
                stage_desc += f"|当前已触发:{','.join('+{}%'.format(s['at']) for s in reached)}"
                if nxt:
                    stage_desc += f"|下一档+{nxt['at']}%"
        else:
            stage_desc = "无"
        q = quant_recs.get(r["code"])
        quant_txt = ""
        if q:
            quant_txt = f"|量化建议:{q['advice']} 比例{q['ratio']:.0%}"
        chg5_txt = "N/A" if r["chg5"] is None else "{:+.1f}%".format(r["chg5"])
        chg20_txt = "N/A" if r["chg20"] is None else "{:+.1f}%".format(r["chg20"])
        lines.append(
            f"  {r['code']} {r['name']} 类型={r['type']} 成本={r['buy_price']:.4f} "
            f"最新={r['current_nav']:.4f} 收益={r['gain']:+.1f}% "
            f"5日={chg5_txt} 20日={chg20_txt} 回撤={r['drawdown']:.1f}% {stage_desc}{quant_txt}"
        )
    return "\n".join(lines)


def format_ratio(r):
    return f"{r*100:.0f}%"


def decide_sell(r, rules, sentiment, tech_label, events):
    gain = r["gain"]
    drawdown = r["drawdown"]
    chg5 = r["chg5"]
    vol_ratio = r["vol_ratio"]
    staged = r.get("staged")
    ratio = 0
    reasons = []

    # 1) 基础判断
    if gain <= rules["stop_loss"]:
        ratio = 1.0
        reasons.append(f"触及止损{rules['stop_loss']}%, 建议清仓")
    elif drawdown >= rules["max_drawdown"]:
        ratio = 1.0
        reasons.append(f"从高点回撤{drawdown:.1f}%> {rules['max_drawdown']}%, 建议清仓")
    elif staged:
        stages = sorted(staged, key=lambda s: s["at"])
        reached = [s for s in stages if gain >= s["at"]]
        nxt = next((s for s in stages if gain < s["at"]), None)
        if reached:
            ratio = 1.0 if reached[-1] is stages[-1] else 1 / 3
            reasons.append(f"已到分批止盈档+{reached[-1]['at']}%: {reached[-1]['action']}")
            if nxt:
                reasons.append(f"下一档+{nxt['at']}%: {nxt['action']}")
        else:
            reasons.append(f"距分批止盈第一档+{stages[0]['at']}%还差{stages[0]['at'] - gain:.1f}%")
    elif gain >= rules["take_profit"]:
        ratio = 1 / 2
        reasons.append(f"已超止盈+{rules['take_profit']}%")
        if sentiment == "强势" and (chg5 or 0) > 0:
            ratio = 1 / 3
            reasons.append("但市场强势, 只先锁1/3")
    elif gain >= rules["take_profit"] * 0.75:
        if sentiment == "弱势":
            ratio = 1 / 3
            reasons.append(f"接近止盈+{rules['take_profit']}%且市场弱势, 提前锁利")
        else:
            reasons.append(f"接近止盈+{rules['take_profit']}%, 持有观察")
    elif gain > 0:
        if sentiment == "弱势" and gain >= 8:
            ratio = 1 / 3
            reasons.append(f"市场弱势, 盈利{gain:.0f}%先落袋1/3")
        else:
            reasons.append(f"收益+{gain:.1f}%, 未到止盈, 持有")
    else:
        reasons.append(f"亏损{gain:.1f}%, 未到止损, 持有")

    # 2) 动量叠加: 短期急涨/急跌
    if chg5 is not None:
        if chg5 > 5:
            if ratio > 0:
                ratio = min(1.0, ratio + 1 / 3)
            else:
                ratio = 1 / 3
            reasons.append(f"5日涨{chg5:.1f}%过快, 有获利回吐风险")
        elif chg5 < -5 and gain > 5:
            ratio = max(ratio, 1 / 2)
            reasons.append(f"5日急跌{chg5:.1f}%, 利润在回吐, 保利润")
        elif chg5 < -5 and gain <= 0:
            ratio = max(ratio, 1 / 3)
            reasons.append(f"5日急跌{chg5:.1f}%, 弱势加剧")

    # 3) 波动率异常(黑天鹅预警)
    if vol_ratio and vol_ratio > 2.0:
        if ratio > 0:
            ratio = min(1.0, ratio + 1 / 6)
        reasons.append(f"近5日波动是20日的{vol_ratio:.1f}倍, 波动异常, 警惕黑天鹅")

    # 4) 事件日历叠加
    for ev in events:
        reasons.append(f"[事件] {ev['label']} 剩{ev['days_left']}天")
        if ev["risk"] == "high" and gain > 15:
            ratio = max(ratio, 1 / 3)
            reasons.append(f"事件风险高且盈利{gain:.0f}%, 至少锁利1/3")

    if ratio <= 0:
        return {"advice": "持有", "ratio": 0, "reasons": reasons}
    if ratio >= 1:
        return {"advice": "清仓", "ratio": 1.0, "reasons": reasons}
    return {"advice": f"卖出{format_ratio(ratio)}", "ratio": ratio, "reasons": reasons}


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

    chg5, chg20, vol20, vol_ratio = calc_momentum(history)

    return {
        "name": name, "code": code, "today": today,
        "current_nav": current_nav, "buy_price": buy_price,
        "buy_date": buy_date, "gain": gain, "peak": peak,
        "drawdown": drawdown, "type": fund_type,
        "chg5": chg5, "chg20": chg20, "vol20": vol20, "vol_ratio": vol_ratio,
        "staged": fund.get("staged"), "amount": fund.get("amount", 0),
    }


def main():
    today = datetime.date.today().isoformat()
    lines = []
    lines.append(f"基金日报 - {today}")
    lines.append("=" * 40)

    hs = fetch_index_kline("sh000300", 60)
    tech = fetch_index_kline("sh000688", 60)
    sent_label, sent_score, sent_data = analyze_sentiment(hs)
    tech_label, tech_score, tech_data = analyze_sentiment(tech)

    lines.append(f"\n市场情绪: {sent_label} (沪深300)")
    if sent_data:
        lines.append(f"  沪深300: {sent_data['current']:.0f} | MA10 {sent_data['ma10']:.0f} | MA30 {sent_data['ma30']:.0f}")
        lines.append(f"  近5日 {sent_data['chg5']:+.1f}% | 近20日 {sent_data['chg20']:+.1f}%")
    lines.append(f"\n科技板块: {tech_label} (科创50)")
    if tech_data:
        lines.append(f"  科创50: {tech_data['current']:.0f} | 近5日 {tech_data['chg5']:+.1f}% | 近20日 {tech_data['chg20']:+.1f}%")

    events = check_events(today)
    if events:
        lines.append("\n风险事件:")
        for ev in events:
            lines.append(f"  ⚠ {ev['label']} 剩{ev['days_left']}天")

    rules = get_dynamic_rules(sent_label)
    lines.append(f"\n当前规则: 止盈+{rules['take_profit']}% | 止损{rules['stop_loss']}% | 回撤>{rules['max_drawdown']}%清仓")

    need_alert = False
    fund_results = []
    quant_recs = {}
    fund_names = {f["code"]: f["name"] for f in FUNDS}
    for fund in FUNDS:
        r = analyze_fund(fund, rules)
        if not r:
            lines.append(f"\n{f['name']} - 获取净值失败")
            continue
        fund_results.append(r)
        lines.append(f"\n{r['name']} ({r['code']})")
        lines.append(f"  最新: {r['current_nav']:.4f} ({r['today']}) | 收益: {'+' if r['gain']>=0 else ''}{r['gain']:.2f}%")
        if r['chg5'] is not None:
            lines.append(f"  近5日: {r['chg5']:+.1f}% | 近20日: {r['chg20']:+.1f}% | 回撤: {r['drawdown']:.1f}%")

        if r['type'] == 'watch':
            if r['gain'] < 0:
                lines.append(f"  >> [观察] 亏损{r['gain']:.1f}% (定投/避险品种, 按计划继续)")
            else:
                lines.append(f"  >> [观察] 收益+{r['gain']:.1f}% (定投/避险品种, 按计划继续)")
        else:
            rec = decide_sell(r, rules, sent_label, tech_label, events)
            quant_recs[r["code"]] = {"advice": rec["advice"], "ratio": rec["ratio"]}
            mv = r['amount'] * (1 + r['gain'] / 100)
            action_tag = "卖出" if rec["ratio"] > 0 else "持有"
            if rec["advice"] == "持有":
                lines.append(f"  >> [建议] 持有")
            else:
                part = mv * rec["ratio"]
                lines.append(f"  >> [建议] {rec['advice']} (当前市值约{mv:.0f}元, 约卖出{part:.0f}元)")
            for reason in rec["reasons"]:
                lines.append(f"     · {reason}")
            if rec["ratio"] >= 1 / 3:
                need_alert = True

    if DEEPSEEK_KEY and fund_results:
        lines.append("\n" + "=" * 40)
        lines.append("AI分析 (DeepSeek)")
        try:
            prompt = build_llm_prompt(rules, sent_label, sent_data, tech_label, tech_data, events, fund_results, quant_recs)
            ai_raw = llm_analyze(prompt)
            ai = json.loads(ai_raw)
            lines.append(f"总体: {ai.get('summary', '')}")
            for code, rec in ai.get("funds", {}).items():
                advice = rec.get("advice", "持有")
                ratio = rec.get("ratio", 0)
                reason = rec.get("reason", "")
                ratio_txt = f"{ratio*100:.0f}%" if ratio and ratio > 0 else "不卖"
                lines.append(f"  {code} {fund_names.get(code, '')}: {advice} {ratio_txt}")
                lines.append(f"     AI理由: {reason}")
                if ratio and ratio >= 0.5:
                    need_alert = True
        except Exception as e:
            lines.append(f"  [AI调用失败, 使用量化判断] {e}")

    output = "\n".join(lines)
    print(output)

    # 检查止损预警
    stop_loss_alerts = check_stop_loss_alert(fund_results, rules)
    if stop_loss_alerts:
        send_stop_loss_alert(stop_loss_alerts)
        lines.append("\n" + "=" * 40)
        lines.append("⚠️ 止损预警")
        for alert in stop_loss_alerts:
            level_emoji = "🔴" if alert["level"] == "critical" else "🟡"
            lines.append(f"  {level_emoji} {alert['code']} {alert['name']}: {alert['gain']:+.2f}% (距止损{alert['distance_to_stop']:.2f}%)")
        output = "\n".join(lines)

    if not SENDKEY:
        print("\n[SCT_SENDKEY 未设置, 跳过推送]")
        return

    title = f"{'[ALERT]' if need_alert else '[DAILY]'} 基金日报 - {sent_label}/{tech_label} - {today}"
    url = f"https://sctapi.ftqq.com/{SENDKEY}.send"
    data = urllib.parse.urlencode({"title": title, "desp": output}).encode()
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
        print(f"\n[推送结果] {resp.read().decode()}")
    except Exception as e:
        print(f"\n[推送失败] {e}")


if __name__ == "__main__":
    main()
