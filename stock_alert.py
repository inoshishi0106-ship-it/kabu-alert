# stock_alert.py  --  買い材料（IR・決算・ニュースのテーマ株）をClaudeが読んで点数を付け、高いものをLINEに送る
#
# 使い方
#   python stock_alert.py          ← 画面に結果を出すだけ（LINEには送りません）
#   python stock_alert.py --send   ← 点数が高いものをLINEに送る
#   python stock_alert.py --send --min 70   ← 送る基準の点数をその回だけ変える
#
# 必要なもの: Claude APIキー（常に必要）、LINEのトークンとユーザーID（--send のときだけ）
# PCで動かすときは、聞かれたときに1回だけ貼り付けます（貼っても画面には表示されません）。
# GitHubで動かすときは、GitHubの「Secrets」に登録した値が自動で使われます。

import getpass
import io
import json
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
import datetime as dt
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from anthropic import Anthropic
from bs4 import BeautifulSoup
from pypdf import PdfReader

# ===== 設定（必要ならここを変えます） =====
MODEL = "claude-haiku-5-5"   # 一次チェックに使うClaudeのモデル（安くて速いもの）
MODEL_CHECK = "claude-sonnet-5-5"  # 二次チェック（基準点以上だけ読み直す、賢いモデル）
HISTORY_SCORE_MIN = 50       # この点数以上は、送らなくても記録する（基準点の比較用）
NIGHT_HOURS = (22, 8)        # この時間帯の通知は貯めて、朝8時にまとめて送る
WEEKLY_REPORT_DAY = 5        # 週のまとめを送る曜日（0=月 … 5=土）
PRICE_MAX = 5000             # 株価の上限（円）
SCORE_MIN = 60               # この点数以上をLINEに送る（0〜100）
MAX_CANDIDATES = 30          # 1回の実行でClaudeに読ませるIR・決算の最大件数
MAX_JUDGE_PER_DAY = 400     # 1日にClaudeに読ませる最大件数（費用の上限。1件0.1円ほど）
MAX_ITEMS_PER_PUSH = 5       # 1回のLINE通知にまとめる最大件数（吹き出し5個まで）
MONTHLY_PUSH_LIMIT = 190     # 1か月のLINE通知の最大回数（無料枠:月200通。少し余裕を残す）
EVENING_RESERVE = 3          # 15時以降の決算・夕方のニュース用に、1日の枠から残しておく回数
MAX_AGE_HOURS = 18           # これより古いIRは判断しない（もう遅いので）
THEME_EVERY_MIN = 60         # ニュースのテーマ株チェックを何分ごとにするか
THEME_HOURS = (7, 22)        # テーマ株チェックをする時間帯（7時〜22時）
NEWS_HOURS = 6               # 何時間以内のニュースを見るか
STATE_FILE = "alert_state.json"
JPX_FILE = "jpx_codes.json"
HEADERS = {"User-Agent": "Mozilla/5.0"}
JST = ZoneInfo("Asia/Tokyo")

# IRの題名に入っていたら「候補」にする言葉
GOOD = [
    "上方修正", "増配", "特別配当", "記念配当", "復配", "株式分割",
    "業務提携", "資本提携", "資本業務提携",
    "受注", "採択", "共同開発", "承認取得", "承認", "黒字化", "黒字転換",
    "過去最高", "最高益", "上場来", "子会社化", "契約締結", "新製品", "開発", "実証",
    "補助金", "認定", "特許", "販売開始", "サービス開始", "量産",
]
# 決算の言葉（これがあれば、中身を読んで良い決算かどうか判断する）
EARNINGS = ["決算短信"]
# 入っていたら除外する言葉（悪材料・紛らわしいもの）
BAD = ["下方修正", "減配", "無配", "赤字", "訂正", "遅延", "疑義", "減損", "特別損失",
       "第三者割当", "新株予約権", "公募増資", "上場廃止", "公開買付", "TOB", "意見表明",
       "REIT", "リート", "投資法人", "ETF", "ETN"]

