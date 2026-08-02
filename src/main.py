import hashlib
import io
import json
import logging
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Set, Dict

import pdfplumber
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.cloud import secretmanager
from pydantic import BaseModel, Field

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# 定数・設定
HISTORY_FILE = Path("analyzed_history.json")
SETAGAYA_SOKUHOU_URL = "https://www.city.setagaya.lg.jp/02030/29172.html"


# ==========================================
# 0. 機密情報ロード処理 (Secret Manager / .env)
# ==========================================

def load_app_secrets(secret_id: str = "record-check-app", project_id: str = None) -> dict:
    """
    ローカルの .env、または GCP Secret Manager の 'record-check-app' から
    環境変数・APIキーを安全に取得する関数
    """
    load_dotenv()

    secrets = {
        "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY", ""),
        "DISCORD_WEBHOOK_URL": os.getenv("DISCORD_WEBHOOK_URL", ""),
        "GEMINI_MODEL": os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
    }

    # すべてローカルで揃っている場合は Secret Manager へのアクセスをスキップ
    if secrets["GEMINI_API_KEY"] and secrets["DISCORD_WEBHOOK_URL"]:
        return secrets

    # GCP プロジェクト ID の取得（環境変数になければ既定のプロジェクトIDを使用）
    if not project_id:
        project_id = os.getenv("GCP_PROJECT_ID", "814563271178")

    try:
        logger.info(f"Secret Manager ({secret_id}) から設定情報をロード中...")
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        secret_payload = response.payload.data.decode("UTF-8").strip()

        # JSON形式としてパースを試みる
        try:
            secret_dict = json.loads(secret_payload)
            for k, v in secret_dict.items():
                if not secrets.get(k):
                    secrets[k] = str(v)
        except json.JSONDecodeError:
            # .env 形式 (KEY=VALUE) としてパース
            for line in secret_payload.splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if not secrets.get(k):
                        secrets[k] = v

    except Exception as e:
        logger.warning(f"Secret Manager ({secret_id}) からの取得をスキップ/失敗しました: {e}")

    return secrets


# 起動時に一括ロード
SECRETS = load_app_secrets("record-check-app")


# ==========================================
# 1. Pydantic データ構造定義
# ==========================================

class ProblematicStatement(BaseModel):
    statement: str = Field(description="問題視される不適切な発言の該当箇所テキスト")
    speaker: str = Field(description="発言者の名前（不明な場合は'不明'）")
    reason: str = Field(description="なぜこの発言が不適切・不穏当と判断されるかの理由説明")
    category: str = Field(description="発言の分類（例: '差別的発言', '人身攻撃', '不穏当な表現', '手続き違反', 'その他'）")
    risk_level: str = Field(description="リスク度（'高', '中', '低'）")


class AnalysisResult(BaseModel):
    committee_name: str = Field(description="会議名または委員会名（例: '世田谷区議会 本会議', '企画総務委員会'など）")
    date: Optional[str] = Field(default=None, description="開催日（判明する場合）")
    summary: str = Field(description="議事録の概要（100〜200文字程度）")
    problematic_statements: List[ProblematicStatement] = Field(
        default_factory=list,
        description="検出された問題発言のリスト（問題がない場合は空配列）"
    )
    overall_risk_score: str = Field(description="会議全体のリスクレベル（'高', '中', '低', '問題なし'）")


# ==========================================
# 2. 重複チェック管理クラス (コスト抑制)
# ==========================================

class HistoryManager:
    """チェック済みコンテンツのハッシュ値を記録し、二重解析によるAPI課金を防止するクラス"""
    def __init__(self, history_file: Path = HISTORY_FILE):
        self.history_file = history_file
        self.processed_hashes: Set[str] = self._load_history()

    def _load_history(self) -> Set[str]:
        if self.history_file.exists():
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return set(data.get("processed_hashes", []))
            except Exception as e:
                logger.warning(f"履歴ファイルの読み込みに失敗しました。新規作成します: {e}")
        return set()

    def is_processed(self, content_hash: str) -> bool:
        return content_hash in self.processed_hashes

    def mark_as_processed(self, content_hash: str):
        self.processed_hashes.add(content_hash)
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump({"processed_hashes": list(self.processed_hashes)}, f, indent=2)
        except Exception as e:
            logger.error(f"履歴ファイルの保存に失敗しました: {e}")

    @staticmethod
    def calculate_hash(text: str) -> str:
        """テキスト内容からMD5ハッシュ値を計算"""
        return hashlib.md5(text.encode("utf-8")).hexdigest()


