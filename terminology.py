"""
校园术语库（Terminology Base）
=============================

这个模块解决一个具体问题：

    LibreTranslate 的离线模型里没有「中文 ⇄ 俄文」直连模型，
    翻译实际要走 中文 -> 英文 -> 俄文 两段跳转。每跳一次，术语就可能被翻坏一次。
    例如「辅导员」会被翻成 counselor，再翻成 консультант（意思是"咨询顾问"），
    而正确的说法是 куратор。

怎么解决（核心就两步）：

    1. 译前「锁定」：句子里凡是命中术语库的词，先换成机器翻译碰不动的占位符 ⟦T0⟧
    2. 译后「回填」：机器翻完，把占位符强制换回术语库里写死的俄文

这样机器翻译根本"看不到"这些词，术语就不会被翻坏。

怎么用（最简单的形式）：

    from libretranslate.terminology import get_terminology
    tb = get_terminology()
    protected, mapping, hits = tb.protect("请辅导员找我")
    russian = 机器翻译(protected)
    final, report = tb.restore(russian, mapping)
    print(report["fidelity"])   # 术语保真率，例如 1.0 表示全部保住

作者注：占位符样式（⟦T0⟧）是否真的能在两段跳转中原样存活，
需要装上模型后实测。若保真率不理想，改 PLACEHOLDER 常量即可，
本模块已经把「丢了几个术语」统计出来了，方便对比不同写法的效果。
"""

import json
import os
import re
from functools import lru_cache

# ---------------------------------------------------------------------------
# 配置区
# ---------------------------------------------------------------------------

# 术语库数据文件的位置。默认放仓库根目录下的 terminology/zh-ru.json。
# 注意：别放在 db/ 里 —— 上游 .gitignore 第 1 行就是 "db/"，那里整个被忽略，
# 放进去会永远提交不上（db/ 是留给 api_keys.db 这类运行时数据库的）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
DEFAULT_DB_PATH = os.path.join(_ROOT, "terminology", "zh-ru.json")

# 占位符样式。{} 里会被填入序号。
# 选 ⟦ ⟧ 是因为这对符号在中俄英三种语料里都极罕见，不容易和正文混淆。
PLACEHOLDER = "\u27e6T{}\u27e7"  # ⟦T0⟧

# 这些缩写/专名一律原样保留，不送进翻译模型。
# 深北莫场景里最常见的就是校名缩写和俄语考试名。
WHITELIST = [
    "MSU-BIT", "MSU", "BIT",
    "GPA", "TOEFL", "IELTS", "MOOC",
    "ТРКИ", "TORFL", "ECTS",
    "深北莫", "深圳北理莫斯科大学",
]

# 这些"噪音"用正则识别后原样保护，典型是课程编号。
# 注意：括号必须写成非捕获形式 (?:...)，否则会打乱占位符的分组。
NOISE_REGEX = [
    r"[A-Z]{2,6}-?\d{2,4}",      # 例如 CS101、MATH-201
    r"[А-Я]{2,6}-?\d{2,4}",      # 例如 МАТ-101
    r"\d+(?:\.\d+)?%",           # 例如 85.5%
]


# ---------------------------------------------------------------------------
# 术语库主体
# ---------------------------------------------------------------------------


