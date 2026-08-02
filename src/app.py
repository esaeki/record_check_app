import os
from pathlib import Path
import streamlit as st

# main.py から構築済みのクラスや設定をインポート
from src.main import (
    SECRETS,
    SETAGAYA_SOKUHOU_URL,
    DiscordNotifier,
    HistoryManager,
    MinutesAnalyzer,
    SetagayaScraper,
)

# Page Configuration
st.set_page_config(
    page_title="議事録リスクチェック アプリ",
    page_icon="📋",
    layout="wide"
)

st.title("📋 世田谷区議会 議事録リスクチェック アプリ")
st.caption("Google Gemini API を活用した議事録の不適切発言・リスク自動抽出ツール")

# ==========================================
# サイドバー設定
# ==========================================
st.sidebar.header("⚙️ ステータス・設定")

# main.py の機能を使ってインスタンス化
history = HistoryManager()
st.sidebar.metric("チェック済み議事録数", f"{len(history.processed_hashes)} 件")

api_key_loaded = "✅ 読み込み完了" if SECRETS.get("GEMINI_API_KEY") else "❌ 未設定"
webhook_loaded = "✅ 読み込み完了" if SECRETS.get("DISCORD_WEBHOOK_URL") else "❌ 未設定"

st.sidebar.write(f"**Gemini API Key:** {api_key_loaded}")
st.sidebar.write(f"**Discord Webhook:** {webhook_loaded}")

if st.sidebar.button("チェック履歴をクリア"):
    history_file = Path("analyzed_history.json")
    if history_file.exists():
        history_file.unlink()
        st.sidebar.success("履歴をクリアしました。")
        st.rerun()

# ==========================================
# メイン画面のタブ設定
# ==========================================
tab1, tab2 = st.tabs(["🚀 自治体サイト自動チェック", "📝 テキスト直接分析"])

with tab1:
    st.subheader("世田谷区議会（速報ページ）のチェック")
    st.write(f"対象ページ: [{SETAGAYA_SOKUHOU_URL}]({SETAGAYA_SOKUHOU_URL})")

    if st.button("最新の議事録を取得＆分析実行", type="primary"):
        if not SECRETS.get("GEMINI_API_KEY"):
            st.error("GEMINI_API_KEY が設定されていません。Secret Manager または .env を確認してください。")
        else:
            scraper = SetagayaScraper()
            analyzer = MinutesAnalyzer()
            notifier = DiscordNotifier()

            with st.spinner("世田谷区の速報ページを確認中..."):
                items = scraper.fetch_sokuhou_items()

            if not items:
                st.warning("対象となる議事録リンクが見つかりませんでした。")
            else:
                st.info(f"{len(items)} 件の議事録リンクを検出しました。順次チェックします。")

                for item in items:
                    title = item["title"]
                    url = item["url"]
                    st.markdown("---")
                    st.write(f"🔍 **対象:** [{title}]({url})")

                    text_content = scraper.extract_text_from_url(url)
                    if not text_content:
                        continue

                    content_hash = history.calculate_hash(text_content)

                    # コスト節約チェック
                    if history.is_processed(content_hash):
                        st.caption("⏩ 既に解析済みのためスキップ（API課金 0円）")
                        continue

                    with st.spinner("Gemini API でリスク分析中..."):
                        try:
                            result = analyzer.analyze(text_content)

                            # 結果画面表示
                            col1, col2 = st.columns([1, 4])
                            with col1:
                                if result.overall_risk_score == "高":
                                    st.error(f"リスク: {result.overall_risk_score}")
                                elif result.overall_risk_score == "中":
                                    st.warning(f"リスク: {result.overall_risk_score}")
                                else:
                                    st.success(f"リスク: {result.overall_risk_score}")
                            with col2:
                                st.write(f"**会議名:** {result.committee_name}")
                                st.write(f"**概要:** {result.summary}")

                            if result.problematic_statements:
                                with st.expander("🚨 検出された問題発言を確認", expanded=True):
                                    for ps in result.problematic_statements:
                                        st.write(f"- **[{ps.risk_level}] {ps.speaker}氏** ({ps.category})")
                                        st.write(f"  > {ps.statement}")
                                        st.caption(f"  理由: {ps.reason}")

                            # Discord通知
                            notifier.notify_analysis_result(result, title, url)
                            st.caption("💬 Discordに通知を送信しました。")

                            # 履歴保持
                            history.mark_as_processed(content_hash)

                        except Exception as e:
                            st.error(f"分析エラー: {e}")

with tab2:
    st.subheader("任意テキストの直接分析")
    input_text = st.text_area("分析したい議事録テキストを入力してください", height=200)

    if st.button("このテキストを分析", type="primary"):
        if not SECRETS.get("GEMINI_API_KEY"):
            st.error("GEMINI_API_KEY が設定されていません。")
        elif not input_text.strip():
            st.warning("テキストを入力してください。")
        else:
            analyzer = MinutesAnalyzer()
            with st.spinner("分析中..."):
                try:
                    result = analyzer.analyze(input_text)
                    st.success("分析完了")
                    st.json(result.model_dump())
                except Exception as e:
                    st.error(f"分析エラー: {e}")