# ==========================================
# 3. 世田谷区議会 スクレイパー＆テキスト抽出
# ==========================================

class SetagayaScraper:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    def fetch_sokuhou_items(self) -> List[Dict[str, str]]:
        """世田谷区の速報ページから各会議のリンクを取得"""
        items = []
        try:
            logger.info(f"🌐 世田谷区議会 速報ページを取得中: {SETAGAYA_SOKUHOU_URL}")
            res = requests.get(SETAGAYA_SOKUHOU_URL, headers=self.headers, timeout=15)
            res.raise_for_status()
            res.encoding = res.apparent_encoding

            soup = BeautifulSoup(res.text, "html.parser")
            
            # 本文エリア内のリンクを探索
            content_area = soup.find("div", id="main") or soup
            for a_tag in content_area.find_all("a", href=True):
                href = a_tag["href"]
                title = a_tag.get_text(strip=True)

                if not title:
                    continue

                # 絶対URLへ変換
                if href.startswith("/"):
                    full_url = f"https://www.city.setagaya.lg.jp{href}"
                elif not href.startswith("http"):
                    full_url = f"https://www.city.setagaya.lg.jp/02030/{href}"
                else:
                    full_url = href

                # 会議録らしきリンクを抽出
                if any(k in title for k in ["定例会", "臨時会", "委員会", "本会議", "速報"]) or href.endswith(".pdf"):
                    items.append({
                        "title": title,
                        "url": full_url
                    })

            logger.info(f"世田谷区速報ページから {len(items)} 件の関連リンクを検出しました。")
            return items

        except Exception as e:
            logger.error(f"世田谷区速報ページの取得に失敗しました: {e}")
            return []

    def extract_text_from_url(self, target_url: str) -> Optional[str]:
        """URLがHTMLかPDFか判定してテキストを取得"""
        try:
            res = requests.get(target_url, headers=self.headers, timeout=20)
            res.raise_for_status()

            # PDF の場合
            if target_url.endswith(".pdf") or "application/pdf" in res.headers.get("Content-Type", ""):
                logger.info(f"📄 PDF データを解析中: {target_url}")
                with pdfplumber.open(io.BytesIO(res.content)) as pdf:
                    pages_text = [p.extract_text() for p in pdf.pages if p.extract_text()]
                return "\n".join(pages_text)

            # HTML の場合
            else:
                res.encoding = res.apparent_encoding
                soup = BeautifulSoup(res.text, "html.parser")
                for tag in soup(["script", "style", "header", "footer", "nav"]):
                    tag.extract()
                return soup.get_text(separator="\n", strip=True)

        except Exception as e:
            logger.error(f"コンテンツの本文抽出に失敗しました ({target_url}): {e}")
            return None


# ==========================================
# 4. Discord 通知クラス
# ==========================================

