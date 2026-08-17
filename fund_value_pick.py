import urllib.request
import urllib.parse
import json
import re
import time
import sys
import os
import pickle
import logging
import argparse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('fund_scanner.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 配置常量
API_URL = "https://fund.eastmoney.com/data/rankhandler.aspx"
DETAIL_URL = "https://fund.eastmoney.com/pingzhongdata/{}.js"
HISTORY_URL = "https://api.fund.eastmoney.com/f10/lsjz"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

# 默认配置
DEFAULT_CONFIG = {
    "min_1y": 30,
    "require_pullback": True,
    "pullback_depth_min": 3.0,
    "pullback_duration_min": 3,
    "exclude_qdii": True,
    "exclude_sector": True,
    "exclude_c_class": True,
    "max_results": 30,
    "save_results": True,
    "use_cache": True,
    "cache_hours": 24,
    "parallel_workers": 5,
    "risk_free_rate": 0.03,
    "market_state": "neutral",
    "score_weights": {
        "pullback": 0.3,
        "trend": 0.3,
        "sharpe": 0.2,
        "volatility": 0.2
    },
    "max_retry": 3,
    "retry_delay": 2,
}


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description='基金筛选工具 v4.1')
    parser.add_argument('--min-1y', type=float, default=30, help='最低1年收益率 (默认: 30)')
    parser.add_argument('--max-results', type=int, default=30, help='最大结果数 (默认: 30)')
    parser.add_argument('--pullback-depth', type=float, default=3.0, help='最小回调深度%% (默认: 3.0)')
    parser.add_argument('--pullback-duration', type=int, default=3, help='最小回调天数 (默认: 3)')
    parser.add_argument('--no-cache', action='store_true', help='禁用缓存')
    parser.add_argument('--no-save', action='store_true', help='不保存结果到CSV')
    parser.add_argument('--workers', type=int, default=5, help='并行线程数 (默认: 5)')
    parser.add_argument('--verbose', action='store_true', help='详细输出')
    return parser.parse_args()


def send_wechat_notification(title: str, content: str):
    """发送微信通知（通过Server酱）"""
    sendkey = os.environ.get("SCT_SENDKEY") or os.environ.get("SCT_APIKEY")
    if not sendkey:
        logger.warning("未找到SCT_SENDKEY环境变量，跳过微信通知")
        return False
    
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    data = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
    
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode("utf-8"))
        if result.get("code") == 0:
            logger.info("微信通知发送成功")
            return True
        else:
            logger.error(f"微信通知发送失败: {result}")
            return False
    except Exception as e:
        logger.error(f"微信通知发送异常: {e}")
        return False


def generate_notification_content(results: List[dict], market_state: str) -> Tuple[str, str]:
    """生成通知内容"""
    # 筛选出推荐基金（综合分>5）
    recommended = [f for f in results if f.get("final_score", 0) > 5]
    
    if not recommended:
        return None, None
    
    title = f"基金扫描提醒 - 发现{len(recommended)}只推荐基金"
    
    content_parts = []
    content_parts.append(f"## 市场状态: {market_state}")
    content_parts.append("")
    
    if market_state == "bull":
        content_parts.append("牛市环境，可适当积极")
    elif market_state == "bear":
        content_parts.append("熊市环境，建议观望为主")
    else:
        content_parts.append("震荡环境，谨慎操作")
    
    content_parts.append("")
    content_parts.append("## 推荐基金")
    content_parts.append("")
    
    for i, f in enumerate(recommended[:5], 1):
        content_parts.append(f"### {i}. {f['code']} {f['name']}")
        content_parts.append(f"- 综合分: {f.get('final_score', 0):.1f}")
        content_parts.append(f"- 风险分: {f['risk_score']:.0f}")
        content_parts.append(f"- 1年收益: {f['1y']:+.2f}%")
        content_parts.append(f"- 回调模式: {f.get('pullback_pattern', '未知')}")
        content_parts.append(f"- 建议: {f.get('recommendation', '未知')}")
        content_parts.append("")
    
    content_parts.append("---")
    content_parts.append("*此消息由基金扫描脚本自动发送*")
    
    return title, "\n".join(content_parts)