# テーマ探しに使うニュースの検索ワード
NEWS_QUERIES = [
    # 株の話題（すでに注目が集まり始めているもの）
    "ストップ高", "株価 急騰", "関連銘柄", "関連株", "テーマ株", "材料視", "思惑買い", "連想買い",
    # 政府・政策（国策テーマの最初のニュース）
    "政府 方針", "閣議決定", "経済対策", "国策", "政府 補助金", "概算要求", "経産省", "防衛 受注",
    # 技術・発見（まだ株の言葉が付いていない最初のニュース）
    "世界初", "実用化", "量産", "実証 成功", "新技術 開発", "試掘", "海底資源", "レアアース",
    # 事件・事故・社会の出来事（そこから連想されるテーマ）
    "情報漏洩", "サイバー攻撃", "大規模障害", "感染拡大", "地震 被害", "供給不足",
    # 海外の大きな動き（日本株に波及しやすいもの）
    "米国 規制", "中国 輸出規制", "関税", "半導体 投資",
    # 東証33業種それぞれのニュース（ヒートマップの全業種をカバー）
    "水産 漁業", "鉱業 資源開発", "建設 大型受注", "食品 値上げ", "繊維 新素材", "製紙",
    "化学 新素材", "新薬 承認", "原油価格", "タイヤ ゴム", "セメント ガラス", "鉄鋼",
    "銅価格 非鉄", "金属製品", "工作機械 受注", "電子部品", "自動車 EV", "医療機器",
    "ゲーム 新作", "電力 原発 再稼働", "鉄道 物流", "海運 運賃", "航空 旅客", "倉庫 物流",
    "通信 新サービス", "商社 投資", "小売 既存店", "銀行 金利", "証券 手数料", "保険 再編",
    "フィンテック", "不動産 再開発", "インバウンド",
    # よく話題になるテーマ
    "宇宙 ロケット", "量子コンピューター", "核融合", "ドローン", "ロボット", "再生可能エネルギー",
    "水素", "全固体電池", "ペロブスカイト", "データセンター", "光通信", "防災 国土強靭化",
    "生成AI", "自動運転", "M&A 買収", "株主還元",
]

PROMPT = """あなたは日本の小型株に詳しい株式アナリストです。
次の適時開示（{kind}）を読んで、「これから買いが集まって株価が上がりそうか」を判断してください。
投資助言ではなく、情報整理の目的です。数字や事実は、下の本文に書かれていることだけを根拠にしてください。
本文にない内容を推測で断定してはいけません。

【銘柄】{code} {name}
【開示時刻】{time}
【題名】{title}
【現在の株価】{price}円（前日比 {chg:+.1f}%）／出来高は直近20日平均の {vol:.1f}倍
【開示本文の冒頭】
{body}

次のJSONだけを出力してください（前後に文章を付けない）:
{{"score": 0〜100の整数, "reason": "なぜ買いが集まりそうか（または集まらなさそうか）を2文以内", "risk": "注意点を1文", "already_moved": true か false}}

採点の目安:
- 80以上: 上方修正の幅が大きい／想定外の好材料／規模の大きい提携・受注など、明確に株価を動かしそうなもの
- 60〜79: 好材料だが、規模や新規性が限定的
- 59以下: 定例的、小さい、すでに織り込み済み、または悪材料
- 決算の場合: 前年同期比の伸び、会社予想に対する進み具合（進捗率）、予想の上方修正や増配があるかを重視。
  減益・予想未達・下方修正なら低い点数にする
- すでに株価が大きく上がり出来高も急増している場合は already_moved を true にし、点数を10〜20点下げる
"""

THEME_PROMPT = """あなたは日本株のテーマ株に詳しいアナリストです。
下は、直近{hours}時間の日本のニュースの見出しです。
この中から「これから日本株で買いが集まりそうなテーマ」を最大5つ選び、
（業種は問いません。銀行・鉄鋼・食品・不動産・海運など、東証33業種のどれでも対象です）
（例: 海底でレアアース発見 → 資源開発・探査の関連株、大規模な情報漏洩 → セキュリティ株、のように、
ニュースから連想される業界まで考えてください。まだ株価で話題になっていない新しいニュースほど重視してください）
それぞれ、そのテーマで直接恩恵を受けそうな東証上場の会社を最大4社挙げてください。
- ニュースに名前が出た会社だけでなく、「連想」で恩恵を受ける会社も挙げてください。
  例: 大手の自動運転レベル4の実証拡大 → 高精度3次元地図・センサー・制御ソフトを供給する会社
      大手の情報漏えい → セキュリティ製品・診断サービスの会社
  ニュースに出た大企業より、その技術や部品を支える小型株の方がよく動きます
- 小型・中型株、最近上場した会社も優先してください
- 証券コード（4桁、例 3905 や 485A）に自信がない会社は挙げないでください
- ニュースに根拠がないテーマは作らないでください。該当がなければ空にしてください
- 情報整理の目的で、投資助言ではありません

【ニュースの見出し】
{news}

次のJSONだけを出力してください（前後に文章を付けない）:
{{"themes": [{{"theme": "テーマ名", "news": "根拠になったニュースを1文で", "stocks": [{{"code": "1234", "name": "会社名", "why": "なぜ関係するかを1文"}}]}}]}}
"""