class DiscordNotifier:
    COLOR_HIGH_RISK = 0xE74C3C    # 赤
    COLOR_MEDIUM_RISK = 0xF1C40F  # 黄
    COLOR_LOW_RISK = 0x3498DB     # 青

    def __init__(self):
        self.webhook_url = SECRETS.get("DISCORD_WEBHOOK_URL")
        if not self.webhook_url:
            logger.warning("DISCORD_WEBHOOK_URL が設定されていません。通知はスキップされます。")

    def send_raw_payload(self, payload: dict):
        if not self.webhook_url:
            return
        try:
            response = requests.post(self.webhook_url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("Discordへ通知を送信しました。")
        except Exception as e:
            logger.error(f"Discord通知の送信に失敗しました: {e}")

    def notify_analysis_result(self, result: AnalysisResult, title: str, url: str):
        color_map = {
            "高": self.COLOR_HIGH_RISK,
            "中": self.COLOR_MEDIUM_RISK,
            "低": self.COLOR_LOW_RISK,
            "問題なし": self.COLOR_LOW_RISK
        }
        embed_color = color_map.get(result.overall_risk_score, self.COLOR_LOW_RISK)

        fields = [
            {"name": "自治体", "value": "東京都世田谷区議会", "inline": True},
            {"name": "会議名", "value": result.committee_name, "inline": True},
            {"name": "全体リスク判定", "value": f"**{result.overall_risk_score}**", "inline": True},
            {"name": "概要", "value": result.summary, "inline": False},
            {"name": "参照元ページ", "value": f"[{title}]({url})", "inline": False}
        ]

        if result.problematic_statements:
            statements_text = ""
            for idx, ps in enumerate(result.problematic_statements, 1):
                statements_text += (
                    f"**{idx}. [{ps.risk_level}] {ps.speaker} 氏** ({ps.category})\n"
                    f"> {ps.statement}\n"
                    f"└ *理由: {ps.reason}*\n\n"
                )
            if len(statements_text) > 1000:
                statements_text = statements_text[:990] + "\n...（省略）"

            fields.append({"name": "🚨 検出された問題発言", "value": statements_text, "inline": False})
        else:
            fields.append({"name": "✅ チェック結果", "value": "問題視される発言は見つかりませんでした。", "inline": False})

        payload = {
            "username": "世田谷区議会 リスクチェック Bot",
            "embeds": [{
                "title": f"📋 議事録リスク分析: {result.committee_name}",
                "color": embed_color,
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }]
        }
        self.send_raw_payload(payload)

    def notify_error(self, task_name: str, error: Exception):
        tb_str = traceback.format_exc()
        if len(tb_str) > 500:
            tb_str = tb_str[-500:]

        payload = {
            "username": "世田谷区議会 リスクチェック Bot",
            "embeds": [{
                "title": "🚨 分析処理エラー発生",
                "description": f"タスク **[{task_name}]** の処理中にエラーが発生しました。",
                "color": self.COLOR_HIGH_RISK,
                "fields": [
                    {"name": "エラー内容", "value": f"```{str(error)[:300]}```", "inline": False},
                    {"name": "スタックトレース", "value": f"```python\n{tb_str}\n```", "inline": False}
                ],
                "timestamp": datetime.now(timezone.utc).isoformat()
            }]
        }
        self.send_raw_payload(payload)


# ==========================================
# 5. Gemini 分析クラス
# ==========================================

class MinutesAnalyzer:
    def __init__(self):
        api_key = SECRETS.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY が取得できませんでした。Secret Manager または .env を確認してください。")
        self.client = genai.Client(api_key=api_key)

    def analyze(self, minutes_text: str) -> AnalysisResult:
        model_name = SECRETS.get("GEMINI_MODEL", "gemini-2.0-flash")

        system_instruction = """
あなたは地方議会の議事録分析の専門家です。
提示された世田谷区議会の議事録テキストを精読し、以下の観点から問題・不適切と思われる発言を抽出・分析してください。

【抽出基準】
1. 人権侵害、差別的表現、特定の個人や団体への誹謗中傷
2. 侮辱的・不穏当なヤジや暴言
3. 議会運営や法令・条例上の手続き違反が懸念される言動
4. 根拠のない事実誤認に基づく過剰な批判

【指示】
・全体概要を簡潔にまとめてください。
・問題発言がある場合は該当部分・発言者・理由・カテゴリ・リスク度を明記してください。
・問題発言がない場合は empty リストで返答してください。
"""

        logger.info(f"Gemini API ({model_name}) で分析を実行中...")
        response = self.client.models.generate_content(
            model=model_name,
            contents=f"以下の議事録テキストを分析してください:\n\n{minutes_text}",
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=AnalysisResult,
                temperature=0.1,
            ),
        )
        return response.parsed


# ==========================================
# 6. メイン実行処理
# ==========================================

if __name__ == "__main__":
    notifier = DiscordNotifier()
    analyzer = MinutesAnalyzer()
    scraper = SetagayaScraper()
    history = HistoryManager()

    # 世田谷区の速報ページからリストを取得
    items = scraper.fetch_sokuhou_items()

    if not items:
        logger.info("対象となる会議録リンクが見つかりませんでした。")
    else:
        for item in items:
            title = item["title"]
            url = item["url"]

            text_content = scraper.extract_text_from_url(url)
            if not text_content:
                continue

            content_hash = history.calculate_hash(text_content)

            # 重複判定（過去に分析済みならGemini APIを呼ばずにスキップ＝課金なし）
            if history.is_processed(content_hash):
                logger.info(f"⏩ スキップ: [{title}] は既に解析済みです。")
                continue

            try:
                logger.info(f"📄 処理開始: [{title}] ({url})")

                # Gemini API で分析
                result: AnalysisResult = analyzer.analyze(text_content)

                # Discord 通知
                notifier.notify_analysis_result(result, title, url)

                # 履歴に登録して二度と分析しないようにする
                history.mark_as_processed(content_hash)
                logger.info(f"✅ 完了: [{title}] の解析・通知が完了しました。")

            except Exception as e:
                logger.error(f"❌ エラー発生 [{title}]: {e}")
                notifier.notify_error(title, e)