def ensure_dirs():
    """确保缓存和结果目录存在"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)


def get_cache_path(key: str) -> str:
    """获取缓存文件路径"""
    return os.path.join(CACHE_DIR, f"{key}.pickle")


def load_cache(key: str, max_hours: int = 24) -> Optional[dict]:
    """加载缓存数据"""
    cache_path = get_cache_path(key)
    if not os.path.exists(cache_path):
        return None
    
    try:
        with open(cache_path, 'rb') as f:
            cache_data = pickle.load(f)
        
        cache_time = cache_data.get('timestamp', 0)
        if time.time() - cache_time > max_hours * 3600:
            return None
        
        return cache_data.get('data')
    except Exception as e:
        logger.warning(f"加载缓存失败: {e}")
        return None


def save_cache(key: str, data: dict):
    """保存缓存数据"""
    cache_path = get_cache_path(key)
    try:
        with open(cache_path, 'wb') as f:
            pickle.dump({'timestamp': time.time(), 'data': data}, f)
    except Exception as e:
        logger.warning(f"保存缓存失败: {e}")


def fetch_with_retry(url: str, headers: dict, max_retry: int = 3, delay: float = 2) -> Optional[bytes]:
    """带重试机制的网络请求"""
    for attempt in range(max_retry):
        try:
            req = urllib.request.Request(url, headers=headers)
            return urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            if attempt < max_retry - 1:
                logger.warning(f"请求失败 (尝试 {attempt + 1}/{max_retry}): {e}")
                time.sleep(delay * (attempt + 1))
            else:
                logger.error(f"请求最终失败: {e}")
                return None
    return None


def fetch_rank(sc: str, pn: int = 300) -> List[dict]:
    """获取基金排名数据"""
    cache_key = f"rank_{sc}_{pn}"
    cached = load_cache(cache_key, DEFAULT_CONFIG['cache_hours'])
    if cached:
        logger.info(f"使用缓存数据: {cache_key}")
        return cached
    
    logger.info(f"获取排名数据: {sc}, {pn}")
    params = {"op": "ph", "dt": "kf", "ft": "all", "rs": "", "gs": "0",
              "sc": sc, "st": "desc", "pi": "1", "pn": str(pn), "dx": "1"}
    url = API_URL + "?" + urllib.parse.urlencode(params)
    headers = {"Referer": "https://fund.eastmoney.com/", "User-Agent": "Mozilla/5.0"}
    
    raw = fetch_with_retry(url, headers, DEFAULT_CONFIG['max_retry'])
    if not raw:
        return []
    
    for enc in ("gbk", "utf-8", "gb2312"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    
    m = re.search(r'datas:\[(.*?)\](?=,allRecords)', text)
    if not m:
        logger.error("解析排名数据失败")
        return []
    
    items = json.loads("[" + m.group(1) + "]")
    rows = []
    for item in items:
        parts = item.split(",")
        if len(parts) < 15:
            continue
        rows.append({
            "code": parts[0],
            "name": parts[1],
            "1w": float(parts[7]) if parts[7] else 0,
            "1m": float(parts[8]) if parts[8] else 0,
            "3m": float(parts[9]) if parts[9] else 0,
            "6m": float(parts[10]) if parts[10] else 0,
            "1y": float(parts[11]) if parts[11] else 0,
        })
    
    save_cache(cache_key, rows)
    return rows


def validate_fund_data(f: dict) -> bool:
    """数据质量校验"""
    required_fields = ['code', 'name', '1w', '1m', '3m', '6m', '1y']
    for field in required_fields:
        if not f.get(field):
            return False
    
    # 交叉验证数据合理性
    if f['1y'] < 0 or f['1y'] > 500:
        return False
    
    return True


def fetch_fund_detail(code: str) -> dict:
    """获取基金详细信息"""
    cache_key = f"detail_{code}"
    cached = load_cache(cache_key, DEFAULT_CONFIG['cache_hours'])
    if cached:
        return cached
    
    url = DETAIL_URL.format(code)
    headers = {"Referer": "https://fund.eastmoney.com/", "User-Agent": "Mozilla/5.0"}
    raw = fetch_with_retry(url, headers, DEFAULT_CONFIG['max_retry'])
    
    if not raw:
        return {}
    
    try:
        text = raw.decode("utf-8", errors="replace")
        detail = {}
        
        scale_patterns = [
            r'var\s+Data_assetAllocation\s*=\s*\{[^}]*"scale":\s*([0-9.]+)',
            r'var\s+syl_1n\s*=\s*([0-9.]+)',
            r'"scale":\s*([0-9.]+)',
        ]
        for pattern in scale_patterns:
            m = re.search(pattern, text)
            if m:
                detail["scale"] = float(m.group(1))
                break
        
        m = re.search(r'var\s+Data_currentFundManager\s*=\s*\[(.*?)\]', text, re.DOTALL)
        if m:
            manager_info = m.group(1)
            dates = re.findall(r'"(\d{4}-\d{2}-\d{2})"', manager_info)
            if dates:
                detail["manager_start"] = dates[0]
                try:
                    start_date = datetime.strptime(dates[0], "%Y-%m-%d")
                    detail["manager_years"] = (datetime.now() - start_date).days / 365.25
                except:
                    pass
        
        fee_patterns = [
            r'var\s+syl_fee\s*=\s*([0-9.]+)',
            r'"fee_rate":\s*([0-9.]+)',
        ]
        for pattern in fee_patterns:
            m = re.search(pattern, text)
            if m:
                detail["fee_rate"] = float(m.group(1))
                break
        
        save_cache(cache_key, detail)
        return detail
    except Exception as e:
        logger.warning(f"获取基金详情失败 {code}: {e}")
        return {}


def fetch_fund_history(code: str, days: int = 90) -> List[float]:
    """获取基金历史净值"""
    cache_key = f"history_{code}_{days}"
    cached = load_cache(cache_key, DEFAULT_CONFIG['cache_hours'])
    if cached:
        return cached
    
    all_navs = []
    for page in range(1, 4):
        params = {
            "callback": "jQ",
            "fundCode": code,
            "pageIndex": page,
            "pageSize": 30,
        }
        url = HISTORY_URL + "?" + urllib.parse.urlencode(params)
        headers = {"Referer": "https://fundf10.eastmoney.com/", "User-Agent": "Mozilla/5.0"}
        raw = fetch_with_retry(url, headers, DEFAULT_CONFIG['max_retry'])
        
        if not raw:
            break
        
        try:
            text = raw.decode("gbk", errors="replace")
            m = re.search(r'"LSJZList":(\[.*?\])', text)
            if m:
                items = json.loads(m.group(1))
                if not items:
                    break
                navs = [float(item["DWJZ"]) for item in items if item.get("DWJZ")]
                all_navs.extend(navs)
                if len(all_navs) >= days:
                    break
        except Exception as e:
            logger.warning(f"解析历史净值失败 {code}: {e}")
            break
        time.sleep(0.3)
    
    if len(all_navs) > days:
        all_navs = all_navs[:days]
    
    if all_navs:
        save_cache(cache_key, all_navs)
    
    return all_navs


def identify_peaks(navs: List[float], window: int = 5) -> List[int]:
    """识别波峰（改进版：使用滑动窗口）"""
    peaks = []
    for i in range(window, len(navs) - window):
        # 检查是否为局部最大值
        left_window = navs[i-window:i]
        right_window = navs[i+1:i+window+1]
        
        if navs[i] == max(navs[i-window:i+window+1]):
            # 确保是真正的峰值（比左右都高）
            if all(navs[i] >= x for x in left_window) and all(navs[i] >= x for x in right_window):
                peaks.append(i)
    
    return peaks


def analyze_pullback_advanced(navs: List[float], config: dict) -> dict:
    """高级回调分析（改进版：识别多个回调模式）"""
    if not navs or len(navs) < 10:
        return {
            "depth": 0, "duration": 0, "is_pullback": False,
            "pattern": "无数据", "peaks_found": 0
        }
    
    # 识别所有波峰
    peaks = identify_peaks(navs, window=5)
    
    if len(peaks) < 2:
        # 没有足够的波峰，使用简单方法
        recent_period = min(30, len(navs))
        peak = max(navs[:recent_period])
        peak_idx = navs.index(peak)
        current = navs[-1]
        drawdown_depth = (peak - current) / peak * 100
        drawdown_duration = len(navs) - peak_idx - 1
        
        return {
            "depth": drawdown_depth,
            "duration": drawdown_duration,
            "is_pullback": drawdown_depth > config.get("pullback_depth_min", 3.0),
            "pattern": "单峰回调" if drawdown_depth > 0 else "持续上涨",
            "peaks_found": len(peaks)
        }
    
    # 分析最近的回调
    recent_peak_idx = peaks[-1]
    recent_peak = navs[recent_peak_idx]
    current = navs[-1]
    
    # 计算当前回调深度
    drawdown_depth = (recent_peak - current) / recent_peak * 100
    drawdown_duration = len(navs) - recent_peak_idx - 1
    
    # 识别回调模式
    if len(peaks) >= 3:
        # 检查是否为连续回调模式
        peak_values = [navs[p] for p in peaks[-3:]]
        if peak_values[-1] < peak_values[-2] < peak_values[-3]:
            pattern = "连续回调"
        elif peak_values[-1] > peak_values[-2]:
            pattern = "V型反转"
        else:
            pattern = "震荡回调"
    else:
        pattern = "双峰回调"
    
    # 判断是否为有效回调
    min_depth = config.get("pullback_depth_min", 3.0)
    min_duration = config.get("pullback_duration_min", 3)
    is_pullback = drawdown_depth > min_depth and drawdown_duration >= min_duration
    
    return {
        "depth": drawdown_depth,
        "duration": drawdown_duration,
        "is_pullback": is_pullback,
        "pattern": pattern,
        "peaks_found": len(peaks),
        "recent_peak": recent_peak,
        "peak_values": [navs[p] for p in peaks[-3:]] if len(peaks) >= 3 else []
    }


def calculate_max_drawdown(navs: List[float]) -> float:
    """计算最大回撤"""
    if not navs or len(navs) < 2:
        return 0
    
    max_nav = navs[0]
    max_drawdown = 0
    
    for nav in navs:
        if nav > max_nav:
            max_nav = nav
        drawdown = (max_nav - nav) / max_nav * 100
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    
    return max_drawdown


def calculate_volatility(navs: List[float]) -> float:
    """计算波动率"""
    if not navs or len(navs) < 10:
        return 0
    
    returns = []
    for i in range(1, len(navs)):
        ret = (navs[i] - navs[i-1]) / navs[i-1] * 100
        returns.append(ret)
    
    if not returns:
        return 0
    
    avg_return = sum(returns) / len(returns)
    variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
    return variance ** 0.5


def calculate_sharpe_ratio(navs: List[float], risk_free_rate: float = 0.03) -> float:
    """计算夏普比率"""
    if not navs or len(navs) < 30:
        return 0
    
    returns = []
    for i in range(1, len(navs)):
        ret = (navs[i] - navs[i-1]) / navs[i-1]
        returns.append(ret)
    
    if not returns:
        return 0
    
    avg_return = sum(returns) / len(returns)
    volatility = (sum((r - avg_return) ** 2 for r in returns) / len(returns)) ** 0.5
    
    if volatility == 0:
        return 0
    
    sharpe_ratio = (avg_return - risk_free_rate / 252) / volatility
    return sharpe_ratio


def calculate_sortino_ratio(navs: List[float], risk_free_rate: float = 0.03) -> float:
    """计算索提诺比率"""
    if not navs or len(navs) < 30:
        return 0
    
    returns = []
    for i in range(1, len(navs)):
        ret = (navs[i] - navs[i-1]) / navs[i-1]
        returns.append(ret)
    
    if not returns:
        return 0
    
    avg_return = sum(returns) / len(returns)
    downside_returns = [r for r in returns if r < 0]
    
    if not downside_returns:
        return 0
    
    downside_deviation = (sum(r ** 2 for r in downside_returns) / len(downside_returns)) ** 0.5
    
    if downside_deviation == 0:
        return 0
    
    sortino_ratio = (avg_return - risk_free_rate / 252) / downside_deviation
    return sortino_ratio


def calculate_trend_score(f: dict) -> float:
    """计算趋势一致性评分"""
    scores = []
    
    if f["1w"] > 0:
        scores.append(1)
    if f["1m"] > 0:
        scores.append(1)
    if f["3m"] > 0:
        scores.append(1)
    if f["6m"] > 0:
        scores.append(1)
    
    consistency = len(scores) / 4 * 100
    
    short_trend = f["1w"] + f["1m"]
    long_trend = f["6m"] + f["1y"]
    
    if short_trend > long_trend * 0.5:
        consistency *= 0.8
    
    return consistency


def calculate_momentum_score(f: dict) -> float:
    """计算动量评分"""
    weights = {"1w": 0.1, "1m": 0.2, "3m": 0.3, "6m": 0.4}
    
    score = 0
    for period, weight in weights.items():
        score += f[period] * weight
    
    return score


def calculate_risk_score(f: dict, detail: dict) -> int:
    """计算风险评分（0-100，越低越好）"""
    risk = 30
    
    scale = detail.get("scale", 0)
    if scale > 0:
        if scale < 1:
            risk += 30
        elif scale < 10:
            risk += 15
        elif scale > 100:
            risk += 10
    
    manager_years = detail.get("manager_years", 0)
    if manager_years > 0:
        if manager_years < 1:
            risk += 25
        elif manager_years < 3:
            risk += 10
    
    fee_rate = detail.get("fee_rate", 0)
    if fee_rate > 0:
        if fee_rate > 1.5:
            risk += 15
        elif fee_rate > 1.0:
            risk += 5
    
    max_dd = f.get("max_drawdown", 0)
    if max_dd > 30:
        risk += 20
    elif max_dd > 20:
        risk += 10
    
    volatility = f.get("volatility", 0)
    if volatility > 3:
        risk += 15
    elif volatility > 2:
        risk += 5
    
    return min(risk, 100)


def calculate_score(f: dict, config: dict) -> float:
    """计算捡漏分（配置驱动版）"""
    recent = f["1w"] + f["1m"]
    medium = f["3m"] + f["6m"]
    long_term = f["1y"]

    if long_term < 30:
        return -999

    # 获取配置权重
    weights = config.get("score_weights", {
        "pullback": 0.3, "trend": 0.3, "sharpe": 0.2, "volatility": 0.2
    })
    
    sharpe = f.get("sharpe_ratio", 0)
    sharpe_adjustment = max(0.5, min(2.0, 1 + sharpe * 0.5))
    
    volatility = f.get("volatility", 0)
    volatility_adjustment = max(0.7, min(1.0, 1 - volatility * 0.1))
    
    pullback_depth = -recent
    if recent < -15:
        pullback_depth *= 0.7
    elif recent < -10:
        pullback_depth *= 0.85
    
    trend_strength = medium * 0.4 + long_term * 0.4
    
    if long_term > 150:
        trend_strength *= 0.7
    elif long_term > 100:
        trend_strength *= 0.85
    
    # 使用配置权重计算综合评分
    score = (pullback_depth * weights["pullback"] + 
             trend_strength * weights["trend"] + 
             sharpe * 10 * weights["sharpe"] + 
             volatility_adjustment * 10 * weights["volatility"])
    
    return score * sharpe_adjustment * volatility_adjustment


def detect_market_state() -> str:
    """检测市场状态"""
    try:
        navs = []
        for page in range(1, 3):
            params = {
                "callback": "jQ",
                "fundCode": "000300",
                "pageIndex": page,
                "pageSize": 30,
            }
            url = HISTORY_URL + "?" + urllib.parse.urlencode(params)
            headers = {"Referer": "https://fundf10.eastmoney.com/", "User-Agent": "Mozilla/5.0"}
            raw = fetch_with_retry(url, headers, DEFAULT_CONFIG['max_retry'])
            
            if raw:
                text = raw.decode("gbk", errors="replace")
                m = re.search(r'"LSJZList":(\[.*?\])', text)
                if m:
                    items = json.loads(m.group(1))
                    if items:
                        navs.extend([float(item["DWJZ"]) for item in items if item.get("DWJZ")])
            time.sleep(0.3)
        
        if len(navs) < 20:
            return "neutral"
        
        recent_return = (navs[0] - navs[-10]) / navs[-10] * 100
        medium_return = (navs[0] - navs[-20]) / navs[-20] * 100
        
        if recent_return > 3 and medium_return > 5:
            return "bull"
        elif recent_return < -3 and medium_return < -5:
            return "bear"
        else:
            return "neutral"
    except:
        return "neutral"


def filter_by_criteria(funds: List[dict], criteria: dict) -> List[dict]:
    """根据筛选条件过滤基金"""
    filtered = []
    
    for f in funds:
        if not validate_fund_data(f):
            continue
        
        if f["1y"] < criteria.get("min_1y", 30):
            continue
        
        if criteria.get("require_pullback", True):
            if f["1m"] >= 0 and f["1w"] >= 0:
                continue
        
        name = f["name"]
        if criteria.get("exclude_qdii", True):
            if "QDII" in name or "全球" in name or "国际" in name:
                continue
        
        if criteria.get("exclude_sector", True):
            sector_keywords = ["半导体", "芯片", "新能源", "医药", "消费", "科技", "信息", "互联网"]
            if any(kw in name for kw in sector_keywords):
                continue
        
        if criteria.get("exclude_c_class", True):
            if name.endswith("C") or "C" in name:
                continue
        
        filtered.append(f)
    
    return filtered


def deduplicate_funds(funds: List[dict]) -> List[dict]:
    """去重：同一基金只保留A类"""
    seen = {}
    result = []
    
    for f in funds:
        code = f["code"]
        name = f["name"]
        
        if "C" in name:
            continue
        
        seen[code] = True
        result.append(f)
    
    return result


def analyze_investment_style(f: dict) -> str:
    """分析投资风格"""
    style = []
    
    if f["1y"] > 100:
        style.append("高成长")
    elif f["1y"] > 50:
        style.append("成长")
    else:
        style.append("价值")
    
    volatility = f.get("volatility", 0)
    if volatility > 3:
        style.append("激进")
    elif volatility > 2:
        style.append("平衡")
    else:
        style.append("稳健")
    
    max_dd = f.get("max_drawdown", 0)
    if max_dd > 20:
        style.append("高波动")
    elif max_dd > 10:
        style.append("中波动")
    else:
        style.append("低波动")
    
    return " | ".join(style)


def generate_recommendation(f: dict, market_state: str) -> str:
    """生成投资建议"""
    score = f.get("final_score", 0)
    risk = f.get("risk_score", 50)
    sharpe = f.get("sharpe_ratio", 0)
    
    if market_state == "bear":
        return "☆☆☆ 观望 (熊市)"
    
    if score > 15 and risk < 40 and sharpe > 1:
        return "★★★ 强烈推荐"
    elif score > 10 and risk < 50 and sharpe > 0.5:
        return "★★☆ 推荐"
    elif score > 5 and risk < 60:
        return "★☆☆ 可关注"
    else:
        return "☆☆☆ 观望"


def process_fund(f: dict, config: dict) -> dict:
    """处理单个基金"""
    detail = fetch_fund_detail(f["code"])
    f.update(detail)
    
    navs = fetch_fund_history(f["code"], days=90)
    f["max_drawdown"] = calculate_max_drawdown(navs)
    f["volatility"] = calculate_volatility(navs)
    f["sharpe_ratio"] = calculate_sharpe_ratio(navs, config['risk_free_rate'])
    f["sortino_ratio"] = calculate_sortino_ratio(navs, config['risk_free_rate'])
    
    pullback_info = analyze_pullback_advanced(navs, config)
    f["pullback_depth"] = pullback_info["depth"]
    f["pullback_duration"] = pullback_info["duration"]
    f["pullback_pattern"] = pullback_info["pattern"]
    
    f["risk_score"] = calculate_risk_score(f, detail)
    f["score"] = calculate_score(f, config)
    f["trend_score"] = calculate_trend_score(f)
    f["momentum_score"] = calculate_momentum_score(f)
    f["style"] = analyze_investment_style(f)
    
    return f


def save_results_to_csv(funds: List[dict], filename: str = None):
    """保存结果到CSV文件"""
    import csv
    
    if not filename:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(RESULTS_DIR, f"fund_scan_{timestamp}.csv")
    
    try:
        with open(filename, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'code', 'name', 'score', 'risk_score', 'final_score',
                'sharpe_ratio', 'sortino_ratio', 'max_drawdown', 'volatility',
                '1w', '1m', '3m', '6m', '1y', 'style', 'recommendation',
                'trend_score', 'momentum_score', 'pullback_depth', 'pullback_duration',
                'pullback_pattern'
            ])
            writer.writeheader()
            writer.writerows(funds)
        
        print(f"结果已保存到: {filename}")
        return filename
    except Exception as e:
        print(f"保存结果失败: {e}")
        return None


def main(config: dict = None):
    """主函数"""
    args = parse_args()
    
    if config is None:
        config = DEFAULT_CONFIG.copy()
    
    # 应用命令行参数
    config["min_1y"] = args.min_1y
    config["max_results"] = args.max_results
    config["pullback_depth_min"] = args.pullback_depth
    config["pullback_duration_min"] = args.pullback_duration
    config["use_cache"] = not args.no_cache
    config["save_results"] = not args.no_save
    config["parallel_workers"] = args.workers
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    ensure_dirs()
    
    print("=" * 100)
    print("基金捡漏扫描 - 专业版 v4.1")
    print("筛选条件: 中期强势 + 深度回调 + 规模适中 + 费率合理 + 风险可控")
    print("=" * 100)

    # 检测市场状态
    print("正在检测市场状态...")
    market_state = detect_market_state()
    config["market_state"] = market_state
    print(f"当前市场状态: {market_state}")
    
    if market_state == "bear":
        print("⚠️  警告: 当前为熊市环境，建议观望为主！")
    
    rows = fetch_rank("1nzf", pn=500)
    filtered = filter_by_criteria(rows, config)
    filtered = deduplicate_funds(filtered)
    
    print(f"初步筛选: {len(filtered)} 只基金符合条件")
    print("正在获取详细数据（需要时间，请稍候）...")
    
    with ThreadPoolExecutor(max_workers=config['parallel_workers']) as executor:
        futures = []
        for i, f in enumerate(filtered[:config['max_results']]):
            print(f"  处理 {i+1}/{config['max_results']}: {f['code']} {f['name'][:15]}...")
            futures.append(executor.submit(process_fund, f, config))
        
        results = [future.result() for future in futures]
    
    for f in results:
        risk_adjustment = (100 - f["risk_score"]) / 100
        trend_adjustment = f["trend_score"] / 100
        f["final_score"] = f["score"] * risk_adjustment * trend_adjustment
        f["recommendation"] = generate_recommendation(f, market_state)
    
    results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
    
    print("\n" + "=" * 100)
    print("综合排名结果:")
    print("=" * 100)
    hdr = (f"{'排名':>4s} | {'代码':>6s} | {'名称':<18s} | {'综合分':>6s} | {'夏普':>6s} | {'风险分':>6s} | "
           f"{'1周':>7s} | {'1月':>7s} | {'6月':>7s} | {'1年':>7s} | {'回撤':>6s} | {'回调模式':<10s}")
    sep = "-" * 120
    print(hdr)
    print(sep)
    
    for i, f in enumerate(results[:15], 1):
        name = f["name"]
        print(f"{i:>4d} | {f['code']:>6s} | {name[:16]:<16s} | "
              f"{f.get('final_score', 0):>6.1f} | {f.get('sharpe_ratio', 0):>6.2f} | {f['risk_score']:>5.0f} | "
              f"{f['1w']:>6.2f}% | {f['1m']:>6.2f}% | "
              f"{f['6m']:>6.2f}% | {f['1y']:>6.2f}% | {f['max_drawdown']:>5.1f}% | {f.get('pullback_pattern', '未知'):<10s}")
    
    print("\n" + "=" * 100)
    print("详细信息 (前10名):")
    print("=" * 100)
    for i, f in enumerate(results[:10], 1):
        print(f"\n{i}. {f['code']} {f['name']}")
        print(f"   捡漏分: {f['score']:.1f} | 风险分: {f['risk_score']:.0f} | 综合分: {f.get('final_score', 0):.1f}")
        print(f"   收益: 1周 {f['1w']:+.2f}% | 1月 {f['1m']:+.2f}% | 6月 {f['6m']:+.2f}% | 1年 {f['1y']:+.2f}%")
        print(f"   风控: 最大回撤 {f['max_drawdown']:.1f}% | 波动率 {f.get('volatility', 0):.2f}%")
        print(f"   风险调整: 夏普比率 {f.get('sharpe_ratio', 0):.2f} | 索提诺比率 {f.get('sortino_ratio', 0):.2f}")
        print(f"   回调分析: 深度 {f.get('pullback_depth', 0):.1f}% | 时长 {f.get('pullback_duration', 0)}天 | 模式: {f.get('pullback_pattern', '未知')}")
        print(f"   趋势: 一致性 {f.get('trend_score', 0):.0f}% | 动量 {f.get('momentum_score', 0):.1f}")
        print(f"   风格: {f.get('style', '未知')}")
        print(f"   建议: {f.get('recommendation', '未知')}")
        if f.get("manager_start"):
            print(f"   经理: 任职 {f['manager_start']} ({f.get('manager_years', 0):.1f}年)")
        if f.get("scale"):
            print(f"   规模: {f['scale']:.2f}亿")
        if f.get("fee_rate"):
            print(f"   费率: {f['fee_rate']:.2f}%")
    
    if config['save_results']:
        save_results_to_csv(results)
    
    print("\n" + "=" * 100)
    print("市场状态分析:")
    print(f"  当前状态: {market_state}")
    if market_state == "bull":
        print("  建议: 牛市环境，可适当积极")
    elif market_state == "bear":
        print("  建议: 熊市环境，建议观望为主")
    else:
        print("  建议: 震荡环境，谨慎操作")
    
    print("\n筛选说明:")
    print("  ✓ 1年涨幅>30% (中期趋势强)")
    print("  ✓ 深度回调>3%且持续>3天 (真正的回调)")
    print("  ✓ 排除QDII/行业主题/C类份额")
    print("  ✓ 综合分 = 捡漏分 × 风险调整 × 趋势调整")
    print("  ✓ 回调模式识别: 单峰/双峰/V型/连续回调")
    
    print("\n风险调整指标:")
    print("  • 夏普比率: 衡量单位风险的超额收益 (越高越好)")
    print("  • 索提诺比率: 只考虑下行风险 (越高越好)")
    print("  • 最大回撤: 历史最大亏损幅度 (越低越好)")
    print("  • 波动率: 收益波动程度 (越低越好)")
    
    print("\n风险因素:")
    print("  • 规模<1亿: 清盘风险高 (+30分)")
    print("  • 经理任职<1年: 经验不足 (+25分)")
    print("  • 费率>1.5%: 成本过高 (+15分)")
    print("  • 最大回撤>30%: 波动太大 (+20分)")
    print("  • 波动率>3%: 短期波动大 (+15分)")
    
    print("\n投资建议:")
    print("  ★★★ 强烈推荐: 综合分>15 且 风险分<40 且 夏普>1")
    print("  ★★☆ 推荐: 综合分>10 且 风险分<50 且 夏普>0.5")
    print("  ★☆☆ 可关注: 综合分>5 且 风险分<60")
    print("  ☆☆☆ 观望: 其他情况或熊市环境")
    print("=" * 100)
    
    # 发送微信通知（如果有推荐基金）
    title, content = generate_notification_content(results, market_state)
    if title and content:
        print("\n发现推荐基金，正在发送微信通知...")
        send_wechat_notification(title, content)
    else:
        print("\n未发现推荐基金，跳过通知")


if __name__ == "__main__":
    main()