class Terminology:
    """一份术语表，外加「锁定 / 回填」两件事。"""

    def __init__(self, terms, whitelist=None, noise_regex=None):
        # terms: [{"zh": "数据结构", "ru": "Структуры данных", "lock": "strict"}, ...]
        self.terms = terms
        self._index = {}
        self._strict = {}
        self._soft = {}
        for t in terms:
            zh = (t.get("zh") or "").strip()
            ru = (t.get("ru") or "").strip()
            if not zh or not ru:
                continue
            self._index[zh] = t
            # lock 决定这个词的"硬度"：
            #   strict = 强制占位替换。课程名、职务名、机构名这类不会用错的专有名词。
            #   soft   = 只在界面提示建议译法，不改写原文。
            #            「通知」「报名」这类既能当名词又能当动词的词属于此列，
            #            否则「请辅导员通知班长」会被锁成"辅导员+通知+班长"三个词粘一起。
            # 没写 lock 字段的老数据默认按 strict 处理，保证兼容。
            if str(t.get("lock") or "strict").lower() == "soft":
                self._soft[zh] = t
            else:
                self._strict[zh] = t
        self.whitelist = list(whitelist if whitelist is not None else WHITELIST)
        self.noise_regex = list(noise_regex if noise_regex is not None else NOISE_REGEX)
        self._strict_pattern, self._soft_pattern = self._compile()

    # ---------- 内部 ----------

    def _compile(self):
        """合成两个大正则：一个管强制替换（strict），一个管只提示（soft）。

        词表里长的排前面，这样「数学分析」不会被先匹配成「数学」。
        白名单词和噪音规则只进 strict —— 它们本来就该原样保留。
        """

        def build(keys, with_noise):
            literals = list(keys)
            if with_noise:
                literals += self.whitelist
            literals.sort(key=len, reverse=True)
            parts = [re.escape(x) for x in literals if x]
            if with_noise:
                parts += self.noise_regex
            if not parts:
                return None
            return re.compile("(" + "|".join(parts) + ")")

        return build(self._strict.keys(), True), build(self._soft.keys(), False)

    @staticmethod
    def _placeholder(i):
        return PLACEHOLDER.format(i)

    # ---------- 对外 ----------

    def protect(self, text):
        """译前锁定。

        返回三样东西：
          protected : 换过占位符的文本，交给机器翻译
          mapping   : 占位符 -> 原词（或该词的俄文），回填时要用
          hits      : 本次命中了哪些词条，网页端高亮要用
        """
        if not text:
            return text, {}, []

        mapping = {}
        hits = []
        counter = [0]

        def _sub(m):
            found = m.group(0)
            ph = self._placeholder(counter[0])
            counter[0] += 1
            if found in self._strict:
                entry = self._strict[found]
                # 占位符最终要换成俄文，所以这里直接存俄文
                mapping[ph] = entry["ru"]
                hits.append(dict(entry, lock="strict"))
            else:
                # 走到这里的都是白名单词或课程编号：原样保留，不翻译
                mapping[ph] = found
                hits.append({"zh": found, "ru": found, "domain": "保留项", "lock": "strict"})
            return ph

        protected = self._strict_pattern.sub(_sub, text) if self._strict_pattern else text

        # 再扫一遍 soft 词：只记录不替换。
        # 这些词（通知、报名、请假…）可能是动词，硬改必错，
        # 但它们的建议译法对使用者有价值，所以放进 hits 交给界面提示。
        if self._soft_pattern:
            for m in self._soft_pattern.finditer(protected):
                entry = self._soft.get(m.group(0))
                if entry:
                    hits.append(dict(entry, lock="soft"))

        return protected, mapping, hits

    def restore(self, text, mapping):
        """译后回填。

        返回 (回填后的文本, 报告)。
        报告里最关键的是 fidelity（术语保真率）：
            保住的术语数 / 送进去的术语数
        如果一个占位符被翻译模型拆散或吞掉，它就会被记进 lost 列表。
        """
        if not text or not mapping:
            return text, {"total": 0, "kept": 0, "lost": [], "fidelity": 1.0}

        lost = []
        for ph, value in mapping.items():
            if ph in text:
                text = text.replace(ph, value)
                continue
            # 容错 1：模型可能在符号之间插了空格，例如 ⟦ T0 ⟧
            loose = re.compile(
                re.escape(ph[0]) + r"\s*" + re.escape(ph[1:-1]) + r"\s*" + re.escape(ph[-1])
            )
            new_text, n = loose.subn(value, text)
            if n:
                text = new_text
                continue
            # 容错 2：符号被吃掉，但 T0 这个编号还在
            num = ph.strip("\u27e6\u27e7")
            bare = re.compile(re.escape(num) + r"(?![0-9])")
            new_text, n = bare.subn(value, text)
            if n:
                text = new_text
                continue
            lost.append({"placeholder": ph, "expected": value})

        total = len(mapping)
        kept = total - len(lost)
        report = {
            "total": total,
            "kept": kept,
            "lost": lost,
            "fidelity": (kept / total) if total else 1.0,
        }
        return text, report

    def guarded(self, translate_fn):
        """包一层：把任意「文本 -> 译文」的函数升级成「带术语保护的」版本。

        用法：
            safe_translate = tb.guarded(lambda s: translator.translate(s))
            result, report = safe_translate("请辅导员找我")
        """

        def _wrapped(text, *args, **kwargs):
            protected, mapping, hits = self.protect(text)
            translated = translate_fn(protected, *args, **kwargs)
            final, report = self.restore(translated, mapping)
            report["hits"] = hits
            return final, report

        return _wrapped

    def lookup(self, query, limit=20):
        """查词。给网页端和浏览器插件用。

        先找完全等于查询词的，再找包含查询词的，最后找俄文里命中的。
        """
        q = (query or "").strip()
        if not q:
            return []
        exact = [t for t in self.terms if t.get("zh") == q]
        contains = [t for t in self.terms if q and q in (t.get("zh") or "")]
        by_ru = [t for t in self.terms if q.lower() in (t.get("ru") or "").lower()]
        out, seen = [], set()
        for group in (exact, contains, by_ru):
            for t in group:
                key = t.get("zh")
                if key in seen:
                    continue
                seen.add(key)
                out.append(t)
            if len(out) >= limit:
                break
        return out[:limit]

    def stats(self):
        """给网页端 /stats 接口用的一条概览。"""
        domains = {}
        locks = {"strict": 0, "soft": 0}
        need_check = 0
        for t in self.terms:
            d = t.get("domain") or "未分类"
            domains[d] = domains.get(d, 0) + 1
            k = "soft" if str(t.get("lock") or "strict").lower() == "soft" else "strict"
            locks[k] += 1
            if t.get("verify"):
                need_check += 1
        return {
            "total": len(self.terms),
            "domains": domains,
            "lock": locks,
            "need_review": need_check,
        }


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