THEME_SCORE_PROMPT = """あなたは日本のテーマ株に詳しい株式アナリストです。
次の銘柄は、今のニュースのテーマに関係すると考えられる銘柄です。
それぞれ「これから買いが集まって株価が上がりそうか」を採点してください。情報整理の目的で、投資助言ではありません。

{items}

採点の目安:
- 80以上: テーマとの結び付きが強く、まだ株価が大きく動いていない（これから来そう）
- 60〜79: 関係はあるが、結び付きが弱い、または一部すでに動いている
- 59以下: 関係が薄い、またはすでに大きく上がった後
- 前日比が+10%以上など、すでに大きく上がっている場合は already_moved を true にして点数を下げる

次のJSONだけを出力してください（前後に文章を付けない）:
{{"items": [{{"code": "1234", "score": 0〜100の整数, "reason": "2文以内", "risk": "1文", "already_moved": true か false}}]}}
"""


# ---------------- 共通 ----------------
def get_secret(env_name, label):
    v = os.environ.get(env_name)
    if v:
        return v.strip()
    return getpass.getpass(f"{label}: ").strip()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("seen", [])
    s.setdefault("push", {})       # 日付 → LINE通知の回数
    s.setdefault("judged", {})     # 日付 → Claudeに読ませた件数
    s.setdefault("theme_sent", {})  # 日付 → その日テーマで通知した銘柄コード
    s.setdefault("theme_last", "")
    s.setdefault("push_month", {})  # 年月 → LINE通知の回数
    s.setdefault("pending", [])     # 送れなかった通知・夜の間に貯めた通知
    s.setdefault("digest_done", "")  # 朝のまとめを送った日
    s.setdefault("weekly_done", "")  # 週のまとめを送った日
    return s


# ---------------- ② 成績の記録 ----------------
def load_history(state):
    return state.setdefault("history", [])


def prune_history(state, today):
    cutoff = (today - dt.timedelta(days=45)).isoformat()
    state["history"] = [h for h in state.get("history", []) if h["date"] >= cutoff]


def record(hist, now, code, name, kind, score, price, sent):
    d = now.date().isoformat()
    for h in hist:
        if h["date"] == d and h["code"] == code and h["kind"] == kind:
            h["sent"] = h["sent"] or sent   # 同じ日・同じ銘柄は1回だけ記録
            return
    hist.append({"date": now.date().isoformat(), "time": now.strftime("%H:%M"), "code": code,
                 "name": name, "kind": kind, "score": score, "price": round(price, 1),
                 "sent": sent})


def after_moves(code, date_str, price):
    """通知した日のあと、翌営業日の終値・3営業日以内の高値・3営業日後の終値の変化率(%)。"""
    try:
        df = yf.Ticker(code + ".T").history(period="1mo", interval="1d")
    except Exception:
        return None
    if df is None or df.empty or not price:
        return None
    d0 = dt.date.fromisoformat(date_str)
    after = df[[ix.date() > d0 for ix in df.index]]
    if len(after) < 1:
        return None
    r = {"next": (after["Close"].iloc[0] / price - 1) * 100}
    if len(after) >= 3:
        r["high3"] = (after["High"].iloc[:3].max() / price - 1) * 100
        r["close3"] = (after["Close"].iloc[2] / price - 1) * 100
    return r


def weekly_report(hist, today):
    """直近2週間の記録から、成績のまとめ文章を作る。"""
    start = (today - dt.timedelta(days=14)).isoformat()
    rows = [dict(h) for h in hist if start <= h["date"] < today.isoformat()]
    if not rows:
        return None
    cache = {}
    for h in rows:
        k = (h["code"], h["date"], h["price"])
        if k not in cache:
            cache[k] = after_moves(h["code"], h["date"], h["price"])
        h["_m"] = cache[k]
    done = [h for h in rows if h["_m"]]
    if not done:
        return None

    def line(label, group):
        g = [h for h in group if h["_m"]]
        if not g:
            return None
        nxt = [h["_m"]["next"] for h in g]
        win = sum(1 for x in nxt if x > 0)
        hi = [h["_m"]["high3"] for h in g if "high3" in h["_m"]]
        s = f"{label}: {len(g)}件 翌日上昇 {win}/{len(g)}（{win * 100 // len(g)}%） 翌日平均 {sum(nxt) / len(nxt):+.1f}%"
        if hi:
            s += f" 3日内の高値 平均 {sum(hi) / len(hi):+.1f}%"
        return s

    out = [f"【今週の成績】直近2週間（{start}〜）", "", "■ 点数別"]
    for lo, hi_, label in ((80, 101, "80点以上"), (70, 80, "70〜79点"), (60, 70, "60〜69点"),
                           (50, 60, "50〜59点（送っていない）")):
        s = line(label, [h for h in done if lo <= h["score"] < hi_])
        if s:
            out.append(s)
    out += ["", "■ 種類別"]
    for kind in ("IR", "決算", "テーマ株"):
        s = line(kind, [h for h in done if h["kind"] == kind and h["sent"]])
        if s:
            out.append(s)
    best = sorted([h for h in done if h["sent"] and "high3" in h["_m"]],
                  key=lambda h: h["_m"]["high3"], reverse=True)[:3]
    if best:
        out += ["", "■ よく上がった通知"]
        for h in best:
            out.append(f"{h['code']} {h['name']}（{h['kind']} {h['score']}点・{h['date'][5:]}）"
                       f" 3日内の高値 {h['_m']['high3']:+.1f}%")
    out += ["", "※通知したときの株価からの変化です。基準点を変えたいときの参考にしてください。"]
    return "\n".join(out)


