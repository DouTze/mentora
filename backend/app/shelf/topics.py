"""What the shelf knows without a model: a small bilingual lexicon of reading topics.

With a model, the scout's profile step writes the search keywords. Without one (the simulated backend, a model that
is down or answers badly) these topics do the same job, less cleverly: a wish such as 強化學習 is widened to the
words its books are likely to carry (reinforcement, Q-learning, RL…), and the tendencies on the home page are the
topics that the person's library, readings and wishes keep coming back to."""
from __future__ import annotations

from collections import Counter

from .books import hit, keywords_of

# (label, keywords); keywords are matched as lower-case substrings of file names, titles, folders and opening text
TOPICS: list[tuple[str, list[str]]] = [
    ("強化學習", ["強化學習", "增強學習", "reinforcement", "rl", "q-learning", "policy gradient", "mdp", "bandit"]),
    ("機器學習", ["機器學習", "machine learning", "ml", "scikit", "sklearn", "監督式", "非監督", "classification", "regression"]),
    ("深度學習", ["深度學習", "deep learning", "dl", "neural network", "神經網路", "pytorch", "tensorflow", "transformer", "cnn"]),
    ("人工智慧", ["人工智慧", "人工智能", "artificial intelligence", "ai", "智慧型", "agent", "代理人"]),
    ("大型語言模型", ["語言模型", "llm", "nlp", "自然語言", "gpt", "prompt", "rag", "提示詞", "生成式", "generative"]),
    ("資料科學", ["資料科學", "數據科學", "data science", "pandas", "資料分析", "數據分析", "data analysis", "大數據", "big data"]),
    ("統計與機率", ["統計", "機率", "概率", "statistics", "probability", "bayesian", "貝氏", "regression analysis"]),
    ("數學", ["線性代數", "linear algebra", "微積分", "calculus", "離散數學", "discrete math", "最佳化", "optimization", "數學"]),
    ("演算法", ["演算法", "算法", "algorithm", "資料結構", "數據結構", "data structure", "leetcode"]),
    ("程式設計", ["程式設計", "編程", "programming", "python", "javascript", "typescript", "java", "rust", "golang", "c++"]),
    ("軟體工程", ["軟體工程", "software engineering", "架構", "architecture", "design pattern", "設計模式", "clean code", "refactor"]),
    ("系統與網路", ["作業系統", "operating system", "計算機網路", "computer network", "分散式", "distributed", "資料庫", "database"]),
    ("資訊安全", ["資訊安全", "資安", "security", "密碼學", "cryptography", "滲透", "penetration"]),
    ("金融與投資", ["金融", "投資", "finance", "investment", "股票", "stock", "基金", "fund", "財務", "量化", "quant", "銀行", "bank"]),
    ("經濟學", ["經濟", "economics", "economy", "總體經濟", "個體經濟", "macro", "micro"]),
    ("商業與管理", ["管理", "management", "商業", "business", "行銷", "marketing", "策略", "strategy", "創業", "startup"]),
    ("物理", ["物理", "physics", "量子", "quantum", "力學", "mechanics", "相對論", "relativity"]),
    ("化學與生物", ["化學", "chemistry", "生物", "biology", "生命科學", "genetics", "基因"]),
    ("醫學與健康", ["醫學", "medicine", "medical", "健康", "health", "護理", "nursing", "藥理"]),
    ("心理學", ["心理學", "psychology", "認知", "cognitive", "行為", "behavior"]),
    ("哲學", ["哲學", "philosophy", "倫理", "ethics", "邏輯", "logic"]),
    ("歷史", ["歷史", "history", "史記", "近代史", "文明", "civilization"]),
    ("語言學習", ["英文", "english", "日文", "日語", "japanese", "toeic", "ielts", "toefl", "多益", "文法", "grammar", "vocabulary", "單字"]),
    ("文學", ["文學", "literature", "詩", "poetry", "散文", "essay", "經典"]),
    ("網路小說", ["小說", "novel", "輕小說", "light novel", "連載", "穿越", "重生", "學院", "英雄", "魔法", "女主角", "主角"]),
    ("推理與奇幻", ["推理", "mystery", "偵探", "detective", "奇幻", "fantasy", "科幻", "sci-fi", "science fiction"]),
    ("設計與藝術", ["設計", "design", "藝術", "art", "攝影", "photography", "ui", "ux", "typography"]),
    ("考試與證照", ["考試", "試題", "證照", "認證", "exam", "certification", "學習指引", "題庫", "國考"]),
]


def _topic_hits(text: str) -> list[str]:
    """Labels whose keywords appear whole in `text`. A keyword found only inside a longer keyword that also appears
    does not count: 化學 inside 強化學習 is not chemistry."""
    hits = [(k, label) for label, kws in TOPICS for k in kws if hit(k, text)]
    words = [k for k, _ in hits]
    return list(dict.fromkeys(label for k, label in hits if not any(len(o) > len(k) and k in o for o in words)))


def expand(query: str) -> tuple[list[str], list[str]]:
    """(keywords to search with, topic labels the query belongs to). The query's own words come first."""
    own = keywords_of(query)
    labels = _topic_hits(query.lower())
    by_label = dict(TOPICS)
    return list(dict.fromkeys(own + [k for label in labels for k in by_label[label]])), labels


def tendencies_from(texts: list[str], *, serials: int = 0, limit: int = 4) -> list[dict]:
    """Topics the person's own material keeps naming, most frequent first; collected serials count for 網路小說."""
    counts: Counter[str] = Counter()
    for t in texts:
        counts.update(_topic_hits(t.lower()))
    if serials:
        counts["網路小說"] += serials
    keywords = dict(TOPICS)
    return [{"label": label, "keywords": keywords[label]} for label, _ in counts.most_common(limit)]