def load_terms(path=None):
    """从 JSON 读词条。文件不存在就返回空列表，不让程序崩掉。"""
    p = path or os.environ.get("LT_TERMINOLOGY") or DEFAULT_DB_PATH
    if not os.path.exists(p):
        return []
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data.get("terms", [])
    if isinstance(data, list):
        return data
    return []


@lru_cache(maxsize=1)
def get_terminology(path=None):
    """进程内只加载一次，避免每次请求都读硬盘。"""
    return Terminology(load_terms(path))


def reload_terminology(path=None):
    """改完词表后调用这个，让缓存失效重新读。"""
    get_terminology.cache_clear()
    return get_terminology(path)


# ---------------------------------------------------------------------------
# 命令行自检：python -m libretranslate.terminology "请辅导员找我"
# 还没装模型也能跑，它用假翻译函数来演示锁定/回填的过程。
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    tb = get_terminology()
    print("已加载词条：%d 条" % len(tb.terms))
    if tb.terms:
        print("分类统计：", tb.stats())

    text = sys.argv[1] if len(sys.argv) > 1 else "请辅导员通知班长，数据结构课改到 MATH-201 教室。"

    def fake_translate(s):
        # 假想中的"坏翻译"：它会把占位符之后的内容随便改写
        return "Пожалуйста, " + s + " сообщите."

    print("\n原文：", text)
    protected, mapping, hits = tb.protect(text)
    print("锁定后：", protected)
    strict = [h.get("zh") for h in hits if h.get("lock") == "strict"]
    soft = [h.get("zh") for h in hits if h.get("lock") == "soft"]
    print("强制替换（strict）：", strict)
    print("仅提示  （soft）  ：", soft, "  ← 这些不改写原文，因为可能是动词")
    out, report = tb.restore(fake_translate(protected), mapping)
    print("回填后：", out)
    print("报告：", report)
