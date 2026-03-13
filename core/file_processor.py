"""cocoro-agent — File Processor
アップロードされたファイルをLLMが処理できるテキストに変換するパイプライン。

対応形式:
- PDF  : PyMuPDF でテキスト抽出
- TXT/MD: そのまま使用
- CSV  : pandas で読み込んで分析用テキストに変換

チャンク分割:
- 長いテキストは CHUNK_SIZE 文字ごとに分割
- 分割時は「チャンク X/N」の形式でGeminiに渡す
"""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cocoro.agent.file_processor")

# チャンクサイズ (Gemini の context window に余裕を持たせる)
CHUNK_SIZE       = int(os.getenv("FILE_CHUNK_SIZE", "30000"))   # 文字数
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "20"))     # MB


# ── ファイル種別判定 ──────────────────────────────────────────────────────────

SUPPORTED_TYPES = {
    "application/pdf":           "pdf",
    "text/plain":                "text",
    "text/markdown":             "text",
    "text/csv":                  "csv",
    "application/csv":           "csv",
    "text/x-csv":                "csv",
    "application/vnd.ms-excel":  "csv",
}

EXTENSION_MAP = {
    ".pdf": "pdf",
    ".txt": "text",
    ".md":  "text",
    ".csv": "csv",
    ".tsv": "csv",
}


def detect_type(filename: str, content_type: str = "") -> Optional[str]:
    """ファイル名/Content-Type からファイル種別を判定"""
    # MIME type から判定
    if content_type in SUPPORTED_TYPES:
        return SUPPORTED_TYPES[content_type]
    # 拡張子から判定
    ext = Path(filename).suffix.lower()
    return EXTENSION_MAP.get(ext)


# ── テキスト抽出 ──────────────────────────────────────────────────────────────

def extract_text_from_pdf(data: bytes) -> str:
    """PyMuPDF (fitz) でPDFからテキストを抽出する"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise ImportError(
            "PyMuPDF is required for PDF processing. "
            "Install with: pip install pymupdf"
        )

    text_parts = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        total_pages = len(doc)
        for i, page in enumerate(doc):
            text = page.get_text("text")
            if text.strip():
                text_parts.append(f"=== ページ {i + 1}/{total_pages} ===\n{text.strip()}")

    if not text_parts:
        return "(PDF からテキストを抽出できませんでした。スキャンPDFの可能性があります)"

    return "\n\n".join(text_parts)


def extract_text_from_csv(data: bytes, filename: str = "") -> str:
    """pandas でCSVを読み込んで分析用テキストに変換する"""
    try:
        import pandas as pd
    except ImportError:
        raise ImportError(
            "pandas is required for CSV processing. "
            "Install with: pip install pandas"
        )

    # TSV/CSV 自動判定
    sep = "\t" if filename.endswith(".tsv") else ","

    try:
        df = pd.read_csv(io.BytesIO(data), sep=sep, encoding="utf-8")
    except UnicodeDecodeError:
        df = pd.read_csv(io.BytesIO(data), sep=sep, encoding="cp932")

    lines = [
        f"ファイル: {filename}",
        f"行数: {len(df)}, 列数: {len(df.columns)}",
        f"列名: {', '.join(df.columns.tolist())}",
        "",
        "【基本統計】",
        df.describe(include="all").to_string(),
        "",
        "【先頭10行】",
        df.head(10).to_string(index=False),
    ]

    # 末尾10行 (先頭と異なる場合のみ)
    if len(df) > 10:
        lines += ["", "【末尾10行】", df.tail(10).to_string(index=False)]

    # 欠損値情報
    null_counts = df.isnull().sum()
    if null_counts.any():
        lines += ["", "【欠損値】", null_counts[null_counts > 0].to_string()]

    return "\n".join(lines)


def extract_text(data: bytes, filename: str, content_type: str = "") -> str:
    """ファイル種別に応じてテキストを抽出する"""
    file_type = detect_type(filename, content_type)

    if file_type is None:
        raise ValueError(
            f"Unsupported file type: '{filename}' ({content_type}). "
            f"Supported: PDF, TXT, MD, CSV"
        )

    if file_type == "pdf":
        logger.info("Extracting text from PDF: %s (%d bytes)", filename, len(data))
        return extract_text_from_pdf(data)

    elif file_type == "csv":
        logger.info("Processing CSV: %s (%d bytes)", filename, len(data))
        return extract_text_from_csv(data, filename)

    else:  # text/markdown
        logger.info("Reading text file: %s (%d bytes)", filename, len(data))
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("cp932", errors="replace")


# ── チャンク分割 ──────────────────────────────────────────────────────────────

def split_into_chunks(text: str, chunk_size: int = CHUNK_SIZE) -> list[str]:
    """長いテキストをチャンクに分割する（段落境界を考慮）"""
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end < len(text):
            # 段落境界 (\n\n) で切る
            boundary = text.rfind("\n\n", start, end)
            if boundary > start + chunk_size // 2:
                end = boundary + 2
            else:
                # 改行で切る
                boundary = text.rfind("\n", start, end)
                if boundary > start:
                    end = boundary + 1
        chunks.append(text[start:end])
        start = end

    return chunks


# ── ファイルからプロンプトを構築 ──────────────────────────────────────────────

def build_file_prompt(
    extracted_text: str,
    instruction: str,
    filename: str,
    chunk_index: int = 0,
    total_chunks: int = 1,
) -> str:
    """抽出テキストとユーザー指示を組み合わせてプロンプトを構築する"""
    header_parts = [f"# ファイル: {filename}"]

    if total_chunks > 1:
        header_parts.append(f"# チャンク: {chunk_index + 1}/{total_chunks}")

    header_parts += [
        "",
        "## ファイル内容",
        "```",
        extracted_text[:CHUNK_SIZE],  # 安全上限
        "```",
        "",
        "## 処理指示",
        instruction,
    ]

    if total_chunks > 1 and chunk_index > 0:
        header_parts.insert(2, "# ※ この内容は長いドキュメントの一部です。")

    return "\n".join(header_parts)


# ── ファイルサイズ検証 ────────────────────────────────────────────────────────

def validate_file_size(data: bytes, max_mb: int = MAX_FILE_SIZE_MB) -> None:
    """ファイルサイズが上限以内かチェックする"""
    size_mb = len(data) / (1024 * 1024)
    if size_mb > max_mb:
        raise ValueError(
            f"File too large: {size_mb:.1f}MB (max: {max_mb}MB)"
        )
