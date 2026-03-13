"""cocoro-agent — Output Formatter
タスク結果を markdown / json / slides / email 形式に整形するユーティリティ。
Gemini への指示フォーマットと、取得した生テキストの後処理を担当する。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger("cocoro.agent.output_formatter")


# ── フォーマット別 Gemini 指示 ────────────────────────────────────────────────

FORMAT_INSTRUCTIONS: dict[str, str] = {
    "markdown": (
        "\n\n【アウトプット形式】\n"
        "マークダウン形式で回答してください。\n"
        "見出し(#/##/###)・箇条書き・強調(**太字**)・コードブロックを適切に使用してください。"
    ),
    "json": (
        "\n\n【アウトプット形式】\n"
        "以下のJSON形式で**のみ**回答してください（説明文は不要）：\n"
        "```json\n"
        "{\n"
        "  \"summary\": \"全体のサマリー（2〜3文）\",\n"
        "  \"points\": [\n"
        "    { \"title\": \"ポイント1\", \"detail\": \"詳細説明\" },\n"
        "    { \"title\": \"ポイント2\", \"detail\": \"詳細説明\" }\n"
        "  ],\n"
        "  \"conclusion\": \"最終的な結論\",\n"
        "  \"confidence\": 0.9\n"
        "}\n"
        "```\n"
        "コードブロック(```json)で囲んで出力してください。"
    ),
    "slides": (
        "\n\n【アウトプット形式】\n"
        "以下のJSON形式でスライド構成を**のみ**回答してください：\n"
        "```json\n"
        "{\n"
        "  \"title\": \"プレゼンテーションタイトル\",\n"
        "  \"slides\": [\n"
        "    {\n"
        "      \"slide_number\": 1,\n"
        "      \"title\": \"スライドタイトル\",\n"
        "      \"content\": \"スライドの本文（箇条書き推奨）\",\n"
        "      \"speaker_notes\": \"発表者ノート（省略可）\"\n"
        "    }\n"
        "  ],\n"
        "  \"total_slides\": 5\n"
        "}\n"
        "```\n"
        "スライドは5〜10枚程度で構成してください。"
    ),
    "email": (
        "\n\n【アウトプット形式】\n"
        "以下のJSON形式でメール文面を**のみ**回答してください：\n"
        "```json\n"
        "{\n"
        "  \"subject\": \"メールの件名\",\n"
        "  \"greeting\": \"お世話になっております。\",\n"
        "  \"body\": \"メール本文（段落ごとに\\n\\nで区切る）\",\n"
        "  \"closing\": \"何卒よろしくお願いいたします。\",\n"
        "  \"signature\": \"[担当者名]\"\n"
        "}\n"
        "```"
    ),
}


def get_format_instruction(output_format: Optional[str]) -> str:
    """出力フォーマット指示文を返す（未指定はmarkdown）"""
    fmt = (output_format or "markdown").lower()
    return FORMAT_INSTRUCTIONS.get(fmt, FORMAT_INSTRUCTIONS["markdown"])


# ── 生テキストのパース ─────────────────────────────────────────────────────────

def parse_result(raw_text: str, output_format: Optional[str]) -> Any:
    """
    Gemini が返した生テキストをフォーマットに応じてパースする。
    JSON系フォーマット (json/slides/email) はコードブロックを取り出してパース。
    markdown はそのまま返す。
    """
    fmt = (output_format or "markdown").lower()

    if fmt == "markdown":
        return raw_text

    # JSON系: ```json ... ``` コードブロックを抽出
    parsed = _extract_json_from_text(raw_text)
    if parsed is not None:
        return parsed

    # フォールバック: 生テキストをそのまま返す
    logger.warning("Could not parse JSON from Gemini output for format '%s', returning raw", fmt)
    return {"raw": raw_text, "parse_error": True}


def _extract_json_from_text(text: str) -> Optional[Any]:
    """テキストから ```json ... ``` ブロックを取り出してパースする"""
    # ```json ... ``` パターン
    match = re.search(r"```json\s*([\s\S]+?)\s*```", text)
    if match:
        json_str = match.group(1).strip()
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.debug("JSON parse error from code block: %s", e)

    # ``` ... ``` （言語指定なし）
    match = re.search(r"```\s*([\s\S]+?)\s*```", text)
    if match:
        json_str = match.group(1).strip()
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

    # コードブロックなし: テキスト全体をそのままパース
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    return None


# ── PDF エクスポート (reportlab) ────────────────────────────────────────────────

def generate_pdf(title: str, content: str, metadata: Optional[dict] = None) -> bytes:
    """
    タスク結果を PDF に変換する (reportlab 使用)。
    reportlab が未インストールの場合は ImportError を raise。
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, HRFlowable, PageBreak
        )
        from reportlab.lib import colors
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        import io as _io
    except ImportError:
        raise ImportError(
            "reportlab is required for PDF export. "
            "Install with: pip install reportlab"
        )

    buffer = _io.BytesIO()

    # 日本語フォント登録
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiMin-W3"))
        jp_font = "HeiseiMin-W3"
    except Exception:
        jp_font = "Helvetica"  # フォールバック

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=25 * mm,
        bottomMargin=20 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CTitle",
        parent=styles["Title"],
        fontName=jp_font,
        fontSize=18,
        spaceAfter=6,
        textColor=colors.HexColor("#1a1a2e"),
    )
    body_style = ParagraphStyle(
        "CBody",
        parent=styles["Normal"],
        fontName=jp_font,
        fontSize=10,
        leading=16,
        spaceAfter=4,
    )

    story = []
    story.append(Paragraph(title, title_style))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.grey))
    story.append(Spacer(1, 6 * mm))

    # メタデータ行
    if metadata:
        for k, v in metadata.items():
            story.append(Paragraph(f"<b>{k}</b>: {v}", body_style))
        story.append(Spacer(1, 4 * mm))

    # 本文を段落ごとに追加
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            story.append(Spacer(1, 3 * mm))
            continue
        # マークダウン見出し → 太字
        if line.startswith("### "):
            story.append(Paragraph(f"<b>{line[4:]}</b>", body_style))
        elif line.startswith("## "):
            story.append(Paragraph(
                f"<b><font size=13>{line[3:]}</font></b>", body_style
            ))
        elif line.startswith("# "):
            story.append(Paragraph(
                f"<b><font size=15>{line[2:]}</font></b>", body_style
            ))
        elif line.startswith("- ") or line.startswith("* "):
            story.append(Paragraph(f"• {line[2:]}", body_style))
        else:
            story.append(Paragraph(line, body_style))

    doc.build(story)
    return buffer.getvalue()
