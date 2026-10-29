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
MODEL = "claude-haiku-5-5"   # 判断に使うClaudeのモデル（安くて速いもの）
PRICE_MAX = 5000             # 株価の上限（円）
SCORE_MIN = 60               # この点数以上をLINEに送る（0〜100）
MAX_CANDIDATES = 15          # 1回の実行でClaudeに読ませるIR・決算の最大件数
MAX_JUDGE_PER_DAY = 150      # 1日にClaudeに読ませる最大件数（費用の上限）
MAX_ITEMS_PER_PUSH = 5       # 1回のLINE通知にまとめる最大件数（吹き出し5個まで）
MAX_PUSH_PER_DAY = 6         # 1日のLINE通知の最大回数（無料枠:月200通を守るため）
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
- 小型・中型株を優先してください
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
    return s


def save_state(state, today):
    state["seen"] = state["seen"][-5000:]
    keep = today.isoformat()
    for k in ("push", "judged", "theme_sent"):
        state[k] = {keep: state[k].get(keep, 0 if k != "theme_sent" else [])}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def ask_claude(client, prompt, max_tokens=3000):
    resp = client.messages.create(
        model=MODEL, max_tokens=max_tokens,
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
    # 新しい順
    out.sort(key=lambda r: (r.get("day", ""), r["time"]), reverse=True)
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
    return data


def format_ir(r, info, res):
    moved = "\n※すでに動き出している可能性あり" if res.get("already_moved") else ""
    label = "決算" if r["kind"] == "決算" else "IR"
    return (
        f"【{label} {res['score']}点】{r['code']} {r['name']}\n"
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

    out = []
    for c in cands:
        x = scores.get(c["code"])
        if not x:
            continue
        try:
            sc = int(x.get("score", 0))
        except Exception:
            continue
        i = c["info"]
        flag = "★送信対象" if sc >= score_min else "  見送り"
        print(f"{flag} {sc:>3}点  {c['code']} {c['name']}  {i['close']:.0f}円 ({i['chg']:+.1f}%) 出来高{i['vol']:.1f}倍  [{c['theme']}]")
        print(f"        根拠: {x.get('reason', '')}")
        if sc >= score_min:
            moved = "\n※すでに動き出している可能性あり" if x.get("already_moved") else ""
            text = (
                f"【テーマ株 {sc}点】{c['code']} {c['name']}\n"
                f"株価 {i['close']:.0f}円（{i['chg']:+.1f}%） 出来高 {i['vol']:.1f}倍{moved}\n"
                f"テーマ: {c['theme']}\n"
                f"ニュース: {c['news']}\n"
                f"根拠: {x.get('reason', '')}\n"
                f"注意: {x.get('risk', '')}（銘柄の関係はClaudeの推定です。ご自身でも確認してください）"
            )
            out.append({"score": sc, "text": text, "code": c["code"]})
    return out


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
        res = judge_ir(client, r, info, pdf_head(r["url"]))
        judged += 1
        judged_today += 1
        seen.add(r["url"])
        if not res:
            print(f"[{r['code']} {r['name']}] 判断結果を読み取れませんでした")
            continue
        flag = "★送信対象" if res["score"] >= score_min else "  見送り"
        print(f"{flag} {res['score']:>3}点  [{r['kind']}] {r['code']} {r['name']}  {info['close']:.0f}円 ({info['chg']:+.1f}%) 出来高{info['vol']:.1f}倍")
        print(f"        {r['kind']}: {r['title'][:70]}")
        print(f"        根拠: {res.get('reason', '')}")
        print(f"        注意: {res.get('risk', '')}")
        if res["score"] >= score_min:
            alerts.append({"score": res["score"], "text": format_ir(r, info, res)})

    # ---- ② ニュースのテーマ株（1時間に1回） ----
    theme_codes = []
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
            theme_codes = [g["code"] for g in got]
            judged_today += 2
        except Exception as e:
            print(f"  テーマ株チェックでエラー: {e}")
        state["theme_last"] = now.isoformat()
    else:
        print("\n■ ニュースのテーマ株チェックは、今回はお休み（1時間に1回／7時〜22時）")

    # ---- LINEに送る ----
    sent = 0
    if send and alerts:
        alerts.sort(key=lambda a: a["score"], reverse=True)
        pushes = state["push"].get(key, 0)
        if pushes >= MAX_PUSH_PER_DAY:
            print(f"\n（今日のLINE通知は上限の{MAX_PUSH_PER_DAY}回に達したため、送りません）")
        else:
            batch = [a["text"] for a in alerts[:MAX_ITEMS_PER_PUSH]]
            code, body = send_line(token, user_id, batch)
            print(f"\nLINE送信: {code}（{len(batch)}件をまとめて1回で送信）")
            if code == 200:
                sent = len(batch)
                state["push"][key] = pushes + 1
                state["theme_sent"].setdefault(key, [])
                state["theme_sent"][key] += theme_codes
            else:
                print(body[:300])

    if send:
        # 画面表示のみのときは「見た」扱いにしない（再実行できるように）
        state["seen"] = list(seen)
        state["judged"][key] = judged_today
        save_state(state, today)
    print(f"\nIR・決算を判断: {judged} 件 / 送信対象: {len(alerts)} 件 / LINEに送った件数: {sent}")


if __name__ == "__main__":
    main()