def push_allowance(state, now):
    """今日、あと何回LINEに送ってよいか。月の残り枠を残りの日数で割って決める。"""
    month = now.strftime("%Y-%m")
    used_month = state["push_month"].get(month, 0)
    used_today = state["push"].get(now.date().isoformat(), 0)
    nxt = (now.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    days_left = (nxt.date() - now.date()).days          # 今日を含む残り日数
    left = MONTHLY_PUSH_LIMIT - (used_month - used_today)  # 今日の分を戻した残り枠
    daily = max(3, min(15, left // max(days_left, 1)))
    if now.hour < 15:
        daily = max(1, daily - EVENING_RESERVE)   # 夕方の決算用に残しておく
    if used_month >= MONTHLY_PUSH_LIMIT:
        return 0, daily
    return max(0, daily - used_today), daily


def save_state(state, today):
    state["seen"] = state["seen"][-5000:]
    keep = today.isoformat()
    for k in ("push", "judged", "theme_sent"):
        state[k] = {keep: state[k].get(keep, 0 if k != "theme_sent" else [])}
    m = today.strftime("%Y-%m")
    state["push_month"] = {m: state["push_month"].get(m, 0)}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def ask_claude(client, prompt, max_tokens=3000, model=None):
    resp = client.messages.create(
        model=model or MODEL, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    # 返事には「考え中」のブロックが混ざることがあるので、文章のブロックだけ集める
    return "".join(
        getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
    )


def extract_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def price_info(code):
    try:
        df = yf.Ticker(code + ".T").history(period="2mo", interval="1d")
        if len(df) < 10:
            return None
        last = df.iloc[-1]
        vol_avg = df["Volume"].iloc[:-1].tail(20).mean()
        return {
            "close": float(last["Close"]),
            "vol": float(last["Volume"] / vol_avg) if vol_avg else 0.0,
            "chg": float((last["Close"] / df["Close"].iloc[-2] - 1) * 100),
        }
    except Exception:
        return None


# ---------------- 東証の上場銘柄一覧（テーマ株のコード確認用） ----------------
def load_jpx(today):
    """東証の上場銘柄一覧（コード→会社名）。1週間に1回だけ取り直す。取れなければ None。"""
    try:
        with open(JPX_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if (today - dt.date.fromisoformat(d["date"])).days < 7:
            return d["codes"]
    except Exception:
        pass
    try:
        import pandas as pd
        # 一覧ページからダウンロード用のリンクを探す（ファイル名が変わっても大丈夫なように）
        page = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
        url = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
        try:
            p = requests.get(page, headers=HEADERS, timeout=30)
            m = re.search(r'href="([^"]*data_j\.xlsx?)"', p.text)
            if m:
                url = urllib.parse.urljoin(page, m.group(1))
        except Exception:
            pass
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        df = pd.read_excel(io.BytesIO(r.content))
        codes = {}
        for _, row in df.iterrows():
            c = str(row["コード"]).strip()
            if c.endswith(".0"):
                c = c[:-2]
            kind = str(row.get("市場・商品区分", ""))
            if "ETF" in kind or "REIT" in kind or "出資証券" in kind:
                continue
            codes[c] = str(row["銘柄名"]).strip()
        with open(JPX_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": today.isoformat(), "codes": codes}, f, ensure_ascii=False)
        return codes
    except Exception as e:
        print(f"  （上場銘柄一覧を取得できませんでした: {e}）")
        return None


# ---------------- 適時開示（IR・決算） ----------------
def parse_tdnet(html):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("#main-list-table tr"):
        t = tr.select_one(".kjTime")
        c = tr.select_one(".kjCode")
        n = tr.select_one(".kjName")
        k = tr.select_one(".kjTitle")
        if not (t and c and n and k):
            continue
        a = k.find("a")
        out.append({
            "time": t.get_text(strip=True),
            "code": c.get_text(strip=True)[:4],
            "name": n.get_text(strip=True),
            "title": k.get_text(strip=True),
            "url": ("https://www.release.tdnet.info/inbs/" + a["href"]) if a and a.get("href") else "",
        })
    return out


def fetch_tdnet(day):
    ymd = day.strftime("%Y%m%d")
    rows = []
    for page in range(1, 40):
        url = f"https://www.release.tdnet.info/inbs/I_list_{page:03d}_{ymd}.html"
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            break
        r.encoding = "utf-8"
        got = parse_tdnet(r.text)
        if not got:
            break
        for x in got:
            x["day"] = ymd
            rows.append(x)
    return rows


def disclosed_at(r):
    try:
        d = dt.datetime.strptime(r["day"] + r["time"], "%Y%m%d%H:%M")
        return d.replace(tzinfo=JST)
    except Exception:
        return None


def pick_candidates(rows, seen, now):
    out = []
    for r in rows:
        if r["url"] in seen:
            continue
        title = r["title"]
        if any(b in title for b in BAD):
            continue
        if any(g in title for g in GOOD):
            r["kind"] = "IR"
        elif any(e in title for e in EARNINGS):
            r["kind"] = "決算"
        else:
            continue
        t = disclosed_at(r)
        r["old"] = bool(t and (now - t).total_seconds() > MAX_AGE_HOURS * 3600)
        out.append(r)
    # IR（上方修正・提携など）を先に、その中で新しい順。決算はそのあと新しい順
    out.sort(key=lambda r: (r["kind"] == "IR", r.get("day", ""), r["time"]), reverse=True)
    return out


def pdf_head(url, n=3000):
    """開示PDFの冒頭の文字を取り出す。取れなければ空文字。"""
    if not url or not url.lower().endswith(".pdf"):
        return ""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        reader = PdfReader(io.BytesIO(r.content))
        text = ""
        for p in reader.pages[:2]:
            text += (p.extract_text() or "") + "\n"
        return re.sub(r"\s+", " ", text)[:n]
    except Exception:
        return ""


def judge_ir(client, r, info, body):
    prompt = PROMPT.format(
        kind="決算短信" if r["kind"] == "決算" else "IR",
        code=r["code"], name=r["name"], time=r["time"], title=r["title"],
        price=f"{info['close']:.0f}", chg=info["chg"], vol=info["vol"],
        body=body or "（本文を取得できませんでした。題名だけで判断してください）",
    )
    data = extract_json(ask_claude(client, prompt))
    if not data or "score" not in data:
        return None
    try:
        data["score"] = int(data["score"])
    except Exception:
        return None
    data["first"] = data["score"]
    return data


def recheck_ir(client, r, info, body, res):
    """④ 一次チェックで基準点以上だったものを、賢いモデルでもう一度読み直す。"""
    prompt = PROMPT.format(
        kind="決算短信" if r["kind"] == "決算" else "IR",
        code=r["code"], name=r["name"], time=r["time"], title=r["title"],
        price=f"{info['close']:.0f}", chg=info["chg"], vol=info["vol"],
        body=body or "（本文を取得できませんでした。題名だけで判断してください）",
    ) + "\n厳しめに採点してください。数字の読み違いや、すでに知られている内容でないかも確認してください。\n"
    try:
        data = extract_json(ask_claude(client, prompt, model=MODEL_CHECK))
        data["score"] = int(data["score"])
    except Exception:
        return res   # 読み直しに失敗したら、一次チェックの結果を使う
    data["first"] = res["first"]
    data["checked"] = True
    return data


def format_ir(r, info, res):
    moved = "\n※すでに動き出している可能性あり" if res.get("already_moved") else ""
    label = "決算" if r["kind"] == "決算" else "IR"
    return (
        f"【{label} {res['score']}点{'（一次' + str(res['first']) + '→精査' + str(res['score']) + '）' if res.get('checked') else ''}】{r['code']} {r['name']}\n"
        f"株価 {info['close']:.0f}円（{info['chg']:+.1f}%） 出来高 {info['vol']:.1f}倍{moved}\n"
        f"{label}: {r['title'][:80]}（{r['time']}）\n"
        f"根拠: {res.get('reason', '')}\n"
        f"注意: {res.get('risk', '')}\n"
        f"{r['url']}"
    )


# ---------------- ニュース → テーマ株 ----------------
def fetch_news(now):
    """Googleニュースから、直近NEWS_HOURS時間の見出しを集める。"""
    titles = []
    for q in NEWS_QUERIES:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
               + "+when:1d&hl=ja&gl=JP&ceid=JP:ja")
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception:
            continue
        for item in root.iter("item"):
            t = (item.findtext("title") or "").strip()
            p = item.findtext("pubDate")
            try:
                when = parsedate_to_datetime(p).astimezone(JST)
            except Exception:
                continue
            if t and (now - when).total_seconds() <= NEWS_HOURS * 3600:
                titles.append(t)
    uniq = list(dict.fromkeys(titles))  # 重複を消す
    return uniq[:300]


def theme_alerts(client, now, state, jpx, score_min):
    """ニュースからテーマ株を探して、送る候補のリストを返す。"""
    theme_alerts.recorded = []
    news = fetch_news(now)
    print(f"\n■ ニュースのテーマ株チェック（直近{NEWS_HOURS}時間の見出し {len(news)} 件）")
    if len(news) < 3:
        print("  見出しが少ないため、今回は見送ります")
        return []
    data = extract_json(ask_claude(client, THEME_PROMPT.format(
        hours=NEWS_HOURS, news="\n".join("- " + t for t in news)), max_tokens=4000))
    if not data or not data.get("themes"):
        print("  目立つテーマはありませんでした")
        return []

    today = now.date().isoformat()
    already = set(state["theme_sent"].get(today, []))
    cands = []
    for th in data["themes"]:
        print(f"  テーマ: {th.get('theme', '')} … {th.get('news', '')}")
        for s in th.get("stocks", [])[:4]:
            code = str(s.get("code", "")).strip().upper()
            if not re.fullmatch(r"[0-9][0-9A-Z]{3}", code) or code in already:
                continue
            if jpx is not None:
                if code not in jpx:
                    print(f"    {code} {s.get('name', '')}: 上場一覧にないため除外")
                    continue
                s["name"] = jpx[code]   # 正式な会社名に置き換える
            info = price_info(code)
            if not info or info["close"] > PRICE_MAX:
                continue
            cands.append({"code": code, "name": s.get("name", ""), "why": s.get("why", ""),
                          "theme": th.get("theme", ""), "news": th.get("news", ""), "info": info})
    if not cands:
        print("  条件（株価5000円以下）に合う銘柄はありませんでした")
        return []

    lines = []
    for c in cands:
        i = c["info"]
        lines.append(f"- {c['code']} {c['name']}／テーマ: {c['theme']}（{c['news']}）／関係: {c['why']}"
                     f"／株価 {i['close']:.0f}円 前日比 {i['chg']:+.1f}% 出来高 平均の{i['vol']:.1f}倍")
    res = extract_json(ask_claude(client, THEME_SCORE_PROMPT.format(items="\n".join(lines))))
    scores = {str(x.get("code", "")).upper(): x for x in (res or {}).get("items", [])}

    # ④ 基準点以上だったものだけ、賢いモデルでまとめて読み直す
    passed = [ln for c, ln in zip(cands, lines)
              if c["code"] in scores and _int(scores[c["code"]].get("score")) >= score_min]
    checked = {}
    if passed:
        try:
            res2 = extract_json(ask_claude(
                client, THEME_SCORE_PROMPT.format(items="\n".join(passed))
                + "\n厳しめに採点してください。会社とテーマの関係が本当にあるか、"
                  "すでに大きく上がった後でないかも確認してください。\n",
                model=MODEL_CHECK))
            checked = {str(x.get("code", "")).upper(): x for x in (res2 or {}).get("items", [])}
        except Exception as e:
            print(f"  （読み直しでエラー: {e}。一次チェックの結果を使います）")

    out = []
    for c in cands:
        x = scores.get(c["code"])
        if not x:
            continue
        sc = first = _int(x.get("score"))
        if c["code"] in checked:
            x = checked[c["code"]]
            sc = _int(x.get("score"))
        i = c["info"]
        flag = "★送信対象" if sc >= score_min else "  見送り"
        two = f"（一次{first}→精査{sc}）" if c["code"] in checked else ""
        print(f"{flag} {sc:>3}点{two}  {c['code']} {c['name']}  {i['close']:.0f}円 ({i['chg']:+.1f}%) 出来高{i['vol']:.1f}倍  [{c['theme']}]")
        print(f"        根拠: {x.get('reason', '')}")
        c.update(score=sc, sent=sc >= score_min)
        if sc >= score_min:
            moved = "\n※すでに動き出している可能性あり" if x.get("already_moved") else ""
            text = (
                f"【テーマ株 {sc}点{two}】{c['code']} {c['name']}\n"
                f"株価 {i['close']:.0f}円（{i['chg']:+.1f}%） 出来高 {i['vol']:.1f}倍{moved}\n"
                f"テーマ: {c['theme']}\n"
                f"ニュース: {c['news']}\n"
                f"根拠: {x.get('reason', '')}\n"
                f"注意: {x.get('risk', '')}（銘柄の関係はClaudeの推定です。ご自身でも確認してください）"
            )
            out.append({"score": sc, "text": text, "code": c["code"]})
    # 記録用（50点以上）
    theme_alerts.recorded = [c for c in cands if "score" in c and c["score"] >= HISTORY_SCORE_MIN]
    return out


def _int(v):
    try:
        return int(v)
    except Exception:
        return 0


# ---------------- LINE ----------------
def send_line(token, user_id, texts):
    msgs = [{"type": "text", "text": t[:4900]} for t in texts[:5]]
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"to": user_id, "messages": msgs},
        timeout=15,
    )
    return r.status_code, r.text


# ---------------- 本体 ----------------
def main():
    send = "--send" in sys.argv
    score_min = SCORE_MIN
    if "--min" in sys.argv:
        score_min = int(sys.argv[sys.argv.index("--min") + 1])
    now = dt.datetime.now(JST)
    today = now.date()
    print(f"=== 実行 {now:%Y-%m-%d %H:%M} / {'LINE送信あり' if send else '画面表示のみ'} / 基準 {score_min}点 ===")

    api_key = get_secret("ANTHROPIC_API_KEY", "ClaudeのAPIキーを貼り付けてEnter")
    client = Anthropic(api_key=api_key)
    token = user_id = None
    if send:
        token = get_secret("LINE_TOKEN", "LINEのトークンを貼り付けてEnter")
        user_id = get_secret("LINE_USER_ID", "LINEのユーザーIDを貼り付けてEnter")
        chk = requests.get("https://api.line.me/v2/bot/info",
                           headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if chk.status_code != 200:
            print(f"\n★LINEのトークンが正しくありません（コード {chk.status_code}）。")
            print("  LINE Developersでトークンを再発行して、もう一度実行してください。")
            sys.exit(1)
        if not user_id.startswith("U"):
            print("\n★ユーザーIDは U から始まる文字です。もう一度実行してください。")
            sys.exit(1)
        print("  LINEの接続OK")

    state = load_state()
    seen = set(state["seen"])
    key = today.isoformat()
    judged_today = state["judged"].get(key, 0)
    alerts = []   # {"score", "text"}
    hist = load_history(state)   # ② 成績の記録（alert_state.json の中に保存）

    # ---- ① IR・決算 ----
    rows = []
    for back in (0, 1):
        day = today - dt.timedelta(days=back)
        try:
            got = fetch_tdnet(day)
        except Exception as e:
            print(f"  {day} の取得に失敗: {e}")
            got = []
        print(f"  {day}: {len(got)} 件の開示")
        rows += got

    cands = pick_candidates(rows, seen, now)
    n_ir = sum(1 for r in cands if r["kind"] == "IR")
    print(f"  新しい候補: IR {n_ir} 件 / 決算 {len(cands) - n_ir} 件"
          f"（株価{PRICE_MAX}円以下に絞って、最大{MAX_CANDIDATES}件を判断します）\n")

    judged = 0
    for r in cands:
        if r["old"]:
            seen.add(r["url"])   # 古すぎるものは判断しない
            continue
        if judged >= MAX_CANDIDATES or judged_today >= MAX_JUDGE_PER_DAY:
            break
        info = price_info(r["code"])
        if not info or info["close"] > PRICE_MAX:
            seen.add(r["url"])
            continue
        body = pdf_head(r["url"])
        res = judge_ir(client, r, info, body)
        judged += 1
        judged_today += 1
        seen.add(r["url"])
        if not res:
            print(f"[{r['code']} {r['name']}] 判断結果を読み取れませんでした")
            continue
        if res["score"] >= score_min:
            res = recheck_ir(client, r, info, body, res)   # ④ 賢いモデルで読み直す
            judged_today += 1
        flag = "★送信対象" if res["score"] >= score_min else "  見送り"
        two = f"（一次{res['first']}→精査{res['score']}）" if res.get("checked") else ""
        print(f"{flag} {res['score']:>3}点{two}  [{r['kind']}] {r['code']} {r['name']}  {info['close']:.0f}円 ({info['chg']:+.1f}%) 出来高{info['vol']:.1f}倍")
        print(f"        {r['kind']}: {r['title'][:70]}")
        print(f"        根拠: {res.get('reason', '')}")
        print(f"        注意: {res.get('risk', '')}")
        ok = res["score"] >= score_min
        if res["score"] >= HISTORY_SCORE_MIN or res["first"] >= HISTORY_SCORE_MIN:
            record(hist, now, r["code"], r["name"], r["kind"], res["score"], info["close"], ok)
        if ok:
            alerts.append({"score": res["score"], "text": format_ir(r, info, res), "code": r["code"]})

    # ---- ニュースのテーマ株（1時間に1回） ----
    last = state.get("theme_last")
    due = True
    if last:
        try:
            due = (now - dt.datetime.fromisoformat(last)).total_seconds() >= THEME_EVERY_MIN * 60
        except Exception:
            due = True
    if THEME_HOURS[0] <= now.hour < THEME_HOURS[1] and (due or not send):
        try:
            jpx = load_jpx(today)
            got = theme_alerts(client, now, state, jpx, score_min)
            alerts += got
            judged_today += 3
            for c in getattr(theme_alerts, "recorded", []):
                record(hist, now, c["code"], c["name"], "テーマ株", c["score"], c["info"]["close"], c["sent"])
        except Exception as e:
            print(f"  テーマ株チェックでエラー: {e}")
        state["theme_last"] = now.isoformat()
    else:
        print("\n■ ニュースのテーマ株チェックは、今回はお休み（1時間に1回／7時〜22時）")

    # ---- LINEに送る ----
    sent = 0
    night = now.hour >= NIGHT_HOURS[0] or now.hour < NIGHT_HOURS[1]

    def fresh(p):
        age = (now - dt.datetime.fromisoformat(p["at"])).total_seconds()
        return age < (14 if p.get("night") else 3) * 3600

    pending = [p for p in state.get("pending", []) if fresh(p)]
    for a in alerts:
        a["at"] = now.isoformat()
        a["night"] = night
    queue, seen_codes = [], set()
    for a in sorted(pending + alerts, key=lambda a: a["score"], reverse=True):
        if a.get("code") and a["code"] in seen_codes:
            continue   # 同じ銘柄は1回だけ
        seen_codes.add(a.get("code"))
        queue.append(a)
    state["pending"] = []

    if send and queue and night:
        # ③ 夜の間は送らずに貯めて、朝8時にまとめて送る
        state["pending"] = queue[:30]
        print(f"\n（夜の時間帯なので、{len(queue)}件を朝8時のまとめに回します）")
    elif send and queue:
        can, daily = push_allowance(state, now)
        if can <= 0:
            print(f"\n（今日のLINE通知は {daily} 回まで。枠が空いたら次の実行で送ります）")
            state["pending"] = queue[:30]
        else:
            texts = [a["text"] for a in queue]
            n_night = sum(1 for a in queue if a.get("night"))
            if n_night and state.get("digest_done") != key:
                # ③ 朝のまとめ: 先頭に見出しとClaudeの一言まとめを付ける
                head = f"【おはようございます】夜〜朝の材料 {n_night}件（点数の高い順）"
                try:
                    summ = ask_claude(client, "次は夜から朝に出た日本株の材料の一覧です。"
                                      "今日の寄り付きに向けて注目すべき点を、日本語で3行以内にまとめてください。"
                                      "前置きや見出しは不要です。\n\n" + "\n\n".join(texts[:15])[:12000],
                                      max_tokens=2000).strip()
                    if summ:
                        head += "\n\n" + summ[:600]
                except Exception:
                    pass
                texts = [head] + texts
                state["digest_done"] = key
            # 1回の通知に吹き出し5個まで。1つの吹き出しに何件か詰めて、なるべく多く送る
            bubbles, used = [], 0
            for t in texts:
                if bubbles and len(bubbles[-1]) + len(t) + 10 <= 4800:
                    bubbles[-1] += "\n\n――――――\n\n" + t
                elif len(bubbles) < 5:
                    bubbles.append(t)
                else:
                    break
                used += 1
            n_items = used - (len(texts) - len(queue))   # 見出しの分を引く
            batch = queue[:n_items]
            code, body = send_line(token, user_id, bubbles)
            print(f"\nLINE送信: {code}（{len(batch)}件を{len(bubbles)}個の吹き出しにまとめて1回で送信／今日の枠 {daily} 回）")
            if code == 200:
                sent = len(batch)
                state["push"][key] = state["push"].get(key, 0) + 1
                m = now.strftime("%Y-%m")
                state["push_month"][m] = state["push_month"].get(m, 0) + 1
                state["theme_sent"].setdefault(key, [])
                state["theme_sent"][key] += [a["code"] for a in batch if a.get("code")]
                state["pending"] = queue[len(batch):len(batch) + 30]  # 入りきらなかった分は次回
            else:
                print(body[:300])
                state["pending"] = queue[:30]
                if state.get("digest_done") == key and n_night:
                    state["digest_done"] = ""

    # ---- ② 週のまとめ（土曜の朝） ----
    if (send and now.weekday() == WEEKLY_REPORT_DAY and now.hour >= 9
            and state.get("weekly_done") != key):
        try:
            rep = weekly_report(hist, today)
        except Exception as e:
            rep = None
            print(f"  週のまとめでエラー: {e}")
        if rep:
            code, body = send_line(token, user_id, [rep])
            print(f"\n週のまとめをLINEに送信: {code}")
            if code == 200:
                m = now.strftime("%Y-%m")
                state["push_month"][m] = state["push_month"].get(m, 0) + 1
                state["push"][key] = state["push"].get(key, 0) + 1
        else:
            print("\n週のまとめ: まだ記録がありません")
        state["weekly_done"] = key
    elif not send and "--report" in sys.argv:
        print("\n" + (weekly_report(hist, today) or "週のまとめ: まだ記録がありません"))

    if send:
        # 画面表示のみのときは「見た」扱いにしない（再実行できるように）
        state["seen"] = list(seen)
        state["judged"][key] = judged_today
        prune_history(state, today)
        save_state(state, today)
    print(f"\nIR・決算を判断: {judged} 件 / 送信対象: {len(alerts)} 件 / LINEに送った件数: {sent}")


if __name__ == "__main__":
    main()
